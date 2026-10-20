#!/bin/bash
# Starts the station: reads the ESP32s over USB, keeps readings coming (demo mode) and,
# if bridge.env exists, pushes everything to the Render URL so P4 has one permanent link.
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
export CONTINUOUS=true
export CONFIDENCE_FLOOR=0.89
if [ -f bridge.env ]; then
  set -a; source bridge.env; set +a
  echo "Pushing to $PUSH_URL"
else
  echo "No bridge.env found: running locally only (see README, 'Render')"
fi
echo "Local: http://localhost:8010/data   graph: http://localhost:8010/ecg/plot"
exec caffeinate -d uvicorn app.main:app --host 0.0.0.0 --port 8010
