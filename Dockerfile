FROM clickhouse/clickhouse-server:26.8@sha256:d3fd302416d9523d5d64239d5713d1c16c20ecea2045ac70d7e941b376cb266e AS runtime
USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/clickhouse-jev
COPY . .

FROM runtime AS demo
RUN python3 scripts/install.py \
      --output-dir /opt/clickhouse-jev/.demo/jev \
      --python /usr/bin/python3 \
      --endpoint http://mock:8765/judge --allow-http \
      --model mock/request --expected-model mock/result \
      --batch-size 2 --concurrency 2 \
    && cp .demo/jev/config.xml /etc/clickhouse-server/config.d/jev.xml \
    && mkdir -p .demo/jev/user_defined \
    && chown -R clickhouse:clickhouse .demo

FROM runtime AS tests
