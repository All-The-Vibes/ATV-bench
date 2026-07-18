"""Allowlist-emit fingerprint probe (eng T1).

The emitter builds the manifest FIELD BY FIELD from a fixed schema. It never copies
a parsed config dict and deletes secrets (a denylist can't guarantee leak-free — a
new secret-bearing field would pass straight through). Every value that lands in the
manifest has been through `scan.is_safe_name` / `scan.is_secret`; anything that fails
becomes an `unknown[{field, reason}]` entry, never a raw emit.

v1 ships live readers for claude-code, copilot-cli, and codex (below); other harnesses
are registered as planned in `atv_bench.harnesses` and the generic `probe()` dispatcher
fails closed on them until a reader lands. See `probe()` at the bottom of this module for
the harness-agnostic entry.
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
_HARNESS_BINARY = {"claude-code": "claude", "copilot-cli": "copilot", "codex": "codex"}
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

    def enabled_plugin_keys(self, enabled_plugins_raw: dict) -> set[str]:
        """Return the set of ENABLED plugin keys from an enabledPlugins map.

        A plugin is enabled ONLY when its value is boolean True — the boolean false IS the
        disable mechanism. A non-boolean value (a string/number/null/list) is a malformed
        entry: it flags plugins malformed and contributes NO plugin name (never treated as
        truthy). This is shared by claude-code + copilot-cli so both honor disable + shape
        identically.
        """
        keys: set[str] = set()
        for k, v in enabled_plugins_raw.items():
            if not isinstance(k, str):
                continue
            if v is True:
                keys.add(k)
            elif v is False:
                continue  # explicit disable
            else:
                self.note_unknown("plugins", reader.REASON_MALFORMED)
        return keys


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
    """Probe a claude-code config root (normally ~/.claude) → leak-safe manifest.

    Real-layout (verified 2026-07-16), manifest-driven — NOT a naive dir glob:
      - model    → settings.json["model"] (allowlisted single value).
      - plugins  → settings.json["enabledPlugins"] keys, truthy only (the false value
        IS the disable mechanism — claude has no separate disabled list). "@marketplace"
        suffix stripped. We do NOT glob ~/.claude/plugins/ (which holds cache/data/
        marketplaces infra dirs + JSON manifests).
      - skills   → top-level ~/.claude/skills/* PLUS skills nested in each ENABLED plugin,
        walked via plugins/installed_plugins.json installPath (each confined under home).
      - nested_skills → skills discovered from nested plugin walk (v2 surface).
      - agents   → top-level ~/.claude/agents/*.md PLUS nested installPath/agents/*.md.
      - mcps     → ~/.claude.json["mcpServers"] keys. NB ~/.claude.json is the PARENT
        dir's file (home root), NOT ~/.claude/.mcp.json (which doesn't exist on a real
        machine). Reads are confined to that parent.
      - tools    → permissions allow/deny from settings.json + mcp server names.

    Only NAMES and COUNTS are read — never a file body. Every value passes the safety
    scan; anything unsafe or unreadable becomes unknown[{field, reason}].
    """
    home = Path(home)
    b = _Builder()

    # --- settings.json: model + enabledPlugins (truthy filter = the disable mechanism) ---
    model = "unknown"
    enabled_plugins_raw: dict = {}
    raw_tools: list[tuple[str, str, bool]] = []
    settings = reader.read_json(home / "settings.json", home)
    if settings.ok and isinstance(settings.value, dict):
        raw_model = settings.value.get("model")
        if isinstance(raw_model, str) and is_safe_name(raw_model):
            model = raw_model
        elif isinstance(raw_model, str):
            # a real string that fails the safety scan → scrub (consent boundary).
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        elif raw_model is not None:
            # wrong TYPE (number/bool/list/dict) → structurally malformed config field.
            b.note_unknown("model", reader.REASON_MALFORMED)
        ep = settings.value.get("enabledPlugins")
        if isinstance(ep, dict):
            enabled_plugins_raw = ep
        elif ep is not None:
            # present but wrong-shaped enabledPlugins (list/scalar, not a dict) → flag
            # plugins malformed, don't silently emit plugins:[].
            b.note_unknown("plugins", reader.REASON_MALFORMED)
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
        # NB: only model + enabledPlugins + permissions are read. env/apiKeyHelper/awsSecret
        # and any future field are never read — allowlist by construction.
    elif settings.ok:
        # Parseable but NOT a dict (a JSON array/scalar/null): the config is structurally
        # wrong, so treat it as malformed rather than silently falling through to an empty
        # confident manifest (M9 fail-closed depends on this unknown[model] marker).
        b.note_unknown("model", reader.REASON_MALFORMED)
    else:
        b.note_unknown("model", settings.reason or reader.REASON_NOT_READABLE)

    # Enabled plugin KEYS ("name@marketplace" truthy only). The strip-@ name is the
    # published plugin; the full key is what installed_plugins.json is keyed by.
    enabled_keys = b.enabled_plugin_keys(enabled_plugins_raw)
    plugin_candidates = [k.split("@", 1)[0] for k in enabled_keys if k.split("@", 1)[0]]
    plugins = b.safe_names(sorted(set(plugin_candidates)), "plugins")

    # --- skills + nested_skills + agents: top-level PLUS nested inside each ENABLED plugin ---
    skill_candidates: list[str] = []
    nested_skill_candidates: list[str] = []
    s_names, s_errs = reader.list_child_dir_names(home / "skills", home)
    for _name, reason in s_errs:
        b.note_unknown("skills", reason)
    skill_candidates.extend(s_names)

    custom_agents_count, a_errs = reader.count_child_files(home / "agents", home, suffix=".md")
    for _name, reason in a_errs:
        b.note_unknown("custom_agents_count", reason)

    # Manifest-driven nested walk: parse installed_plugins.json, iterate installPath for
    # ENABLED plugin keys only, read installPath/skills + installPath/agents (confined).
    manifest_out = reader.read_json(home / "plugins" / "installed_plugins.json", home)
    if manifest_out.ok and isinstance(manifest_out.value, dict) \
            and manifest_out.value.get("version") == 2:
        plugins_map = manifest_out.value.get("plugins")
        if isinstance(plugins_map, dict):
            for key, entries in plugins_map.items():
                if key not in enabled_keys:  # M5: only enabled plugins contribute
                    continue
                if not isinstance(entries, list):
                    # Enabled plugin with a wrong-shaped entry list → its nested skills/
                    # agents are silently lost; surface a marker so the gap is visible.
                    b.note_unknown("plugins", reader.REASON_MALFORMED)
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        b.note_unknown("plugins", reader.REASON_MALFORMED)
                        continue
                    install_path = entry.get("installPath")
                    if not isinstance(install_path, str) or not install_path:
                        b.note_unknown("plugins", reader.REASON_MALFORMED)
                        continue
                    proot = Path(install_path)
                    ns_names, ns_errs = reader.list_child_dir_names(proot / "skills", home)
                    for _name, reason in ns_errs:
                        b.note_unknown("skills", reason)
                    # Add to BOTH skill_candidates (main compat) and nested_skill_candidates (v2)
                    skill_candidates.extend(ns_names)
                    nested_skill_candidates.extend(ns_names)
                    na_count, na_errs = reader.count_child_files(
                        proot / "agents", home, suffix=".md")
                    for _name, reason in na_errs:
                        b.note_unknown("custom_agents_count", reason)
                    custom_agents_count += na_count
        elif plugins_map is not None:
            # version==2 but plugins is present and not a dict → malformed manifest.
            b.note_unknown("plugins", reader.REASON_MALFORMED)
    elif manifest_out.ok and enabled_keys:
        # Parseable but unusable (non-dict, or version != 2) installed_plugins.json while
        # enabled plugins exist → their nested skills/agents can't be resolved; flag it so
        # the loss is visible rather than a silent undercount.
        b.note_unknown("plugins", reader.REASON_MALFORMED)
    elif not manifest_out.ok and manifest_out.reason != reader.REASON_ABSENT:
        b.note_unknown("plugins", manifest_out.reason or reader.REASON_MALFORMED)

    # Dedupe skills across top-level + all plugins.
    skills = b.safe_names(sorted(set(skill_candidates)), "skills")
    nested_skills = b.safe_names(sorted(set(nested_skill_candidates)), "nested_skills")

    # gstack present as either a plugin or a (top-level/nested) skill.
    gstack_flag = "gstack" in plugins or "gstack" in skills

    # --- mcps (server NAMES from ~/.claude.json["mcpServers"] keys) ---
    # ~/.claude.json is the PARENT of ~/.claude (home root file), so confine to that parent.
    mcps: list[str] = []
    claude_json = home.parent / ".claude.json"
    mcp_cfg = reader.read_json(claude_json, home.parent)
    if mcp_cfg.ok and isinstance(mcp_cfg.value, dict):
        servers = mcp_cfg.value.get("mcpServers")
        if isinstance(servers, dict):
            # Read only the KEYS (server names), never the values (command/env/url/token).
            mcps = b.safe_names(list(servers.keys()), "mcps")
            # MCP servers also expose tools; record the server as a tool source (name only).
            for sname in servers.keys():
                raw_tools.append((sname, "mcp", True))
        elif servers is not None:
            # present but wrong-shaped mcpServers field (a list/scalar, not a dict) →
            # flag malformed, don't silently emit mcps:[] as "no MCP servers".
            b.note_unknown("mcps", reader.REASON_MALFORMED)
    elif mcp_cfg.ok:
        # Parseable but non-dict ~/.claude.json → malformed mcp source, flag it (don't
        # present mcps:[] as a confident "no MCP servers").
        b.note_unknown("mcps", reader.REASON_MALFORMED)
    elif mcp_cfg.reason != reader.REASON_ABSENT:
        b.note_unknown("mcps", mcp_cfg.reason or reader.REASON_MALFORMED)

    tools = _tool_entries(b, raw_tools)

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
        elif isinstance(raw_model, str):
            # a real string that fails the safety scan → scrub (consent boundary).
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        elif raw_model is not None:
            # wrong TYPE (number/bool/list/dict) → structurally malformed config field.
            b.note_unknown("model", reader.REASON_MALFORMED)
        ep = settings.value.get("enabledPlugins")
        if isinstance(ep, dict):
            enabled_plugins_raw = ep
        elif ep is not None:
            # present but wrong-shaped enabledPlugins → flag plugins malformed.
            b.note_unknown("plugins", reader.REASON_MALFORMED)
        ds = settings.value.get("disabledSkills")
        if isinstance(ds, list):
            disabled_skills = {s for s in ds if isinstance(s, str)}
        dm = settings.value.get("disabledMcpServers")
        if isinstance(dm, list):
            disabled_mcps = {s for s in dm if isinstance(s, str)}
        # NB: we read ONLY these named keys. theme/logLevel/experimental/etc. and any
        # future field are never emitted — allowlist by construction.
    elif settings.ok:
        # Parseable but NOT a dict → structurally wrong config; flag malformed so the
        # fail-closed guard fires instead of publishing an empty confident manifest.
        b.note_unknown("model", reader.REASON_MALFORMED)
    else:
        b.note_unknown("model", settings.reason or reader.REASON_NOT_READABLE)

    # --- plugins (enabledPlugins keys are "name@marketplace"; take the name) ---
    # A plugin is published ONLY when its enabledPlugins value is boolean true (false is
    # the disable mechanism; a non-bool value flags plugins malformed and is not published).
    enabled_keys = b.enabled_plugin_keys(enabled_plugins_raw)
    plugin_candidates = [
        k.split("@", 1)[0] for k in enabled_keys if k.split("@", 1)[0]
    ]
    plugins = b.safe_names(sorted(set(plugin_candidates)), "plugins")

    # --- skills + custom_agents_count (nested inside each installed plugin) ---
    # Copilot's skills are ALL nested under installed-plugins/<mkt>/<plugin>/skills.
    # For main compat: these go into `skills`. For v2: they ALSO go into `nested_skills`.
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
            # M5 (copilot): only ENABLED plugins contribute nested skills/agents. The dir
            # layout <marketplace>/<plugin> maps to the enabledPlugins key <plugin>@<mkt>;
            # a disabled (or non-enabled) plugin's nested content must not leak.
            if f"{plug}@{mkt}" not in enabled_keys:
                continue
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
    # For copilot, all skills are nested — copy to nested_skills for v2 schema.
    nested_skills = skills.copy()

    # gstack ships as either a plugin or a nested skill; count it present either way.
    gstack_flag = "gstack" in plugins or "gstack" in skills

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
        elif servers is not None:
            # present but wrong-shaped mcpServers field → flag malformed, not silent [].
            b.note_unknown("mcps", reader.REASON_MALFORMED)
    elif mcp_cfg.ok:
        # Parseable but non-dict mcp-config.json → malformed mcp source, flag it.
        b.note_unknown("mcps", reader.REASON_MALFORMED)
    elif mcp_cfg.reason != reader.REASON_ABSENT:
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
          f"{len(skills)} skills, {len(tools)} tools, {len(mcps)} mcps, "
          f"{len(plugins)} plugins, {custom_agents_count} agents, {len(b.unknown)} unknown")
    return ProbeResult(manifest=manifest, log="\n".join(b.log_lines))


def probe_codex(home: Path) -> ProbeResult:
    """Probe an OpenAI Codex CLI config root (normally ~/.codex) → leak-safe manifest.

    codex config is TOML (`config.toml`). Same allowlist-emit discipline as the other
    readers, mapped onto codex's layout:
      - model         → ONLY the top-level `config.toml["model"]` key. model_provider,
        [model_providers.*], and http_headers carry base_urls + embedded api keys
        (`sk-godmode`) and are NEVER read (allowlist by construction).
      - skills        → basenames under ~/.codex/skills/
      - mcps          → keys of the [mcp_servers.*] tables (names only, never table bodies)
      - plugins       → [] (codex has no plugin concept)
      - custom_agents → count of ~/.codex/prompts/*.md (0 when the dir is absent)

    Only NAMES and COUNTS are read — never a file body. Every value passes the safety
    scan; anything unsafe or unreadable becomes unknown[{field, reason}].
    """
    home = Path(home)
    b = _Builder()

    # --- config.toml: model (top-level ONLY) + mcp server names ---
    model = "unknown"
    mcp_candidates: list[str] = []
    config = reader.read_toml(home / "config.toml", home)
    if config.ok and isinstance(config.value, dict):
        raw_model = config.value.get("model")
        if isinstance(raw_model, str) and is_safe_name(raw_model):
            model = raw_model
        elif isinstance(raw_model, str):
            # a real string that fails the safety scan → scrub (consent boundary).
            b.note_unknown("model", reader.REASON_NAME_UNSAFE)
        elif raw_model is not None:
            # wrong TYPE (number/bool/list/dict) → structurally malformed config field.
            b.note_unknown("model", reader.REASON_MALFORMED)
        # NB: we touch ONLY config.value["model"]. model_provider / model_providers /
        # http_headers (base_urls + api keys) are never read — allowlist by construction.
        servers = config.value.get("mcp_servers")
        if isinstance(servers, dict):
            # Read only the KEYS (server names), never the table bodies (command/env/url).
            mcp_candidates = list(servers.keys())
        elif servers is not None:
            # present but wrong-shaped mcp_servers (an array/scalar, not a table) →
            # flag malformed, don't silently emit mcps:[] as "no MCP servers".
            b.note_unknown("mcps", reader.REASON_MALFORMED)
    elif not config.ok and config.reason != reader.REASON_ABSENT:
        # config.toml is the single untrusted source for BOTH model and mcps — an
        # unreadable/empty/malformed config must flag every dependent field, else
        # mcps=[] reads as a confident "no MCP servers" when the truth is "unreadable".
        reason = config.reason or reader.REASON_MALFORMED
        b.note_unknown("model", reason)
        b.note_unknown("mcps", reason)

    mcps = b.safe_names(mcp_candidates, "mcps")

    # --- skills (dir basenames only) ---
    skill_names, skill_errs = reader.list_child_dir_names(home / "skills", home)
    for _name, reason in skill_errs:
        b.note_unknown("skills", reason)
    skills = b.safe_names(skill_names, "skills")

    # --- custom_agents_count (count of prompts/*.md; 0 when absent — UC2 resolution) ---
    custom_agents_count, agent_errs = reader.count_child_files(
        home / "prompts", home, suffix=".md")
    for _name, reason in agent_errs:
        b.note_unknown("custom_agents_count", reason)

    gstack_flag = "gstack" in skills

    # --- runtime surface honesty (real CLI version/path/hash) ---
    cli_version, unknown_runtime = read_cli_runtime("codex")

    manifest: dict[str, Any] = {
        "harness": "codex",
        "model": model,
        "gstack": gstack_flag,
        "skills": skills,
        "nested_skills": [],
        "tools": [],
        "mcps": mcps,
        "plugins": [],
        "custom_agents_count": custom_agents_count,
        "cli_version": cli_version,
        "unknown_runtime": unknown_runtime,
        "unknown": b.unknown,
        "probe_version": PROBE_VERSION,
    }
    b.log(f"probed codex: "
          f"{len(skills)} skills, {len(mcps)} mcps, 0 plugins, "
          f"{custom_agents_count} agents, {len(b.unknown)} unknown")
    return ProbeResult(manifest=manifest, log="\n".join(b.log_lines))


# Harness key -> reader. Only LIVE harnesses (a real leak-safe reader) appear here; a
# planned harness has no entry and `probe()` fails closed rather than emit a placeholder.
_READERS = {
    "claude-code": probe_claude_code,
    "copilot-cli": probe_copilot_cli,
    "codex": probe_codex,
}


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

    # When an explicit config root is given without --harness, resolve the harness from
    # that root's basename (e.g. .codex → codex) instead of auto-detecting against $HOME —
    # otherwise a codex root passed via --home is mis-probed as claude-code on a machine
    # that also has ~/.claude.
    key = harness
    if key is None and home is not None:
        key = hz.harness_for_root(Path(home))
    key = key or hz.detect_harness() or hz.DEFAULT_HARNESS
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
