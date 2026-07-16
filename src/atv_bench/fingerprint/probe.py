"""Allowlist-emit fingerprint probe (eng T1).

The emitter builds the manifest FIELD BY FIELD from a fixed schema. It never copies
a parsed config dict and deletes secrets (a denylist can't guarantee leak-free — a
new secret-bearing field would pass straight through). Every value that lands in the
manifest has been through `scan.is_safe_name` / `scan.is_secret`; anything that fails
becomes an `unknown[{field, reason}]` entry, never a raw emit.

v1 parity target: claude-code only. Other harnesses emit their surfaces as unknown[].
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atv_bench.fingerprint import reader
from atv_bench.fingerprint.scan import is_safe_name, is_secret

PROBE_VERSION = "1.0.0"

# The fixed, published schema. Exactly these top-level keys, always.
FINGERPRINT_SCHEMA_KEYS = (
    "harness",
    "model",
    "gstack",
    "skills",
    "mcps",
    "plugins",
    "custom_agents_count",
    "unknown",
    "probe_version",
)


@dataclass
class ProbeResult:
    manifest: dict[str, Any]
    log: str


@dataclass
class _Builder:
    """Accumulates allowlisted names and structured unknowns as we walk config."""
    unknown: list[dict[str, str]] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    def note_unknown(self, field_name: str, reason: str) -> None:
        # field_name is a schema field label, never user content — safe to log.
        self.unknown.append({"field": field_name, "reason": reason})

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)

    def safe_names(self, candidates: list[str], field_label: str) -> list[str]:
        """Return only the names that pass the safety scan; unsafe → unknown[]."""
        out: list[str] = []
        for name in candidates:
            if is_safe_name(name):
                out.append(name)
            else:
                self.note_unknown(field_label, reader.REASON_NAME_UNSAFE)
        return sorted(out)


def probe_claude_code(home: Path) -> ProbeResult:
    """Probe a claude-code config root (normally ~/.claude) → leak-safe manifest."""
    home = Path(home)
    b = _Builder()

    # --- model (allowlisted single value from settings.json) ---
    model = "unknown"
    settings = reader.read_json(home / "settings.json", home)
    gstack_flag = False
    if settings.ok and isinstance(settings.value, dict):
        raw_model = settings.value.get("model")
        if isinstance(raw_model, str) and is_safe_name(raw_model):
            model = raw_model
        elif raw_model is not None:
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        # NB: we deliberately touch ONLY settings.value["model"]. env/apiKeyHelper/
        # awsSecret and any other field are never read — allowlist by construction.
    elif not settings.ok:
        b.note_unknown("model", settings.reason or reader.REASON_NOT_READABLE)

    # --- skills (dir basenames only) ---
    skill_names, skill_errs = reader.list_child_dir_names(home / "skills", home)
    for _name, reason in skill_errs:
        b.note_unknown("skills", reason)
    skills = b.safe_names(skill_names, "skills")
    if "gstack" in skills:
        gstack_flag = True

    # --- plugins (dir basenames only) ---
    plugin_names, plugin_errs = reader.list_child_dir_names(home / "plugins", home)
    for _name, reason in plugin_errs:
        b.note_unknown("plugins", reason)
    plugins = b.safe_names(plugin_names, "plugins")

    # --- mcps (server NAMES only, from .mcp.json keys) ---
    mcps: list[str] = []
    mcp_cfg = reader.read_json(home / ".mcp.json", home)
    if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
        servers = mcp_cfg.value.get("mcpServers")
        if isinstance(servers, dict):
            # We read only the KEYS (server names), never the values (command/env/url).
            mcps = b.safe_names(list(servers.keys()), "mcps")
    elif not mcp_cfg.ok and mcp_cfg.reason != reader.REASON_NOT_READABLE:
        b.note_unknown("mcps", mcp_cfg.reason or reader.REASON_MALFORMED)

    # --- custom_agents_count (count of agent files, never their contents) ---
    custom_agents_count, agent_errs = reader.count_child_files(home / "agents", home, suffix=".md")
    for _name, reason in agent_errs:
        b.note_unknown("custom_agents_count", reason)

    manifest: dict[str, Any] = {
        "harness": "claude-code",
        "model": model,
        "gstack": gstack_flag,
        "skills": skills,
        "mcps": mcps,
        "plugins": plugins,
        "custom_agents_count": custom_agents_count,
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    b.log(f"probed claude-code: "
          f"{len(skills)} skills, {len(mcps)} mcps, {len(plugins)} plugins, "
          f"{custom_agents_count} agents, {len(b.unknown)} unknown")
    return ProbeResult(manifest=manifest, log="\n".join(b.log_lines))
