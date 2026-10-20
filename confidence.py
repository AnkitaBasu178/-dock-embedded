"""Per-reading confidence, computed from measurement quality only.

confidence = signal_quality * stability_quality   (clamped to 0.05 .. 0.98)

* stability_quality: how much the samples in the window agree (max - min;
  for gsr, relative to its median).
* signal_quality:
    - spo2 (MAX30102): how healthy the IR signal is (no finger / weak -> low;
      near ADC saturation -> low).
    - heart_rate from the ECG: `q` (rhythm strength measured on the 8 s ECG window by the
      firmware, 0.8+ is a clean trace), or else how regular the R-R intervals are (`rrcv`, the
      coefficient of variation of the beat-to-beat intervals; erratic = noisy).
    - everything else: 1.0.

Nothing here looks at whether a value is "high" or "low" clinically.
"""
from statistics import mean

from .parser import Sample

# (spread that is perfectly good, spread that is as bad as it gets)
SPREAD = {
    "heart_rate": (3.0, 15.0),
    "spo2": (2.0, 8.0),
    "temperature": (0.2, 1.0),
    "weight": (0.3, 3.0),
    "height": (1.0, 5.0),
    "gsr": (0.05, 0.40),  # RELATIVE spread: (max - min) / median
}
MIN_Q = 0.1


def _stability(vtype: str, values: list[float]) -> float:
    good, bad = SPREAD[vtype]
    spread = max(values) - min(values)
    if vtype == "gsr":
        ref = sorted(values)[len(values) // 2]
        spread = spread / max(abs(ref), 1e-6)
    if spread <= good:
        return 1.0
    if spread >= bad:
        return MIN_Q
    return 1.0 - (1.0 - MIN_Q) * (spread - good) / (bad - good)


def _signal(ir: float) -> float:
    """MAX30102 IR level (18-bit ADC, max 262143). Sweet spot ~40k..200k."""
    if ir >= 250_000:
        return 0.3
    if ir > 200_000:
        return 1.0 - 0.5 * (ir - 200_000) / 50_000  # 1.0 -> 0.5
    if ir >= 40_000:
        return 1.0
    if ir >= 15_000:
        return 0.3 + 0.7 * (ir - 15_000) / 25_000
    return 0.1


def _rr_quality(cv: float) -> float:
    """R-R interval coefficient of variation: <= 0.10 steady, >= 0.35 looks like noise."""
    if cv <= 0.10:
        return 1.0
    if cv >= 0.35:
        return 0.2
    return 1.0 - 0.8 * (cv - 0.10) / 0.25


def score(vtype: str, samples: list[Sample]) -> float:
    values = [s.value for s in samples]
    q = _stability(vtype, values)
    if vtype in ("spo2", "heart_rate"):
        irs = [s.extras["ir"] for s in samples if "ir" in s.extras]
        cvs = [s.extras["rrcv"] for s in samples if "rrcv" in s.extras]
        qs = [s.extras["q"] for s in samples if "q" in s.extras]
        if qs:
            q *= min(1.0, max(0.1, mean(qs) / 0.8))  # ECG rhythm quality from the firmware, 0.8+ = clean
        elif cvs:
            q *= _rr_quality(mean(cvs))
        elif irs:
            q *= _signal(mean(irs))
        else:
            q *= 0.5  # no quality info -> cannot vouch for the signal
    return round(min(0.98, max(0.05, q)), 2)
