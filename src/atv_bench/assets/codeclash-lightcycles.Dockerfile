FROM ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates git python3 python-is-python3 tar \
 && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/CodeClash-ai/LightCycles.git /workspace \
 && cd /workspace \
 && git checkout --detach 32e4218844805340371e9fe11902a49e5a1e40a6 \
 && git remote set-url origin https://github.com/CodeClash-ai/LightCycles.git

LABEL org.opencontainers.image.source="https://github.com/CodeClash-ai/LightCycles" \
      org.opencontainers.image.revision="32e4218844805340371e9fe11902a49e5a1e40a6" \
      org.opencontainers.image.atv.codeclash-pin="f0694c64ecf6abfca2bc867bad2de9333fef5be8" \
      org.opencontainers.image.atv.base-digest="sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982"

WORKDIR /workspace
