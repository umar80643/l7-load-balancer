# Single-stage build: this project has no compiled artifacts and a small
# pure-Python dependency set (aiohttp, pydantic, prometheus_client), so a
# multi-stage build wouldn't meaningfully reduce final image size here --
# it would mostly add complexity. python:3.12-slim keeps the base small
# without the multi-stage machinery.
FROM python:3.12-slim

WORKDIR /app

# Copy dependency manifest first and install before copying the rest of the
# source tree -- this lets Docker cache the (slow) pip install layer across
# rebuilds where only application code changed, not dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY configs/ ./configs/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Runs as a non-root user -- standard container hardening practice; the
# process has no need for root privileges.
RUN useradd --create-home --shell /bin/bash lbuser
USER lbuser

EXPOSE 8080

ENTRYPOINT ["python3", "-m", "lb.main"]
CMD ["--config", "configs/config.json", "--log-level", "info"]
