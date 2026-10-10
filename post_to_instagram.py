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
    cands = [p for p in posts if p["category"] not in SKIP_CATEGORIES and p["link"] not in done][:8]
    if len(cands) <= 1:
        return cands[0] if cands else None
    try:  # 초보 독자에게 가장 가까운 글을 고른다
        items = "\n".join(f"{i + 1}. [{p['category']}] {p['title']}" for i, p in enumerate(cands))
        msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=MODEL, max_tokens=10,
            messages=[{"role": "user", "content": PICK_PROMPT.replace("{reader}", READER).replace("{items}", items)}])
        k = int(re.search(r"\d+", msg.content[0].text).group()) - 1
        if 0 <= k < len(cands):
            return cands[k]
    except Exception as e:
        print(f"글 선택 AI 실패(최신 글 사용): {e}")
    return cands[0]


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
READER = "투자를 막 시작한 2030 직장인(성별 무관). 주식 앱을 깔고 ETF를 몇 개 사봤거나, 적금만 하다 이제 투자를 해보려는 사람. 월급·적금·카드값은 익숙하지만 세금·금리·ETF 구조 같은 용어는 아직 낯설다."

PROMPT = """너는 'WhiteCoffee 의 주식농사' 인스타그램 캐러셀 에디터다.
[독자] {reader}
이 독자가 피드를 내리다 멈추고 "어? 나도 해당되나?" 하고 끝까지 넘겨 보게 만드는 6장 캐러셀을 만든다.

[핵심 방침: 요약하지 말고, 한 가지만 쉽게]
- 블로그 글은 어렵다. 글 전체를 요약하지 않는다. 이 독자에게 가장 와닿는 "오 그렇구나" 포인트 딱 하나만 골라 그것만 쉽게 설명한다.
- 글의 나머지 디테일(세부 계산, 예외, 여러 시나리오)은 과감히 버린다. 궁금한 사람은 프로필 링크로 가면 된다.
- 일상 상황으로 시작한다(월급날, 적금 만기, 카드값, 해외여행 환전, 연말정산, 커피 한 잔 값 등).
- 말투: 친한 언니/선배가 카톡하듯 친근한 존댓말("~해요", "~거든요"). 제목·본문·쿼카 대사 모두 존댓말. 반말은 chat의 "me"(독자) 대사에만 허용. 훈계·전문가 톤 금지.

[쉬운 말 규칙]
- 금지 용어(쓰지 말고 풀어쓴다): 레버리지, 듀레이션, 과세표준, 손익분기, 순자산, 원천징수, 종합과세, 배분, 시나리오, 편차, 평가액, 비대칭, 리밸런싱, 분할매수, 권리락, 인적분할, 환헤지 등.
  상품명·은어(SGOV, 달러 파킹, 직투, 빚투 등)도 처음 나올 때 풀이한다.
  꼭 필요한 용어(ETF, 배당, 금리 정도)는 처음 나올 때 괄호로 한 줄 풀이: "ETF(여러 회사를 한 바구니에 담은 상품)".
- 숫자는 캐러셀 전체에서 핵심 숫자 3~4개만. 한 장에 숫자 최대 2개. 큰 금액은 체감되게 바꿔 쓸 수 있다(원문 숫자 그대로 쓸 것, 새 계산 금지).
- 비율 표기(50:50, 30:70) 금지 → "반반", "10만원 중 7만원은 ~".
- 문장은 짧게, 한 줄 16자 안팎. 줄바꿈은 | 로 의미 단위로.
- 숫자·사실은 반드시 본문에 있는 것만. 투자 권유처럼 들리는 말 금지("사세요" ✕ → "저는 이렇게 해요" ○).

[구성: 8장 (내용이 부족하면 7장)]
1장 cover: 독자 일상 속 질문 + 힌트. 최종 답은 절대 표지에 쓰지 않는다(답을 보면 넘길 이유가 사라짐).
   단, 질문만 덩그러니 두지도 않는다. big에는 '힌트'(판단 기준의 개수, 반전 예고, 놀라운 숫자의 일부)를 넣는다.
   예) 질문 "월급 남으면…|대출부터? 투자부터?" + big "기준은 딱 1개" / big "의외의 답" / big "숫자 하나로 끝"
   나쁜 예) big "반반이 답", "−3.4%", "2배" ← 결론·핵심 숫자를 표지에 쓰면 안 된다. 이런 건 2장에.
2장 = slides[0]: type "bignum", badge "💡 결론부터". 인스타는 1장에서 안 넘긴 사람에게 2장을 다시 보여준다.
   그래서 topic(작은 주제 제목)을 꼭 넣어 2장만 봐도 '무슨 얘기 + 답'이 이해되게 한다.
3장 = slides[1]: 공감·문제 — chat(대화형) 추천. "나도 이거 헷갈려" 상황.
4장 = slides[2]: 원인·원리 — 왜 그 답인지 핵심 원리 1개 (cards 또는 compare).
5장 = slides[3]: 기준·단계 — 내 상황에 대입하는 체크 기준 (steps).
6장 = slides[4]: 예시 — 가상 인물 숫자 예시 1개 (compare, table, bars 중 하나. 숫자는 원문 것만).
7장 = slides[5]: 흔한 실수·예외 — "이런 경우는 반대예요" (cards, 아이콘은 ⚠️/❌ 등). 내용이 부족하면 이 장을 생략해 slides를 5개로.
마지막 closing: 3줄 요약 + 보내기.
- 억지로 채운 빈 장보다 빼는 게 낫다. 각 장은 서로 다른 내용이어야 한다.
- 1장의 질문에 대한 답을 2장에서 반드시 준다(낚시 금지). "공유하면 공개", "댓글 달면 알려줌" 같은 조건부 문구 절대 금지.

[출력: JSON만, 코드블록 없이]
{
 "point": "이번 캐러셀이 전달할 단 하나의 포인트(내부용, 한 문장)",
 "cover": {"badge": "카테고리 + 이모지", "context": "누구 얘기인지 일상어로 한 줄(최대 30자, 예: 대출 있는 직장인이 월급 남는 돈을 어디에 쓸지)", "title": "훅, | 로 2~3줄, 최대 22자", "big": "힌트(정답 금지), 최대 8자", "sub": "| 로 2줄, 최대 30자", "quokka": "쿼카 키", "bubble": "쿼카 한마디 최대 10자", "save_sticker": true/false},
 "slides": [  // 6개(또는 5개), 순서는 위 [구성] 그대로. 아래는 type별 형식 예시
   {"type": "bignum", "badge": "💡 결론부터", "topic": "작은 주제 제목 최대 16자(예: 대출 vs 투자, 뭐부터?)", "title": "| 로 2줄", "value": "최대 6자", "label": "이 숫자가 뭔지 일상어로", "body": "1~2줄", "quokka": "쿼카 키"},
   {"type": "chat", "badge": "...", "title": "| 로 2줄", "lines": [{"who": "me", "text": "초보 독자의 솔직한 질문(최대 30자)"}, {"who": "quokka", "text": "쿼카의 쉬운 답(최대 40자)"}]},  // 3~5개 말풍선, me로 시작해서 반드시 quokka 답으로 끝낸다(질문으로 끝내지 않기)
   {"type": "cards", "badge": "...", "title": "...", "items": [{"icon": "이모지", "title": "최대 16자", "desc": "최대 30자, 일상어"}]},  // 2~3개
   {"type": "steps", "badge": "...", "title": "...", "items": [{"title": "최대 16자", "desc": "최대 30자"}]},  // 3개, '오늘 해볼 것' 같은 행동 위주
   {"type": "compare", "badge": "...", "title": "...", "left": {"label": "", "value": "최대 6자", "points": ["✅/❌ 짧은 줄"]}, "right": {...}, "body": "1줄", "quokka": "쿼카 키(선택)"},
   {"type": "table", "badge": "...", "title": "...", "explain": "👀 읽는 법 한 줄", "headers": ["",""], "rows": [["",""]], "highlight": -1, "note": "※ 근거"},  // 최대 3행 3열
   {"type": "bars", "badge": "...", "title": "...", "sub": "무엇을 비교했는지", "items": [{"label": "최대 10자", "value": 숫자, "display": ""}], "takeaway": "👉 한 줄"}  // 최대 4개
 ],
 "closing": {"badge": "🌿 WhiteCoffee의 한마디", "title": "| 로 2줄", "summary": ["요약 1(최대 20자)", "요약 2", "요약 3"], "quokka": "쿼카 키(표지와 다른 것)"},
 "caption": "인스타 본문(\\n 줄바꿈). 공감 한 줄 + 핵심 2줄 + 빈 줄 + '📌 저장해두고 ~ 꺼내 보세요' + '💌 ~한 친구에게 보내주세요' + '💬 가볍게 답할 수 있는 질문' (해시태그·링크 금지, 350자 이내)",
 "hashtags": ["#태그"]  // 5~8개, 초보가 검색할 만한 말(#재테크초보 #월급관리 #사회초년생재테크 등 포함)
}
쿼카 키 목록(놀람·뿌듯·걱정·설명 중 맞는 표정): {quokkas}

[블로그 글]
제목: {title}
카테고리: {category}
본문:
{body}
"""

REVIEW_PROMPT = """너는 아래 독자 본인이다: {reader}
인스타 캐러셀 JSON을 1장부터 순서대로 읽어 보고, 네가 이해 못 하거나 지루해서 넘기다 멈출 곳을 모두 고쳐라.

점검:
1. 모르는 단어가 하나라도 있나(상품명·은어 포함)? → 일상어로 바꾸거나 처음 나올 때 괄호 풀이.
1-1. 대화형(chat)이 질문으로 끝나면 쿼카 답을 붙인다.
2. 한 장에 숫자가 2개를 넘나? → 줄인다. 무슨 숫자인지 바로 알 수 있나?
3. 1장만 봐도 "내 얘기다" 싶은가? cover.big·sub·bubble에 결론이나 2장 숫자가 들어 있으면 반드시 힌트로 바꾼다(예: "기준은 딱 1개"). 2장만 봐도 주제(topic)와 결론이 보이나?
3-1. 내용이 겹치거나 억지로 채운 장이 있으면 지운다(최소 5개는 유지).
4. 말투가 친한 선배 카톡처럼 편한 존댓말인가(반말은 chat의 me 대사만)? 딱딱하거나 가르치는 말투면 고친다.
5. 블로그 디테일을 너무 많이 넣지 않았나? 포인트 하나(point)에 집중하도록 덜어낸다.
6. 숫자는 원문에 있는 것만인가? 원문에 없는 숫자는 삭제.

같은 JSON 구조·키·길이 제한, | 줄바꿈, <g> 강조 규칙을 유지하고 고친 전체 JSON만 출력한다(설명·코드블록 없이).

[원문 본문]
{body}

[캐러셀 JSON]
{spec}
"""

PICK_PROMPT = """아래는 아직 인스타에 안 올린 블로그 글 목록이다. 독자: {reader}
이 독자가 피드에서 보고 "어? 나도 궁금했는데" 하고 멈출 만한, 일상과 가장 가까운 글 1개를 골라 번호만 답하라.
(세금 신고 세부, 기업 구조 변경, 채권 계산처럼 초보에게 먼 주제는 뒤로.)

{items}
"""


def enforce_structure(spec: dict) -> dict:
    sl = spec.get("slides", [])
    k = next((i for i, x in enumerate(sl) if x.get("type") == "bignum"), None)
    if k not in (None, 0):
        sl.insert(0, sl.pop(k))
    if sl:
        sl[0]["badge"] = "💡 결론부터"
    spec["slides"] = sl[:6]
    return spec


def review_spec(spec: dict, body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = REVIEW_PROMPT.replace("{reader}", READER).replace("{body}", body[:9000]).replace("{spec}", json.dumps(spec, ensure_ascii=False, indent=1))
    try:
        msg = client.messages.create(model=MODEL, max_tokens=5000, messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        fixed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        assert fixed["cover"] and len(fixed["slides"]) >= 3 and fixed["closing"] and fixed["caption"]
        fixed["slides"] = fixed["slides"][:6]
        print("첫 독자 검토 반영 완료")
        return fixed
    except Exception as e:
        print(f"검토 단계 실패(원안 사용): {e}")
        return spec


def build_spec(title: str, category: str, body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = PROMPT.replace("{reader}", READER).replace("{title}", title).replace("{category}", category or "직장인 투자").replace("{body}", body).replace("{quokkas}", ", ".join(f"{k}={v}" for k, v in QUOKKA.items()))
    last_err = None
    for _ in range(2):
        msg = client.messages.create(model=MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw).strip()
        try:
            spec = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            assert spec["cover"] and len(spec["slides"]) >= 3 and spec["closing"] and spec["caption"]
            spec["slides"] = spec["slides"][:6]
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
.big{display:inline-block;align-self:flex-start;max-width:560px;font-size:170px;font-weight:900;color:var(--navy);letter-spacing:-.04em;line-height:1.05;margin-top:40px;
  background:var(--yel);padding:6px 32px 14px;border-radius:28px;transform:rotate(-2deg);box-shadow:0 14px 0 var(--gold),0 24px 40px rgba(0,0,0,.35)}
.sticker{position:absolute;right:70px;top:196px;background:#fff;color:var(--navy);font-weight:900;font-size:30px;padding:14px 24px;border-radius:18px;transform:rotate(6deg);box-shadow:0 8px 24px rgba(0,0,0,.3)}
.qc{position:absolute;right:-20px;bottom:100px;height:560px;filter:drop-shadow(0 20px 30px rgba(0,0,0,.45))}
.bubble{position:absolute;right:50px;bottom:650px;background:#fff;color:var(--navy);font-weight:900;font-size:36px;padding:18px 28px;border-radius:30px;box-shadow:0 10px 30px rgba(0,0,0,.3);max-width:420px;text-align:center;line-height:1.3}
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
.ctx{display:inline-flex;align-self:flex-start;gap:12px;align-items:center;margin-top:30px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);border-radius:20px;padding:14px 22px;font-size:30px;font-weight:700;color:var(--txt);max-width:920px;line-height:1.4}
.ctx b{color:var(--gold);white-space:nowrap}
.how{margin-top:20px;font-size:32px;font-weight:700;color:var(--yel);line-height:1.45}
.chat{margin-top:36px;display:flex;flex-direction:column;gap:22px}
.msg{display:flex;align-items:flex-end;gap:14px}
.msg.me{justify-content:flex-end}
.bub{max-width:700px;font-size:38px;font-weight:700;line-height:1.42;padding:22px 30px;border-radius:34px;text-wrap:pretty}
.msg.me .bub{background:var(--yel);color:var(--navy);border-bottom-right-radius:8px}
.msg.qk .bub{background:#fff;color:var(--navy);border-bottom-left-radius:8px}
.msg.qk .bub .g{color:var(--navy);background:linear-gradient(transparent 60%,rgba(255,215,106,.9) 60%)}
.msg.me .bub .g{color:var(--navy);background:rgba(255,255,255,.6)}
.av{width:86px;height:86px;object-fit:contain;flex:none;border-radius:50%;background:rgba(255,255,255,.12);padding:4px}
.topic{margin-top:40px;font-size:34px;font-weight:800;color:var(--gold)}
.topic+h2{margin-top:10px}
.mini{margin-top:22px;font-size:32px;font-weight:700;color:var(--txt)}
"""

CREAM_CSS = """
.s.cream{background:radial-gradient(900px 700px at 0% 0%,rgba(255,215,106,.35),transparent 60%),#FFF8EC;color:#1E2D4F}
.s.cream::before{background-image:radial-gradient(rgba(30,45,79,.06) 2px,transparent 2px)}
.s.cream h1{color:#1E2D4F;text-shadow:none}
.s.cream .sub{color:#4a5675}
.s.cream .ctx{background:rgba(30,45,79,.06);border-color:rgba(30,45,79,.12);color:#1E2D4F}
.s.cream .ctx b{color:#b08a2e}
.s.cream .pg{background:rgba(30,45,79,.08);color:#b08a2e}
.s.cream .foot{color:#4a5675}
.s.cream .bubble{background:#1E2D4F;color:#fff}
.s.cream .bubble::after{border-top-color:#1E2D4F}
"""


def cover_variant(h: dict) -> str:
    """표지 A/B: 직전 발행과 반대 색으로 번갈아 (기록 없던 초기 발행은 남색)."""
    last = next((x for x in reversed(h.get("log", [])) if x.get("media_id")), None)
    return "navy" if last and last.get("cover") == "cream" else "cream"


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


def _next_label(badge) -> str:
    b = re.sub(EMOJI, "", plain(badge)).strip()
    return b[:10]


def foot(last=False, nxt=None) -> str:
    if last:
        sw = ""
    elif nxt:
        lab = "넘겨서 답 보기" if "답 보기" in str(nxt) else "다음: " + _next_label(nxt)
        sw = f'<span class="swipe">{t(lab)} →</span>'
    else:
        sw = '<span class="swipe">넘기기 →</span>'
    return f'<div class="foot"><span class="logo">주식<br>농사</span>WhiteCoffee 의 주식농사 <b>@ayunfafa</b>{sw}</div>'


def ctx_chip(c) -> str:
    return f'<div class="ctx"><b>👋 이런 분께</b><span>{t(c.get("context"))}</span></div>' if c.get("context") else ""


def slide_cover(c, n, total, variant="navy") -> str:
    q = quokka_img(c.get("quokka") or "pointer_explain", "qc")
    bubble = f'<div class="bubble">{t(plain(c.get("bubble")))}</div>' if q and c.get("bubble") else ""
    sticker = '<span class="sticker">📌 저장 필수</span>' if c.get("save_sticker") and not c.get("context") else ""
    return (f'<section class="s {variant}">{top(c.get("badge"), n, total)}{sticker}'
            f'<div class="bd" style="max-width:920px">{ctx_chip(c)}<h1 style="{_fit(c.get("title"), 104, 10)}">{t(c.get("title"))}</h1>'
            f'<div class="big" style="{_fit(c.get("big"), 150, 5)}">{t(plain(c.get("big")))}</div>'
            f'<p class="sub" style="max-width:600px">{t(c.get("sub"))}</p></div>{q}{bubble}{foot(nxt="👉 답 보기")}</section>')


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
        how = f'<p class="how">{t(s.get("explain"))}</p>' if s.get("explain") else ""
        return f'{how}<table{tstyle}><tr>{head}</tr>{rows}</table><p class="note">{t(s.get("note"))}</p>'
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
    if ty == "chat":
        av = quokka_img("phone_up_smile", "av")
        out = '<div class="chat">'
        lines = (s.get("lines") or [])[:5]
        while lines and lines[-1].get("who") == "me":
            lines = lines[:-1]
        for ln in lines:
            if ln.get("who") == "me":
                out += f'<div class="msg me"><span class="bub">{t(ln.get("text"))}</span></div>'
            else:
                out += f'<div class="msg qk">{av}<span class="bub">{t(ln.get("text"))}</span></div>'
        return out + "</div>"
    if ty in ("cards", "steps"):
        out = '<div style="margin-top:34px">'
        for k, i in enumerate(s.get("items", [])[:4]):
            mark = f'<span class="num">{k + 1}</span>' if ty == "steps" else f'<span class="ic">{t(i.get("icon"))}</span>'
            ttl = re.sub(r"^\s*\d+[.)]\s*", "", str(i.get("title") or "")) if ty == "steps" else i.get("title")
            out += f'<div class="card"><div class="row">{mark}<div>{t(ttl)}<small>{t(i.get("desc"))}</small></div></div></div>'
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


def slide_mid(s, n, total, nxt=None) -> str:
    q = quokka_img(s.get("quokka"), "qm") if s.get("type") in ("compare", "bignum") else ""
    topic = f'<p class="topic">{t(s.get("topic"))}</p>' if s.get("topic") else ""
    return (f'<section class="s">{top(s.get("badge"), n, total)}'
            f'<div class="bd">{topic}<h2>{t(s.get("title"))}</h2>{slide_body(s)}</div>{q}{foot(nxt=nxt)}</section>')


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


def render(spec: dict, out_dir: Path, variant: str = "navy") -> list[Path]:
    total = len(spec["slides"]) + 2
    parts = [slide_cover(spec["cover"], 1, total, variant)]
    sl = spec["slides"]
    parts += [slide_mid(x, i + 2, total, sl[i + 1].get("badge") if i + 1 < len(sl) else "📝 3줄 요약")
              for i, x in enumerate(sl)]
    parts.append(slide_close(spec["closing"], total, total))
    doc = f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><style>{CSS}{CREAM_CSS}</style></head><body>{''.join(parts)}</body></html>"
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
    now = datetime.now(KST)
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not (19 <= now.hour <= 23):
        print(f"예약 실행이 너무 늦게 도착함({now:%H:%M}) — 저녁 시간대가 아니라 건너뜀")
        return
    last = next((x for x in reversed(h.get("log", [])) if x.get("media_id")), None)
    if last and not FORCE_URL and not DRY_RUN and \
            datetime.fromisoformat(last["at"]) > datetime.now(KST) - timedelta(hours=12):
        print(f"최근 12시간 안에 이미 발행함({last['at']}) — 중복 방지로 건너뜀")
        return
    post = pick_post(h)
    if not post:
        print("올릴 새 글이 없습니다.")
        return
    title, body = fetch_article_text(post["link"])
    title = post["title"] or title
    print(f"선택된 글: {title}\n{post['link']}")

    spec = enforce_structure(review_spec(enforce_structure(build_spec(title, post["category"], body)), body))
    cap = re.sub(r"<br\s*/?>", "\n", spec["caption"]).replace("<g>", "").replace("</g>", "")
    caption = re.sub(r"\n{3,}", "\n\n", cap).strip() + "\n\n" + " ".join(spec.get("hashtags", [])[:8])
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    out_dir = IMAGES_ROOT / stamp
    variant = cover_variant(h)
    print(f"표지 버전: {variant}")
    paths = render(spec, out_dir, variant)
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
                                    "title": title, "media_id": media_id, "images": stamp, "cover": variant})
    save_history(h)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"실패: {e}")
        sys.exit(1)
