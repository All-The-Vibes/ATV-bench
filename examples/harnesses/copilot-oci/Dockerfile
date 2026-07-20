# syntax=docker/dockerfile:1.10

# The source, package, and base-image identities below are intentionally immutable.
# Re-pin every digest together; do not update only a display version.
FROM rust:1.88.0-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS phoenix-builder

ADD --checksum=sha256:86c52f63c8a8e692995ef36c4737e1cf9c2568adf2593d0bf9362469ab80560f \
    https://github.com/All-The-Vibes/ATV-Phoenix/archive/233e8e1e968bbc0b1dc446d7830efa82489bf118.tar.gz \
    /tmp/phoenix.tar.gz
RUN mkdir -p /src/phoenix \
    && tar -xzf /tmp/phoenix.tar.gz --strip-components=1 -C /src/phoenix \
    && test "$(sed -n 's/^version = "\(.*\)"/\1/p' /src/phoenix/Cargo.toml | head -1)" = "0.4.0" \
    && cargo build \
         --locked \
         --release \
         --bin phoenix-mcp \
         --manifest-path /src/phoenix/Cargo.toml

FROM node:22.17.0-bookworm-slim@sha256:b04ce4ae4e95b522112c2e5c52f781471a5cbc3b594527bcddedee9bc48c03a0 AS runtime

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates \
         git \
         python3 \
    && rm -rf /var/lib/apt/lists/*

# GitHub Copilot CLI npm 0.0.394 displays 1.0.72-1 and records build commit
# 3d79feb. Both the JS package and Linux native package are pinned by SHA-256.
ADD --checksum=sha256:0362769b6fb2aeb7f908a5d9ee1b9e2a5fee6ce6ea7f80c5533d9950252211bd \
    https://registry.npmjs.org/@github/copilot/-/copilot-0.0.394.tgz \
    /tmp/copilot.tgz
ADD --checksum=sha256:7d2c7cd9da1d0442f27112807621890b8eefa7b9bbf223cfec6a27d495ee4393 \
    https://registry.npmjs.org/@github/copilot-linux-x64/-/copilot-linux-x64-0.0.394.tgz \
    /tmp/copilot-linux-x64.tgz

# hve-core contains Git symlinks into its source tree. Extract and dereference
# them inside Linux instead of copying a core.symlinks=false Windows checkout.
ADD --checksum=sha256:0fdbcd46102fb708038bdcd6ea9ed8ae5a4b94e294f18848a283e5023102b7c0 \
    https://github.com/microsoft/hve-core/archive/5c15a03c78da2408527693e0fc3b3e387bf99cb2.tar.gz \
    /tmp/hve-core.tar.gz

RUN mkdir -p \
         /opt/atv/bin \
         /opt/atv/copilot-home/phoenix \
         /opt/atv/hve-source \
         /opt/copilot/node_modules/@github/copilot \
         /opt/copilot/node_modules/@github/copilot-linux-x64 \
    && tar -xzf /tmp/copilot.tgz \
         --strip-components=1 \
         -C /opt/copilot/node_modules/@github/copilot \
    && tar -xzf /tmp/copilot-linux-x64.tgz \
         --strip-components=1 \
         -C /opt/copilot/node_modules/@github/copilot-linux-x64 \
    && test "$(node -p "require('/opt/copilot/node_modules/@github/copilot/package.json').version")" = "0.0.394" \
    && test "$(node -p "require('/opt/copilot/node_modules/@github/copilot/package.json').buildMetadata.gitCommit")" = "3d79feb" \
    && ln -s /opt/copilot/node_modules/@github/copilot/npm-loader.js /opt/atv/bin/copilot \
    && chmod 0555 /opt/copilot/node_modules/@github/copilot/npm-loader.js \
    && tar -xzf /tmp/hve-core.tar.gz \
         --strip-components=1 \
         -C /opt/atv/hve-source \
    && test "$(node -p "require('/opt/atv/hve-source/package.json').version")" = "3.3.101" \
    && cp -aL /opt/atv/hve-source/plugins/hve-core /opt/atv/hve-plugin \
    && test -f /opt/atv/hve-plugin/.github/plugin/plugin.json \
    && test -f /opt/atv/hve-plugin/agents/hve-core/rpi-agent.md \
    && rm -rf /opt/atv/hve-source /tmp/*.tgz /tmp/*.tar.gz

COPY --from=phoenix-builder /src/phoenix/target/release/phoenix-mcp /opt/atv/bin/phoenix-mcp
COPY protocol_wrapper.py /opt/atv/bin/protocol-wrapper

# ``doctor --fix`` installs the exact agent, skills, and MCP registration
# embedded in the pinned Phoenix binary.
RUN chmod 0555 /opt/atv/bin/phoenix-mcp /opt/atv/bin/protocol-wrapper \
    && /opt/atv/bin/phoenix-mcp doctor \
         --fix \
         --home /opt/atv/copilot-home/phoenix \
    && chmod -R a=rX /opt/atv /opt/copilot

ENV PATH="/opt/atv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

WORKDIR /workspace
USER 65534:65534

# ATV-Bench supplies the manifest entrypoint as the container command.
CMD ["/opt/atv/bin/protocol-wrapper", "--help"]
