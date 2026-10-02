# OpsMind portal: serves both the JSON API and the dashboard UI from one
# container, so the browser never holds a GCP credential.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app/opsmind/

ENV PORT=8080 DATA_SOURCE=gcp
EXPOSE 8080
CMD exec uvicorn opsmind.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log
