"""Regenerate the wheel fallback from the canonical repository schemas."""
from __future__ import annotations

import base64
import json
import textwrap
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
OUTPUT = ROOT / "src" / "atv_bench" / "protocol" / "_embedded_schemas.py"


def main() -> None:
    payload = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(SCHEMAS.glob("*.schema.json"))
    }
    encoded = base64.b64encode(
        zlib.compress(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8"),
            level=9,
        )
    ).decode("ascii")
    bundle = "\n".join(textwrap.wrap(encoded, width=120))
    source = f'''"""Generated wheel fallback for the canonical repository ``schemas/`` files."""
from __future__ import annotations

import base64
import json
import zlib

_BUNDLE = """
{bundle}
"""


def embedded_schema_texts() -> dict[str, str]:
    encoded = "".join(_BUNDLE.split())
    compressed = base64.b64decode(encoded)
    payload = zlib.decompress(compressed).decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(text, str)
        for key, text in value.items()
    ):
        raise RuntimeError("embedded protocol schema bundle is malformed")
    return value
'''
    OUTPUT.write_text(source, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
