# CPU image — the default build.
#
#   docker build -t framefrog-pipeline:cpu .
#   docker run --rm -p 8080:8080 framefrog-pipeline:cpu
#
# The image contains no secrets and no model weights. GPU runtimes live in
# Dockerfile.gpu.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    WORK_DIR=/app/work \
    MODEL_CACHE_DIR=/app/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user: the container never runs as root.
RUN groupadd --system framefrog \
    && useradd --system --gid framefrog --create-home --home-dir /home/framefrog framefrog \
    && mkdir -p /app/work /app/models \
    && chown -R framefrog:framefrog /app

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY assets/luts ./assets/luts
COPY scripts ./scripts
COPY main.py demo.py ./

RUN pip install --no-cache-dir . \
    && chown -R framefrog:framefrog /app

USER framefrog

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
