"""WhiteCoffee 영상 엔진(wcv.py)을 구글 클라우드 음성(Chirp 3 HD)으로 쓰게 패치한 사본을 만든다.

사용: python3 wcv_google_tts.py <원본 scripts 폴더> [/tmp/eng]
그다음 사본 폴더에서 `GOOGLE_TTS_KEY=<키> python3 wcv.py tts ...` 처럼 실행.
- GOOGLE_TTS_KEY 가 없으면 원래 edge-tts 를 그대로 쓴다.
- edge 목소리 이름은 자동으로 구글 목소리로 바뀐다(쿼카 Q = Aoede, WhiteCoffee A = Charon).
- 속도(rate "+12%")는 반영, 높낮이(pitch)는 Chirp 3 HD 가 지원하지 않아 무시.
- 단어 타이밍은 실제 음성 길이 안에서 글자 수 비례로 추정(자막 싱크용).
"""
import os, shutil, sys

PATCH = r'''

# ---- Google Cloud TTS (Chirp 3 HD) override — tools/wcv_google_tts.py ----
import base64 as _b64, json as _json, re as _re, subprocess as _sp, urllib.request as _ur
_GFEMALE = ("SunHi", "JiMin", "SeoHyeon", "SoonBok", "YuJin")
_edge_tts_orig = _tts

def _g_voice(voice):
    if voice.startswith("ko-KR-Chirp3") or voice.startswith("ko-KR-Neural2"):
        return voice
    female = any(f in voice for f in _GFEMALE)
    return os.environ.get("GOOGLE_TTS_FEMALE" if female else "GOOGLE_TTS_MALE",
                          "ko-KR-Chirp3-HD-Aoede" if female else "ko-KR-Chirp3-HD-Charon")

def _g_rate(rate):
    m = _re.match(r"([+-]?\d+)%", str(rate or "+0%"))
    r = 1 + (int(m.group(1)) / 100 if m else 0)
    return max(0.5, min(2.0, r))

async def _tts(text, voice, rate, mp3, pitch="+0Hz"):
    key = os.environ.get("GOOGLE_TTS_KEY")
    if not key:
        return await _edge_tts_orig(text, voice, rate, mp3, pitch)
    body = {"input": {"text": text}, "voice": {"languageCode": "ko-KR", "name": _g_voice(voice)},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": _g_rate(rate)}}
    req = _ur.Request("https://texttospeech.googleapis.com/v1/text:synthesize?key=" + key,
                      data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=60) as r:
        open(mp3, "wb").write(_b64.b64decode(_json.load(r)["audioContent"]))
    dur = float(_sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp3],
                        capture_output=True, text=True).stdout.strip() or 1)
    toks = text.split() or [text]
    lead, tail = 0.08, 0.2
    span = max(0.3, dur - lead - tail)
    weights = [len(t) + 1 for t in toks]
    tot = sum(weights)
    words, t = [], lead
    for tok, w in zip(toks, weights):
        d = span * w / tot
        words.append([round(t, 3), round(d * 0.92, 3), tok])
        t += d
    return words
# ---- end override ----

'''

def main():
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "/tmp/eng"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    p = os.path.join(dst, "wcv.py")
    s = open(p, encoding="utf-8").read()
    anchor = "\n\ndef tts_line("
    assert anchor in s, "wcv.py 구조가 바뀜: tts_line 을 찾지 못함"
    s = s.replace(anchor, PATCH + "\ndef tts_line(", 1)
    open(p, "w", encoding="utf-8").write(s)
    print(f"구글 음성 패치 완료 → {dst}/wcv.py (GOOGLE_TTS_KEY 있을 때만 사용)")

if __name__ == "__main__":
    main()
