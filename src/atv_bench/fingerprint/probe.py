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

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atv_bench.fingerprint import reader
from atv_bench.fingerprint.scan import _has_secret_pattern, is_safe_name, is_secret

PROBE_VERSION = "1.0.0"

# The fixed, published schema. Exactly these top-level keys, always.
FINGERPRINT_SCHEMA_KEYS = (
    "harness",
    "model",
    "gstack",
    "skills",
    "nested_skills",
    "tools",
    "mcps",
    "plugins",
    "custom_agents_count",
    "cli_version",
    "unknown_runtime",
    "unknown",
    "probe_version",
)

# Which CLI binary each harness invokes at runtime (for the runtime-surface read).
_HARNESS_BINARY = {"claude-code": "claude", "copilot-cli": "copilot"}
# Cap the binary hash read so a huge/streamed binary can't stall the probe.
_MAX_BINARY_HASH_BYTES = 64 * 1024 * 1024


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


def read_cli_runtime(harness_key: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Read the REAL installed CLI's version/path/hash (runtime honesty, gap #16).

    Config-dir names are not the whole harness. This captures the actual binary the
    harness invokes so the fingerprint admits its runtime surface. Returns
    (cli_version, unknown_runtime). Everything is leak-safe scanned; a path or version
    that fails the scan is recorded in unknown_runtime, never emitted raw.
    """
    binary = _HARNESS_BINARY.get(harness_key)
    unknown_runtime: list[dict[str, str]] = []
    cli: dict[str, Any] = {"binary": binary or "unknown", "version": "unknown",
                           "path": "unknown", "sha256": "unknown"}
    if not binary:
        unknown_runtime.append({"field": "cli_binary", "reason": "no_binary_for_harness"})
        return cli, unknown_runtime

    path = shutil.which(binary)
    if not path:
        unknown_runtime.append({"field": "cli_path", "reason": "not_on_path"})
        return cli, unknown_runtime

    # The resolved path can carry a username; scan before emitting.
    rp = Path(path).resolve()
    posix = rp.as_posix()
    cli["path"] = posix if not is_secret(posix) else "redacted"
    if is_secret(posix):
        unknown_runtime.append({"field": "cli_path", "reason": reader.REASON_NAME_UNSAFE})

    # version string from `<cli> --version`
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        raw = (proc.stdout or proc.stderr or "").strip().splitlines()
        ver = raw[0].strip() if raw else ""
        ver = ver[:80]
        # A version banner ("2.1.195 (Claude Code)") is not a name — don't run the
        # name-entropy gate on it. Only reject if it matches a hard secret PATTERN
        # (token shapes / credentials-in-URL), which a version string never should.
        if ver and not _has_secret_pattern(ver):
            cli["version"] = ver
        elif ver:
            unknown_runtime.append({"field": "cli_version", "reason": reader.REASON_NAME_UNSAFE})
    except Exception:
        unknown_runtime.append({"field": "cli_version", "reason": reader.REASON_NOT_READABLE})

    # sha256 of the binary (identity of what actually runs). Bounded read.
    try:
        size = rp.stat().st_size
        if size <= _MAX_BINARY_HASH_BYTES:
            h = hashlib.sha256()
            with rp.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            cli["sha256"] = h.hexdigest()
        else:
            unknown_runtime.append({"field": "cli_sha256", "reason": "binary_too_large"})
    except Exception:
        unknown_runtime.append({"field": "cli_sha256", "reason": reader.REASON_NOT_READABLE})

    return cli, unknown_runtime


def _tool_entries(b: "_Builder", raw_tools: list[tuple[str, str, bool]]) -> list[dict[str, Any]]:
    """Build leak-safe tool entries {name, source, enabled}; unsafe names → unknown[].

    `raw_tools` is (name, source, enabled). source ∈ {permission,builtin,mcp,plugin,
    unknown}. A tool whose name fails the safety scan is dropped into unknown[] with a
    reason rather than emitted.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for name, source, enabled in raw_tools:
        if not is_safe_name(name):
            b.note_unknown("tools", reader.REASON_NAME_UNSAFE)
            continue
        key = (name, source)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "source": source, "enabled": bool(enabled)})
    out.sort(key=lambda t: (t["name"], t["source"]))
    return out


def probe_claude_code(home: Path) -> ProbeResult:
    """Probe a claude-code config root (normally ~/.claude) → leak-safe manifest."""
    home = Path(home)
    b = _Builder()

    # --- model (allowlisted single value from settings.json) ---
    model = "unknown"
    settings = reader.read_json(home / "settings.json", home)
    gstack_flag = False
    raw_tools: list[tuple[str, str, bool]] = []
    if settings.ok and isinstance(settings.value, dict):
        raw_model = settings.value.get("model")
        if isinstance(raw_model, str) and is_safe_name(raw_model):
            model = raw_model
        elif raw_model is not None:
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        # --- tools from permissions (allow => enabled, deny => disabled) ---
        perms = settings.value.get("permissions")
        if isinstance(perms, dict):
            for tname in perms.get("allow", []) or []:
                if isinstance(tname, str):
                    # a permission entry can be "Bash(git:*)"; take the tool head only.
                    raw_tools.append((tname.split("(", 1)[0], "permission", True))
            for tname in perms.get("deny", []) or []:
                if isinstance(tname, str):
                    raw_tools.append((tname.split("(", 1)[0], "permission", False))
        # NB: we deliberately touch ONLY settings.value["model"] + ["permissions"].
        # env/apiKeyHelper/awsSecret and any other field are never read.
    elif not settings.ok:
        b.note_unknown("model", settings.reason or reader.REASON_NOT_READABLE)

    # --- skills (top-level dir basenames only) ---
    skill_names, skill_errs = reader.list_child_dir_names(home / "skills", home)
    for _name, reason in skill_errs:
        b.note_unknown("skills", reason)
    skills = b.safe_names(skill_names, "skills")
    if "gstack" in skills:
        gstack_flag = True

    # --- plugins (dir basenames only) + nested skills under plugins ---
    # Two real claude layouts exist:
    #   (a) plugins/<plugin>/skills/<skill>            (per-plugin install)
    #   (b) plugins/marketplaces/<marketplace>/skills/<skill>  (marketplace clone)
    # We only emit real plugin/marketplace names (dir basenames), never config JSON files.
    plugin_names, plugin_errs = reader.list_child_dir_names(home / "plugins", home)
    for _name, reason in plugin_errs:
        b.note_unknown("plugins", reason)
    # Structural subdirs of ~/.claude/plugins are not plugins themselves.
    _PLUGIN_STRUCT_DIRS = {"marketplaces", "cache", "data", "repos"}
    plugin_names = [p for p in plugin_names if p not in _PLUGIN_STRUCT_DIRS]
    plugins = b.safe_names(plugin_names, "plugins")

    nested_candidates: list[str] = []
    # layout (a): plugins/<plugin>/skills/
    for plug in plugin_names:
        ns_names, ns_errs = reader.list_child_dir_names(
            home / "plugins" / plug / "skills", home)
        for _name, reason in ns_errs:
            b.note_unknown("nested_skills", reason)
        nested_candidates.extend(ns_names)
    # layout (b): plugins/marketplaces/<marketplace>/skills/
    mkt_names, _mkt_errs = reader.list_child_dir_names(home / "plugins" / "marketplaces", home)
    for mkt in mkt_names:
        ns_names, ns_errs = reader.list_child_dir_names(
            home / "plugins" / "marketplaces" / mkt / "skills", home)
        for _name, reason in ns_errs:
            b.note_unknown("nested_skills", reason)
        nested_candidates.extend(ns_names)
    nested_skills = b.safe_names(nested_candidates, "nested_skills")
    if "gstack" in nested_skills or "gstack" in plugins:
        gstack_flag = True

    # --- mcps (server NAMES only, from .mcp.json keys) ---
    mcps: list[str] = []
    mcp_cfg = reader.read_json(home / ".mcp.json", home)
    if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
        servers = mcp_cfg.value.get("mcpServers")
        if isinstance(servers, dict):
            # We read only the KEYS (server names), never the values (command/env/url).
            mcps = b.safe_names(list(servers.keys()), "mcps")
            # MCP servers also expose tools; record the server as a tool source (name only).
            for sname in servers.keys():
                raw_tools.append((sname, "mcp", True))
    elif not mcp_cfg.ok and mcp_cfg.reason != reader.REASON_NOT_READABLE:
        b.note_unknown("mcps", mcp_cfg.reason or reader.REASON_MALFORMED)

    tools = _tool_entries(b, raw_tools)

    # --- custom_agents_count (count of agent files, never their contents) ---
    custom_agents_count, agent_errs = reader.count_child_files(home / "agents", home, suffix=".md")
    for _name, reason in agent_errs:
        b.note_unknown("custom_agents_count", reason)

    # --- runtime surface honesty (real CLI version/path/hash) ---
    cli_version, unknown_runtime = read_cli_runtime("claude-code")

    manifest: dict[str, Any] = {
        "harness": "claude-code",
        "model": model,
        "gstack": gstack_flag,
        "skills": skills,
        "nested_skills": nested_skills,
        "tools": tools,
        "mcps": mcps,
        "plugins": plugins,
        "custom_agents_count": custom_agents_count,
        "cli_version": cli_version,
        "unknown_runtime": unknown_runtime,
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    b.log(f"probed claude-code: "
          f"{len(skills)} skills, {len(nested_skills)} nested, {len(tools)} tools, "
          f"{len(mcps)} mcps, {len(plugins)} plugins, "
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
    # Copilot's skills are ALL nested under installed-plugins/<mkt>/<plugin>/skills, so
    # they populate nested_skills; the top-level `skills` list stays empty for copilot
    # (no top-level skills dir in its layout) unless one is found.
    nested_candidates: list[str] = []
    custom_agents_count = 0
    plugins_root = home / "installed-plugins"
    mkt_names, mkt_errs = reader.list_child_dir_names(plugins_root, home)
    for _name, reason in mkt_errs:
        b.note_unknown("nested_skills", reason)
    for mkt in mkt_names:
        plug_names, plug_errs = reader.list_child_dir_names(plugins_root / mkt, home)
        for _name, reason in plug_errs:
            b.note_unknown("nested_skills", reason)
        for plug in plug_names:
            s_names, s_errs = reader.list_child_dir_names(plugins_root / mkt / plug / "skills", home)
            for _name, reason in s_errs:
                b.note_unknown("nested_skills", reason)
            nested_candidates.extend(s_names)
            a_count, a_errs = reader.count_child_files(
                plugins_root / mkt / plug / "agents", home, suffix=".md")
            for _name, reason in a_errs:
                b.note_unknown("custom_agents_count", reason)
            custom_agents_count += a_count

    # Dedupe across plugins and drop skills explicitly disabled in settings.
    effective_nested = sorted({s for s in nested_candidates if s not in disabled_skills})
    nested_skills = b.safe_names(effective_nested, "nested_skills")
    skills: list[str] = []  # copilot has no top-level skills dir; all are nested

    # gstack ships as either a plugin or a nested skill; count it present either way.
    gstack_flag = "gstack" in plugins or "gstack" in nested_skills

    # --- mcps (server NAMES from mcp-config.json keys, minus disabled) ---
    mcps: list[str] = []
    raw_tools: list[tuple[str, str, bool]] = []
    mcp_cfg = reader.read_json(home / "mcp-config.json", home)
    if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
        servers = mcp_cfg.value.get("mcpServers")
        if isinstance(servers, dict):
            # Read only the KEYS (server names), never the values (command/env/url/token).
            names = [k for k in servers.keys() if k not in disabled_mcps]
            mcps = b.safe_names(names, "mcps")
            for sname in names:
                raw_tools.append((sname, "mcp", True))
    elif not mcp_cfg.ok and mcp_cfg.reason != reader.REASON_NOT_READABLE:
        b.note_unknown("mcps", mcp_cfg.reason or reader.REASON_MALFORMED)

    # enabled plugins are also a tool source (name only).
    for pname in plugins:
        raw_tools.append((pname, "plugin", True))
    tools = _tool_entries(b, raw_tools)

    # --- runtime surface honesty (real CLI version/path/hash) ---
    cli_version, unknown_runtime = read_cli_runtime("copilot-cli")

    manifest: dict[str, Any] = {
        "harness": "copilot-cli",
        "model": model,
        "gstack": gstack_flag,
        "skills": skills,
        "nested_skills": nested_skills,
        "tools": tools,
        "mcps": mcps,
        "plugins": plugins,
        "custom_agents_count": custom_agents_count,
        "cli_version": cli_version,
        "unknown_runtime": unknown_runtime,
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    b.log(f"probed copilot-cli: "
          f"{len(nested_skills)} nested skills, {len(tools)} tools, {len(mcps)} mcps, "
          f"{len(plugins)} plugins, {custom_agents_count} agents, {len(b.unknown)} unknown")
    return ProbeResult(manifest=manifest, log="\n".join(b.log_lines))


# Harness key -> reader. Only LIVE harnesses (a real leak-safe reader) appear here; a
# planned harness has no entry and `probe()` fails closed rather than emit a placeholder.
_READERS = {"claude-code": probe_claude_code, "copilot-cli": probe_copilot_cli}


# Structural dirs under a repo's plugins/ that are not themselves plugins.
_REPO_PLUGIN_STRUCT = {".git", ".github", "node_modules", "__pycache__"}


def probe_repo(repo_root: Path, *, harness_name: str) -> dict[str, Any]:
    """Fingerprint a GitHub REPO that ships a harness, in its real on-disk layout.

    A repo is NOT a machine's ~/.claude — it declares its harness as committed files:
      - top-level  skills/<skill>
      - plugins/<plugin>/{skills/<skill>, agents/<agent>}
      - .copilot-plugin/skills/<skill>   and   .github/skills/<skill>   (nested)
    This reads ONLY those, leak-safe, and is HONEST: gstack is True only if a gstack
    plugin/skill is actually present; model is 'unknown' unless the repo declares one
    (most don't). The result is the same fixed schema as the machine probes, plus a
    `harness_name` = the repo name (the leaderboard identity, per spec).
    """
    root = Path(repo_root)
    b = _Builder()

    # top-level skills/
    top_skill_names, top_errs = reader.list_child_dir_names(root / "skills", root)
    for _n, reason in top_errs:
        b.note_unknown("skills", reason)
    skills = b.safe_names(top_skill_names, "skills")

    # plugins/<plugin>/{skills,agents}
    plugin_names_raw, plug_errs = reader.list_child_dir_names(root / "plugins", root)
    for _n, reason in plug_errs:
        b.note_unknown("plugins", reason)
    plugin_names = [p for p in plugin_names_raw if p not in _REPO_PLUGIN_STRUCT]
    plugins = b.safe_names(plugin_names, "plugins")

    nested_candidates: list[str] = []
    custom_agents_count = 0
    for plug in plugin_names:
        ns_names, ns_errs = reader.list_child_dir_names(root / "plugins" / plug / "skills", root)
        for _n, reason in ns_errs:
            b.note_unknown("nested_skills", reason)
        nested_candidates.extend(ns_names)
        a_count, a_errs = reader.count_child_files(
            root / "plugins" / plug / "agents", root, suffix=".md")
        # agents can also be dirs; count child dirs too.
        a_dirs, _ = reader.list_child_dir_names(root / "plugins" / plug / "agents", root)
        for _n, reason in a_errs:
            b.note_unknown("custom_agents_count", reason)
        custom_agents_count += a_count + len(a_dirs)

    # nested skills under .copilot-plugin/skills and .github/skills
    for nested_root in (".copilot-plugin", ".github"):
        ns_names, ns_errs = reader.list_child_dir_names(root / nested_root / "skills", root)
        for _n, reason in ns_errs:
            b.note_unknown("nested_skills", reason)
        nested_candidates.extend(ns_names)
    nested_skills = b.safe_names(nested_candidates, "nested_skills")

    # gstack ONLY if genuinely present (honest — no machine bleed-through).
    gstack_flag = "gstack" in plugins or "gstack" in skills or "gstack" in nested_skills

    # A repo may commit an MCP config; read names only if present, else nothing.
    mcps: list[str] = []
    for mcp_name in (".mcp.json", "mcp-config.json"):
        mcp_cfg = reader.read_json(root / mcp_name, root)
        if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
            servers = mcp_cfg.value.get("mcpServers")
            if isinstance(servers, dict):
                mcps = b.safe_names(list(servers.keys()), "mcps")
                break

    manifest: dict[str, Any] = {
        "harness": "repo",
        "harness_name": harness_name,
        "model": "unknown",  # a repo declares no runtime model; honest by default
        "gstack": gstack_flag,
        "skills": skills,
        "nested_skills": nested_skills,
        "tools": [],
        "mcps": mcps,
        "plugins": plugins,
        "custom_agents_count": custom_agents_count,
        "cli_version": {"binary": "repo", "version": "unknown", "path": "unknown",
                        "sha256": "unknown"},
        "unknown_runtime": [{"field": "runtime", "reason": "repo_static_only"}],
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    return manifest


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
