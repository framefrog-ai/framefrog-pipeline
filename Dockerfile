FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional RIFE backend: ONNX Runtime + the 21 MB graph. Build with
#   --build-arg WITH_RIFE=0   to keep the image lean and use FFmpeg fallback.
ARG WITH_RIFE=1
RUN if [ "$WITH_RIFE" = "1" ]; then \
      pip install --no-cache-dir onnxruntime && \
      python scripts/fetch_models.py ; \
    fi

ENV FRAMEFROG_OUTPUT_DIR=/tmp/framefrog
EXPOSE 8080

# HTTP surface. For a queue-worker deployment, override the command with:
#   python main.py --input "$INPUT_URI" --out /tmp/framefrog
CMD ["uvicorn", "framefrog.api:app", "--host", "0.0.0.0", "--port", "8080"]
