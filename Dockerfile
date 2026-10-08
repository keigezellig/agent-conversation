FROM python:3.12-slim AS base
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 agent
USER agent

# Runner with read-aloud: adds paplay/pactl to play speech on the host's PulseAudio or PipeWire server.
FROM base AS runner-voice
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*
USER agent

FROM base AS test
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q"]

# The bridge is the last stage so a plain build produces it.
FROM base AS bridge
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
