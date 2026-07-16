"""Render demo video frames for ATV-bench. 1920x1080, 30fps.

Beat-synced glow-up: title cards, terminal panes, and rendered leaderboard/arena
screenshots composited into a scripted sequence that pulses on the 123 BPM grid
of scripts/make_demo_music.py. Adds a sidechain "pump" on every kick, additive
bloom/glow, a drifting particle field, and eased kinetic typography.

Pure PIL + numpy — no external services. Emits PNG frames; ffmpeg stitches.
"""
import os
import sys
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

W, H = 1920, 1080
FPS = 30
BPM = 123.0
BEAT = 60.0 / BPM
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/frames"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(OUT, exist_ok=True)

# palette (dark luxury / neon terminal)
BG = (11, 13, 18)
BG2 = (16, 19, 27)
FG = (232, 236, 245)
MUT = (126, 134, 152)
ACCENT = (108, 231, 190)     # mint
ACCENT2 = (122, 162, 255)    # periwinkle
WARN = (255, 196, 92)
RED = (255, 111, 116)
GRID = (30, 35, 46)

FDIR = "/usr/share/fonts/truetype/dejavu"
def font(name, sz):
    return ImageFont.truetype(f"{FDIR}/{name}", sz)
F_MONO = lambda s: font("DejaVuSansMono.ttf", s)
F_MONOB = lambda s: font("DejaVuSansMono-Bold.ttf", s)
F_SANS = lambda s: font("DejaVuSans.ttf", s)
F_SANSB = lambda s: font("DejaVuSans-Bold.ttf", s)

_frame = 0

def now():
    """Absolute time (s) of the frame about to be emitted."""
    return _frame / FPS

def beat_pulse(strength=1.0, sharp=6.0):
    """0..1 envelope that spikes on each 123 BPM kick and decays."""
    ph = (now() % BEAT) / BEAT
    return strength * math.exp(-ph * sharp)

def bar_phase():
    """0..1 position within the current 4-beat bar."""
    return (now() % (BEAT * 4)) / (BEAT * 4)

# ---------- particle field (drifting motes, seeded/deterministic) ----------
_rng = np.random.default_rng(42)
NP = 90
_px = _rng.uniform(0, W, NP)
_py = _rng.uniform(0, H, NP)
_pv = _rng.uniform(6, 26, NP)
_ps = _rng.uniform(1.0, 3.2, NP)
_pa = _rng.uniform(0.15, 0.5, NP)

def draw_particles(d, pump):
    dt = 1.0 / FPS
    for k in range(NP):
        _py[k] = (_py[k] - _pv[k] * dt) % H
        _px[k] = (_px[k] + math.sin(now() * 0.3 + k) * 4 * dt) % W
        r = _ps[k] * (1 + 0.5 * pump)
        a = int(255 * _pa[k] * (0.5 + 0.5 * pump))
        col = ACCENT if k % 3 == 0 else (ACCENT2 if k % 3 == 1 else MUT)
        d.ellipse([_px[k]-r, _py[k]-r, _px[k]+r, _py[k]+r],
                  fill=(col[0], col[1], col[2], a))

def emit_glow(img, amount=1.0):
    """Additive bloom: blur bright areas back over the frame."""
    if amount <= 0:
        img.save(f"{OUT}/f{_globinc():05d}.png")
        return
    blur = img.filter(ImageFilter.GaussianBlur(9))
    glow = ImageChops.screen(img, Image.eval(blur, lambda v: int(v * 0.55 * amount)))
    glow.save(f"{OUT}/f{_globinc():05d}.png")

def _globinc():
    global _frame
    f = _frame
    _frame += 1
    return f

def emit(img, glow=0.8):
    emit_glow(img, glow)

def secs(s):
    return int(s * FPS)

def ease(x):
    return 1 - (1 - x) ** 3

def ease_io(x):
    return 3 * x * x - 2 * x * x * x

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))

def base_layer(shift=0, pump=0.0):
    """RGBA canvas with grid + pump-brightened vignette; returns (img, draw)."""
    img = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(img, "RGBA")
    g = lerp(GRID, ACCENT, 0.06 + 0.12 * pump)
    for x in range(-H, W, 64):
        d.line([(x + shift, 0), (x + shift + H, H)], fill=g + (255,), width=1)
    return img, d

def center_text(d, cx, y, text, fnt, fill, anchor="mm"):
    if len(fill) == 3:
        fill = fill + (255,)
    d.text((cx, y), text, font=fnt, fill=fill, anchor=anchor)

def load(name):
    return Image.open(os.path.join(ROOT, name)).convert("RGB")

board = load("screenshots/populated_desktop.png")
board_low = load("screenshots/low_confidence_desktop.png")
arena = load("docs/proof/item1-adjudication/board_render.png")

def fit(img, maxw, maxh):
    r = min(maxw / img.width, maxh / img.height)
    return img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)

def flatten(img):
    return img.convert("RGB")

# =========================================================
# SCENE 1 — HOOK (title)  ~3.5s   (intro: gentle, pump ramps in)
# =========================================================
hook_lines = ["Everyone benchmarks the", "MODEL."]
sub = "Nobody benchmarks the harness."
N = secs(3.5)
for i in range(N):
    p = i / N
    pump = beat_pulse(0.35 + 0.55 * p)          # pump grows as intro filter opens
    img, d = base_layer(shift=int(p * 40), pump=pump)
    scale = 1 + 0.012 * pump
    center_text(d, W//2, H//2 - 120, hook_lines[0], F_SANSB(64),
                lerp(MUT, FG, ease(min(1, p*2))))
    big = F_SANSB(int(150 * scale))
    center_text(d, W//2, H//2 - 10, hook_lines[1], big,
                lerp(BG, MUT, ease(min(1, p*2))))
    if p > 0.45:
        pp = ease(min(1, (p - 0.45) / 0.4))
        center_text(d, W//2, H//2 + 130, sub, F_SANSB(58), lerp(BG, ACCENT, pp))
    draw_particles(d, pump)
    emit(flatten(img), glow=0.5 + 0.5 * p)

# =========================================================
# SCENE 2 — PROBLEM statement  ~4.2s   (lines punch in on the beat)
# =========================================================
lines = [
    ("Your skills.", ACCENT2),
    ("Your MCP servers.", ACCENT2),
    ("Your plugins, agents, config.", ACCENT2),
    ("That's what actually ships code.", FG),
    ("So that's what we rank.", ACCENT),
]
N = secs(4.2)
for i in range(N):
    p = i / N
    pump = beat_pulse(0.9)
    img, d = base_layer(shift=40, pump=pump)
    y = 300
    for idx, (ln, col) in enumerate(lines):
        appear = idx * 0.14
        if p > appear:
            pp = ease(min(1, (p - appear) / 0.18))
            kick = beat_pulse(0.5) if pp > 0.85 else 0
            sz = (66 if idx >= 3 else 56)
            fnt = F_SANSB(int(sz * (1 + 0.03 * kick)))
            hi = lerp(col, (255, 255, 255), 0.25 * kick)
            center_text(d, W//2, y, ln, fnt, lerp(BG, hi, pp))
        y += 100 if idx < 3 else 120
    draw_particles(d, pump)
    emit(flatten(img), glow=0.7)

# =========================================================
# SCENE 3 — TERMINAL: submit + tests  ~6s
# =========================================================
term_lines = [
    ("$ atv-bench fingerprint --dry-run", FG),
    ("  Will publish:  claude-code · opus-4.8 · gstack ✓", ACCENT),
    ("                 skills[7]  mcps[2]  plugins[1]", MUT),
    ("  Scrubbed:      3 secret-shaped values withheld", WARN),
    ("", FG),
    ("$ atv-bench submit ./main.py --game tron --identity you", FG),
    ("  ✓ 7/7 preflight checks passed → submission.json", ACCENT),
    ("", FG),
    ("$ pytest -q", FG),
    ("  299 passed  ·  5 docker-adjudication  ·  4 sandbox", ACCENT),
    ("  ALL GREEN", ACCENT),
]
def draw_term(d, x0, y0, w, h, reveal_chars, cursor_on, title="atv-bench"):
    d.rounded_rectangle([x0, y0, x0+w, y0+h], radius=18, fill=BG2 + (255,),
                        outline=GRID + (255,), width=2)
    for ci, c in enumerate([RED, WARN, ACCENT]):
        d.ellipse([x0+24+ci*30, y0+22, x0+40+ci*30, y0+38], fill=c + (255,))
    center_text(d, x0+w//2, y0+30, title, F_MONO(22), MUT)
    fnt = F_MONO(30)
    y = y0 + 74
    shown = reveal_chars
    last_pos = None
    for ln, col in term_lines:
        if shown <= 0:
            break
        seg = ln[:shown]
        d.text((x0+34, y), seg, font=fnt, fill=col + (255,))
        if shown < len(ln):
            last_pos = (x0 + 34 + int(fnt.getlength(seg)), y)
        shown -= max(len(ln), 1)
        y += 44
    if cursor_on and last_pos:
        d.rectangle([last_pos[0], last_pos[1]+4, last_pos[0]+14, last_pos[1]+34],
                    fill=ACCENT + (255,))
total_chars = sum(max(len(l), 1) for l, _ in term_lines)
N = secs(6.0)
for i in range(N):
    p = i / N
    pump = beat_pulse(0.6)
    img, d = base_layer(shift=40, pump=pump)
    center_text(d, W//2, 70, "Submit a bot — never a self-reported score",
                F_SANSB(40), FG)
    reveal = int(ease(min(1, p*1.15)) * total_chars)
    cursor = (now() % 0.6) < 0.3
    draw_term(d, 210, 150, 1500, 560, reveal, cursor)
    draw_particles(d, pump)
    emit(flatten(img), glow=0.6)

# =========================================================
# SCENE 4 — ARENA adjudication (trust boundary)  ~5s
# =========================================================
N = secs(5.0)
ar = fit(arena, 760, 720)
for i in range(N):
    p = i / N
    pump = beat_pulse(0.7)
    img, d = base_layer(shift=40, pump=pump)
    center_text(d, W//2, 66, "A trusted referee runs the match", F_SANSB(46), ACCENT)
    ax = int(-800 + ease(min(1, p*1.4)) * (150 + 800))
    axp = max(60, ax)
    # glow frame that pulses around the arena
    fr = lerp(GRID, ACCENT, 0.3 + 0.5 * pump)
    d.rounded_rectangle([axp-10, 140, axp+ar.width+10, 150+ar.height+0],
                        radius=16, outline=fr + (255,), width=3)
    img.paste(ar, (axp, 150))
    bl = [
        ("Bot = move-only subprocess", FG),
        ("One direction per turn, timed", MUT),
        ("Fake a result → forfeit", RED),
        ("Outcome authored from real play", ACCENT),
    ]
    y = 250
    for idx, (ln, col) in enumerate(bl):
        ap = idx * 0.16 + 0.3
        if p > ap:
            pp = ease(min(1, (p-ap)/0.2))
            kick = beat_pulse(0.4) if pp > 0.85 else 0
            hi = lerp(col, (255, 255, 255), 0.2 * kick)
            d.text((980, y), "▸ " + ln, font=F_SANSB(42), fill=lerp(BG, hi, pp) + (255,))
        y += 110
    draw_particles(d, pump)
    emit(flatten(img), glow=0.65)

# =========================================================
# SCENE 5 — LEADERBOARD reveal  ~6.5s
# =========================================================
N = secs(6.5)
bd = fit(board, 1180, 980)
for i in range(N):
    p = i / N
    pump = beat_pulse(0.7)
    img, d = base_layer(shift=40, pump=pump)
    center_text(d, W//2, 54, "The Community League — live on GitHub Pages",
                F_SANSB(42), FG)
    pp = ease(min(1, p*1.3))
    by = int(120 + (1-pp)*120)
    bx = (W - bd.width)//2
    fr = lerp(BG, ACCENT, min(1, pp + 0.4 * pump))
    d.rounded_rectangle([bx-14, by-14, bx+bd.width+14,
                         by+min(bd.height, H-by-20)+14],
                        radius=20, outline=fr + (255,), width=3)
    crop_h = min(bd.height, H - by - 30)
    img.paste(bd.crop((0, 0, bd.width, crop_h)), (bx, by))
    draw_particles(d, pump)
    emit(flatten(img), glow=0.6)

# =========================================================
# SCENE 6 — CTA / outro  ~4.2s   (final logo pops on the beat)
# =========================================================
N = secs(4.2)
for i in range(N):
    p = i / N
    pump = beat_pulse(1.0)
    img, d = base_layer(shift=int(40+p*30), pump=pump)
    pp = ease(min(1, p*1.5))
    logo_sz = int(140 * (1 + 0.02 * pump))
    center_text(d, W//2, H//2 - 150, "ATV-bench", F_SANSB(logo_sz), lerp(BG, FG, pp))
    center_text(d, W//2, H//2 - 30, "Community League", F_SANSB(72),
                lerp(BG, ACCENT, pp))
    if p > 0.35:
        qp = ease(min(1, (p-0.35)/0.4))
        center_text(d, W//2, H//2 + 90, "Rank the whole harness. Not just the model.",
                    F_SANS(46), lerp(BG, MUT, qp))
        center_text(d, W//2, H//2 + 190,
                    "uv pip install -e '.[dev]'  ·  atv-bench submit",
                    F_MONOB(38), lerp(BG, ACCENT2, qp))
    draw_particles(d, pump)
    emit(flatten(img), glow=0.7 + 0.3 * pump)

print(f"wrote {_frame} frames to {OUT} ({_frame/FPS:.1f}s)")
