# Copilot OCI protocol-v1 harnesses

This example builds one OCI image for the actual GitHub Copilot CLI entry
surfaces of:

- ATV-Phoenix: `copilot --agent phoenix`
- microsoft/hve-core: `copilot --plugin-dir /opt/atv/hve-plugin --agent hve-core:rpi-agent`

The wrapper is standalone and model-provider agnostic. It speaks the
controller-authority-preserving ATV v1 sequence:

1. read one `atv.trial-request/v1`;
2. emit raw harness `hello` with `harness_sequence: 0`;
3. wait for and verify the controller-authored `accepted` event and exact
   request digest;
4. launch Copilot with an argv vector, never a shell command;
5. translate Copilot JSONL model/tool/usage events;
6. accept controller cancellation, terminate the complete process tree, hash
   permitted changed workspace artifacts, and emit exactly one terminal
   `result`.

Protocol stdout is JSONL only. Diagnostics go to stderr. Copilot stdout and
stderr are drained concurrently and bounded by the negotiated trial limits.

## Pinned inputs

Pins recorded on July 20, 2026:

| Input | Pin | SHA-256 / image digest |
|---|---|---|
| GitHub Copilot npm package | `@github/copilot@0.0.394` (display `1.0.72-1`, build `3d79feb`) | `0362769b6fb2aeb7f908a5d9ee1b9e2a5fee6ce6ea7f80c5533d9950252211bd` |
| Copilot Linux x64 package | `@github/copilot-linux-x64@0.0.394` | `7d2c7cd9da1d0442f27112807621890b8eefa7b9bbf223cfec6a27d495ee4393` |
| ATV-Phoenix | `233e8e1e968bbc0b1dc446d7830efa82489bf118` (`0.4.0`) | archive `86c52f63c8a8e692995ef36c4737e1cf9c2568adf2593d0bf9362469ab80560f` |
| microsoft/hve-core | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` (`3.3.101`) | archive `0fdbcd46102fb708038bdcd6ea9ed8ae5a4b94e294f18848a283e5023102b7c0` |
| Rust builder | `rust:1.88.0-slim-bookworm` | OCI index `sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89` |
| Node runtime | `node:22.17.0-bookworm-slim` | OCI index `sha256:b04ce4ae4e95b522112c2e5c52f781471a5cbc3b594527bcddedee9bc48c03a0` |

The Dockerfile verifies all remote archives with BuildKit `ADD --checksum`.
The image still installs Debian `git`, `python3`, and CA packages from the
Bookworm repository during the build. Capture and publish the resulting image
by immutable digest; do not claim byte-for-byte rebuild reproducibility from
the Dockerfile alone.

## Build and materialize manifests

```bash
docker build \
  --file examples/harnesses/copilot-oci/Dockerfile \
  --tag atv-copilot-harnesses:local \
  examples/harnesses/copilot-oci

IMAGE_ID=$(docker image inspect atv-copilot-harnesses:local --format '{{.Id}}')
IMAGE_DIGEST=${IMAGE_ID#sha256:}

sed "s/__IMAGE_DIGEST__/${IMAGE_DIGEST}/g" \
  examples/harnesses/copilot-oci/phoenix.harness.json.template \
  > phoenix.harness.json
sed "s/__IMAGE_DIGEST__/${IMAGE_DIGEST}/g" \
  examples/harnesses/copilot-oci/hve-core.harness.json.template \
  > hve-core.harness.json

atv-bench benchmark harness validate phoenix.harness.json
atv-bench benchmark harness validate hve-core.harness.json
```

For a registry publication, replace the local config digest with the immutable
registry manifest digest returned after push. Both harnesses must use the same
common image digest in a Controlled comparison.

## Runtime contract

The controller injects one credential only:

```text
ATV_MODEL_GATEWAY_HANDLE
```

The wrapper converts the request's model gateway and selected model into:

```text
COPILOT_OFFLINE=true
COPILOT_PROVIDER_BASE_URL=https://<gateway>/v1
COPILOT_PROVIDER_TYPE=openai
COPILOT_PROVIDER_BEARER_TOKEN=<ATV_MODEL_GATEWAY_HANDLE>
COPILOT_PROVIDER_WIRE_API=responses
COPILOT_MODEL=<first allowed model>
```

`HOME`, `COPILOT_HOME`, XDG directories, `TMPDIR`, Phoenix state, and hve-core
state are fresh per trial under `/tmp`. GitHub/provider tokens, ambient Copilot
profiles, proxy variables, MCP servers, and inherited user skills are not
forwarded. Phoenix receives its pinned agent, skills, and `phoenix-mcp`
registration from a read-only image template. hve-core receives the
dereferenced pinned plugin tree. hve-core telemetry is disabled.

The wrapper emits cost as unsupported. Copilot's premium-request counter is not
USD and is therefore not written into `cost_microusd`.

## Credibility boundary

This wrapper makes both harnesses executable through the same protocol and
image. It does not by itself establish a benchmark winner.

For Controlled eligibility, the external model gateway must prove that every
top-level and subagent request resolved to the preregistered model. hve-core
agents can express their own model preferences. If gateway receipts do not
prove that `--model` overrode every subagent call, classify that run under
Systems rather than Controlled.

The public ATV-Bench CLI does not yet provide the production OpenAI Responses
gateway/operator needed for a real model-backed Phoenix versus hve-core
experiment. Unit tests inject a fake Copilot executable and require no network
or authentication.
