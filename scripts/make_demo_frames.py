"""Render demo video frames for ATV-bench. 1920x1080, 30fps.

Composites title cards, terminal panes, and the rendered leaderboard/arena
screenshots into a scripted sequence. Emits PNG frames to a dir; ffmpeg stitches.
Pure PIL — no external services.
"""
import os
import sys
import math
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FPS = 30
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
def emit(img):
    global _frame
    img.save(f"{OUT}/f{_frame:05d}.png")
    _frame += 1

def secs(s):
    return int(s * FPS)

def ease(x):
    return 1 - (1 - x) ** 3

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))

def bg_base(draw, shift=0):
    draw.rectangle([0, 0, W, H], fill=BG)
    # subtle diagonal grid glow
    for x in range(-H, W, 64):
        draw.line([(x + shift, 0), (x + shift + H, H)], fill=GRID, width=1)

def center_text(draw, cx, y, text, fnt, fill, anchor="mm"):
    draw.text((cx, y), text, font=fnt, fill=fill, anchor=anchor)

def load(name):
    return Image.open(os.path.join(ROOT, name)).convert("RGB")

# ---------- pre-load assets ----------
board = load("screenshots/populated_desktop.png")
board_low = load("screenshots/low_confidence_desktop.png")
arena = load("docs/proof/item1-adjudication/board_render.png")

def fit(img, maxw, maxh):
    r = min(maxw / img.width, maxh / img.height)
    return img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)

# =========================================================
# SCENE 1 — HOOK (title)  ~3.5s
# =========================================================
hook_lines = ["Everyone benchmarks the", "MODEL."]
sub = "Nobody benchmarks the harness."
N = secs(3.5)
for i in range(N):
    p = i / N
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=int(p * 40))
    # vignette pulse
    a = 0.5 + 0.5 * math.sin(p * math.pi)
    center_text(d, W//2, H//2 - 120, hook_lines[0], F_SANSB(64), lerp(MUT, FG, ease(min(1, p*2))))
    big = F_SANSB(150)
    center_text(d, W//2, H//2 - 10, hook_lines[1], big, lerp(BG, MUT, ease(min(1, p*2))))
    if p > 0.45:
        pp = ease(min(1, (p - 0.45) / 0.4))
        center_text(d, W//2, H//2 + 130, sub, F_SANSB(58), lerp(BG, ACCENT, pp))
    emit(img)

# =========================================================
# SCENE 2 — PROBLEM statement  ~4s
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
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=40)
    y = 300
    for idx, (ln, col) in enumerate(lines):
        appear = idx * 0.14
        if p > appear:
            pp = ease(min(1, (p - appear) / 0.18))
            fnt = F_SANSB(66 if idx >= 3 else 56)
            center_text(d, W//2, y, ln, fnt, lerp(BG, col, pp))
        y += 100 if idx < 3 else 120
    emit(img)

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
def draw_term(d, x0, y0, w, h, reveal_chars, title="atv-bench"):
    d.rounded_rectangle([x0, y0, x0+w, y0+h], radius=18, fill=BG2, outline=GRID, width=2)
    for ci, c in enumerate([RED, WARN, ACCENT]):
        d.ellipse([x0+24+ci*30, y0+22, x0+40+ci*30, y0+38], fill=c)
    center_text(d, x0+w//2, y0+30, title, F_MONO(22), MUT)
    fnt = F_MONO(30)
    y = y0 + 74
    shown = reveal_chars
    for ln, col in term_lines:
        if shown <= 0:
            break
        seg = ln[:shown]
        d.text((x0+34, y), seg, font=fnt, fill=col)
        shown -= max(len(ln), 1)
        y += 44
total_chars = sum(max(len(l), 1) for l, _ in term_lines)
N = secs(6.0)
for i in range(N):
    p = i / N
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=40)
    center_text(d, W//2, 70, "Submit a bot — never a self-reported score", F_SANSB(40), FG)
    reveal = int(ease(min(1, p*1.15)) * total_chars)
    draw_term(d, 210, 150, 1500, 560, reveal)
    emit(img)

# =========================================================
# SCENE 4 — ARENA adjudication (trust boundary)  ~5s
# =========================================================
N = secs(5.0)
ar = fit(arena, 760, 720)
for i in range(N):
    p = i / N
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=40)
    center_text(d, W//2, 66, "A trusted referee runs the match", F_SANSB(46), ACCENT)
    # arena image slides in from left
    ax = int(-800 + ease(min(1, p*1.4)) * (150 + 800))
    img.paste(ar, (max(60, ax), 150))
    # bullets on right
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
            d.text((980, y), "▸ " + ln, font=F_SANSB(42), fill=lerp(BG, col, pp))
        y += 110
    emit(img)

# =========================================================
# SCENE 5 — LEADERBOARD reveal  ~6.5s
# =========================================================
N = secs(6.5)
bd = fit(board, 1180, 980)
for i in range(N):
    p = i / N
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=40)
    center_text(d, W//2, 54, "The Community League — live on GitHub Pages", F_SANSB(42), FG)
    # board rises + fades
    pp = ease(min(1, p*1.3))
    by = int(120 + (1-pp)*120)
    bx = (W - bd.width)//2
    # glow frame
    d.rounded_rectangle([bx-14, by-14, bx+bd.width+14, by+min(bd.height, H-by-20)+14],
                        radius=20, outline=lerp(BG, ACCENT, pp), width=3)
    crop_h = min(bd.height, H - by - 30)
    img.paste(bd.crop((0, 0, bd.width, crop_h)), (bx, by))
    emit(img)

# =========================================================
# SCENE 6 — CTA / outro  ~4s
# =========================================================
N = secs(4.2)
for i in range(N):
    p = i / N
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bg_base(d, shift=int(40+p*30))
    pp = ease(min(1, p*1.5))
    center_text(d, W//2, H//2 - 150, "ATV-bench", F_SANSB(140), lerp(BG, FG, pp))
    center_text(d, W//2, H//2 - 30, "Community League", F_SANSB(72), lerp(BG, ACCENT, pp))
    if p > 0.35:
        qp = ease(min(1, (p-0.35)/0.4))
        center_text(d, W//2, H//2 + 90, "Rank the whole harness. Not just the model.",
                    F_SANS(46), lerp(BG, MUT, qp))
        center_text(d, W//2, H//2 + 190, "uv pip install -e '.[dev]'  ·  atv-bench submit",
                    F_MONOB(38), lerp(BG, ACCENT2, qp))
    emit(img)

print(f"wrote {_frame} frames to {OUT} ({_frame/FPS:.1f}s)")
