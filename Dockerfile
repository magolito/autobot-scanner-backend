FROM python:3.12-slim

WORKDIR /app

# system deps for pandas/numpy wheels build faster with these present
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
EXPOSE 8501
EXPOSE 8502

# Runs the API (with in-process scheduler) and BOTH Streamlit dashboards
# as three processes sharing this container's local filesystem — see
# DEPLOYMENT.md's "Deployment topology" section for why this is the
# correct default with SQLite storage, and what the alternatives are.
#
# To run only the API (e.g. if you're deploying the dashboard separately
# via a different topology), override the command with:
#   uvicorn opportunity_scanner.api:app --host 0.0.0.0 --port 8000
CMD ["./start.sh"]
