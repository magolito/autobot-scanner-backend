#!/bin/bash
# Runs the API (with its in-process scheduler, if RUN_SCHEDULER_IN_PROCESS=true)
# and BOTH Streamlit dashboards as three processes in one container. All
# three read/write the same local SQLite files, which is why this is the
# recommended topology for now — see DEPLOYMENT.md's "Deployment topology"
# section for the reasoning and the alternatives that DON'T work correctly
# with SQLite.
set -e

uvicorn opportunity_scanner.api:app --host 0.0.0.0 --port "${API_PORT:-8000}" &
API_PID=$!

streamlit run opportunity_scanner/dashboard.py \
  --server.port "${DASHBOARD_PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true &
DASHBOARD_PID=$!

streamlit run opportunity_scanner/meme_dashboard.py \
  --server.port "${MEME_DASHBOARD_PORT:-8502}" \
  --server.address 0.0.0.0 \
  --server.headless true &
MEME_DASHBOARD_PID=$!

# If any process dies, bring the whole container down rather than limping
# along with only part of the system running silently.
wait -n "$API_PID" "$DASHBOARD_PID" "$MEME_DASHBOARD_PID"
EXIT_CODE=$?
echo "One of the processes exited (code $EXIT_CODE) — shutting down the others."
kill "$API_PID" "$DASHBOARD_PID" "$MEME_DASHBOARD_PID" 2>/dev/null || true
exit "$EXIT_CODE"
