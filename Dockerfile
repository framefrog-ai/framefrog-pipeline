FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FRAMEFROG_OUTPUT_DIR=/tmp/framefrog
EXPOSE 8080

# HTTP surface. For a queue-worker deployment, override the command with:
#   python main.py --input "$INPUT_URI" --out /tmp/framefrog
CMD ["uvicorn", "framefrog.api:app", "--host", "0.0.0.0", "--port", "8080"]
