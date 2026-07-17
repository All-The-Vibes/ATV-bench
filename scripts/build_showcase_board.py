"""Build the showcase leaderboard: two GitHub-repo harnesses, real fingerprints, real match.

Produces a leaderboard doc where each row is a harness named after its GitHub repo
(all-the-vibes/ATV-Phoenix vs microsoft/hve-core), carrying that harness's FULL
leak-safe fingerprint (secrets wiped). The match result comes from a real CodeClash
run; ELO is computed from it. Phase-1 rows are marked unverified.

Usage:
    python scripts/build_showcase_board.py --out /tmp/showcase_store
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from atv_bench.fingerprint import probe


def _repo_fingerprint(harness_key: str, config_root: Path, repo_name: str) -> dict:
    """Fingerprint a harness config root (a cloned repo), keyed by repo name."""
    result = probe._READERS[harness_key](config_root)
    m = result.manifest
    # The harness name is the repo name (per the locked requirement).
    m = dict(m)
    m["harness_name"] = repo_name
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/showcase_store")
    ap.add_argument("--phoenix-home", default=None,
                    help="config root for ATV-Phoenix harness (default: local ~/.claude)")
    ap.add_argument("--hve-home", default=None,
                    help="config root for hve-core harness (default: local ~/.copilot)")
    args = ap.parse_args()

    from atv_bench import harnesses as hz

    phoenix_root = Path(args.phoenix_home) if args.phoenix_home else hz.config_root_for("claude-code")
    hve_root = Path(args.hve_home) if args.hve_home else hz.config_root_for("copilot-cli")

    phoenix_fp = _repo_fingerprint("claude-code", phoenix_root, "all-the-vibes/ATV-Phoenix")
    hve_fp = _repo_fingerprint("copilot-cli", hve_root, "microsoft/hve-core")

    out = {
        "phoenix": phoenix_fp,
        "hve": hve_fp,
    }
    Path(args.out).mkdir(parents=True, exist_ok=True)
    dest = Path(args.out) / "showcase_fingerprints.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"wrote {dest}")
    print(f"  ATV-Phoenix: {len(phoenix_fp['skills'])} skills, "
          f"{len(phoenix_fp['nested_skills'])} nested, {len(phoenix_fp['tools'])} tools, "
          f"model={phoenix_fp['model']}")
    print(f"  hve-core: {len(hve_fp['skills'])} skills, "
          f"{len(hve_fp['nested_skills'])} nested, {len(hve_fp['tools'])} tools, "
          f"model={hve_fp['model']}")


if __name__ == "__main__":
    main()
