FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Colombo

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api_sample_api.py config.json ./

# run as non-root user with fixed UID 1000 (host data folder is chowned to 1000)
RUN useradd -m -u 1000 collector && mkdir -p /app/data && chown -R collector /app
USER collector

# healthy if a cycle finished within the last 2 hours
HEALTHCHECK --interval=5m --timeout=10s --start-period=3m \
  CMD python -c "import time,sys;t=int(open('/app/data/.heartbeat').read());sys.exit(0 if time.time()-t<7200 else 1)"

CMD ["python", "api_sample_api.py"]
