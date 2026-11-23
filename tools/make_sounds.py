"""Make the activity sounds: one short WAV per activity kind (see algotrade/activity.py).

The same files are used by the dashboard (algotrade/static/sounds/<kind>.wav, played while the page is open) and
by the Android app (android/app/src/main/res/raw/at_<kind>.wav, the sound of that kind's notification channel),
so a sound means the same thing everywhere. Run again after changing a sound:  python tools/make_sounds.py

Each sound is a few notes: (frequency Hz, start s, length s, wave, volume). Rising = good / new, falling = bad,
square = needs you."""
import math
import struct
import wave
from pathlib import Path

RATE = 22050
ROOT = Path(__file__).resolve().parent.parent
OUT_WEB = ROOT / "algotrade" / "static" / "sounds"
OUT_APP = ROOT / "android" / "app" / "src" / "main" / "res" / "raw"

C5, D5, E5, G5, A5, B4, C6, E6, G6 = 523.25, 587.33, 659.25, 783.99, 880.0, 493.88, 1046.5, 1318.5, 1568.0
SOUNDS = {
    # bright two-note chime going up: something new to look at
    "signal": [(A5, 0.00, 0.16, "sine", 0.9), (E6, 0.14, 0.32, "sine", 0.9)],
    # one warm note: a signal's entry price was reached
    "entry": [(G5, 0.00, 0.35, "triangle", 0.9)],
    # two quick clicks: an order went to the broker
    "placed": [(1200, 0.00, 0.06, "triangle", 0.7), (1200, 0.10, 0.06, "triangle", 0.7)],
    # major triad going up: a trade opened
    "filled": [(C5, 0.00, 0.14, "sine", 0.8), (E5, 0.11, 0.14, "sine", 0.8), (G5, 0.22, 0.40, "sine", 0.9)],
    # quick arpeggio up to the octave: closed in profit
    "profit": [(C5, 0.00, 0.10, "sine", 0.8), (E5, 0.08, 0.10, "sine", 0.8), (G5, 0.16, 0.10, "sine", 0.8),
               (C6, 0.24, 0.55, "sine", 1.0)],
    # three notes falling, slower: closed at a loss
    "loss": [(B4, 0.00, 0.22, "triangle", 0.9), (392.0, 0.20, 0.22, "triangle", 0.9), (293.66, 0.40, 0.50, "triangle", 0.9)],
    # the same note twice: closed about even
    "closed": [(D5, 0.00, 0.14, "sine", 0.8), (D5, 0.18, 0.30, "sine", 0.8)],
    # one low note sliding down: cancelled
    "cancelled": [(330.0, 0.00, 0.28, "sine", 0.8, 247.0)],
    # three sharp beeps, twice: needs you
    "problem": [(f, s, 0.09, "square", 0.45) for s in (0.0, 0.13, 0.26, 0.55, 0.68, 0.81) for f in (988.0,)],
    # a bell: price alert
    "alert": [(C6, 0.00, 1.20, "bell", 0.9)],
    # one soft high tick: for your information
    "info": [(G6, 0.00, 0.08, "sine", 0.35)],
}


def _wave(kind: str, phase: float) -> float:
    if kind == "square":
        return 1.0 if math.sin(phase) >= 0 else -1.0
    if kind == "triangle":
        return 2 / math.pi * math.asin(math.sin(phase))
    if kind == "bell":   # a few inharmonic partials, the higher ones fading faster
        return (math.sin(phase) + 0.5 * math.sin(2.76 * phase) + 0.25 * math.sin(5.40 * phase)) / 1.75
    return math.sin(phase)


def render(notes) -> list[float]:
    end = max(n[1] + n[2] for n in notes) + 0.05
    buf = [0.0] * int(end * RATE)
    for note in notes:
        f0, start, length, kind, vol = note[:5]
        f1 = note[5] if len(note) > 5 else f0   # optional slide to this frequency
        i0, n = int(start * RATE), int(length * RATE)
        phase = 0.0
        for i in range(n):
            t = i / RATE
            f = f0 + (f1 - f0) * (i / n)
            phase += 2 * math.pi * f / RATE
            attack = min(1.0, t / 0.006)
            decay = math.exp(-t * (3.0 if kind == "bell" else 5.0 / max(length, 0.05)))
            release = min(1.0, (n - i) / (0.012 * RATE))
            buf[i0 + i] += vol * attack * decay * release * _wave(kind, phase)
    peak = max(1e-9, max(abs(x) for x in buf))
    return [x * 0.8 / peak for x in buf]


def write(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767)) for x in samples))


if __name__ == "__main__":
    for kind, notes in SOUNDS.items():
        s = render(notes)
        write(OUT_WEB / f"{kind}.wav", s)
        write(OUT_APP / f"at_{kind}.wav", s)
        print(f"{kind:10s} {len(s) / RATE:.2f} s")
