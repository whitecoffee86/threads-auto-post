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
IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_SECRET_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
DRY_RUN = os.environ.get("IG_DRY_RUN") == "1"
FORCE_URL = os.environ.get("IG_POST_URL", "").strip()


QUOKKA_DIR = Path("assets/quokka")
QUOKKA = {
 "balance_scale": "저울(비교·균형)", "calculator_happy": "계산기(계산 결과)", "clipboard_check": "체크리스트",
 "coins_jump_joy": "동전 들고 기쁨", "confident_thumbs": "엄지척(확신)", "currency_exchange": "달러·동전(환전·환율)",
 "dividend_mailbox": "우편함에 배당", "drought_sad_phone": "가뭄 속 슬픔(하락장)", "eight_pots": "화분 8개(분할매수)",
 "fence_calm": "평온", "harvest_gold_fruit": "황금 수확(수익 실현)", "jumping_joy_phone": "점프 환호(급등)",
 "leaking_sack": "새는 돈자루(새는 비용·세금)", "magnifier_phone": "돋보기(분석·확인)", "paperwork_pile": "서류 더미(신고·복잡함)",
 "phone_up_smile": "폰 들고 미소", "pointer_explain": "지시봉 설명", "pose_basket_cheer": "바구니 환호",
 "pose_chest_cheer": "보물상자(큰 수익)", "pose_endure_eyes_closed": "눈 감고 버팀(존버)", "pose_falling_panic": "추락 패닉(폭락)",
 "pose_fence_phone": "폰 확인", "pose_napping": "낮잠(장기 보유)", "pose_planting_coin": "동전 심기(투자 시작)",
 "pose_rocket_ride": "로켓(급등·레버리지)", "pose_sad_sitting_phone": "앉아서 슬픔(손실)", "pose_standing_blush": "수줍음",
 "pose_trophy_cheer": "트로피(성공)", "rate_stairs": "금리 계단 오름", "sad_red_chart": "빨간 차트(손실)",
 "sad_walk_phone": "실망", "scissors_cut": "가위(손절·비용 절감)", "seed_qqq": "QQQ 씨앗", "seed_schd": "SCHD 씨앗",
 "shock_closeup": "깜짝 놀람", "sleeping_dream": "꿈", "sowing_dark_sky": "어둠 속 씨뿌리기(하락장 매수)",
 "spring_bounce": "스프링 반등", "stairs_down": "계단 내려감(하락·금리 인하)", "stopwatch_ready": "스톱워치(타이밍)",
 "surprised_closed_weekend": "휴장 놀람", "tax_shock": "세금 고지서 충격", "thinking_question": "물음표 고민",
 "three_piggy": "돼지저금통 3개(분산·저축)", "two_paths": "갈림길(선택)", "umbrella_shield": "우산(방어·안전자산)",
 "vault_safe": "금고(안전·보관)", "waiting_moneybag": "돈자루 기다림(이자·배당)", "walking_sunset_back": "노을 걷기(마무리)",
 "warning_sign": "경고", "watering_dca": "물주기(적립식)",
}


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
 "cover": {"badge": "카테고리명 + 이모지 1개", "title": "질문형 훅 2줄(<br>), 최대 22자", "big": "가장 강력한 숫자/결론 최대 7자", "sub": "부연 2줄, 최대 40자, 마지막에 반전/궁금증", "quokka": "쿼카 키", "bubble": "쿼카 말풍선 감탄 최대 9자 (예: 세금이 이만큼?!)"},
 "slides": [  // 정확히 4개
   {"type": "table", "badge": "...", "title": "...", "headers": ["", "", ""], "rows": [["", "", ""]], "highlight": 행번호(0부터, 없으면 -1), "note": "※ 계산 근거 한 줄"},
   {"type": "bars", "badge": "...", "title": "...", "sub": "...", "items": [{"label": "", "value": 숫자(음수 가능), "display": "표시 문자열"}], "takeaway": "👉 한 줄"},
   {"type": "cards", "badge": "...", "title": "...", "items": [{"icon": "이모지", "title": "", "desc": ""}]},   // 2~4개
   {"type": "compare", "badge": "...", "title": "...", "left": {"label": "", "value": ""}, "right": {"label": "", "value": ""}, "body": "1~2줄"},
   {"type": "bignum", "badge": "...", "title": "...", "value": "", "label": "", "body": "1~2줄"},
   {"type": "steps", "badge": "...", "title": "...", "items": [{"title": "", "desc": ""}]}  // 3~4개
 ],
 "closing": {"badge": "🌿 WhiteCoffee의 관점", "title": "2줄 이내", "body": "1~2줄(본문의 필자 관점/경험이 있으면 그것)", "question": "댓글을 부르는 질문 1개(이모지 없이, 최대 30자)", "quokka": "쿼카 키(표지와 다른 것)"},
 "caption": "인스타 본문(태그 없이 일반 줄바꿈 \\n 사용). 1줄 훅 + 핵심 2~3줄 + 빈 줄 + '📌 저장해두고 ~ 꺼내 보세요' + '💬 질문' (해시태그 제외, 링크 금지, 400자 이내)",
 "hashtags": ["#태그", "..."]   // 5~8개, 검색량 있는 한국어/종목 태그
}
compare·bignum 슬라이드에는 "quokka": "쿼카 키"를 넣을 수 있다(선택, 내용 분위기와 맞을 때만).
쿼카 키 목록(내용 분위기에 맞게 고른다): {quokkas}
표(table) rows는 최대 4행 4열, 각 칸은 8자 이내(긴 설명은 cards로), bars items는 3~6개. 각 title은 최대 2줄 22자 내외.

[블로그 글]
제목: {title}
카테고리: {category}
본문:
{body}
"""


def build_spec(title: str, category: str, body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = PROMPT.replace("{title}", title).replace("{category}", category or "직장인 투자").replace("{body}", body).replace("{quokkas}", ", ".join(f"{k}={v}" for k, v in QUOKKA.items()))
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


def plain(s) -> str:
    return re.sub(r"<[^>]+>", "", str(s or ""))


def quokka_img(key, cls: str) -> str:
    path = QUOKKA_DIR / f"{key}.webp"
    if not key or not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'<img class="{cls}" src="data:image/webp;base64,{b64}">'


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR',sans-serif;color:#fff;word-break:keep-all}
.s{width:1080px;height:1350px;position:relative;overflow:hidden;padding:90px 80px;display:flex;flex-direction:column;
  background:radial-gradient(900px 700px at 0% 0%,rgba(201,168,75,.20),transparent 60%),
             radial-gradient(800px 800px at 110% 105%,rgba(91,140,255,.16),transparent 60%),#1e2d4f}
.s::before{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.06) 2px,transparent 2px);background-size:36px 36px;pointer-events:none}
.s>*{position:relative}
.top{display:flex;align-items:center;gap:16px}
.badge{display:inline-block;background:#c9a84b;color:#1e2d4f;font-weight:900;font-size:30px;padding:10px 26px;border-radius:40px;box-shadow:0 6px 0 #8a7238}
.pg{margin-left:auto;font-size:26px;color:rgba(255,255,255,.55);font-weight:800;background:rgba(255,255,255,.08);padding:8px 18px;border-radius:30px}
.foot{position:absolute;left:80px;right:80px;bottom:60px;display:flex;align-items:center;gap:16px;font-size:26px;color:rgba(255,255,255,.75);font-weight:700}
.logo{width:54px;height:54px;border-radius:50%;border:3px solid #c9a84b;color:#c9a84b;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:17px;line-height:1;text-align:center;background:#1e2d4f}
.swipe{margin-left:auto;color:#1e2d4f;background:#c9a84b;padding:10px 22px;border-radius:30px;font-weight:900}
h1{font-size:86px;line-height:1.2;font-weight:900;margin-top:56px;letter-spacing:-2.5px;text-shadow:0 4px 18px rgba(0,0,0,.25)}
h2{font-size:64px;line-height:1.24;font-weight:900;margin-top:46px;letter-spacing:-1.5px}
.g{color:#ffd76a;background:linear-gradient(transparent 62%,rgba(201,168,75,.38) 62%);padding:0 4px;border-radius:4px}
.sub{font-size:35px;color:rgba(255,255,255,.8);margin-top:26px;line-height:1.5;font-weight:500}
.big{display:inline-block;align-self:flex-start;font-size:170px;font-weight:900;color:#1e2d4f;letter-spacing:-6px;line-height:1.08;margin-top:40px;
  background:#ffd76a;padding:4px 30px 12px;border-radius:28px;transform:rotate(-2deg);box-shadow:0 14px 0 #c9a84b,0 24px 40px rgba(0,0,0,.35)}
.sticker{position:absolute;right:70px;top:190px;background:#fff;color:#1e2d4f;font-weight:900;font-size:30px;padding:14px 24px;border-radius:18px;transform:rotate(6deg);box-shadow:0 8px 24px rgba(0,0,0,.3)}
.qc{position:absolute;right:-20px;bottom:100px;height:600px;filter:drop-shadow(0 20px 30px rgba(0,0,0,.45))}
.bubble{position:absolute;right:50px;bottom:690px;background:#fff;color:#1e2d4f;font-weight:900;font-size:38px;padding:20px 30px;border-radius:30px;box-shadow:0 10px 30px rgba(0,0,0,.3);white-space:nowrap}
.bubble::after{content:"";position:absolute;left:50%;bottom:-28px;border:16px solid transparent;border-top-color:#fff}
.card{background:linear-gradient(135deg,rgba(255,255,255,.11),rgba(255,255,255,.04));border:1px solid rgba(255,255,255,.14);border-radius:28px;padding:30px 36px;margin-top:20px;box-shadow:0 10px 30px rgba(0,0,0,.18)}
table{width:100%;border-collapse:separate;border-spacing:0;margin-top:44px;font-size:36px;background:rgba(255,255,255,.05);border-radius:24px;overflow:hidden}
th{font-size:28px;color:#1e2d4f;background:#c9a84b;padding:20px 18px;text-align:right;font-weight:900}
th:first-child,td:first-child{text-align:left}
td{padding:24px 18px;border-top:1px solid rgba(255,255,255,.1);text-align:right;font-weight:700}
td.nw{white-space:nowrap}
tr.hl td{color:#1e2d4f;background:#ffd76a;font-weight:900}
tr.hl .g{color:#1e2d4f;background:rgba(255,255,255,.55)}
.note{font-size:24px;color:rgba(255,255,255,.55);margin-top:24px;line-height:1.5}
.bar{display:flex;align-items:center;gap:20px;margin-top:28px;font-size:32px;font-weight:700}
.bar .l{width:230px}
.bar .tr{flex:1;height:62px;position:relative;background:rgba(255,255,255,.05);border-radius:14px}
.bar .c{position:absolute;left:var(--z);top:-8px;bottom:-8px;width:3px;background:rgba(255,255,255,.35)}
.bar .f{position:absolute;top:0;height:62px;border-radius:14px}
.bar .v{width:210px;text-align:right;font-weight:900}
.neg{background:linear-gradient(90deg,#ff8a7a,#e0675a)}.pos{background:linear-gradient(90deg,#c9a84b,#ffd76a)}
.row{display:flex;gap:26px;align-items:center;font-size:38px;line-height:1.4;font-weight:800}
.ic{width:84px;height:84px;flex:none;border-radius:24px;background:rgba(201,168,75,.18);display:flex;align-items:center;justify-content:center;font-size:48px}
.row small{display:block;font-size:28px;font-weight:500;color:rgba(255,255,255,.7);margin-top:4px}
.cmp{display:flex;gap:26px;margin-top:46px;align-items:stretch;position:relative}
.cmp .card{flex:1;text-align:center;margin-top:0;padding:40px 20px}
.cmp .vs{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:86px;height:86px;border-radius:50%;background:#e0675a;color:#fff;font-weight:900;font-size:34px;display:flex;align-items:center;justify-content:center;box-shadow:0 8px 20px rgba(0,0,0,.35)}
.cmp .n{font-size:92px;font-weight:900;letter-spacing:-3px;line-height:1.2;margin-top:10px}
.num{width:72px;height:72px;border-radius:50%;background:#c9a84b;color:#1e2d4f;font-weight:900;font-size:36px;display:flex;align-items:center;justify-content:center;flex:none;box-shadow:0 5px 0 #8a7238}
.bn{font-size:200px;font-weight:900;color:#ffd76a;letter-spacing:-6px;line-height:1.05;margin-top:56px;text-shadow:0 10px 0 rgba(0,0,0,.2)}
.qm{position:absolute;right:30px;bottom:120px;height:330px;filter:drop-shadow(0 14px 20px rgba(0,0,0,.4))}
.qe{position:absolute;right:-20px;bottom:110px;height:500px;filter:drop-shadow(0 20px 30px rgba(0,0,0,.45))}
.ask{margin-top:40px;width:640px;background:#fff;color:#1e2d4f;border-radius:36px;padding:34px 40px;font-size:44px;font-weight:900;line-height:1.35;position:relative;box-shadow:0 14px 40px rgba(0,0,0,.3)}
.ask::before{content:"💬";position:absolute;left:-18px;top:-30px;font-size:56px}
.ask::after{content:"";position:absolute;right:-22px;top:60px;border:18px solid transparent;border-left-color:#fff}
.cta{display:flex;gap:16px;margin-top:40px}
.cta span{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:40px;padding:14px 24px;font-size:28px;font-weight:800}
"""

FIT_JS = """() => document.querySelectorAll('section.s').forEach(sec => {
  const bd = sec.querySelector('.bd'); if (!bd) return;
  const limit = sec.getBoundingClientRect().top + 1350 - 150;
  for (let z = 1; z > 0.6; z -= 0.04) {
    bd.style.zoom = z;
    if (bd.getBoundingClientRect().bottom <= limit) break;
  }
})"""


def _fit(text, base: int, max_chars: int) -> str:
    n = len(plain(text))
    return f"font-size:{base if n <= max_chars else int(base * max_chars / n)}px"


def top(badge, n, total) -> str:
    return f'<div class="top"><span class="badge">{t(badge)}</span><span class="pg">{n} / {total}</span></div>'


def foot(last=False) -> str:
    sw = "" if last else '<span class="swipe">넘기기 →</span>'
    return f'<div class="foot"><span class="logo">주식<br>농사</span>WhiteCoffee 의 주식농사{sw}</div>'


def slide_cover(c, n, total) -> str:
    q = quokka_img(c.get("quokka") or "pointer_explain", "qc")
    bubble = f'<div class="bubble">{t(plain(c.get("bubble")))}</div>' if q and c.get("bubble") else ""
    return (f'<section class="s">{top(c.get("badge"), n, total)}<span class="sticker">📌 저장 필수</span>'
            f'<h1>{t(c.get("title"))}</h1><div class="big" style="{_fit(c.get("big"), 170, 5)}">{t(plain(c.get("big")))}</div>'
            f'<p class="sub" style="max-width:640px">{t(c.get("sub"))}</p>{q}{bubble}{foot()}</section>')


def slide_body(s) -> str:
    ty = s.get("type")
    if ty == "table":
        hl = s.get("highlight", -1)
        nc = len(s.get("headers", [])) or 3
        s["rows"] = [(list(r[:nc - 1]) + [" ".join(map(str, r[nc - 1:]))]) if len(r) > nc else list(r) + [""] * (nc - len(r))
                     for r in s.get("rows", [])]
        longest = max([len(plain(x)) for r in s.get("rows", []) for x in r] + [0])
        tstyle = ' style="font-size:30px"' if longest > 9 else ""
        head = "".join(f"<th>{t(x)}</th>" for x in s.get("headers", []))
        rows = "".join(
            f'<tr class="{"hl" if i == hl else ""}">' + "".join(f'<td{" class=nw" if len(plain(x)) <= 6 else ""}>{t(x)}</td>' for x in r) + "</tr>"
            for i, r in enumerate(s.get("rows", [])[:5]))
        return f'<table{tstyle}><tr>{head}</tr>{rows}</table><p class="note">{t(s.get("note"))}</p>'
    if ty == "bars":
        items = s.get("items", [])[:6]
        vals = [float(i.get("value") or 0) for i in items] or [0]
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        span = (hi - lo) or 1
        z = (0 - lo) / span * 100
        out = f'<p class="sub">{t(s.get("sub"))}</p><div class="card" style="margin-top:30px;padding:16px 30px 34px">'
        for i, v in zip(items, vals):
            w = abs(v) / span * 100
            pos = f"left:{z}%" if v >= 0 else f"right:{100 - z}%"
            cls = "pos" if v >= 0 else "neg"
            out += (f'<div class="bar"><span class="l">{t(i.get("label"))}</span>'
                    f'<span class="tr" style="--z:{z}%"><i class="c"></i><i class="f {cls}" style="{pos};width:{w}%"></i></span>'
                    f'<span class="v">{t(i.get("display"))}</span></div>')
        return out + f'</div><p class="sub" style="font-size:33px;margin-top:36px;font-weight:700;color:#fff">{t(s.get("takeaway"))}</p>'
    if ty == "cards":
        return '<div style="margin-top:34px">' + "".join(
            f'<div class="card"><div class="row"><span class="ic">{t(i.get("icon"))}</span><div>{t(i.get("title"))}'
            f'<small>{t(i.get("desc"))}</small></div></div></div>' for i in s.get("items", [])[:4]) + "</div>"
    if ty == "compare":
        L, R = s.get("left", {}), s.get("right", {})
        return (f'<div class="cmp"><div class="card"><div class="sub" style="margin:0">{t(L.get("label"))}</div><div class="n">{t(L.get("value"))}</div></div>'
                f'<div class="card" style="border:3px solid #ffd76a"><div class="sub" style="margin:0">{t(R.get("label"))}</div><div class="n" style="color:#ffd76a">{t(R.get("value"))}</div></div>'
                f'<span class="vs">VS</span></div><p class="sub" style="margin-top:40px;max-width:{620 if s.get("quokka") else 920}px">{t(s.get("body"))}</p>')
    if ty == "bignum":
        return (f'<div class="bn" style="{_fit(s.get("value"), 200, 6)}">{t(plain(s.get("value")))}</div>'
                f'<p class="sub" style="font-size:42px;font-weight:800;color:#fff">{t(s.get("label"))}</p>'
                f'<p class="sub" style="max-width:{620 if s.get("quokka") else 920}px">{t(s.get("body"))}</p>')
    if ty == "steps":
        return '<div style="margin-top:34px">' + "".join(
            f'<div class="card"><div class="row"><span class="num">{k + 1}</span><div>{t(i.get("title"))}'
            f'<small>{t(i.get("desc"))}</small></div></div></div>' for k, i in enumerate(s.get("items", [])[:4])) + "</div>"
    return f'<p class="sub">{t(s.get("body"))}</p>'


def slide_mid(s, n, total) -> str:
    q = quokka_img(s.get("quokka"), "qm") if s.get("type") in ("compare", "bignum") else ""
    return (f'<section class="s">{top(s.get("badge"), n, total)}'
            f'<div class="bd"><h2>{t(s.get("title"))}</h2>{slide_body(s)}</div>{q}{foot()}</section>')


def slide_close(c, n, total) -> str:
    q = quokka_img(c.get("quokka") or "thinking_question", "qe")
    return (f'<section class="s">{top(c.get("badge"), n, total)}'
            f'<h2>{t(c.get("title"))}</h2><p class="sub" style="max-width:700px">{t(c.get("body"))}</p>'
            f'<div class="ask">{t(plain(c.get("question")).lstrip("💬 "))}</div>'
            f'<div class="cta"><span>📌 저장</span><span>🔁 공유</span><span>➕ 팔로우</span></div>'
            f'<p class="note" style="margin-top:28px;max-width:640px">전체 계산은 프로필 링크 · 개인 기록이며 특정 상품의 매수를 권하지 않습니다</p>'
            f'{q}{foot(True)}</section>')


def render(spec: dict, out_dir: Path) -> list[Path]:
    total = len(spec["slides"]) + 2
    parts = [slide_cover(spec["cover"], 1, total)]
    parts += [slide_mid(s, i + 2, total) for i, s in enumerate(spec["slides"])]
    parts.append(slide_close(spec["closing"], total, total))
    doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.set_content(doc)
        pg.wait_for_timeout(800)
        pg.evaluate(FIT_JS)
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
    cap = re.sub(r"<br\s*/?>", "\n", spec["caption"]).replace("<g>", "").replace("</g>", "")
    caption = re.sub(r"\n{3,}", "\n\n", cap).strip() + "\n\n" + " ".join(spec.get("hashtags", [])[:8])
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    out_dir = IMAGES_ROOT / stamp
    paths = render(spec, out_dir)
    (out_dir / "caption.txt").write_text(caption, encoding="utf-8")
    (out_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"렌더링 완료: {len(paths)}장 → {out_dir}\n\n[캡션]\n{caption}\n")

    if DRY_RUN:
        if IG_USER_ID and IG_SECRET_TOKEN:
            me = ig("GET", "me", load_token(), fields="user_id,username,account_type")
            print(f"토큰 확인 OK: @{me.get('username')} ({me.get('account_type')}), id={me.get('user_id')}")
        print("DRY RUN — 업로드하지 않고 종료합니다. (Actions 아티팩트에서 이미지 확인)")
        return

    if not (IG_USER_ID and IG_SECRET_TOKEN):
        raise RuntimeError("GitHub Secrets에 IG_USER_ID / IG_ACCESS_TOKEN이 없습니다")
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
