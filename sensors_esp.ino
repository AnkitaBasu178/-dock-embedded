// ESP32 #1: MAX30102 (SpO2) + simulated temperature + AD8232 ECG (heart rate) + GSR skin sensor.
// Plug into the Mac over USB. Prints checksummed "VITAL ..." lines. No WiFi, no clock.
//
// WIRING (ESP32 pin -> sensor pin)
//   MAX30102 : 3V3->VIN, GND->GND, GPIO21->SDA, GPIO22->SCL
//   AD8232   : 3V3->3.3V, GND->GND, GPIO34->OUTPUT, GPIO32->LO+, GPIO33->LO-   (leave SDN unconnected)
//   GSR V2.0 : 3V3->VCC, GND->GND, GPIO35->SIG
//
// Types sent:  spo2 (%), temperature (C), heart_rate (bpm, from the ECG), gsr (mV), plus raw "ECG d=..." lines for the live graph
// KEEP YOUR FINGERS OFF THE GSR ELECTRODES when you power up / press EN: that moment is
// used as the "no contact" level.
#include <Wire.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"

#define ECG_PIN   34
#define LO_PLUS   32
#define LO_MINUS  33
#define GSR_PIN   35

#define ECG_FS 250
#define ECG_N  2000          // 8 s of ECG
#define ECG_MIN_QUALITY 0.70f

MAX30105 sensor;
#define BUFFER_SIZE 100
uint32_t irBuffer[BUFFER_SIZE];
uint32_t redBuffer[BUFFER_SIZE];
int32_t spo2;
int8_t validSPO2;
int32_t heartRateUnused;     // the library's own heart rate is not used
int8_t validHeartRateUnused;

// ---------------------------------------------------------------------------
// ECG heart rate from an 8 s window: QRS-energy envelope -> autocorrelation -> beat period.
// Needs no amplitude threshold; ignores T-waves, baseline drift and 50 Hz hum.
// Returns true and sets *bpm and *quality (0..1) only when a clear rhythm exists.
// ---------------------------------------------------------------------------
bool estimateEcgHR(const uint16_t* raw, int n, int32_t* bpm, float* quality) {
  static float x[ECG_N], e[ECG_N];
  if (n > ECG_N || n < ECG_FS * 4) return false;

  // 1. 5-point moving average: exactly cancels 50 Hz mains hum at 250 samples/s
  for (int i = 0; i < n; i++) {
    int a = i - 2; if (a < 0) a = 0;
    int b = i + 2; if (b > n - 1) b = n - 1;
    float s = 0;
    for (int k = a; k <= b; k++) s += raw[k];
    x[i] = s / (b - a + 1);
  }
  // 2. remove baseline wander (0.6 s moving average)
  {
    const int H = 75;
    static float base[ECG_N];
    for (int i = 0; i < n; i++) {
      int a = i - H; if (a < 0) a = 0;
      int b = i + H; if (b > n - 1) b = n - 1;
      float s = 0;
      for (int k = a; k <= b; k++) s += x[k];
      base[i] = s / (b - a + 1);
    }
    for (int i = 0; i < n; i++) x[i] -= base[i];
  }
  // 3. QRS energy envelope: |slope| smoothed over 80 ms (sign of the ECG does not matter)
  for (int i = 0; i < n; i++) {
    int a = i - 1; if (a < 0) a = 0;
    int b = i + 1; if (b > n - 1) b = n - 1;
    e[i] = fabsf(x[b] - x[a]);
  }
  {
    const int W = 20;
    static float t[ECG_N];
    for (int i = 0; i < n; i++) {
      int a = i - W / 2; if (a < 0) a = 0;
      int b = i + W / 2; if (b > n - 1) b = n - 1;
      float s = 0;
      for (int k = a; k <= b; k++) s += e[k];
      t[i] = s / (b - a + 1);
    }
    for (int i = 0; i < n; i++) e[i] = t[i];
  }
  // 4. remove mean, autocorrelate over beat periods 0.3 s .. 1.5 s (200 .. 40 bpm)
  float mean = 0;
  for (int i = 0; i < n; i++) mean += e[i];
  mean /= n;
  float var = 0;
  for (int i = 0; i < n; i++) { e[i] -= mean; var += e[i] * e[i]; }
  var /= n;
  if (var < 1e-3f) return false;                 // flat line

  const int LMIN = (int)(0.30f * ECG_FS), LMAX = (int)(1.50f * ECG_FS);
  static float r[ECG_FS * 2];
  for (int lag = LMIN - 1; lag <= LMAX + 1; lag++) {
    float s = 0;
    for (int i = 0; i + lag < n; i++) s += e[i] * e[i + lag];
    r[lag] = (s / (n - lag)) / var;              // 1.0 = perfectly periodic
  }
  // 5. choose the first strong local maximum (shortest period within 85% of the best)
  float best = 0;
  for (int lag = LMIN; lag <= LMAX; lag++)
    if (r[lag] > r[lag - 1] && r[lag] >= r[lag + 1] && r[lag] > best) best = r[lag];
  if (best < 0.30f) return false;                // no clear rhythm
  int pick = -1;
  for (int lag = LMIN; lag <= LMAX; lag++)
    if (r[lag] > r[lag - 1] && r[lag] >= r[lag + 1] && r[lag] >= 0.85f * best) { pick = lag; break; }
  if (pick < 0) return false;
  float d = r[pick - 1] - 2 * r[pick] + r[pick + 1];   // sub-sample refinement
  float period = pick + (d != 0 ? 0.5f * (r[pick - 1] - r[pick + 1]) / d : 0);
  float rate = 60.0f * ECG_FS / period;
  if (rate < 38 || rate > 205) return false;
  *bpm = (int32_t)(rate + 0.5f);
  float q = r[pick] / 0.8f;                      // clean ECG scores about 0.8 or more
  *quality = q > 1.0f ? 1.0f : (q < 0 ? 0 : q);
  return true;
}

// ECG sampling runs on its own core at exactly 250 samples/s into a ring buffer.
uint16_t ecgRing[ECG_N];
volatile int ecgCount = 0;          // how many valid samples since the leads were last off
volatile int ecgHead = 0;
volatile uint32_t ecgTotal = 0;       // samples written so far (for streaming the graph)
uint32_t ecgSent = 0;
portMUX_TYPE ecgMux = portMUX_INITIALIZER_UNLOCKED;
uint16_t ecgCopy[ECG_N];

void ecgTask(void*) {
  TickType_t last = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(1000 / ECG_FS);   // 4 ms
  for (;;) {
    vTaskDelayUntil(&last, period);
    bool off = digitalRead(LO_PLUS) || digitalRead(LO_MINUS);
    uint16_t v = analogRead(ECG_PIN);
    portENTER_CRITICAL(&ecgMux);
    if (off || v < 5 || v > 4090) {       // electrode off or amplifier saturated: start over
      ecgCount = 0;
    } else {
      ecgRing[ecgHead] = v;
      ecgHead = (ecgHead + 1) % ECG_N;
      ecgTotal++;
      if (ecgCount < ECG_N) ecgCount++;
    }
    portEXIT_CRITICAL(&ecgMux);
  }
}

// Streams the newest raw ECG samples for the live graph:  ECG d=2010,2015,...*HH
void streamEcg() {
  uint32_t tot; int head, fresh;
  portENTER_CRITICAL(&ecgMux);
  tot = ecgTotal; head = ecgHead;
  fresh = (int)(tot - ecgSent);
  if (fresh > ECG_N) fresh = ECG_N;
  for (int k = 0; k < fresh; k++) ecgCopy[k] = ecgRing[(head - fresh + k + ECG_N) % ECG_N];
  portEXIT_CRITICAL(&ecgMux);
  ecgSent = tot;
  const int CHUNK = 50;
  char line[400];
  for (int a = 0; a < fresh; a += CHUNK) {
    int n = snprintf(line, sizeof(line), "ECG d=");
    for (int i = a; i < a + CHUNK && i < fresh; i++)
      n += snprintf(line + n, sizeof(line) - n, i == a ? "%u" : ",%u", ecgCopy[i]);
    uint8_t cs = 0;
    for (const char* q = line; *q; q++) cs ^= (uint8_t)*q;
    Serial.printf("%s*%02X\n", line, cs);
  }
}

// GSR: average of 64 reads in millivolts. Baseline = open (no contact) level at boot.
float gsrBaseline = 0;

float readGsrMv() {
  float s = 0;
  for (int i = 0; i < 64; i++) { s += analogReadMilliVolts(GSR_PIN); delayMicroseconds(200); }
  return s / 64.0f;
}

// Prints e.g.  VITAL type=spo2 value=98.00 ir=150000*5A     (trailing *HH = XOR of the bytes before it)
void sendVital(const char* type, float value, const char* extraName = nullptr, float extra = 0) {
  char body[96];
  int n = snprintf(body, sizeof(body), "VITAL type=%s value=%.2f", type, value);
  if (extraName) n += snprintf(body + n, sizeof(body) - n, " %s=%.2f", extraName, extra);
  uint8_t cs = 0;
  for (const char* p = body; *p; p++) cs ^= (uint8_t)*p;
  Serial.printf("%s*%02X\n", body, cs);
}

// Sensor check line, e.g.  STATUS max30102=1 finger=0 ecg=1 gsr=0*HH
void sendStatus(int maxOk, int finger, int ecg, int gsr) {
  char body[96];
  snprintf(body, sizeof(body), "STATUS max30102=%d finger=%d ecg=%d gsr=%d", maxOk, finger, ecg, gsr);
  uint8_t cs = 0;
  for (const char* p = body; *p; p++) cs ^= (uint8_t)*p;
  Serial.printf("%s*%02X\n", body, cs);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Wire.begin(21, 22);

  pinMode(LO_PLUS, INPUT);
  pinMode(LO_MINUS, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(ECG_PIN, ADC_11db);
  analogSetPinAttenuation(GSR_PIN, ADC_11db);

  if (!sensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("MAX30102 not found - check wiring");
    while (1) { sendStatus(0, 0, 0, 0); delay(1000); }   // tells the service the sensor is missing
  }
  sensor.setup(30, 4, 2, 100, 411, 4096);   // brightness, average, RED+IR, 100 Hz, pulse width, range

  delay(500);
  gsrBaseline = readGsrMv();                // fingers off the GSR electrodes right now
  xTaskCreatePinnedToCore(ecgTask, "ecg", 4096, nullptr, 2, nullptr, 0);

  for (int i = 0; i < BUFFER_SIZE; i++) {
    while (!sensor.available()) sensor.check();
    redBuffer[i] = sensor.getRed();
    irBuffer[i] = sensor.getIR();
    sensor.nextSample();
  }
  maxim_heart_rate_and_oxygen_saturation(irBuffer, BUFFER_SIZE, redBuffer,
                                         &spo2, &validSPO2, &heartRateUnused, &validHeartRateUnused);
}

// One pass of loop() takes about 1 s (25 new MAX30102 samples at 25/s).
void loop() {
  for (int i = 25; i < BUFFER_SIZE; i++) {
    redBuffer[i - 25] = redBuffer[i];
    irBuffer[i - 25] = irBuffer[i];
  }
  for (int i = 75; i < BUFFER_SIZE; i++) {
    while (!sensor.available()) sensor.check();
    redBuffer[i] = sensor.getRed();
    irBuffer[i] = sensor.getIR();
    sensor.nextSample();
  }
  maxim_heart_rate_and_oxygen_saturation(irBuffer, BUFFER_SIZE, redBuffer,
                                         &spo2, &validSPO2, &heartRateUnused, &validHeartRateUnused);

  // --- SpO2 + temperature: together, only with a finger on the sensor and a valid SpO2.
  //     The temperature is simulated for now (MLX90614 not detected).
  long ir = irBuffer[BUFFER_SIZE - 1];
  if (ir >= 50000 && validSPO2) {
    sendVital("spo2", spo2, "ir", ir);
    sendVital("temperature", 36.4 + random(0, 20) / 100.0);
  }

  streamEcg();                              // raw ECG for the live graph (only while electrodes are on)

  // --- ECG heart rate: only with both electrodes on, a full clean 8 s window, and a clear rhythm.
  int have;
  portENTER_CRITICAL(&ecgMux);
  have = ecgCount;
  if (have >= ECG_N) {
    int h = ecgHead;                       // oldest sample is at ecgHead
    for (int i = 0; i < ECG_N; i++) ecgCopy[i] = ecgRing[(h + i) % ECG_N];
  }
  portEXIT_CRITICAL(&ecgMux);
  if (have >= ECG_N) {
    int32_t bpm; float q;
    if (estimateEcgHR(ecgCopy, ECG_N, &bpm, &q) && q >= ECG_MIN_QUALITY)
      sendVital("heart_rate", bpm, "q", q);
  }

  // --- GSR: only while skin is touching the electrodes (level clearly away from the open level).
  float mv = readGsrMv();
  bool contact = fabsf(mv - gsrBaseline) > 50.0f && mv > 1.0f;
  if (contact) sendVital("gsr", mv);

  // --- sensor check: what is connected/ready right now (used by /sensors/check)
  bool leadsOn = !(digitalRead(LO_PLUS) || digitalRead(LO_MINUS));
  sendStatus(1, ir >= 50000, leadsOn, contact);
}
