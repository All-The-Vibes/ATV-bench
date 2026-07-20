"""Synthesize a royalty-free deep/melodic house beat for the ATV-bench demo.

Pure-numpy synthesis — no samples, no external assets, fully original and
license-clean. The track is built for a ~30s product showcase:

  * 123 BPM four-on-the-floor kick with a tuned pitch-drop body
  * rolling offbeat sub-bass with a moving low-pass filter
  * sidechain "pump" — everything but the kick ducks on each beat
  * filtered intro that opens up into the main groove (the "drop")
  * lush detuned reverb pad + shimmer arp for the melodic top
  * offbeat open hats, ghost closed hats, and a backbeat clap
  * classic Cm -> Ab -> Eb -> Bb progression, one chord per bar

Written to a 44.1k mono WAV.
"""
import sys
import wave as wavelib

import numpy as np
from scipy.signal import lfilter

SR = 44100
BPM = 123.0
BEAT = 60.0 / BPM            # seconds per beat
BAR = BEAT * 4
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
OUT = sys.argv[1] if len(sys.argv) > 1 else "demo_music.wav"

n = int(DUR * SR)
t = np.arange(n) / SR

# Separate buses so we can sidechain the musical parts against the kick.
drums = np.zeros(n)     # kick only (the sidechain trigger source)
perc = np.zeros(n)      # hats + claps
music = np.zeros(n)     # bass + pad + stabs + arp (ducked by the kick)


def _place(buf, sig, start):
    i = int(start * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i]


def one_pole_lp(x, cutoff):
    """One-pole low-pass. cutoff in Hz — scalar (fast, vectorized) or a
    per-sample array (time-varying sweep, chunked to stay fast)."""
    dt = 1.0 / SR
    if np.isscalar(cutoff):
        rc = 1.0 / (2 * np.pi * max(cutoff, 1.0))
        alpha = dt / (rc + dt)
        # y[k] = y[k-1] + alpha*(x[k]-y[k-1])  ->  IIR: b=[a], a=[1, a-1]
        return lfilter([alpha], [1.0, alpha - 1.0], x)
    # time-varying: approximate by processing in short constant-cutoff blocks
    rc = 1.0 / (2 * np.pi * np.maximum(cutoff, 1.0))
    alpha = dt / (rc + dt)
    y = np.empty_like(x)
    prev = 0.0
    block = 256
    for s in range(0, len(x), block):
        e = min(len(x), s + block)
        a = float(alpha[s])
        seg = lfilter([a], [1.0, a - 1.0], x[s:e], zi=[prev * (1 - a)])[0]
        y[s:e] = seg
        prev = y[e - 1]
    return y


# ---------------------------------------------------------------- instruments
def kick(length=0.42):
    m = int(length * SR)
    tt = np.arange(m) / SR
    f = 135 * np.exp(-tt * 34) + 47          # pitch drop into a deep tone
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-tt * 6.2)
    click = np.exp(-tt * 240) * 0.55
    return (body + click) * 1.05


def hat(length=0.06, open_=False):
    m = int(length * SR)
    tt = np.arange(m) / SR
    noise = np.random.default_rng(1).standard_normal(m)
    d = 7 if open_ else 60
    return noise * np.exp(-tt * d) * (0.18 if open_ else 0.12)


def clap(length=0.24):
    m = int(length * SR)
    tt = np.arange(m) / SR
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(m)
    # three quick transients then a body — that "spread" clap sound
    e = np.zeros(m)
    for off in (0.0, 0.008, 0.016):
        e += np.exp(-((tt - off) * 130) ** 2)
    e = 0.6 * e + np.exp(-tt * 16)
    return noise * e * 0.3


def sub_bass(freq, length):
    """Rolling sub with a little 2nd/3rd harmonic and a soft filter sweep."""
    m = int(length * SR)
    tt = np.arange(m) / SR
    wave = (np.sin(2 * np.pi * freq * tt)
            + 0.35 * np.sin(2 * np.pi * 2 * freq * tt)
            + 0.12 * np.sin(2 * np.pi * 3 * freq * tt))
    e = np.minimum(1, tt * 80) * np.exp(-tt * 1.9)
    return wave * e * 0.34


def pad(freqs, length):
    """Lush detuned pad — several slightly detuned saws, slow attack."""
    m = int(length * SR)
    tt = np.arange(m) / SR
    w = np.zeros(m)
    for f in freqs:
        for det in (-0.18, 0.0, 0.2):
            fr = f * (1 + det / 100.0)
            # band-limited-ish saw via a few harmonics
            for h in (1, 2, 3, 4):
                w += (1.0 / h) * np.sin(2 * np.pi * fr * h * tt)
    w /= (len(freqs) * 3)
    attack = np.minimum(1, tt * 3.5)
    release = np.minimum(1, (length - tt) * 6)
    e = attack * np.clip(release, 0, 1)
    return one_pole_lp(w, 1600) * e * 0.09


def stab(freqs, length):
    m = int(length * SR)
    tt = np.arange(m) / SR
    w = np.zeros(m)
    for f in freqs:
        w += np.sin(2 * np.pi * f * tt) + 0.2 * np.sin(2 * np.pi * 2 * f * tt)
    w /= len(freqs)
    e = np.exp(-tt * 9) * (1 - np.exp(-tt * 400))
    return one_pole_lp(w, 2600) * e * 0.22


def pluck(freq, length):
    """Short shimmer arp voice for the melodic top."""
    m = int(length * SR)
    tt = np.arange(m) / SR
    w = np.sin(2 * np.pi * freq * tt) + 0.4 * np.sin(2 * np.pi * 2 * freq * tt)
    e = np.exp(-tt * 12) * (1 - np.exp(-tt * 500))
    return w * e * 0.10


# ---------------------------------------------------------------- arrangement
# Cm -> Ab -> Eb -> Bb, one chord per bar
CHORDS = [
    [261.63, 311.13, 392.00],   # Cm
    [207.65, 261.63, 311.13],   # Ab
    [311.13, 392.00, 466.16],   # Eb
    [233.08, 293.66, 349.23],   # Bb
]
PADCH = [[f * 0.5 for f in c] + c for c in CHORDS]   # add an octave below
BASSNOTES = [65.41, 51.91, 77.78, 58.27]             # C Ab Eb Bb
# arp pattern (indices into the chord) — 8 sixteenths per bar
ARP = [0, 1, 2, 1, 0, 2, 1, 2]

nbars = int(np.ceil(DUR / BAR))
INTRO_BARS = 2   # filtered build before the groove fully opens

for b in range(nbars):
    bar_t = b * BAR
    ch = CHORDS[b % 4]
    padch = PADCH[b % 4]
    bn = BASSNOTES[b % 4]
    dropped = b >= INTRO_BARS

    # pad sustains the whole bar for atmosphere
    _place(music, pad(padch, BAR * 0.98), bar_t)

    for beat in range(4):
        bt = bar_t + beat * BEAT
        _place(drums, kick(), bt)                        # four on the floor
        _place(perc, hat(), bt + BEAT * 0.5)             # offbeat closed
        if dropped:
            _place(perc, hat(open_=True), bt + BEAT * 0.5)
            _place(perc, hat(), bt + BEAT * 0.25)
            _place(perc, hat(), bt + BEAT * 0.75)
        if beat in (1, 3):
            _place(perc, clap(), bt)                     # backbeat clap
        # rolling sub on 8ths
        _place(music, sub_bass(bn, BEAT * 0.5), bt)
        _place(music, sub_bass(bn, BEAT * 0.5), bt + BEAT * 0.5)

    if dropped:
        # chord stabs on the & of 2 and 4
        _place(music, stab(ch, BEAT * 0.6), bar_t + BEAT * 1.5)
        _place(music, stab(ch, BEAT * 0.6), bar_t + BEAT * 3.5)
        # shimmer arp across the bar
        for s, idx in enumerate(ARP):
            note = ch[idx % len(ch)] * 2      # up an octave
            _place(music, pluck(note, BEAT * 0.5), bar_t + s * (BEAT / 2))

# ---------------------------------------------------------------- sidechain
# Duck the music bus on every kick — the classic house "pump".
pump = np.ones(n)
depth = 0.72
for b in range(nbars):
    for beat in range(4):
        i = int((b * BAR + beat * BEAT) * SR)
        if i >= n:
            continue
        rel = int(BEAT * 0.85 * SR)
        j = min(n, i + rel)
        shape = 1 - depth * np.exp(-np.linspace(0, 1, j - i) / 0.16)
        pump[i:j] = np.minimum(pump[i:j], shape)
# smooth the envelope so it doesn't click
pump = one_pole_lp(pump, 90)
music *= pump
perc *= (0.55 + 0.45 * pump)   # hats breathe a little too

# ---------------------------------------------------------------- intro filter
# Sweep a low-pass over the whole mix during the intro so it "opens up".
intro_end = INTRO_BARS * BAR
cutoff = np.full(n, SR / 2.0)
mask = t < intro_end
sweep = 320 + (6000 - 320) * (t[mask] / max(intro_end, 1e-9)) ** 2
cutoff[mask] = sweep

mix = drums + perc + music
mix[mask] = one_pole_lp(mix[mask], cutoff[mask])

# ---------------------------------------------------------------- master bus
mix = np.tanh(mix * 1.25)                     # gentle glue/saturation
fade = int(0.5 * SR)
mix[:fade] *= np.linspace(0, 1, fade)
mix[-fade:] *= np.linspace(1, 0, fade)
mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.92

pcm = (mix * 32767).astype("<i2")
with wavelib.open(OUT, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print(f"wrote {OUT} ({DUR:.1f}s, {BPM:.0f} BPM deep house)")
