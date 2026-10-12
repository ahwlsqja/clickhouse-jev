FROM clickhouse/clickhouse-server:26.8@sha256:d3fd302416d9523d5d64239d5713d1c16c20ecea2045ac70d7e941b376cb266e
USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/clickhouse-jev
COPY . .
