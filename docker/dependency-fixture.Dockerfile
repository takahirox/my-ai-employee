# Model-free dependency contract fixture; supply the built worker's immutable ID.
ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
COPY dependency-fixture /usr/local/lib/fleet-dependency-fixture
COPY dependency-fixture/run-tests /usr/local/bin/fleet-image-tests
RUN chmod 755 /usr/local/bin/fleet-image-tests \
 && ln -s value.py /usr/local/lib/fleet-dependency-fixture/linked_value.py \
 && ln -s /home/fleet/native-canary /usr/local/lib/fleet-dependency-fixture/control-link
