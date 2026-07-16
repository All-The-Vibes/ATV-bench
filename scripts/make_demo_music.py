"""Synthesize a royalty-free house beat (~124 BPM) for the ATV-bench demo video.

Pure-numpy synthesis: four-on-the-floor kick, offbeat open hats, claps,
a rolling sub-bass, and a Cmin chord-stab progression. Written to a 44.1k WAV.
No samples, no external assets — fully original / license-clean.
"""
import sys
import numpy as np

SR = 44100
BPM = 124.0
BEAT = 60.0 / BPM            # seconds per beat
BAR = BEAT * 4
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 32.0
OUT = sys.argv[1] if len(sys.argv) > 1 else "demo_music.wav"

n = int(DUR * SR)
t = np.arange(n) / SR
mix = np.zeros(n)


def env(length, attack=0.002, decay=0.15, kind="exp"):
    m = int(length * SR)
    e = np.zeros(m)
    a = max(1, int(attack * SR))
    e[:a] = np.linspace(0, 1, a)
    rest = m - a
    if rest > 0:
        if kind == "exp":
            e[a:] = np.exp(-np.linspace(0, 1, rest) / decay)
        else:
            e[a:] = np.linspace(1, 0, rest)
    return e


def add(sig, start):
    i = int(start * SR)
    j = min(len(mix), i + len(sig))
    if i < len(mix):
        mix[i:j] += sig[: j - i]


def kick(length=0.32):
    m = int(length * SR)
    tt = np.arange(m) / SR
    f = 130 * np.exp(-tt * 32) + 48          # pitch drop
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-tt * 7.5)
    click = np.exp(-tt * 220) * 0.6
    return (body + click) * 0.95


def hat(length=0.05, open_=False):
    m = int(length * SR)
    tt = np.arange(m) / SR
    noise = np.random.default_rng(1).standard_normal(m)
    d = 6 if open_ else 55
    return noise * np.exp(-tt * d) * (0.22 if open_ else 0.16)


def clap(length=0.2):
    m = int(length * SR)
    tt = np.arange(m) / SR
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(m)
    e = np.exp(-tt * 18) + 0.5 * np.exp(-((tt - 0.01) * 90) ** 2)
    return noise * e * 0.35


def bass(freq, length):
    m = int(length * SR)
    tt = np.arange(m) / SR
    wave = np.sin(2 * np.pi * freq * tt) + 0.3 * np.sin(2 * np.pi * 2 * freq * tt)
    e = np.minimum(1, tt * 60) * np.exp(-tt * 2.2)
    return wave * e * 0.3


def stab(freqs, length):
    m = int(length * SR)
    tt = np.arange(m) / SR
    w = np.zeros(m)
    for f in freqs:
        w += np.sin(2 * np.pi * f * tt) + 0.2 * np.sin(2 * np.pi * 2 * f * tt)
    w /= len(freqs)
    e = np.exp(-tt * 9) * (1 - np.exp(-tt * 400))
    return w * e * 0.28


# Cmin -> Abmaj -> Ebmaj -> Bbmaj (classic house progression), one chord/bar
CHORDS = [
    [261.63, 311.13, 392.00],   # Cm
    [207.65, 261.63, 311.13],   # Ab
    [311.13, 392.00, 466.16],   # Eb
    [233.08, 293.66, 349.23],   # Bb
]
BASSNOTES = [65.41, 51.91, 77.78, 58.27]

nbars = int(np.ceil(DUR / BAR))
for b in range(nbars):
    bar_t = b * BAR
    ch = CHORDS[b % 4]
    bn = BASSNOTES[b % 4]
    for beat in range(4):
        bt = bar_t + beat * BEAT
        add(kick(), bt)                              # four on the floor
        add(hat(open_=True), bt + BEAT * 0.5)        # offbeat open hat
        add(hat(), bt + BEAT * 0.25)
        add(hat(), bt + BEAT * 0.75)
        if beat in (1, 3):
            add(clap(), bt)                          # backbeat clap
        # bassline: root on 8ths
        add(bass(bn, BEAT * 0.5), bt)
        add(bass(bn, BEAT * 0.5), bt + BEAT * 0.5)
    # chord stabs on the & of 2 and 4 (skip first bar for an intro build)
    if b >= 1:
        add(stab(ch, BEAT * 0.6), bar_t + BEAT * 1.5)
        add(stab(ch, BEAT * 0.6), bar_t + BEAT * 3.5)

# gentle master bus: soft clip + fade in/out
mix = np.tanh(mix * 1.3)
fade = int(0.5 * SR)
mix[:fade] *= np.linspace(0, 1, fade)
mix[-fade:] *= np.linspace(1, 0, fade)
mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.89

pcm = (mix * 32767).astype("<i2")
import wave as wavelib
with wavelib.open(OUT, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print(f"wrote {OUT} ({DUR:.1f}s, {BPM:.0f} BPM)")
