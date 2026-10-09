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
화자는 직장인 투자자 'WhiteCoffee'와 캐릭터 '주식농부쿼카'. 친한 선배가 숫자로 솔직하게 알려주는 톤.

[절대 규칙]
- 숫자·사실은 반드시 본문에 있는 것만 쓴다. 새 숫자를 지어내거나 계산하지 않는다.
- 특정 상품 매수 권유처럼 보이는 문장 금지. 기록·정리 톤.
- 짧게. 한 장에 메시지 하나. 본문 장은 제목 포함 90자 이내.
- 핵심 숫자/단어는 <g>...</g>로 강조(한 문장에 최대 1~2개).
- 줄바꿈은 | 로 직접 표시한다. 의미 단위로 끊는다: 쉼표·물음표 뒤 → 조사(은/는/이/가/을/를/에/의/로)·연결어미(-고/-면/-서/-지만)로 끝나는 어절 뒤. 숫자와 그 숫자가 꾸미는 명사, 꾸밈말과 명사 사이는 끊지 않는다. 각 줄 길이는 비슷하게.
- 이모지는 배지에 1개, 항목 아이콘에 1개까지만.

[구성]
1장 cover: 숫자+물음표 훅이 기본("월 30만원으로|10년 뒤 5,000만원?" 같은 식).
2장 = slides[0]: 반드시 type "bignum", badge "💡 결론부터". 캐러셀을 안 넘긴 사람에게 인스타가 2장을 다시 보여주므로, 이 장만 봐도 결론이 이해돼야 한다.
3~5장 = slides[1..3]: 내용에 맞는 형태를 골라 서로 다르게(같은 type 반복 금지).
6장 closing: 3줄 요약(저장할 이유가 되는 장).

[출력: JSON만, 코드블록 없이]
{
 "cover": {"badge": "카테고리명 + 이모지", "title": "훅, | 로 2~3줄, 최대 20자", "big": "가장 강한 숫자/결론, 최대 6자", "sub": "부연, | 로 2줄, 최대 30자", "quokka": "쿼카 키", "bubble": "쿼카 감탄 한마디 최대 8자", "save_sticker": true/false (체크리스트·요약·표처럼 저장할 가치가 큰 글이면 true)},
 "slides": [  // 정확히 4개, 첫 번째는 bignum
   {"type": "bignum", "badge": "💡 결론부터", "title": "| 로 2줄 이내", "value": "결론 숫자 최대 6자", "label": "숫자 설명 한 줄", "body": "이유 1~2줄", "quokka": "쿼카 키"},
   {"type": "table", "badge": "...", "title": "...", "headers": ["", "", ""], "rows": [["", "", ""]], "highlight": 행번호(0부터, 없으면 -1), "note": "※ 계산 근거 한 줄"},
   {"type": "bars", "badge": "...", "title": "...", "sub": "...", "items": [{"label": "최대 7자", "value": 숫자(손실·하락은 음수), "display": "표시 문자열"}], "takeaway": "👉 한 줄"},
   {"type": "cards", "badge": "...", "title": "...", "items": [{"icon": "이모지", "title": "최대 14자", "desc": "최대 24자"}]},   // 2~3개
   {"type": "compare", "badge": "...", "title": "...", "left": {"label": "", "value": "최대 6자", "points": ["✅/❌로 시작하는 짧은 줄", "..."]}, "right": {...같은 형식}, "body": "1줄", "quokka": "쿼카 키(선택)"},
   {"type": "steps", "badge": "...", "title": "...", "items": [{"title": "최대 14자", "desc": "최대 24자"}]}  // 3~4개
 ],
 "closing": {"badge": "🌿 WhiteCoffee의 관점", "title": "| 로 2줄 이내, 필자의 관점 한마디", "summary": ["요약 1(최대 22자)", "요약 2", "요약 3"], "quokka": "쿼카 키(표지와 다른 것)"},
 "caption": "인스타 본문(일반 줄바꿈 \\n). 1줄 훅 + 핵심 2~3줄 + 빈 줄 + '📌 저장해두고 ~ 꺼내 보세요' + '💌 ~한 동료에게 보내주세요' + '💬 댓글을 부르는 질문 1개' (해시태그·링크 금지, 400자 이내)",
 "hashtags": ["#태그", "..."]   // 5~8개, 검색량 있는 한국어/종목 태그
}
표(table): 최대 4행 4열, 각 칸 8자 이내(긴 설명은 cards로). bars: 3~5개. compare points: 각 0~3줄.
쿼카 키 목록(내용 분위기에 맞게, 놀람·뿌듯·걱정·설명 중 주제에 맞는 표정): {quokkas}

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
            if spec["slides"][0].get("type") != "bignum":
                print("경고: 2장이 bignum이 아님 — 그대로 진행")
            return spec
        except Exception as e:
            last_err = e
            print(f"슬라이드 JSON 파싱 실패, 재시도: {e}")
    raise RuntimeError(f"슬라이드 구성 실패: {last_err}")


# ─── 렌더링 ────────────────────────────────────────
EMOJI = r"[\U0001F300-\U0001FAFF☀-➿⬀-⯿]"
NUM_UNIT = re.compile(r"([+\-−]?\d[\d,.]*\s?(?:%p|%|만\s?원|억\s?원|조\s?원|천\s?원|원|만|억|조|배|년|개월|일|주|달러|bp|세|명|개|장|번|회|위|등)?)")
NB_WORDS = ["S&amp;P 500", "S&amp;P500", "나스닥 100", "ISA 계좌", "연금저축", "미국 직투", "국내 상장"]


def t(s) -> str:
    """사용자 텍스트 → 안전한 HTML. 허용: <g>(강조), | 또는 <br>(줄바꿈).
    한국어 줄바꿈 보정: 숫자+단위·고유명사는 nowrap, 이모지는 앞 어절에 붙임."""
    s = html.escape(str(s or ""), quote=False)  # 따옴표는 그대로(엔티티 숫자가 단위 묶기에 걸리지 않게)
    s = re.sub(r"&lt;br\s*/?&gt;", "|", s)
    s = NUM_UNIT.sub(lambda m: '<span class="nb">' + m.group(1).replace(" ", "&nbsp;") + "</span>" if re.search(r"\d", m.group(1)) else m.group(1), s)
    for w in NB_WORDS:
        s = s.replace(w, f'<span class="nb">{w.replace(" ", "&nbsp;")}</span>')
    s = re.sub(r" (?=" + EMOJI + ")", "&nbsp;", s)
    s = s.replace("&lt;g&gt;", '<span class="g">').replace("&lt;/g&gt;", "</span>")
    return re.sub(r"\s*\|\s*", "<br>", s.strip("| "))


def plain(s) -> str:
    return re.sub(r"<[^>]+>|\|", "", str(s or ""))


def quokka_img(key, cls: str) -> str:
    path = QUOKKA_DIR / f"{key}.webp"
    if not key or not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'<img class="{cls}" src="data:image/webp;base64,{b64}">'


CSS = """
:root{--navy:#1E2D4F;--card:#26375F;--gold:#C9A84B;--yel:#FFD76A;--txt:#DCE3F0;--up:#FF6B6B;--down:#4DA3FF;--cream:#FFF8EC}
*{margin:0;padding:0;box-sizing:border-box;word-break:keep-all;overflow-wrap:anywhere;line-break:strict}
body{background:#111;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR',sans-serif;color:#fff}
h1,h2,.ttl,.big,.bn,.bubble,.ask{text-wrap:balance}
p,small,.sub,.note,li{text-wrap:pretty}
.nb{white-space:nowrap}
.s{width:1080px;height:1350px;position:relative;overflow:hidden;padding:96px 80px;display:flex;flex-direction:column;
  background:radial-gradient(900px 700px at 0% 0%,rgba(201,168,75,.18),transparent 60%),
             radial-gradient(800px 800px at 110% 105%,rgba(91,140,255,.14),transparent 60%),var(--navy)}
.s::before{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.05) 2px,transparent 2px);background-size:36px 36px;pointer-events:none}
.s>*{position:relative}
.top{display:flex;align-items:center;gap:16px}
.badge{display:inline-block;background:var(--gold);color:var(--navy);font-weight:900;font-size:30px;padding:10px 26px;border-radius:40px;box-shadow:0 6px 0 #8a7238}
.pg{margin-left:auto;font-size:28px;color:var(--gold);font-weight:800;background:rgba(255,255,255,.07);padding:8px 18px;border-radius:30px}
.foot{position:absolute;left:80px;right:80px;bottom:60px;display:flex;align-items:center;gap:14px;font-size:28px;color:rgba(255,255,255,.75);font-weight:600}
.logo{width:54px;height:54px;border-radius:50%;border:3px solid var(--gold);color:var(--gold);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:17px;line-height:1;text-align:center;background:var(--navy)}
.foot b{color:var(--gold);font-weight:700}
.swipe{margin-left:auto;color:var(--navy);background:var(--gold);padding:10px 24px;border-radius:30px;font-weight:900}
h1{font-size:104px;line-height:1.2;font-weight:900;margin-top:56px;letter-spacing:-.03em;text-shadow:0 4px 18px rgba(0,0,0,.25);max-width:920px}
h2{font-size:68px;line-height:1.28;font-weight:900;margin-top:46px;letter-spacing:-.02em}
.g{color:var(--yel);background:linear-gradient(transparent 64%,rgba(201,168,75,.35) 64%);padding:0 4px;border-radius:4px}
.sub{font-size:42px;color:var(--txt);margin-top:28px;line-height:1.55;font-weight:700;letter-spacing:-.01em}
.big{display:inline-block;align-self:flex-start;font-size:170px;font-weight:900;color:var(--navy);letter-spacing:-.04em;line-height:1.05;margin-top:40px;
  background:var(--yel);padding:6px 32px 14px;border-radius:28px;transform:rotate(-2deg);box-shadow:0 14px 0 var(--gold),0 24px 40px rgba(0,0,0,.35)}
.sticker{position:absolute;right:70px;top:196px;background:#fff;color:var(--navy);font-weight:900;font-size:30px;padding:14px 24px;border-radius:18px;transform:rotate(6deg);box-shadow:0 8px 24px rgba(0,0,0,.3)}
.qc{position:absolute;right:-20px;bottom:100px;height:560px;filter:drop-shadow(0 20px 30px rgba(0,0,0,.45))}
.bubble{position:absolute;right:50px;bottom:650px;background:#fff;color:var(--navy);font-weight:900;font-size:36px;padding:18px 28px;border-radius:30px;box-shadow:0 10px 30px rgba(0,0,0,.3);white-space:nowrap}
.bubble::after{content:"";position:absolute;left:50%;bottom:-28px;border:16px solid transparent;border-top-color:#fff}
.card{background:var(--card);border:1px solid rgba(255,255,255,.12);border-radius:28px;padding:30px 36px;margin-top:20px;box-shadow:0 10px 30px rgba(0,0,0,.18)}
table{width:100%;border-collapse:separate;border-spacing:0;margin-top:44px;font-size:38px;background:var(--card);border-radius:24px;overflow:hidden}
th{font-size:30px;color:var(--navy);background:var(--gold);padding:20px 18px;text-align:right;font-weight:900}
th:first-child,td:first-child{text-align:left}
td{padding:24px 18px;border-top:1px solid rgba(255,255,255,.1);text-align:right;font-weight:700;color:var(--txt);line-height:1.35}
td.nw{white-space:nowrap}
tr.hl td{color:var(--navy);background:var(--yel);font-weight:900}
tr.hl .g{color:var(--navy);background:rgba(255,255,255,.6)}
.note{font-size:28px;color:rgba(220,227,240,.7);margin-top:24px;line-height:1.5;font-weight:500}
.bar{display:flex;align-items:center;gap:20px;margin-top:28px;font-size:34px;font-weight:700;color:var(--txt)}
.bar .l{width:240px;line-height:1.3}
.bar .tr{flex:1;height:62px;position:relative;background:rgba(255,255,255,.05);border-radius:14px}
.bar .c{position:absolute;left:var(--z);top:-8px;bottom:-8px;width:3px;background:rgba(255,255,255,.35)}
.bar .f{position:absolute;top:0;height:62px;border-radius:14px}
.bar .v{width:210px;text-align:right;font-weight:900;color:#fff}
.bar.top1 .v{color:var(--yel)}
.neg{background:linear-gradient(90deg,#7cbcff,var(--down))}.pos{background:#3A4C78}.pos.hi{background:linear-gradient(90deg,var(--gold),var(--yel))}
.row{display:flex;gap:26px;align-items:center;font-size:42px;line-height:1.35;font-weight:900}
.ic{width:88px;height:88px;flex:none;border-radius:24px;background:rgba(201,168,75,.18);display:flex;align-items:center;justify-content:center;font-size:50px}
.row small{display:block;font-size:34px;font-weight:600;color:var(--txt);margin-top:6px;line-height:1.45}
.cmp{display:flex;gap:26px;margin-top:46px;align-items:stretch;position:relative}
.cmp .card{flex:1;margin-top:0;padding:36px 30px;text-align:left}
.cmp .lb{font-size:34px;color:var(--txt);font-weight:700}
.cmp .n{font-size:84px;font-weight:900;letter-spacing:-.03em;line-height:1.15;margin:8px 0 10px}
.cmp ul{list-style:none;font-size:32px;line-height:1.45;color:var(--txt);font-weight:600}
.cmp li{margin-top:8px}
.cmp .vs{position:absolute;left:50%;top:0;transform:translate(-50%,-50%);width:80px;height:80px;border-radius:50%;background:#e0675a;color:#fff;font-weight:900;font-size:34px;display:flex;align-items:center;justify-content:center;box-shadow:0 8px 20px rgba(0,0,0,.35)}
.num{width:72px;height:72px;border-radius:50%;background:var(--gold);color:var(--navy);font-weight:900;font-size:36px;display:flex;align-items:center;justify-content:center;flex:none;box-shadow:0 5px 0 #8a7238}
.bn{display:inline-block;align-self:flex-start;font-size:190px;font-weight:900;color:var(--navy);background:var(--yel);letter-spacing:-.04em;line-height:1.05;margin-top:50px;padding:6px 32px 14px;border-radius:28px;box-shadow:0 14px 0 var(--gold)}
.qm{position:absolute;right:20px;bottom:120px;height:340px;filter:drop-shadow(0 14px 20px rgba(0,0,0,.4))}
.qe{position:absolute;right:60px;top:180px;height:230px;filter:drop-shadow(0 20px 30px rgba(0,0,0,.45))}
.sum{margin-top:40px;background:var(--cream);color:var(--navy);border-radius:32px;padding:34px 40px;box-shadow:0 14px 40px rgba(0,0,0,.3)}
.sum .h{font-size:30px;font-weight:900;color:#8a7238}
.sum li{list-style:none;font-size:40px;font-weight:800;line-height:1.4;margin-top:16px;display:flex;gap:14px}
.sum .g{color:var(--navy);background:linear-gradient(transparent 60%,rgba(255,215,106,.85) 60%)}
.send{margin-top:40px;align-self:flex-start;background:var(--yel);color:var(--navy);font-weight:900;font-size:46px;padding:22px 36px;border-radius:40px;box-shadow:0 8px 0 var(--gold)}
.mini{margin-top:22px;font-size:32px;font-weight:700;color:var(--txt)}
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
    """가장 긴 줄 기준으로 글자 크기 축소."""
    lines = re.split(r"\||<br\s*/?>", str(text or ""))
    n = max(len(plain(x).strip()) for x in lines) if lines else 0
    return f"font-size:{base if n <= max_chars else int(base * max_chars / n)}px"


def top(badge, n, total) -> str:
    return f'<div class="top"><span class="badge">{t(badge)}</span><span class="pg">{n} / {total}</span></div>'


def foot(last=False) -> str:
    sw = "" if last else '<span class="swipe">넘기기 →</span>'
    return f'<div class="foot"><span class="logo">주식<br>농사</span>WhiteCoffee 의 주식농사 <b>@ayunfafa</b>{sw}</div>'


def slide_cover(c, n, total) -> str:
    q = quokka_img(c.get("quokka") or "pointer_explain", "qc")
    bubble = f'<div class="bubble">{t(plain(c.get("bubble")))}</div>' if q and c.get("bubble") else ""
    sticker = '<span class="sticker">📌 저장 필수</span>' if c.get("save_sticker") else ""
    return (f'<section class="s">{top(c.get("badge"), n, total)}{sticker}'
            f'<div class="bd" style="max-width:920px"><h1 style="{_fit(c.get("title"), 104, 10)}">{t(c.get("title"))}</h1>'
            f'<div class="big" style="{_fit(c.get("big"), 170, 5)}">{t(plain(c.get("big")))}</div>'
            f'<p class="sub" style="max-width:600px">{t(c.get("sub"))}</p></div>{q}{bubble}{foot()}</section>')


def slide_body(s) -> str:
    ty = s.get("type")
    if ty == "table":
        hl = s.get("highlight", -1)
        nc = len(s.get("headers", [])) or 3
        s["rows"] = [(list(r[:nc - 1]) + [" ".join(map(str, r[nc - 1:]))]) if len(r) > nc else list(r) + [""] * (nc - len(r))
                     for r in s.get("rows", [])]
        longest = max([len(plain(x)) for r in s.get("rows", []) for x in r] + [0])
        tstyle = ' style="font-size:32px"' if longest > 9 else ""
        head = "".join(f"<th>{t(x)}</th>" for x in s.get("headers", []))
        rows = "".join(
            f'<tr class="{"hl" if i == hl else ""}">'
            + "".join(f'<td{" class=nw" if len(plain(x)) <= 6 else ""}>{t(x)}</td>' for x in r) + "</tr>"
            for i, r in enumerate(s.get("rows", [])[:4]))
        return f'<table{tstyle}><tr>{head}</tr>{rows}</table><p class="note">{t(s.get("note"))}</p>'
    if ty == "bars":
        items = s.get("items", [])[:5]
        vals = [float(i.get("value") or 0) for i in items] or [0]
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        span = (hi - lo) or 1
        z = (0 - lo) / span * 100
        best = max(range(len(vals)), key=lambda k: abs(vals[k])) if vals else -1
        out = f'<p class="sub">{t(s.get("sub"))}</p><div class="card" style="margin-top:30px;padding:16px 30px 34px">'
        for k, (i, v) in enumerate(zip(items, vals)):
            w = abs(v) / span * 100
            pos = f"left:{z}%" if v >= 0 else f"right:{100 - z}%"
            cls = "neg" if v < 0 else ("pos hi" if k == best else "pos")
            out += (f'<div class="bar{" top1" if k == best else ""}"><span class="l">{t(i.get("label"))}</span>'
                    f'<span class="tr" style="--z:{z}%"><i class="c"></i><i class="f {cls}" style="{pos};width:{w}%"></i></span>'
                    f'<span class="v">{t(i.get("display"))}</span></div>')
        return out + f'</div><p class="sub" style="margin-top:36px;color:#fff">{t(s.get("takeaway"))}</p>'
    if ty in ("cards", "steps"):
        out = '<div style="margin-top:34px">'
        for k, i in enumerate(s.get("items", [])[:4]):
            mark = f'<span class="num">{k + 1}</span>' if ty == "steps" else f'<span class="ic">{t(i.get("icon"))}</span>'
            out += f'<div class="card"><div class="row">{mark}<div>{t(i.get("title"))}<small>{t(i.get("desc"))}</small></div></div></div>'
        return out + "</div>"
    if ty == "compare":
        def side(d, hi):
            pts = "".join(f"<li>{t(p)}</li>" for p in (d.get("points") or [])[:3])
            col = "color:var(--yel)" if hi else ""
            border = ' style="border:3px solid var(--yel)"' if hi else ""
            return (f'<div class="card"{border}><div class="lb">{t(d.get("label"))}</div>'
                    f'<div class="n" style="{col}">{t(d.get("value"))}</div><ul>{pts}</ul></div>')
        return (f'<div class="cmp">{side(s.get("left", {}), False)}{side(s.get("right", {}), True)}<span class="vs">VS</span></div>'
                f'<p class="sub" style="margin-top:40px;max-width:{640 if s.get("quokka") else 920}px">{t(s.get("body"))}</p>')
    if ty == "bignum":
        return (f'<div class="bn" style="{_fit(s.get("value"), 190, 5)}">{t(plain(s.get("value")))}</div>'
                f'<p class="sub" style="font-size:46px;font-weight:900;color:#fff;margin-top:52px;max-width:{640 if s.get("quokka") else 920}px">{t(s.get("label"))}</p>'
                f'<p class="sub" style="max-width:{620 if s.get("quokka") else 920}px">{t(s.get("body"))}</p>')
    return f'<p class="sub">{t(s.get("body"))}</p>'


def slide_mid(s, n, total) -> str:
    q = quokka_img(s.get("quokka"), "qm") if s.get("type") in ("compare", "bignum") else ""
    return (f'<section class="s">{top(s.get("badge"), n, total)}'
            f'<div class="bd"><h2>{t(s.get("title"))}</h2>{slide_body(s)}</div>{q}{foot()}</section>')


def slide_close(c, n, total) -> str:
    q = quokka_img(c.get("quokka") or "confident_thumbs", "qe")
    summ = c.get("summary") or []
    if not summ and c.get("body"):
        summ = [c.get("body")]
    lis = "".join(f"<li><span>✅</span><span>{t(x)}</span></li>" for x in summ[:3])
    return (f'<section class="s">{top(c.get("badge"), n, total)}'
            f'<div class="bd"><h2 style="max-width:660px">{t(c.get("title"))}</h2>'
            f'<div class="sum"><div class="h">📝 3줄 요약</div><ul>{lis}</ul></div>'
            f'<div class="send">💌 필요한 동료에게 보내기</div>'
            f'<p class="mini">📌 저장 · ➕ 팔로우 @ayunfafa</p>'
            f'<p class="note" style="max-width:600px">개인 기록이며 특정 상품의 매수를 권하지 않습니다 · 전체 계산은 프로필 링크</p></div>'
            f'{q}{foot(True)}</section>')


def render(spec: dict, out_dir: Path) -> list[Path]:
    total = len(spec["slides"]) + 2
    parts = [slide_cover(spec["cover"], 1, total)]
    parts += [slide_mid(s, i + 2, total) for i, s in enumerate(spec["slides"])]
    parts.append(slide_close(spec["closing"], total, total))
    doc = f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
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
    subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", BRANCH], check=True)
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
