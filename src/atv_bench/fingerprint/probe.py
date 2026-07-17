"""Allowlist-emit fingerprint probe (eng T1).

The emitter builds the manifest FIELD BY FIELD from a fixed schema. It never copies
a parsed config dict and deletes secrets (a denylist can't guarantee leak-free — a
new secret-bearing field would pass straight through). Every value that lands in the
manifest has been through `scan.is_safe_name` / `scan.is_secret`; anything that fails
becomes an `unknown[{field, reason}]` entry, never a raw emit.

v1 ships live readers for claude-code and copilot-cli (below); other harnesses are
registered as planned in `atv_bench.harnesses` and the generic `probe()` dispatcher fails
closed on them until a reader lands. See `probe()` at the bottom of this module for the
harness-agnostic entry.
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


def probe_copilot_cli(home: Path) -> ProbeResult:
    """Probe a GitHub Copilot CLI config root (normally ~/.copilot) → leak-safe manifest.

    Same allowlist-emit discipline as claude-code, mapped onto Copilot's layout:
      - model / enabled plugins / disabled skills+MCPs  → settings.json (values allowlisted)
      - MCP server names                                → mcp-config.json["mcpServers"] keys
      - skills + custom agents                          → nested under
        installed-plugins/<marketplace>/<plugin>/{skills/<name>, agents/*.md}

    Only NAMES and COUNTS are read — never a file body. Every value passes the safety scan;
    anything unsafe or unreadable becomes unknown[{field, reason}]. Disabled skills/MCPs
    (denylists in settings.json) are subtracted so the fingerprint reflects the *effective*
    harness, not everything installed.
    """
    home = Path(home)
    b = _Builder()

    # --- settings.json: model + enabled plugins + disabled skills/mcps ---
    model = "unknown"
    enabled_plugins_raw: dict = {}
    disabled_skills: set[str] = set()
    disabled_mcps: set[str] = set()
    settings = reader.read_json(home / "settings.json", home)
    if settings.ok and isinstance(settings.value, dict):
        raw_model = settings.value.get("model")
        if isinstance(raw_model, str) and is_safe_name(raw_model):
            model = raw_model
        elif raw_model is not None:
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        ep = settings.value.get("enabledPlugins")
        if isinstance(ep, dict):
            enabled_plugins_raw = ep
        ds = settings.value.get("disabledSkills")
        if isinstance(ds, list):
            disabled_skills = {s for s in ds if isinstance(s, str)}
        dm = settings.value.get("disabledMcpServers")
        if isinstance(dm, list):
            disabled_mcps = {s for s in dm if isinstance(s, str)}
        # NB: we read ONLY these named keys. theme/logLevel/experimental/etc. and any
        # future field are never emitted — allowlist by construction.
    elif not settings.ok:
        b.note_unknown("model", settings.reason or reader.REASON_NOT_READABLE)

    # --- plugins (enabledPlugins keys are "name@marketplace"; take the name) ---
    plugin_candidates = [
        k.split("@", 1)[0] for k in enabled_plugins_raw if isinstance(k, str) and k.split("@", 1)[0]
    ]
    plugins = b.safe_names(plugin_candidates, "plugins")

    # --- skills + custom_agents_count (nested inside each installed plugin) ---
    skill_candidates: list[str] = []
    custom_agents_count = 0
    plugins_root = home / "installed-plugins"
    mkt_names, mkt_errs = reader.list_child_dir_names(plugins_root, home)
    for _name, reason in mkt_errs:
        b.note_unknown("skills", reason)
    for mkt in mkt_names:
        plug_names, plug_errs = reader.list_child_dir_names(plugins_root / mkt, home)
        for _name, reason in plug_errs:
            b.note_unknown("skills", reason)
        for plug in plug_names:
            s_names, s_errs = reader.list_child_dir_names(plugins_root / mkt / plug / "skills", home)
            for _name, reason in s_errs:
                b.note_unknown("skills", reason)
            skill_candidates.extend(s_names)
            a_count, a_errs = reader.count_child_files(
                plugins_root / mkt / plug / "agents", home, suffix=".md")
            for _name, reason in a_errs:
                b.note_unknown("custom_agents_count", reason)
            custom_agents_count += a_count

    # Dedupe across plugins and drop skills explicitly disabled in settings.
    effective_skills = sorted({s for s in skill_candidates if s not in disabled_skills})
    skills = b.safe_names(effective_skills, "skills")

    # gstack ships as either a plugin or a nested skill; count it present either way.
    gstack_flag = "gstack" in plugins or "gstack" in skills

    # --- mcps (server NAMES from mcp-config.json keys, minus disabled) ---
    mcps: list[str] = []
    mcp_cfg = reader.read_json(home / "mcp-config.json", home)
    if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
        servers = mcp_cfg.value.get("mcpServers")
        if isinstance(servers, dict):
            # Read only the KEYS (server names), never the values (command/env/url/token).
            names = [k for k in servers.keys() if k not in disabled_mcps]
            mcps = b.safe_names(names, "mcps")
    elif not mcp_cfg.ok and mcp_cfg.reason != reader.REASON_NOT_READABLE:
        b.note_unknown("mcps", mcp_cfg.reason or reader.REASON_MALFORMED)

    manifest: dict[str, Any] = {
        "harness": "copilot-cli",
        "model": model,
        "gstack": gstack_flag,
        "skills": skills,
        "mcps": mcps,
        "plugins": plugins,
        "custom_agents_count": custom_agents_count,
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    b.log(f"probed copilot-cli: "
          f"{len(skills)} skills, {len(mcps)} mcps, {len(plugins)} plugins, "
          f"{custom_agents_count} agents, {len(b.unknown)} unknown")
    return ProbeResult(manifest=manifest, log="\n".join(b.log_lines))


# Harness key -> reader. Only LIVE harnesses (a real leak-safe reader) appear here; a
# planned harness has no entry and `probe()` fails closed rather than emit a placeholder.
_READERS = {"claude-code": probe_claude_code, "copilot-cli": probe_copilot_cli}


def probe(home: Path | None = None, harness: str | None = None) -> ProbeResult:
    """Harness-agnostic entry point: fingerprint the local coding-agent harness.

    Resolves the harness (explicit `harness=`, else auto-detected from which config dir
    exists, else the default) via `atv_bench.harnesses`, then dispatches to that harness's
    leak-safe reader. `home` overrides the config root's parent ($HOME); when omitted the
    harness registry supplies the standard per-harness config dir.

    Fails closed with a ValueError (actionable message) for an unknown or planned harness —
    a harness with no reader must never yield an empty/placeholder fingerprint.
    """
    from atv_bench import harnesses as hz

    key = harness or hz.detect_harness() or hz.DEFAULT_HARNESS
    hz.assert_probeable(key)  # raises ValueError for unknown/planned harness

    reader_fn = _READERS.get(key)
    if reader_fn is None:  # live in registry but no reader wired — treat as planned
        raise ValueError(
            f"harness {key!r} has no fingerprint reader wired in this build "
            f"(see `atv-bench harnesses`)."
        )

    # If the caller passed an explicit config root, honor it; otherwise use the standard
    # per-harness dir under $HOME from the registry.
    root = Path(home) if home is not None else hz.config_root_for(key)
    return reader_fn(root)
