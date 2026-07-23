#!/usr/bin/env python3
"""Render captured terminal text into a PNG "screenshot" for E2E proof.

Usage: shot_terminal.py <title> <input_text_file> <output_png>
Reads UTF-8 text (ANSI codes stripped) and renders it on a dark terminal
background with a title bar, producing verifiable visual evidence of a
command's real output.
"""
import re
import sys
from PIL import Image, ImageDraw, ImageFont

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    title, in_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    text = ANSI.sub("", raw).replace("\t", "    ")
    # Cap very long captures so the image stays legible.
    lines = text.split("\n")
    if len(lines) > 60:
        lines = lines[:57] + ["", f"... ({len(text.splitlines())} total lines, truncated) ..."]

    fs = 15
    lh = fs + 5
    font = load_font(fs)
    tfont = load_font(fs + 1, bold=True)
    pad = 18
    bar = 40
    maxw = max((len(l) for l in lines), default=40)
    char_w = font.getlength("M") or 9
    width = int(min(max(maxw, len(title) + 6), 200) * char_w) + pad * 2
    height = bar + len(lines) * lh + pad * 2

    img = Image.new("RGB", (width, height), "#0d1117")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar], fill="#161b22")
    for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        d.ellipse([14 + i * 22, 14, 27 + i * 22, 27], fill=c)
    d.text((90, 12), title, font=tfont, fill="#c9d1d9")
    y = bar + pad
    for l in lines:
        color = "#c9d1d9"
        low = l.lower()
        if any(k in low for k in ("error", "fail", "traceback", "critical", "✗")):
            color = "#ff7b72"
        elif any(k in l for k in ("PASS", "✅", "passed", "OK", "✓", "NICE")):
            color = "#7ee787"
        d.text((pad, y), l, font=font, fill=color)
        y += lh
    img.save(out_path)
    print(f"wrote {out_path} ({width}x{height})")


if __name__ == "__main__":
    main()
