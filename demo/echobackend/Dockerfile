# A minimal image for the echo backend used in local demos, docker-compose,
# and as the image the autoscaling simulation launches new instances of.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir aiohttp==3.14.1

COPY demo/echobackend/main.py ./main.py

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --shell /bin/bash backenduser
USER backenduser

EXPOSE 9000

ENTRYPOINT ["python3", "main.py"]
CMD ["--port", "9000", "--id", "backend"]
