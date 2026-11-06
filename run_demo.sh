#!/usr/bin/env bash
# Starts the finance API and the Streamlit app together (Ctrl+C stops both).
# First time only:  python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
set -euo pipefail
cd "$(dirname "$0")"

if [ -d .venv ]; then
  source .venv/bin/activate
fi

if [ ! -f models/recommendation_rf.pkl ]; then
  echo "Training the recommendation model (first run)..."
  python -m scripts.train_recommendation_model
fi

uvicorn app.api.main:app --port 8000 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null' EXIT

until curl -s http://127.0.0.1:8000/health > /dev/null; do sleep 0.5; done
echo "API ready on http://127.0.0.1:8000  (docs: http://127.0.0.1:8000/docs)"

streamlit run streamlit_app.py --server.port 8501
