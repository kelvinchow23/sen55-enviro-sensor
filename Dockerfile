FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sen55.py config.py storage.py sampler.py app.py healthcheck.py ./

ENV PYTHONUNBUFFERED=1

# Report container health from the app's own /health endpoint. Uses python
# (the slim base has no curl/wget); exits non-zero unless state == "ok", which
# marks the container "unhealthy" once the sampler is dead or stale. Note: this
# is a *signal* for `docker inspect`/monitoring; compose's restart policy fires
# on process exit, not on health status, so pair with an external watcher if
# you want automatic recovery.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "healthcheck.py"]

# Sampler + API run in one process; uvicorn launches the sampler on startup.
# Single worker on purpose: multiple workers would each spawn a sampler thread
# and double-write. The Pi Zero 2 W is fine with one worker at this load.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
