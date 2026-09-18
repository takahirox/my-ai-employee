# Optional model-free PostgreSQL/Redis application fixture, not a production default.
ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-15 redis-server \
 && rm -rf /var/lib/apt/lists/*
COPY service-fixture.py /usr/local/lib/fleet-service-fixture.py
COPY service-fixture-tests /usr/local/bin/fleet-service-tests
RUN chmod 755 /usr/local/bin/fleet-service-tests
