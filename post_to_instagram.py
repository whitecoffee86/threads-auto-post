"""티스토리 글 → 인스타그램 캐러셀(6장) 자동 발행.

흐름: RSS에서 아직 안 올린 글 고르기 → 본문 읽기 → Claude가 슬라이드 구성(JSON) 작성
     → HTML 템플릿으로 1080×1350 JPEG 렌더링 → 리포에 커밋/푸시(공개 URL 확보)
     → Instagram API로 캐러셀 업로드 → 발행 기록 저장.

토큰: 60일짜리 장기 토큰을 7일마다 자동 갱신. 갱신된 토큰은 ig_token.enc에 암호화 저장
     (키 = 처음 등록한 IG_ACCESS_TOKEN 시크릿에서 파생). 시크릿을 새 토큰으로 바꾸면
     복호화가 실패하므로 자동으로 새 시크릿 토큰을 쓰게 됨.

환경변수
  ANTHROPIC_API_KEY, IG_USER_ID, IG_ACCESS_TOKEN  (필수)
  IG_DRY_RUN=1   이미지까지만 만들고 업로드는 안 함 (out_dir에 저장)
  IG_POST_URL    특정 글 URL을 강제로 지정
"""
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import sync_playwright

# ─── 설정 ──────────────────────────────────────────
TISTORY_RSS = "https://ideas07576.tistory.com/rss"
SKIP_CATEGORIES = {"단기 투자"}           # 매매일지는 캐러셀에 안 맞아서 제외
HISTORY_FILE = Path("instagram_history.json")
TOKEN_FILE = Path("ig_token.enc")
IMAGES_ROOT = Path("ig_images")
REPO = "whitecoffee86/threads-auto-post"
BRANCH = "main"
GRAPH = "https://graph.instagram.com"
MODEL = "claude-opus-4-5"
REFRESH_EVERY_DAYS = 7
KST = timezone(timedelta(hours=9))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
IG_USER_ID = os.environ["IG_USER_ID"]
IG_SECRET_TOKEN = os.environ["IG_ACCESS_TOKEN"]
DRY_RUN = os.environ.get("IG_DRY_RUN") == "1"
FORCE_URL = os.environ.get("IG_POST_URL", "").strip()


# ─── 기록 ──────────────────────────────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"posted": [], "token_refreshed_at": None, "log": []}


def save_history(h: dict):
    h["log"] = h.get("log", [])[-60:]
    HISTORY_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── 토큰 (암호화 저장 + 자동 갱신) ─────────────────
def _key() -> bytes:
    return hashlib.sha256(("wc-ig|" + IG_SECRET_TOKEN).encode()).digest()


def load_token() -> str:
    if TOKEN_FILE.exists():
        try:
            raw = base64.b64decode(TOKEN_FILE.read_text().strip())
            return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode()
        except Exception:
            print("저장된 토큰 복호화 실패 → 시크릿 토큰 사용 (시크릿이 새 토큰으로 바뀐 경우 정상)")
    return IG_SECRET_TOKEN


def save_token(token: str):
    nonce = os.urandom(12)
    TOKEN_FILE.write_text(base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, token.encode(), None)).decode())


def maybe_refresh(token: str, h: dict) -> str:
    last = h.get("token_refreshed_at")
    if last and datetime.fromisoformat(last) > datetime.now(KST) - timedelta(days=REFRESH_EVERY_DAYS):
        return token
    r = requests.get(f"{GRAPH}/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30)
    if r.ok and r.json().get("access_token"):
        new = r.json()["access_token"]
        save_token(new)
        h["token_refreshed_at"] = datetime.now(KST).isoformat()
        print(f"토큰 갱신 완료 (유효 {r.json().get('expires_in', 0) // 86400}일)")
        return new
    print(f"토큰 갱신 실패(기존 토큰 계속 사용): {r.text[:200]}")
    return token


# ─── 글 고르기 / 본문 읽기 ─────────────────────────
def pick_post(h: dict) -> dict | None:
    feed = feedparser.parse(TISTORY_RSS)
    posts = []
    for e in feed.entries:
        tags = [t.term for t in getattr(e, "tags", [])]
        posts.append({"title": html.unescape(e.title), "link": e.link,
                      "category": tags[0] if tags else ""})
    if FORCE_URL:
        for p in posts:
            if p["link"].rstrip("/") == FORCE_URL.rstrip("/"):
                return p
        return {"title": "", "link": FORCE_URL, "category": ""}
    done = set(h.get("posted", []))
    for p in posts:  # RSS는 최신순 → 아직 안 올린 가장 최근 글
        if p["category"] not in SKIP_CATEGORIES and p["link"] not in done:
            return p
    return None


def fetch_article_text(url: str) -> tuple[str, str]:
    page = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    title = html.unescape(title.group(1)).strip() if title else ""
    m = re.search(r'<div[^>]+class="[^"]*(tt_article_useless_p_margin|entry-content|contents_style)[^"]*"', page)
    body = page[m.start():] if m else page
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)</(td|th)>", " | ", body)
    body = re.sub(r"(?i)</(tr|p|div|li|h[1-6])>|<br\s*/?>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    cut = text.find("함께 보면 좋은 글")
    if cut > 0:
        text = text[:cut]
    return title, text[:14000]


# ─── Claude: 슬라이드 구성 ─────────────────────────
PROMPT = """너는 'WhiteCoffee 의 주식농사' 인스타그램 캐러셀 에디터다. 아래 블로그 글을 1080×1350 캐러셀 6장으로 재구성한다.

[절대 규칙]
- 숫자·사실은 반드시 본문에 있는 것만 쓴다. 새 숫자를 지어내거나 계산하지 않는다.
- 특정 상품 매수 권유처럼 보이는 문장 금지. 기록·정리 톤.
- 짧게. 모바일에서 1초에 읽혀야 한다. 강조하고 싶은 핵심 단어/숫자는 <g>...</g>로 감싼다(금색). 줄바꿈은 <br>.
- 이모지를 적당히 쓴다(배지, 카드 아이콘).
- 가운데 4장은 내용에 맞는 형태를 골라 서로 다르게 구성한다(같은 type 최대 2번).

[출력: JSON만, 코드블록 없이]
{
 "cover": {"badge": "카테고리명 + 이모지 1개", "title": "질문형 훅 2줄(<br>), 최대 26자", "big": "가장 강력한 숫자/결론 최대 8자", "sub": "부연 2줄, 최대 50자, 마지막에 반전/궁금증"},
 "slides": [  // 정확히 4개
   {"type": "table", "badge": "...", "title": "...", "headers": ["", "", ""], "rows": [["", "", ""]], "highlight": 행번호(0부터, 없으면 -1), "note": "※ 계산 근거 한 줄"},
   {"type": "bars", "badge": "...", "title": "...", "sub": "...", "items": [{"label": "", "value": 숫자(음수 가능), "display": "표시 문자열"}], "takeaway": "👉 한 줄"},
   {"type": "cards", "badge": "...", "title": "...", "items": [{"icon": "이모지", "title": "", "desc": ""}]},   // 2~4개
   {"type": "compare", "badge": "...", "title": "...", "left": {"label": "", "value": ""}, "right": {"label": "", "value": ""}, "body": "1~2줄"},
   {"type": "bignum", "badge": "...", "title": "...", "value": "", "label": "", "body": "1~2줄"},
   {"type": "steps", "badge": "...", "title": "...", "items": [{"title": "", "desc": ""}]}  // 3~4개
 ],
 "closing": {"badge": "🌿 WhiteCoffee의 관점", "title": "2줄 이내", "body": "1~2줄(본문의 필자 관점/경험이 있으면 그것)", "question": "💬 댓글을 부르는 질문 1개"},
 "caption": "인스타 본문. 1줄 훅 + 핵심 2~3줄 + 빈 줄 + '📌 저장해두고 ~ 꺼내 보세요' + '💬 질문' (해시태그 제외, 링크 금지, 400자 이내)",
 "hashtags": ["#태그", "..."]   // 5~8개, 검색량 있는 한국어/종목 태그
}
표(table) rows는 최대 5행 4열, bars items는 3~6개. 각 title은 최대 2줄 22자 내외.

[블로그 글]
제목: {title}
카테고리: {category}
본문:
{body}
"""


def build_spec(title: str, category: str, body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = PROMPT.replace("{title}", title).replace("{category}", category or "직장인 투자").replace("{body}", body)
    last_err = None
    for _ in range(2):
        msg = client.messages.create(model=MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw).strip()
        try:
            spec = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            assert spec["cover"] and len(spec["slides"]) >= 3 and spec["closing"] and spec["caption"]
            spec["slides"] = spec["slides"][:4]
            return spec
        except Exception as e:
            last_err = e
            print(f"슬라이드 JSON 파싱 실패, 재시도: {e}")
    raise RuntimeError(f"슬라이드 구성 실패: {last_err}")


# ─── 렌더링 ────────────────────────────────────────
def t(s) -> str:
    """사용자 텍스트 이스케이프 후 <g>, <br>만 허용."""
    s = html.escape(str(s or ""))
    s = s.replace("&lt;g&gt;", '<span class="g">').replace("&lt;/g&gt;", "</span>")
    return re.sub(r"&lt;br\s*/?&gt;", "<br>", s)


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR',sans-serif;color:#fff}
.s{width:1080px;height:1350px;background:#1e2d4f;position:relative;overflow:hidden;padding:90px 80px;display:flex;flex-direction:column}
.s::before{content:"";position:absolute;width:700px;height:700px;border-radius:50%;border:2px solid rgba(201,168,75,.18);right:-260px;top:-260px}
.s::after{content:"";position:absolute;width:420px;height:420px;border-radius:50%;background:rgba(201,168,75,.06);left:-160px;bottom:-160px}
.badge{display:inline-block;align-self:flex-start;background:#c9a84b;color:#1e2d4f;font-weight:900;font-size:30px;padding:10px 26px;border-radius:40px}
.pg{position:absolute;right:80px;top:96px;font-size:28px;color:rgba(255,255,255,.5);font-weight:700}
.foot{position:absolute;left:80px;right:80px;bottom:64px;display:flex;align-items:center;gap:16px;font-size:26px;color:rgba(255,255,255,.7);font-weight:700}
.logo{width:52px;height:52px;border-radius:50%;border:3px solid #c9a84b;color:#c9a84b;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:17px;line-height:1;text-align:center}
.swipe{margin-left:auto;color:#c9a84b}
h1{font-size:84px;line-height:1.22;font-weight:900;margin-top:60px;letter-spacing:-2px}
h2{font-size:64px;line-height:1.25;font-weight:900;margin-top:50px;letter-spacing:-1.5px}
.g{color:#c9a84b}
.sub{font-size:36px;color:rgba(255,255,255,.75);margin-top:28px;line-height:1.5;font-weight:500}
.big{font-size:180px;font-weight:900;color:#c9a84b;letter-spacing:-6px;line-height:1.05;margin-top:50px}
.card{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);border-radius:28px;padding:32px 40px;margin-top:22px}
table{width:100%;border-collapse:collapse;margin-top:50px;font-size:36px}
th{font-size:28px;color:#c9a84b;padding:18px 10px;text-align:right;font-weight:700}
th:first-child,td:first-child{text-align:left}
td{padding:28px 10px;border-top:1px solid rgba(255,255,255,.14);text-align:right;font-weight:700}
tr.hl td{color:#1e2d4f;background:#c9a84b}
tr.hl td:first-child{border-radius:16px 0 0 16px} tr.hl td:last-child{border-radius:0 16px 16px 0}
.note{font-size:24px;color:rgba(255,255,255,.5);margin-top:26px;line-height:1.5}
.bar{display:flex;align-items:center;gap:20px;margin-top:30px;font-size:32px;font-weight:700}
.bar .l{width:230px}
.bar .tr{flex:1;height:60px;position:relative}
.bar .c{position:absolute;left:var(--z);top:-8px;bottom:-8px;width:2px;background:rgba(255,255,255,.3)}
.bar .f{position:absolute;top:0;height:60px;border-radius:12px}
.bar .v{width:210px;text-align:right}
.neg{background:#e0675a}.pos{background:#c9a84b}
.row{display:flex;gap:24px;align-items:flex-start;font-size:38px;line-height:1.45;font-weight:700}
.row .ic{font-size:46px}
.row small{display:block;font-size:28px;font-weight:500;color:rgba(255,255,255,.65)}
.cmp{display:flex;gap:26px;margin-top:50px}
.cmp .card{flex:1;text-align:center;margin-top:0}
.cmp .n{font-size:96px;font-weight:900;letter-spacing:-3px;line-height:1.2}
.num{width:64px;height:64px;border-radius:50%;background:#c9a84b;color:#1e2d4f;font-weight:900;font-size:34px;display:flex;align-items:center;justify-content:center;flex:none}
.bn{font-size:200px;font-weight:900;color:#c9a84b;letter-spacing:-6px;line-height:1.05;margin-top:60px}
"""


def foot(last=False) -> str:
    sw = "" if last else '<span class="swipe">→</span>'
    return f'<div class="foot"><span class="logo">주식<br>농사</span>WhiteCoffee 의 주식농사{sw}</div>'


def _fit(text, base: int, max_chars: int) -> str:
    n = len(re.sub(r"<[^>]+>", "", str(text or "")))
    return f"font-size:{base if n <= max_chars else int(base * max_chars / n)}px"


def slide_cover(c, n, total) -> str:
    return (f'<section class="s"><span class="badge">{t(c.get("badge"))}</span><span class="pg">{n}/{total}</span>'
            f'<h1>{t(c.get("title"))}</h1><div class="big" style="{_fit(c.get('big'), 180, 6)}">{t(c.get("big"))}</div>'
            f'<p class="sub">{t(c.get("sub"))}</p>'
            f'<div class="foot"><span class="logo">주식<br>농사</span>WhiteCoffee 의 주식농사<span class="swipe">넘겨보세요 →</span></div></section>')


def slide_body(s) -> str:
    ty = s.get("type")
    if ty == "table":
        hl = s.get("highlight", -1)
        head = "".join(f"<th>{t(x)}</th>" for x in s.get("headers", []))
        rows = "".join(
            f'<tr class="{"hl" if i == hl else ""}">' + "".join(f"<td>{t(x)}</td>" for x in r) + "</tr>"
            for i, r in enumerate(s.get("rows", [])[:5]))
        return f'<table><tr>{head}</tr>{rows}</table><p class="note">{t(s.get("note"))}</p>'
    if ty == "bars":
        items = s.get("items", [])[:6]
        vals = [float(i.get("value") or 0) for i in items] or [0]
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        span = (hi - lo) or 1
        z = (0 - lo) / span * 100
        out = f'<p class="sub">{t(s.get("sub"))}</p><div style="margin-top:30px">'
        for i, v in zip(items, vals):
            w = abs(v) / span * 100
            pos = f"left:{z}%" if v >= 0 else f"right:{100 - z}%"
            cls = "pos" if v >= 0 else "neg"
            out += (f'<div class="bar"><span class="l">{t(i.get("label"))}</span>'
                    f'<span class="tr" style="--z:{z}%"><i class="c"></i><i class="f {cls}" style="{pos};width:{w}%"></i></span>'
                    f'<span class="v">{t(i.get("display"))}</span></div>')
        return out + f'</div><p class="sub" style="font-size:32px;margin-top:44px">{t(s.get("takeaway"))}</p>'
    if ty == "cards":
        return '<div style="margin-top:36px">' + "".join(
            f'<div class="card"><div class="row"><span class="ic">{t(i.get("icon"))}</span><div>{t(i.get("title"))}'
            f'<small>{t(i.get("desc"))}</small></div></div></div>' for i in s.get("items", [])[:4]) + "</div>"
    if ty == "compare":
        L, R = s.get("left", {}), s.get("right", {})
        return (f'<div class="cmp"><div class="card"><div class="sub" style="margin:0">{t(L.get("label"))}</div><div class="n">{t(L.get("value"))}</div></div>'
                f'<div class="card" style="border-color:#c9a84b"><div class="sub" style="margin:0">{t(R.get("label"))}</div><div class="n g">{t(R.get("value"))}</div></div></div>'
                f'<p class="sub" style="margin-top:44px">{t(s.get("body"))}</p>')
    if ty == "bignum":
        return (f'<div class="bn" style="{_fit(s.get('value'), 200, 6)}">{t(s.get("value"))}</div><p class="sub" style="font-size:40px;font-weight:700;color:#fff">{t(s.get("label"))}</p>'
                f'<p class="sub">{t(s.get("body"))}</p>')
    if ty == "steps":
        return '<div style="margin-top:36px">' + "".join(
            f'<div class="card"><div class="row"><span class="num">{k + 1}</span><div>{t(i.get("title"))}'
            f'<small>{t(i.get("desc"))}</small></div></div></div>' for k, i in enumerate(s.get("items", [])[:4])) + "</div>"
    return f'<p class="sub">{t(s.get("body"))}</p>'


def slide_mid(s, n, total) -> str:
    return (f'<section class="s"><span class="badge">{t(s.get("badge"))}</span><span class="pg">{n}/{total}</span>'
            f'<h2>{t(s.get("title"))}</h2>{slide_body(s)}{foot()}</section>')


def slide_close(c, n, total) -> str:
    return (f'<section class="s"><span class="badge">{t(c.get("badge"))}</span><span class="pg">{n}/{total}</span>'
            f'<h2>{t(c.get("title"))}</h2><p class="sub" style="margin-top:44px">{t(c.get("body"))}</p>'
            f'<h2 style="font-size:54px;margin-top:40px">{t(c.get("question"))}</h2>'
            f'<p class="note" style="margin-top:40px">📌 저장해두고 필요할 때 꺼내 보세요 · 전체 계산은 프로필 링크<br>'
            f'개인 기록이며 특정 상품의 매수를 권하지 않습니다</p>{foot(True)}</section>')


def render(spec: dict, out_dir: Path) -> list[Path]:
    total = len(spec["slides"]) + 2
    parts = [slide_cover(spec["cover"], 1, total)]
    parts += [slide_mid(s, i + 2, total) for i, s in enumerate(spec["slides"])]
    parts.append(slide_close(spec["closing"], total, total))
    doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "carousel.html").write_text(doc, encoding="utf-8")
    paths = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.set_content(doc)
        pg.wait_for_timeout(600)
        for i, el in enumerate(pg.locator("section.s").all(), 1):
            path = out_dir / f"{i}.jpg"
            el.screenshot(path=str(path), type="jpeg", quality=92)
            paths.append(path)
        b.close()
    return paths


# ─── 업로드 ────────────────────────────────────────
def push_images(paths: list[Path]) -> list[str]:
    subprocess.run(["git", "add", *map(str, paths)], check=True)
    subprocess.run(["git", "commit", "-m", f"인스타 캐러셀 이미지: {paths[0].parent.name}"], check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], check=True)
    subprocess.run(["git", "push"], check=True)
    urls = [f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{p.as_posix()}" for p in paths]
    for u in urls:  # raw 서버에 반영될 때까지 대기
        for _ in range(12):
            if requests.head(u, timeout=15).status_code == 200:
                break
            time.sleep(5)
    return urls


def ig(method: str, path: str, token: str, **data) -> dict:
    fn = requests.post if method == "POST" else requests.get
    kw = {"data" if method == "POST" else "params": {**data, "access_token": token}}
    r = fn(f"{GRAPH}/{path}", timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"Instagram API 오류 {path}: {r.text[:400]}")
    return r.json()


def wait_finished(cid: str, token: str):
    for _ in range(30):
        st = ig("GET", cid, token, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 처리 실패: {cid} {st}")
        time.sleep(4)
    raise RuntimeError(f"컨테이너 처리 시간 초과: {cid}")


def publish_carousel(urls: list[str], caption: str, token: str) -> str:
    children = []
    for u in urls:
        cid = ig("POST", f"{IG_USER_ID}/media", token, image_url=u, is_carousel_item="true")["id"]
        children.append(cid)
    for cid in children:
        wait_finished(cid, token)
    parent = ig("POST", f"{IG_USER_ID}/media", token, media_type="CAROUSEL",
                children=",".join(children), caption=caption)["id"]
    wait_finished(parent, token)
    return ig("POST", f"{IG_USER_ID}/media_publish", token, creation_id=parent)["id"]


# ─── 메인 ──────────────────────────────────────────
def main():
    h = load_history()
    post = pick_post(h)
    if not post:
        print("올릴 새 글이 없습니다.")
        return
    title, body = fetch_article_text(post["link"])
    title = post["title"] or title
    print(f"선택된 글: {title}\n{post['link']}")

    spec = build_spec(title, post["category"], body)
    caption = spec["caption"].strip() + "\n\n" + " ".join(spec.get("hashtags", [])[:8])
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    out_dir = IMAGES_ROOT / stamp
    paths = render(spec, out_dir)
    (out_dir / "caption.txt").write_text(caption, encoding="utf-8")
    (out_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"렌더링 완료: {len(paths)}장 → {out_dir}\n\n[캡션]\n{caption}\n")

    if DRY_RUN:
        print("DRY RUN — 업로드하지 않고 종료합니다. (Actions 아티팩트에서 이미지 확인)")
        return

    token = maybe_refresh(load_token(), h)
    save_history(h)  # 갱신 시각은 발행 실패와 무관하게 남김
    urls = push_images(paths)
    media_id = publish_carousel(urls, caption, token)
    print(f"발행 완료! media_id={media_id}")
    h.setdefault("posted", []).append(post["link"])
    h.setdefault("log", []).append({"at": datetime.now(KST).isoformat(), "link": post["link"],
                                    "title": title, "media_id": media_id, "images": stamp})
    save_history(h)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"실패: {e}")
        sys.exit(1)
