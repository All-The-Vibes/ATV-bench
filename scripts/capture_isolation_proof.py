"""Live network-isolation PROOF: run a hostile bot under the exact production sandbox
flags and capture terminal evidence that egress is blocked.

This executes the SAME `docker run` flag set the league Action uses (asserted in sync by
tests/test_sandbox_flag_parity.py) against a real bot that tries to (a) exfiltrate over
the network, (b) read GITHUB_TOKEN / secrets from the environment, and (c) write to the
read-only rootfs. It records each attempt's outcome to a JSON + text log that can be
committed as isolation proof.

Usage: python scripts/capture_isolation_proof.py [out_dir]
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARENA_DOCKERFILE_DIR = ROOT / "arena"
TAG = "atv-bench/arena:isolation-proof"

# The exact production sandbox flags (mirrors tests/test_action_malicious_bot.py, which is
# kept in sync with .github/workflows/league.yml by test_sandbox_flag_parity.py).
SANDBOX_FLAGS = [
    "--rm", "--network", "none",
    "--memory", "512m", "--memory-swap", "512m",
    "--cpus", "1", "--pids-limit", "128",
    "--read-only", "--user", "65534:65534",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
]

ATTACKS = {
    "network_exfil": {
        "desc": "bot tries to reach http://example.com and exfiltrate a secret",
        "bot": """
            import urllib.request
            try:
                urllib.request.urlopen("http://example.com", timeout=5)
                print("EXFIL_OK")
            except Exception:
                print("BLOCKED_NETWORK")
        """,
        "expect_present": "BLOCKED_NETWORK",
        "expect_absent": "EXFIL_OK",
    },
    "secret_read": {
        "desc": "bot scans env for GITHUB_TOKEN / SECRET / *TOKEN*",
        "bot": """
            import os
            leaked = [k for k in os.environ if "TOKEN" in k or "SECRET" in k or k == "GITHUB_TOKEN"]
            print("LEAKED:" + ",".join(leaked))
        """,
        "expect_present": "LEAKED:",
        "expect_absent": "LEAKED:GITHUB_TOKEN",
    },
    "rootfs_write": {
        "desc": "bot tries to write to the read-only rootfs",
        "bot": """
            try:
                open("/evil.sh", "w").write("pwned")
                print("WROTE_ROOTFS")
            except Exception:
                print("BLOCKED_READONLY")
        """,
        "expect_present": "BLOCKED_READONLY",
        "expect_absent": "WROTE_ROOTFS",
    },
}


def _build_image() -> str:
    print(f"building arena image {TAG} …", flush=True)
    proc = subprocess.run(
        ["docker", "build", "-t", TAG, str(ARENA_DOCKERFILE_DIR)],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise SystemExit(f"arena image build failed:\n{proc.stderr[-2000:]}")
    print("  built.", flush=True)
    return TAG


def _run_attack(tag: str, tmp: Path, bot_src: str) -> subprocess.CompletedProcess:
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    (work / "main.py").write_text(textwrap.dedent(bot_src))
    cmd = [
        "docker", "run", *SANDBOX_FLAGS,
        "--entrypoint", "python3",
        "-v", f"{work}:/work:ro",
        tag, "/work/main.py",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]).resolve() if argv else (ROOT / "docs" / "proof" / "isolation")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_work"
    tmp.mkdir(exist_ok=True)

    tag = _build_image()
    results = []
    lines = [
        "NETWORK-ISOLATION / SANDBOX-CONTAINMENT PROOF",
        "=" * 60,
        "docker run flags (identical to .github/workflows/league.yml):",
        "  " + " ".join(SANDBOX_FLAGS),
        "",
    ]
    all_pass = True
    for name, spec in ATTACKS.items():
        proc = _run_attack(tag, tmp, spec["bot"])
        out = proc.stdout.strip()
        present_ok = spec["expect_present"] in out
        absent_ok = spec["expect_absent"] not in out
        contained = present_ok and absent_ok
        all_pass = all_pass and contained
        results.append({
            "attack": name, "desc": spec["desc"], "stdout": out,
            "stderr": proc.stderr.strip()[-400:], "returncode": proc.returncode,
            "contained": contained,
        })
        verdict = "CONTAINED ✅" if contained else "ESCAPED ❌"
        lines += [f"[{verdict}] {name}: {spec['desc']}", f"    bot stdout: {out!r}", ""]
        print(f"  {verdict}  {name}", flush=True)

    summary = {"all_contained": all_pass, "flags": SANDBOX_FLAGS,
               "generated_by": "scripts/capture_isolation_proof.py",
               "attacks": results, "ts": int(time.time())}
    (out_dir / "isolation_proof.json").write_text(json.dumps(summary, indent=2))
    lines.append(f"OVERALL: {'ALL ATTACKS CONTAINED ✅' if all_pass else 'CONTAINMENT FAILURE ❌'}")
    (out_dir / "isolation_proof.txt").write_text("\n".join(lines))
    # clean the scratch work dir (keep only the proof artifacts)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n=== isolation proof written to {out_dir} (all_contained={all_pass})", flush=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
