"""슬라이드 릴스용 배경음악 생성기 (직접 합성 → 저작권 걱정 없음)

예전엔 사인파 5개를 한 화음으로 겹쳐 '뚜~~~' 하는 지속음만 나왔다(10/19 지적).
여기서는 코드 진행·베이스·드럼·멜로디가 있는 짧은 곡을 numpy 로 합성해
assets/bgm/*.m4a 로 저장한다. post_to_instagram.py 의 make_slideshow 가 이 중 하나를 골라 쓴다.

    python tools/make_bgm.py          # assets/bgm/ 에 4곡 생성
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, sosfilt

SR = 44100
OUT = Path(__file__).resolve().parent.parent / "assets" / "bgm"

NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
        "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
QUAL = {"maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10], "7": [0, 4, 7, 10], "maj": [0, 4, 7, 12],
        "min": [0, 3, 7, 12], "m9": [0, 3, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "sus": [0, 5, 7, 10],
        "add9": [0, 4, 7, 14]}


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def chord(name: str, octave: int = 4) -> tuple[int, list[int]]:
    root = name[:2] if len(name) > 1 and name[1] in "#b" else name[:1]
    q = name[len(root):] or "maj"
    r = 12 * (octave + 1) + NOTE[root]
    return r, [r + i for i in QUAL[q]]


def env(n: int, a: float, d: float) -> np.ndarray:
    t = np.arange(n) / SR
    e = np.exp(-t / d)
    na = max(1, int(a * SR))
    e[:na] *= np.linspace(0, 1, na)
    return e


def release(x: np.ndarray, r: float = 0.06) -> np.ndarray:
    nr = min(len(x), int(r * SR))
    x[-nr:] *= np.linspace(1, 0, nr)
    return x


# ─── 악기 ────────────────────────────────────────────
def epiano(f: float, dur: float, vel: float) -> np.ndarray:
    """로즈 느낌 FM 일렉 피아노"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    idx = 1.4 * np.exp(-t / 0.25)
    y = np.sin(2 * np.pi * f * t + idx * np.sin(2 * np.pi * f * t))
    y += 0.12 * np.sin(2 * np.pi * f * 14 * t) * np.exp(-t / 0.03)   # 건반 '팅' 소리
    return release(y * env(n, 0.004, 1.6) * vel)


def pluck(f: float, dur: float, vel: float) -> np.ndarray:
    """멜로디용 뜯는 소리(배음별로 빠르게 감쇠)"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = np.zeros(n)
    for k in range(1, 9):
        y += (1 / k ** 1.4) * np.sin(2 * np.pi * f * k * t) * np.exp(-t * (2.5 + 2.2 * k))
    return release(y * env(n, 0.002, 2.0) * vel, 0.03)


def bell(f: float, dur: float, vel: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * f * t + 2.0 * np.exp(-t / 0.4) * np.sin(2 * np.pi * f * 3.5 * t))
    return release(y * env(n, 0.003, 0.9) * vel)


def bass(f: float, dur: float, vel: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * 2 * f * t) + 0.08 * np.sin(2 * np.pi * 3 * f * t)
    e = env(n, 0.008, 0.9) * 0.6 + 0.4 * env(n, 0.008, 3.0)
    return release(np.tanh(1.3 * y) * e * vel, 0.05)


def kick(vel: float) -> np.ndarray:
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 95 * np.exp(-t / 0.035)
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.sin(ph) * np.exp(-t / 0.16) + 0.3 * np.random.randn(n) * np.exp(-t / 0.004)
    return y * vel


_SNARE_BP = butter(2, [900, 6000], "bandpass", fs=SR, output="sos")
_HAT_HP = butter(4, 7000, "highpass", fs=SR, output="sos")


def snare(vel: float) -> np.ndarray:
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    noise = sosfilt(_SNARE_BP, np.random.randn(n)) * np.exp(-t / 0.07)
    body = np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.05)
    return (1.1 * noise + 0.5 * body) * vel


def clap(vel: float) -> np.ndarray:
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    e = np.zeros(n)
    for o in (0, 0.011, 0.022):
        s = int(o * SR)
        e[s:] += np.exp(-(t[: n - s]) / (0.012 if o < 0.02 else 0.09))
    return sosfilt(_SNARE_BP, np.random.randn(n)) * e * vel


def hat(vel: float, open_: bool = False) -> np.ndarray:
    n = int((0.22 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    return sosfilt(_HAT_HP, np.random.randn(n)) * np.exp(-t / (0.07 if open_ else 0.015)) * vel


# ─── 믹스 유틸 ────────────────────────────────────────
class Track:
    def __init__(self, seconds: float):
        self.n = int(seconds * SR)
        self.buses: dict[str, np.ndarray] = {}

    def add(self, bus: str, at: float, x: np.ndarray, pan: float = 0.0):
        b = self.buses.setdefault(bus, np.zeros((self.n, 2)))
        s = int(at * SR)
        if s >= self.n or s < 0:
            return
        x = x[: self.n - s]
        l, r = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
        b[s: s + len(x), 0] += x * l * 1.414
        b[s: s + len(x), 1] += x * r * 1.414


def reverb(x: np.ndarray, sec: float = 1.4, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(sec * SR)
    t = np.arange(n) / SR
    out = np.zeros_like(x)
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-t / (sec / 5))
        ir[: int(0.012 * SR)] = 0
        ir /= np.sqrt(np.sum(ir ** 2))
        out[:, ch] = fftconvolve(x[:, ch], ir)[: len(x)]
    return out


def haas(x: np.ndarray, ms: float = 11) -> np.ndarray:
    d = int(ms * SR / 1000)
    y = x.copy()
    y[d:, 1] = x[:-d, 1]
    return y


# ─── 곡 정의 ──────────────────────────────────────────
SONGS = [
    # 이름, bpm, 코드(마디당 1개), 스윙, 분위기, 저역통과(Hz)
    # 10/19: 잔잔한 로파이 대신 신나는 곡으로만 돌리기로 함(사용자 요청). 로파이 스타일 코드는 남겨 둠.
    dict(name="bgm_02_bright_groove", bpm=114, chords=["Cmaj7", "Am7", "Dm7", "G7"], swing=0.5,
         style="pop", lp=15000, seed=23),
    dict(name="bgm_04_fresh_start", bpm=118, chords=["Am7", "Fmaj7", "Cmaj7", "Gsus"], swing=0.5,
         style="pop", lp=15000, seed=41),
    dict(name="bgm_05_sunny_drive", bpm=122, chords=["Fmaj7", "G7", "Em7", "Am7"], swing=0.5,
         style="pop", lp=15000, seed=53),
    dict(name="bgm_06_level_up", bpm=124, chords=["Dm7", "Bbmaj7", "Cmaj", "Am7"], swing=0.5,
         style="pop", lp=15000, seed=67),
]


def render(song: dict, bars: int = 16) -> np.ndarray:
    rng = np.random.default_rng(song["seed"])
    np.random.seed(song["seed"])
    beat = 60 / song["bpm"]
    bar = 4 * beat
    tr = Track(bars * bar + 2.5)
    sw = song["swing"]
    pop = song["style"] == "pop"

    def eighth(b: int, i: int) -> float:          # i: 0~7 (8분음표), 스윙 적용
        q, h = divmod(i, 2)
        return b * bar + q * beat + (sw * beat if h else 0) + rng.normal(0, 0.004)

    for b in range(bars):
        name = song["chords"][b % len(song["chords"])]
        root, notes = chord(name, 3)
        nxt_root, _ = chord(song["chords"][(b + 1) % len(song["chords"])], 3)
        # 건반: 1박 + (팝은 2박 엇박, 로파이는 3박 엇박) 컴핑
        comp = [(0, 1.0, 2.2 * beat), (5 if not pop else 3, 0.7, 1.5 * beat)]
        if pop:
            comp.append((6, 0.55, 1.0 * beat))
        for i, v, d in comp:
            t0 = eighth(b, i)
            for k, m in enumerate(notes[1:] + [notes[0] + 12]):     # 근음은 베이스가 맡음
                tr.add("keys", t0 + k * 0.006, epiano(hz(m + 12), d, 0.16 * v * rng.uniform(0.85, 1)),
                       pan=(k - 1.5) * 0.25)
        # 베이스: 1박 근음, 3박 근음/5도, 마디 끝 다음 코드로 이어주는 음
        bpat = [(0, root, 1.4 * beat), (4, root + (7 if b % 2 else 0), 0.9 * beat), (7, nxt_root - 1 if b % 4 == 3 else root, 0.4 * beat)]
        if pop:
            bpat = [(0, root, 0.9 * beat), (3, root, 0.4 * beat), (4, root + 7, 0.9 * beat), (6, root + 12, 0.4 * beat), (7, root, 0.4 * beat)]
        for i, m, d in bpat:
            tr.add("bass", eighth(b, i), bass(hz(m - 12), d, 0.55))

        # 드럼 (첫 마디는 가볍게 시작, 8마디째 끝은 쉼)
        fill_gap = b % 8 == 7
        for i in range(8):
            t0 = eighth(b, i)
            if i in ((0, 4) if not pop else (0, 2, 4, 6)):
                if not (fill_gap and i == 6):
                    tr.add("drums", t0, kick(0.95 if pop else 0.85))
            if not pop and i == 5 and b % 2 == 1:
                tr.add("drums", t0, kick(0.5))
            if i in (2, 6) and b > 0:
                tr.add("drums", t0, (clap if pop else snare)(0.42 if pop else 0.38), pan=0.05)
            if b > 0 or i >= 4:
                o = pop and i % 2 == 1
                tr.add("hats", t0, hat((0.16 if i % 2 == 0 else 0.1) * rng.uniform(0.7, 1.1), open_=o),
                       pan=0.3)

        # 멜로디: 3마디째부터. 코드톤+펜타토닉에서 고른 짧은 프레이즈(2마디 단위로 반복 변형)
        if b >= 2:
            scale_root = chord(song["chords"][0], 5)[0]
            penta = [scale_root + x for x in (0, 2, 4, 7, 9, 12, 14, 16)]
            tones = sorted(set([n + 24 for n in notes[1:]] + penta))
            motif_rng = np.random.default_rng(song["seed"] * 100 + (b % 4))
            slots = sorted(motif_rng.choice(8, size=3 if not pop else 4, replace=False))
            for j, i in enumerate(slots):
                m = tones[motif_rng.integers(0, len(tones))]
                inst = bell if pop else pluck
                tr.add("lead", eighth(b, i), inst(hz(m), 0.9 * beat, 0.13 if pop else 0.15),
                       pan=0.35 if j % 2 else -0.35)

        # 패드: 은은한 화음을 깔아 빈 공간을 채움(뚜~ 소리가 안 나게 아주 작게, 마디마다 코드 바뀜)
        n = int(bar * SR)
        t = np.arange(n) / SR
        pad = np.zeros(n)
        for m in notes[:3]:
            for det in (-0.08, 0.08):
                pad += np.sin(2 * np.pi * hz(m + 12 + det) * t)
        pe = np.minimum(1, t / 0.5) * np.minimum(1, (bar - t) / 0.4)
        tr.add("pad", b * bar, pad * pe * 0.018)

    # 레코드 잡음(로파이만)
    if not pop:
        hiss = sosfilt(butter(2, [2000, 8000], "bandpass", fs=SR, output="sos"), np.random.randn(tr.n)) * 0.006
        crack = (np.random.rand(tr.n) > 0.99985) * np.random.randn(tr.n) * 0.25
        tr.buses["noise"] = np.stack([hiss + crack, hiss + np.roll(crack, 37)], 1)

    # 믹스: 킥이 칠 때 건반·패드가 살짝 눌리는 덕킹
    drums = tr.buses["drums"]
    kick_env = np.abs(drums[:, 0])
    kick_env = np.convolve(kick_env, np.ones(int(0.06 * SR)) / int(0.06 * SR), "same")
    duck = 1 - np.clip(kick_env * (1.6 if pop else 1.1), 0, 0.45)
    keys = haas(tr.buses["keys"]) * duck[:, None]
    pad = tr.buses["pad"] * duck[:, None]
    lead = tr.buses.get("lead", np.zeros((tr.n, 2)))
    wet = reverb(keys * 0.5 + lead * 0.8 + pad * 0.5 + tr.buses["drums"] * 0.08, 1.8 if not pop else 1.2)
    mix = (keys + pad + lead * 0.9 + tr.buses["bass"] * 0.85 + drums * 0.9 + tr.buses["hats"]
           + wet * (0.35 if not pop else 0.25) + tr.buses.get("noise", 0))
    mix = sosfilt(butter(2, 30, "highpass", fs=SR, output="sos"), mix, axis=0)
    mix = sosfilt(butter(2, song["lp"], "lowpass", fs=SR, output="sos"), mix, axis=0)
    # 음량 맞추기 + 부드러운 클리핑
    mix /= np.sqrt(np.mean(mix ** 2)) / 0.16
    mix = np.tanh(mix * 1.1) / np.tanh(1.1)
    mix *= 0.89 / np.max(np.abs(mix))
    return mix[: int(bars * bar * SR)]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for s in SONGS:
        if only and s["name"] not in only:
            continue
        y = render(s)
        wav = OUT / f"{s['name']}.wav"
        wavfile.write(wav, SR, (y * 32767).astype(np.int16))
        m4a = wav.with_suffix(".m4a")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "160k",
                        str(m4a)], check=True)
        wav.unlink()
        print(f"{m4a.name}: {len(y) / SR:.1f}초")


if __name__ == "__main__":
    main()
