"""Bundled sample bot: a greedy survivor for the demo head-to-head.

Speaks the arena line protocol — one JSON observation in on stdin, one direction word
out on stdout per turn. Keeps heading if safe, else turns to the first safe neighbor.
Pure stdlib; safe to ship and run in the demo with zero setup.
"""
import sys
import json

_DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_REV = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _safe(d, you, opp, w, h, blocked):
    dx, dy = _DELTA[d]
    nx, ny = you["pos"][0] + dx, you["pos"][1] + dy
    if not (0 <= nx < w and 0 <= ny < h):
        return False
    return (nx, ny) not in blocked


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except (ValueError, TypeError):
            break
        w, h = o["width"], o["height"]
        you, opp = o["you"], o["opponent"]
        blocked = {tuple(c) for c in you["trail"]} | {tuple(c) for c in opp["trail"]}
        cur = you["dir"]
        order = [cur] + [d for d in ("up", "right", "down", "left")
                         if d not in (cur, _REV[cur])]
        mv = next((d for d in order if _safe(d, you, opp, w, h, blocked)), cur)
        print(mv, flush=True)


if __name__ == "__main__":
    main()
