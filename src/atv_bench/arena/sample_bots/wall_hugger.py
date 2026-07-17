"""Bundled sample bot: a wall-hugger for the demo head-to-head.

A genuinely different strategy from `greedy_survivor` so the demo is a real
head-to-head between two distinct bots — NOT one deterministic bot playing itself
(which mirrors to a draw and produces a flat, zero-spread board).

Strategy: prefer to TURN toward the nearest wall and trace along it, maximizing the
open area it leaves itself while forcing the opponent into the center. Concretely it
biases its move order toward the wall it is closest to, falling back to any safe move.
Speaks the same line protocol: one JSON observation in on stdin, one direction word
out on stdout per turn. Pure stdlib; safe to ship and run with zero setup.
"""
import sys
import json

_DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_REV = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _safe(d, pos, w, h, blocked):
    dx, dy = _DELTA[d]
    nx, ny = pos[0] + dx, pos[1] + dy
    if not (0 <= nx < w and 0 <= ny < h):
        return False
    return (nx, ny) not in blocked


def _wall_preference(pos, w, h):
    """Move order biased toward the nearest wall, so the bot hugs the perimeter.

    Distinct from greedy_survivor's 'keep heading, else first safe neighbor' — this
    deliberately steers to whichever wall is closest, tracing it. That divergence is
    what makes the deterministic demo match decisive instead of a mirrored draw.
    """
    x, y = pos
    dist_left, dist_right = x, (w - 1 - x)
    dist_up, dist_down = y, (h - 1 - y)
    # Order candidate directions by how much they drive toward the nearest wall.
    horiz = "left" if dist_left <= dist_right else "right"
    vert = "up" if dist_up <= dist_down else "down"
    # Prefer the axis we are FURTHER from its near wall (more room to run to it first).
    if min(dist_left, dist_right) <= min(dist_up, dist_down):
        return [vert, horiz, _REV[horiz], _REV[vert]]
    return [horiz, vert, _REV[vert], _REV[horiz]]


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
        pos = tuple(you["pos"])
        order = _wall_preference(pos, w, h)
        mv = next((d for d in order if _safe(d, pos, w, h, blocked)), you["dir"])
        print(mv, flush=True)


if __name__ == "__main__":
    main()
