"""숫자 한 방 캐러셀 렌더러 (2026-11-24, 사용자와 함께 만듦).

인스타 '음악 넣은 캐러셀'용 1080×1350 PNG 6장 안팎을 만든다. 음악은 API로 못 붙여서(릴스만 지원,
그것도 Facebook 로그인 토큰만) 이 장표는 사용자가 앱에서 직접 올리고 음악을 고른다.

사용: python3 tools/num_carousel.py spec.json out_dir
spec = {"slides": [ {type, theme(dark|light, 생략하면 번갈아), ...}, ... ]}

슬라이드 종류
  hook     : kicker, title(<em>강조</em>), big, quokka(assets/quokka_a 파일 이름), hint
  guess    : kicker, drop{from,to,pct}(선택), question, options[]
  answer   : label, big, sub, timeline{from,to,mid[{label,pos(0~1, 시간 비율)}]}(선택)
  twist    : kicker, title, big, emoji, sub
  compare  : title, sub, bars[{label,value,tag,ratio(0~1),color(coral|green|gold)}], why
  steps    : kicker, title, title_size(선택), steps[{top(블록 위 글자), label, note, w(가로 비율, 예: 걸린 개월),
             color}], base(첫 계단 높이 0~1, 기본 .5), why — 계단 그래프(가로=걸린 시간, 높이=쌓인 양)
  takeaway : title, checks[], foot, blog, quokka(선택)
2장은 넘기지 않은 사람에게 다시 뜨는 자리라, 그 장만 봐도 궁금해지게 쓴다.
"""
import html
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "assets" / "quokka_a"

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans CJK KR','Noto Sans KR','Noto Color Emoji',sans-serif;background:#000}
section.s{width:1080px;height:1350px;position:relative;overflow:hidden;padding:120px 92px 140px;display:flex;flex-direction:column}
.dark{background:radial-gradient(1200px 900px at 85% -10%,#24345f 0%,#16213e 55%,#101a33 100%);color:#fff6e6}
.light{background:radial-gradient(1100px 800px at 10% -10%,#fffdf8 0%,#fff3e0 60%,#ffe9cc 100%);color:#16213e}
.dark em{color:#ffc93c;font-style:normal}
.light em{color:#ff5e5b;font-style:normal}
.top{position:absolute;top:52px;left:92px;right:92px;display:flex;justify-content:space-between;align-items:center;font-size:30px;font-weight:700;opacity:.85}
.brand{padding:10px 22px;border-radius:40px;font-size:28px}
.dark .brand{background:rgba(255,246,230,.12)}
.light .brand{background:rgba(22,33,62,.08)}
.pg{font-size:28px;letter-spacing:2px}
.kicker{font-size:42px;font-weight:700;opacity:.8;margin-bottom:26px}
.title{font-size:80px;font-weight:900;line-height:1.22;letter-spacing:-2px;word-break:keep-all}
.big{font-size:150px;font-weight:900;line-height:1.08;letter-spacing:-5px;white-space:nowrap}
.sub{font-size:44px;font-weight:500;line-height:1.45;opacity:.9;word-break:keep-all}
.hint{position:absolute;bottom:64px;right:92px;font-size:36px;font-weight:700;opacity:.75}
.foot{position:absolute;bottom:58px;left:92px;right:92px;font-size:26px;line-height:1.5;opacity:.6;word-break:keep-all}
.q{position:absolute;right:40px;bottom:96px;height:430px}
.qs{position:absolute;right:60px;bottom:150px;height:340px}
.drop{display:flex;align-items:center;gap:24px;margin:26px 0 48px}
.chip{padding:20px 30px;border-radius:28px;font-size:54px;font-weight:900;white-space:nowrap}
.light .chip.a{background:#16213e;color:#fff6e6}
.light .chip.b{background:#ff5e5b;color:#fff}
.arrow{font-size:64px;font-weight:900}
.pct{font-size:96px;font-weight:900;color:#ff5e5b;letter-spacing:-3px;margin-bottom:24px}
.question{font-size:62px;font-weight:900;line-height:1.25;margin-bottom:34px;word-break:keep-all}
.opts{display:flex;flex-direction:column;gap:20px}
.opt{font-size:52px;font-weight:800;padding:20px 40px;border-radius:28px;border:5px solid currentColor}
.light .opt{background:#fff}
.dark .opt{background:rgba(255,255,255,.06)}
.label{display:inline-block;font-size:46px;font-weight:900;padding:14px 32px;border-radius:40px;margin-bottom:34px;align-self:flex-start}
.dark .label{background:#ffc93c;color:#16213e}
.light .label{background:#ff5e5b;color:#fff}
.tl{margin-top:90px;position:relative;height:190px}
.tl .line{position:absolute;top:60px;left:0;right:0;height:14px;border-radius:7px;background:linear-gradient(90deg,#ff5e5b,#ffc93c 60%,#22b573)}
.tl .dot{position:absolute;top:44px;width:46px;height:46px;border-radius:50%;background:#fff6e6;border:8px solid #16213e}
.light .tl .dot{background:#16213e;border-color:#fff3e0}
.tl .t{position:absolute;top:118px;font-size:36px;font-weight:800;white-space:nowrap}
.tl .m{position:absolute;top:-6px;font-size:30px;font-weight:700;opacity:.85;white-space:nowrap}
.emoji{font-size:150px;margin-bottom:10px}
.bars{display:flex;flex-direction:column;gap:54px;margin:64px 0 50px}
.bar .bl{font-size:44px;font-weight:800;margin-bottom:16px;display:flex;justify-content:space-between;align-items:baseline}
.bar .bl b{font-size:52px}
.bar .track{height:96px;border-radius:24px;position:relative}
.dark .bar .track{background:rgba(255,255,255,.08)}
.light .bar .track{background:rgba(22,33,62,.07)}
.bar .fill{height:100%;border-radius:24px;display:flex;align-items:center;justify-content:flex-end;padding-right:28px;font-size:46px;font-weight:900;color:#fff;min-width:210px}
.coral{background:#ff5e5b}.green{background:#22b573}.gold{background:#ffb703}
.why{font-size:46px;font-weight:700;line-height:1.45;padding:30px 38px;border-radius:26px;word-break:keep-all}
.dark .why{background:rgba(255,201,60,.14)}
.light .why{background:rgba(255,94,91,.10)}
.checks{display:flex;flex-direction:column;gap:30px;margin-top:56px}
.check{font-size:62px;font-weight:900;padding:34px 40px;border-radius:30px;display:flex;gap:24px;align-items:center;word-break:keep-all}
.light .check{background:#fff;box-shadow:0 10px 0 rgba(22,33,62,.08)}
.dark .check{background:rgba(255,255,255,.08)}
.stairs{display:flex;align-items:flex-end;gap:16px;height:500px;margin:46px 0 40px}
.stp{display:flex;flex-direction:column;min-width:0}
.stp-top{font-size:38px;font-weight:900;text-align:center;margin-bottom:12px;white-space:nowrap}
.stp-box{flex:1;border-radius:26px 26px 10px 10px;display:flex;flex-direction:column;align-items:center;padding-top:26px;color:#fff}
.stp-box b{font-size:66px;font-weight:900;letter-spacing:-2px;line-height:1.1}
.stp-box span{font-size:48px;font-weight:700;opacity:.9;margin-top:6px;white-space:nowrap}
.blog{margin-top:auto;font-size:38px;font-weight:800;opacity:.85}
.grow{flex:1}
"""

GROW = "<div class='grow'></div>"

FIT_JS = """
document.querySelectorAll('.fit').forEach(el=>{
  let s=parseFloat(getComputedStyle(el).fontSize);
  const max=el.parentElement.clientWidth - (el.dataset.pad?parseFloat(el.dataset.pad):0);
  while(el.scrollWidth>max && s>40){s-=4;el.style.fontSize=s+'px'}
});
"""


def e(s):  # 강조 태그(<em>, <br>)만 살리고 나머지는 이스케이프
    t = html.escape(str(s or ""))
    for a, b in (("&lt;em&gt;", "<em>"), ("&lt;/em&gt;", "</em>"), ("&lt;br&gt;", "<br>")):
        t = t.replace(a, b)
    return t


def top(n, total):
    return f"<div class='top'><span class='brand'>🌱 주식농사</span><span class='pg'>{n} / {total}</span></div>"


def slide(s, n, total):
    k = s["type"]
    hint = "" if n == total else f"<div class='hint'>{e(s.get('hint') or '넘기기 👉')}</div>"
    if k == "hook":
        q = ""
        if s.get("quokka") and (QA / f"{s['quokka']}.webp").exists():
            q = f"<img class='q' src='{(QA / (s['quokka'] + '.webp')).as_uri()}'>"
        body = (f"<div class='grow' style='flex:.22'></div><div class='kicker'>{e(s.get('kicker'))}</div>"
                f"<div class='title'>{e(s.get('title'))}</div>"
                f"<div class='big' style='margin-top:44px;font-size:112px;white-space:normal;word-break:keep-all;max-width:640px;line-height:1.15'>{e(s.get('big'))}</div>{q}")
    elif k == "guess":
        d = s.get("drop")
        drop = ""
        if d:
            drop = (f"<div class='pct'>{e(d.get('pct'))}</div><div class='drop'><span class='chip a'>{e(d['from'])}</span>"
                    f"<span class='arrow'>→</span><span class='chip b'>{e(d['to'])}</span></div>")
        opts = "".join(f"<div class='opt'>{e(o)}</div>" for o in s.get("options", []))
        body = (f"<div class='grow' style='flex:.12'></div><div class='kicker'>{e(s.get('kicker'))}</div>{drop}"
                f"<div class='question'>{e(s.get('question'))}</div><div class='opts'>{opts}</div>{GROW}")
    elif k == "answer":
        tl = ""
        t = s.get("timeline")
        if t:
            mids = t.get("mid", [])
            marks = "".join(
                f"<div class='dot' style='left:calc({float(m.get('pos', (i + 1) / (len(mids) + 1))) * 100}% - 23px)'></div>"
                f"<div class='m' style='left:max(0px,calc({float(m.get('pos', (i + 1) / (len(mids) + 1))) * 100}% - 110px))'>{e(m.get('label'))}</div>"
                for i, m in enumerate(mids))
            tl = (f"<div class='tl'><div class='line'></div><div class='dot' style='left:-8px'></div>"
                  f"<div class='dot' style='right:-8px'></div>{marks}"
                  f"<div class='t' style='left:0'>{e(t.get('from'))}</div>"
                  f"<div class='t' style='right:0'>{e(t.get('to'))}</div></div><div class='grow'></div>")
        body = (f"<div class='grow' style='flex:.55'></div><span class='label'>{e(s.get('label'))}</span>"
                f"<div class='big fit'>{e(s.get('big'))}</div>"
                f"<div class='sub' style='margin-top:30px'>{e(s.get('sub'))}</div>{tl or GROW}")
    elif k == "twist":
        body = (f"<div class='grow' style='flex:.5'></div><div class='kicker'>{e(s.get('kicker'))}</div>"
                f"<div class='title'>{e(s.get('title'))}</div>"
                f"<div class='emoji' style='margin-top:60px'>{e(s.get('emoji'))}</div>"
                f"<div class='big fit'><em>{e(s.get('big'))}</em></div>"
                f"<div class='sub' style='margin-top:26px'>{e(s.get('sub'))}</div><div class='grow'></div>")
    elif k == "compare":
        bars = "".join(
            f"<div class='bar'><div class='bl'><span>{e(b.get('label'))}</span><b>{e(b.get('value'))}</b></div>"
            f"<div class='track'><div class='fill {b.get('color', 'green')}' style='width:{max(0.2, min(1, float(b.get('ratio', 1)))) * 100}%'>"
            f"{e(b.get('tag'))}</div></div></div>" for b in s.get("bars", []))
        body = (f"<div class='title' style='font-size:68px'>{e(s.get('title'))}</div>"
                f"<div class='sub' style='margin-top:18px'>{e(s.get('sub'))}</div>"
                f"<div class='bars'>{bars}</div><div class='why'>{e(s.get('why'))}</div>")
    elif k == "steps":
        st = s.get("steps", [])
        n_st = max(1, len(st))
        tot = sum(float(x.get("w", 1)) for x in st) or 1
        lo = float(s.get("base", 0.5))
        cols = "".join(
            f"<div class='stp' style='flex:{float(x.get('w', 1)) / tot:.4f} 1 0;"
            f"height:{(lo + (1 - lo) * (i / (n_st - 1) if n_st > 1 else 1)) * 100:.1f}%'>"
            f"<div class='stp-top'>{e(x.get('top'))}</div>"
            f"<div class='stp-box {x.get('color', 'green')}'><b>{e(x.get('label'))}</b>"
            f"<span>{e(x.get('note'))}</span></div></div>"
            for i, x in enumerate(st))
        body = (f"<div class='grow' style='flex:.2'></div><div class='kicker'>{e(s.get('kicker'))}</div>"
                f"<div class='title' style='font-size:{int(s.get('title_size', 72))}px'>{e(s.get('title'))}</div>"
                f"<div class='stairs'>{cols}</div><div class='why'>{e(s.get('why'))}</div>{GROW}")
    elif k == "takeaway":
        checks = "".join(f"<div class='check'>✅ <span>{e(c)}</span></div>" for c in s.get("checks", []))
        blog = f"<div class='blog'>📎 {e(s['blog'])}</div>" if s.get("blog") else ""
        if s.get("quokka") and (QA / f"{s['quokka']}.webp").exists():
            blog += f"<img class='qs' src='{(QA / (s['quokka'] + '.webp')).as_uri()}'>"
        body = (f"<div class='grow' style='flex:.15'></div><div class='title'>{e(s.get('title'))}</div>"
                f"<div class='checks'>{checks}</div>{blog}"
                f"<div class='foot'>{e(s.get('foot'))}</div>")
    else:
        raise ValueError(f"모르는 슬라이드 종류: {k}")
    return f"<section class='s {s['theme']}'>{top(n, total)}{body}{hint}</section>"


def render(spec, out_dir: Path):
    sl = spec["slides"]
    for i, s in enumerate(sl):
        s.setdefault("theme", "dark" if i % 2 == 0 else "light")
    doc = ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><style>" + CSS + "</style></head><body>"
           + "".join(slide(s, i + 1, len(sl)) for i, s in enumerate(sl)) + "</body></html>")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "carousel.html").write_text(doc, encoding="utf-8")
    paths = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto((out_dir / "carousel.html").resolve().as_uri())
        pg.wait_for_timeout(900)
        pg.evaluate(FIT_JS)
        for i, el in enumerate(pg.locator("section.s").all(), 1):
            path = out_dir / f"{i:02d}.png"
            el.screenshot(path=str(path))
            paths.append(path)
        b.close()
    return paths


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for p in render(spec, Path(sys.argv[2])):
        print(p)
