# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Non-root user; add to dialout so it can reach /dev/ttyUSB* serial devices.
RUN groupadd -r smlgw && useradd -r -g smlgw -G dialout smlgw

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Default config location inside the container; mount a volume here.
# Create it writable by the non-root user so the web UI can persist edits.
ENV SMLGW_CONFIG=/config/config.yaml
RUN mkdir -p /config && chown -R smlgw:smlgw /config
VOLUME ["/config"]
EXPOSE 8000

USER smlgw
ENTRYPOINT ["smlgw"]
CMD ["run", "--host", "0.0.0.0", "--port", "8000"]
