# The start gate. Small on purpose: a person is waiting on its cold start.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY osce_gate ./osce_gate
COPY static ./static
# Cloud Run sets PORT.
CMD exec uvicorn osce_gate.main:build_from_env --factory --host 0.0.0.0 --port ${PORT:-8080}
