FROM python:3.12-slim

# System deps: ffmpeg for audio extraction/transcoding, ca-certificates for HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy everything pip needs to build the package.
# (hatchling reads packages = ["src/recap_bot"] and `readme = "README.md"`, so
# both must exist at install time.)
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Runtime config — per-action model assignments. Read at startup from CWD.
COPY models.yaml ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

VOLUME ["/data"]

CMD ["python", "-m", "recap_bot"]
