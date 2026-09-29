import os
import html
import json
import time
import subprocess
import feedparser
import anthropic
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# ── 설정 ──────────────────────────────────────────
TISTORY_RSS   = "https://ideas07576.tistory.com/rss"
POSTS_PER_RUN = 1
HISTORY_FILE  = "published_history.json"
SHORT_TERM_CATEGORY = "단기 투자"

REPO_OWNER  = "whitecoffee86"
REPO_NAME   = "threads-auto-post"
REPO_BRANCH = "main"
IMAGES_DIR  = Path("images")

BRAND_NAVY = "#1e2d4f"
BRAND_NAVY_DEEP = "#12192c"
BRAND_GOLD = "#c9a84b"
BRAND_GOLD_LIGHT = "#e8cf8a"
BRAND_GOLD_DIM = "#8a7238"
FONT = "'Pretendard', 'Noto Sans KR', 'Malgun Gothic', sans-serif"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
THREADS_USER_ID   = os.environ["THREADS_USER_ID"]
THREADS_TOKEN     = os.environ["THREADS_ACCESS_TOKEN"]
# ─────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))


def load_data() -> dict:
    if Path(HISTORY_FILE).exists():
        with open(HISTORY_FILE) as f:
            data = json.load(f)
            return {
                "cycle_published": set(data.get("cycle_published", [])),
                "short_term_done": set(data.get("short_term_done", [])),
            }
    return {"cycle_published": set(), "short_term_done": set()}


def save_data(data: dict):
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "cycle_published": list(data["cycle_published"]),
            "short_term_done": list(data["short_term_done"]),
        }, f, ensure_ascii=False, indent=2)


def fetch_rss() -> list:
    feed = feedparser.parse(TISTORY_RSS)
    posts = []
    for entry in feed.entries:
        tags = [t.term for t in getattr(entry, "tags", [])]
        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(KST).date()
        posts.append({
            "title":    entry.title,
            "link":     entry.link,
            "summary":  entry.get("summary", "")[:800],
            "category": tags[0] if tags else "",
            "pub_date": pub_date,
        })
    return posts


def generate_threads_post(post: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 스레드(Threads)에 올릴 홍보글로 작성해줘.

글 제목: {post['title']}
글 링크: {post['link']}
내용 요약: {post['summary']}

스타일 가이드:
- 광고글처럼 보이면 안 됨. 직장인이 퇴근 후 자연스럽게 공유하는 느낌으로
- "나도 처음엔 몰랐는데", "알고 보니", "생각보다" 같은 자연스러운 구어체 표현 활용
- 독자가 "어? 이거 나 얘기네" 싶게 공감 포인트를 첫 문장에 넣기
- 핵심 인사이트를 2~4문장으로 풀어서 설명 (단순 나열 금지)
- 링크는 본문에 넣지 않음 (댓글에 따로 달 예정이므로 절대 URL을 포함하지 말 것)

형식:
1. 첫 줄: 공감 또는 궁금증을 유발하는 후킹 문장 (이모지 1개 포함)
2. 본문: 핵심 내용을 이야기하듯 3~5문장으로 풀어서 설명
3. 본문: URL 자체는 절대 쓰지 말 것
4. 해시태그: 2~3개 (맨 마지막)

조건:
- 반드시 450자 이내 (띄어쓰기 포함, 이 조건 최우선)
- 재테크/투자 관심 직장인 타깃
- 절대 광고처럼 보이지 않게
- 본문에 URL을 절대 포함하지 말 것 (링크는 별도로 댓글에 게시됨)

홍보글만 출력해줘. 다른 말 없이."""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    text = msg.content[0].text.strip()
    if len(text) > 490:
        text = text[:490]
    return text


def generate_card_hook(post: dict) -> str:
    """카드 이미지에 크게 들어갈 한 줄 후킹 문구 생성 (이모지 포함, 짧고 임팩트 있게)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 SNS 카드 이미지에 큼직하게 넣을 한 줄 후킹 문구로 요약해줘.

글 제목: {post['title']}
내용 요약: {post['summary']}

조건:
- 반드시 한 문장, 25자 이내 (띄어쓰기 포함)
- 이모지 포함, 해시태그, 따옴표 없이 텍스트만
- 궁금증이나 공감을 유발하는 임팩트 있는 문구
- 광고 카피처럼 과장하지 말 것

문구만 출력해줘. 다른 말 없이."""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    hook = msg.content[0].text.strip().strip('"').strip("'")
    if len(hook) > 40:
        hook = hook[:40]
    return hook


CONTENT_TYPES = ("bar", "line", "donut", "stat", "table", "process")


def _esc(value, max_len: int) -> str:
    """LLM이 준 문자열을 길이 제한 후 HTML 이스케이프 (<, & 등으로 카드 레이아웃이 깨지는 것 방지)."""
    return html.escape(str(value).strip()[:max_len])


def extract_visual_content(post: dict) -> dict | None:
    """글 내용에 가장 잘 맞는 카드 형태를 자동으로 골라 데이터를 추출.

    후보 형태 (content_type):
    - "bar":     서로 다른 대상 간의 수치 비교 (지역/상품/연도 등, 2개 이상)
    - "line":    시간 흐름에 따른 추이 (최소 3개 시점)
    - "donut":   전체 대비 구성비/배분 (합쳐서 의미 있는 비율)
    - "stat":    비교 데이터는 없지만 임팩트 있는 숫자 하나가 핵심인 글 ("이것 하나만 기억해" 류)
    - "table":   두 대상을 여러 항목에 걸쳐 비교하는 글 (A vs B, Before/After)
    - "process": 단계/순서가 있는 글 (절차, 체크리스트, 확인 방법 등)

    데이터도 표도 순서도 아무것도 못 만들 만큼 내용이 빈약하면 None 반환
    (이 경우 호출부에서 기존 후킹 문구 카드로 폴백).

    반환 형식은 content_type에 따라 다름 — 아래 각 렌더 함수의 docstring 참고.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 SNS 카드 이미지로 만들려고 해. 글 내용에 가장 잘 맞는 형태를 하나만 골라서
데이터를 뽑아줘.

글 제목: {post['title']}
내용 요약: {post['summary']}

선택 가능한 content_type과 형식:

1. "bar" — 서로 다른 대상 간 수치 비교 (2~6개)
   {{"content_type": "bar", "hook": "...", "labels": ["...", "..."], "values": [0, 0], "unit": "...", "is_approx": false}}

2. "line" — 시간 흐름에 따른 추이 (최소 3개 시점)
   {{"content_type": "line", "hook": "...", "labels": ["...", "..."], "values": [0, 0, 0], "unit": "...", "is_approx": false}}

3. "donut" — 전체 대비 구성비/배분 (합쳐서 의미 있는 비율, 2~4개)
   {{"content_type": "donut", "hook": "...", "labels": ["...", "..."], "values": [0, 0], "unit": "...", "is_approx": false}}

4. "stat" — 비교 데이터는 없지만 임팩트 있는 숫자/사실 하나가 핵심인 글
   {{"content_type": "stat", "hook": "...", "stat_value": "800만원", "stat_label": "3년 방치하면 나는 차이", "stat_icon": "💰"}}

5. "table" — 두 대상을 여러 항목으로 비교하는 글 (2~5개 행, 2개 열)
   {{"content_type": "table", "hook": "...", "table_columns": ["A안", "B안"],
     "table_rows": [{{"label": "수수료", "values": ["0.03%", "0.15%"]}}, ...]}}

6. "process" — 단계/순서/절차가 있는 글 (3~5단계)
   {{"content_type": "process", "hook": "...", "process_steps": [{{"emoji": "🔍", "title": "...", "desc": "..."}}, ...]}}

데이터/근거가 전혀 없으면:
   {{"content_type": "none"}}

규칙:
- 글에 실제로 언급된 내용만 사용해. 숫자든 절차든 지어내지 마.
- "절반을 넘긴", "두 배 가까이" 같은 정성적 비교 표현은 맥락을 벗어나지 않는 선에서 근사치로 변환 가능
  (bar/line/donut에서 이 경우 is_approx를 true로). stat/table/process는 텍스트 그대로 요약하면 되므로
  is_approx가 필요 없음.
- 후보가 여러 개 가능하면 가장 읽는 사람이 이해하기 쉽고 글의 핵심을 잘 담는 것 하나만 선택해.
- hook은 카드 제목 역할, 25자 이내, 이모지 1개 포함.
- stat_value는 "800만원", "3배", "1위"처럼 단위까지 포함한 문자열로.
- table_rows는 2~5개, table_columns는 정확히 2개.
- process_steps는 3~5개, 각 title은 10자 이내, desc는 20자 이내, emoji는 단계 내용에 맞는 것 1개.
- 형태 선택 우선순위: 방법/확인법/절차 글이면 process, 두 대상 비교면 table, 대표 숫자 하나면 stat,
  비교 가능한 수치 세트가 있으면 bar/line/donut. 글의 핵심 메시지를 가장 잘 보여주는 것을 골라.

아래 JSON 형식으로만 답해. 다른 설명 없이 JSON만 출력."""

    try:
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # 코드블록으로 감싸져 오는 경우 방어
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)

        content_type = data.get("content_type")
        if content_type not in CONTENT_TYPES:
            return None

        hook = _esc(data.get("hook", ""), 40)

        if content_type in ("bar", "line", "donut"):
            labels = data.get("labels") or []
            values = data.get("values") or []
            if len(labels) < 2 or len(labels) != len(values):
                return None
            return {
                "content_type": content_type,
                "hook": hook,
                "labels": [_esc(l, 14) for l in labels],
                "values": [float(v) for v in values],
                "unit": _esc(data.get("unit", ""), 6),
                "is_approx": bool(data.get("is_approx", False)),
            }

        if content_type == "stat":
            stat_value = str(data.get("stat_value", "")).strip()
            stat_label = str(data.get("stat_label", "")).strip()
            if not stat_value or not stat_label:
                return None
            return {
                "content_type": "stat",
                "hook": hook,
                "stat_value": _esc(stat_value, 16),
                "stat_label": _esc(stat_label, 30),
                "stat_icon": _esc(data.get("stat_icon", "📌"), 8) or "📌",
            }

        if content_type == "table":
            columns = data.get("table_columns") or []
            rows = data.get("table_rows") or []
            if len(columns) != 2 or not (2 <= len(rows) <= 5):
                return None
            clean_rows = []
            for r in rows:
                vals = r.get("values") or []
                if len(vals) != 2:
                    return None
                clean_rows.append({"label": _esc(r.get("label", ""), 14), "values": [_esc(v, 14) for v in vals]})
            return {
                "content_type": "table",
                "hook": hook,
                "table_columns": [_esc(c, 10) for c in columns],
                "table_rows": clean_rows,
            }

        if content_type == "process":
            steps = data.get("process_steps") or []
            if not (3 <= len(steps) <= 5):
                return None
            clean_steps = [
                {
                    "emoji": _esc(s.get("emoji", ""), 8),
                    "title": _esc(s.get("title", ""), 12),
                    "desc": _esc(s.get("desc", ""), 24),
                }
                for s in steps
            ]
            return {
                "content_type": "process",
                "hook": hook,
                "process_steps": clean_steps,
            }

        return None
    except Exception as e:
        print(f"시각 콘텐츠 추출 실패(폴백 예정): {e}")
        return None


# ── 차트 렌더링 (SVG) ────────────────────────────────

def _render_bar_chart_svg(labels: list, values: list, unit: str,
                           w: int = 920, h: int = 460, x0: int = 0, y0: int = 0) -> str:
    n = len(values)
    max_v = max(values) * 1.15 if max(values) > 0 else 1
    max_idx = values.index(max(values))
    plot_h = h - 90
    gap = 28
    bar_w = (w - gap * (n - 1)) / n

    defs = []
    bars = []
    for i, (label, v) in enumerate(zip(labels, values)):
        bar_h = max((v / max_v) * plot_h, 4)
        x = x0 + i * (bar_w + gap)
        y = y0 + plot_h - bar_h
        is_max = i == max_idx
        gid = f"barGrad{i}"

        if is_max:
            defs.append(f'''
                <linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="{BRAND_GOLD_LIGHT}"/>
                    <stop offset="100%" stop-color="{BRAND_GOLD}"/>
                </linearGradient>
            ''')
            glow = f'<rect x="{x-10:.1f}" y="{y-10:.1f}" width="{bar_w+20:.1f}" height="{bar_h+10:.1f}" rx="16" fill="{BRAND_GOLD}" opacity="0.35" filter="blur(20px)"/>'
            fill = f"url(#{gid})"
            value_fill = BRAND_GOLD_LIGHT
            value_size = 44
        else:
            defs.append(f'''
                <linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="rgba(255,255,255,0.28)"/>
                    <stop offset="100%" stop-color="rgba(255,255,255,0.12)"/>
                </linearGradient>
            ''')
            glow = ""
            fill = f"url(#{gid})"
            value_fill = "rgba(255,255,255,0.75)"
            value_size = 36

        bars.append(f'''
            {glow}
            <rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="12" fill="{fill}"/>
            <text x="{x + bar_w/2:.1f}" y="{y - 20}" text-anchor="middle"
                  font-family="{FONT}" font-size="{value_size}" font-weight="800" fill="{value_fill}">{v:g}{unit}</text>
            <text x="{x + bar_w/2:.1f}" y="{y0 + plot_h + 46}" text-anchor="middle"
                  font-family="{FONT}" font-size="27" font-weight="600" fill="rgba(255,255,255,0.6)">{label}</text>
        ''')

    baseline = f'<line x1="{x0}" y1="{y0+plot_h}" x2="{x0+w}" y2="{y0+plot_h}" stroke="rgba(255,255,255,0.18)" stroke-width="2"/>'
    return f"<defs>{''.join(defs)}</defs>" + baseline + "".join(bars)


def _render_line_chart_svg(labels: list, values: list, unit: str,
                            w: int = 920, h: int = 460, x0: int = 0, y0: int = 0) -> str:
    n = len(values)
    max_v = max(values)
    min_v = min(values)
    span = (max_v - min_v) or 1
    plot_h = h - 90
    pad = 0.15 * span
    lo, hi = min_v - pad, max_v + pad

    def px(i):
        return x0 + (i / (n - 1)) * w if n > 1 else x0 + w / 2

    def py(v):
        return y0 + plot_h - ((v - lo) / (hi - lo)) * plot_h

    pts = [(px(i), py(v)) for i, v in enumerate(values)]
    path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area_d = path_d + f" L {pts[-1][0]:.1f} {y0+plot_h} L {pts[0][0]:.1f} {y0+plot_h} Z"

    dots = []
    labels_svg = []
    for i, ((x, y), label, v) in enumerate(zip(pts, labels, values)):
        is_last = i == n - 1
        r = 13 if is_last else 7
        fill = BRAND_GOLD_LIGHT if is_last else "rgba(255,255,255,0.85)"
        if is_last:
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="26" fill="{BRAND_GOLD}" opacity="0.4" filter="blur(12px)"/>')
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}"/>')
        if is_last:
            dots.append(
                f'<text x="{x:.1f}" y="{y-30:.1f}" text-anchor="end" '
                f'font-family="{FONT}" font-size="46" font-weight="800" fill="{BRAND_GOLD_LIGHT}">{v:g}{unit}</text>'
            )
        labels_svg.append(
            f'<text x="{x:.1f}" y="{y0+plot_h+44}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="24" font-weight="500" fill="rgba(255,255,255,0.6)">{label}</text>'
        )

    baseline = f'<line x1="{x0}" y1="{y0+plot_h}" x2="{x0+w}" y2="{y0+plot_h}" stroke="rgba(255,255,255,0.18)" stroke-width="2"/>'

    return f'''
        <defs>
            <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="{BRAND_GOLD}" stop-opacity="0.55"/>
                <stop offset="55%" stop-color="{BRAND_GOLD}" stop-opacity="0.12"/>
                <stop offset="100%" stop-color="{BRAND_GOLD}" stop-opacity="0"/>
            </linearGradient>
            <linearGradient id="lineStroke" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stop-color="{BRAND_GOLD_DIM}"/>
                <stop offset="100%" stop-color="{BRAND_GOLD_LIGHT}"/>
            </linearGradient>
        </defs>
        <path d="{area_d}" fill="url(#areaFill)"/>
        <path d="{path_d}" fill="none" stroke="url(#lineStroke)" stroke-width="7" stroke-linejoin="round" stroke-linecap="round"
              filter="drop-shadow(0 0 14px rgba(201,168,75,0.55))"/>
        {"".join(dots)}
        {baseline}
        {"".join(labels_svg)}
    '''


def _render_donut_chart_svg(labels: list, values: list, unit: str,
                             cx: int = 460, cy: int = 250, r: int = 190) -> str:
    total = sum(values) or 1
    colors = [BRAND_GOLD, "rgba(255,255,255,0.55)", BRAND_GOLD_DIM, "rgba(255,255,255,0.28)"]

    segs = []
    glow = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{BRAND_GOLD}" stroke-width="74" opacity="0.25" filter="blur(18px)"/>'
    start_angle = -90
    stroke_w = 64
    circumference = 2 * 3.14159265 * r

    labels_svg = []
    for i, (label, v) in enumerate(zip(labels[:4], values[:4])):
        frac = v / total
        seg_len = frac * circumference
        gap_len = circumference - seg_len
        rotate = start_angle
        seg_gap = max(circumference * 0.006, 3)
        segs.append(f'''
            <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colors[i % len(colors)]}"
                    stroke-width="{stroke_w}" stroke-dasharray="{seg_len - seg_gap:.1f} {gap_len + seg_gap:.1f}"
                    transform="rotate({rotate} {cx} {cy})" stroke-linecap="round"/>
        ''')
        start_angle += frac * 360
        ly = cy - 90 + i * 56
        lx = cx + r + 70
        labels_svg.append(f'''
            <rect x="{lx}" y="{ly-24}" width="26" height="26" rx="6" fill="{colors[i % len(colors)]}"/>
            <text x="{lx+40}" y="{ly-3}" font-family="{FONT}" font-size="30" font-weight="700" fill="#ffffff">{label}</text>
            <text x="{lx+40}" y="{ly+28}" font-family="{FONT}" font-size="26" font-weight="500" fill="rgba(255,255,255,0.6)">{v:g}{unit}</text>
        ''')

    center_label = f'''
        <text x="{cx}" y="{cy-8}" text-anchor="middle" font-family="{FONT}" font-size="30" font-weight="600" fill="rgba(255,255,255,0.6)">합계</text>
        <text x="{cx}" y="{cy+38}" text-anchor="middle" font-family="{FONT}" font-size="44" font-weight="800" fill="#ffffff">{total:g}{unit}</text>
    '''

    return glow + "".join(segs) + center_label + "".join(labels_svg)


def _screenshot(html: str, out_path: Path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1080})
        page.set_content(html)
        page.screenshot(path=str(out_path))
        browser.close()


def _card_shell(hook: str, category: str, middle_html: str, extra_style: str = "",
                 approx_note: bool = False) -> str:
    """모든 인포그래픽 카드가 공유하는 배경/헤더/푸터 틀. middle_html만 형태별로 달라짐."""
    return f"""
    <html>
    <head>
    <style>
        body {{ margin: 0; }}
        .card {{
            position: relative;
            width: 1080px;
            height: 1080px;
            background:
                radial-gradient(ellipse 900px 600px at 50% 68%, rgba(201,168,75,0.16) 0%, rgba(201,168,75,0) 65%),
                radial-gradient(ellipse 700px 500px at 90% 5%, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0) 60%),
                linear-gradient(160deg, #2c4270 0%, {BRAND_NAVY} 45%, {BRAND_NAVY_DEEP} 100%);
            overflow: hidden;
            box-sizing: border-box;
            font-family: {FONT};
        }}
        .content {{
            position: relative;
            height: 100%;
            display: flex;
            flex-direction: column;
            padding: 64px 80px 56px;
            box-sizing: border-box;
        }}
        .eyebrow {{
            display: flex;
            align-items: center;
            gap: 14px;
            color: {BRAND_GOLD};
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }}
        .eyebrow .dot {{ width: 9px; height: 9px; border-radius: 50%; background: {BRAND_GOLD}; }}
        .hook {{
            color: #ffffff;
            font-size: 54px;
            font-weight: 800;
            line-height: 1.32;
            word-break: keep-all;
            letter-spacing: -1px;
            margin-top: 22px;
            max-width: 920px;
        }}
        .mid-wrap {{
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-top: 20px;
        }}
        .footer {{
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .badge {{
            background: {BRAND_GOLD};
            color: {BRAND_NAVY_DEEP};
            font-size: 26px;
            font-weight: 700;
            padding: 12px 28px;
            border-radius: 999px;
        }}
        .wordmark {{ color: rgba(255,255,255,0.5); font-size: 24px; font-weight: 500; }}
        .approx-note {{ color: rgba(255,255,255,0.4); font-size: 20px; font-weight: 500; margin-top: 6px; }}
        {extra_style}
    </style>
    </head>
    <body>
        <div class="card">
            <div class="content">
                <div class="eyebrow"><span class="dot"></span>WhiteCoffee · 주식농사</div>
                <div class="hook">{hook}</div>
                <div class="mid-wrap">
                    {middle_html}
                </div>
                <div class="footer">
                    <div>
                        <div class="badge">{category}</div>
                        {'<div class="approx-note">* 본문 내용을 바탕으로 한 추정치</div>' if approx_note else ''}
                    </div>
                    <div class="wordmark">ideas07576.tistory.com</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


def render_data_card(hook: str, category: str, chart: dict, out_path: Path):
    """막대/라인/도넛 차트 카드. chart: extract_visual_content가 content_type in (bar,line,donut)일 때 반환한 dict."""
    content_type = chart["content_type"]
    labels, values, unit = chart["labels"], chart["values"], chart["unit"]

    if content_type == "bar":
        chart_svg = _render_bar_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 460", 460
    elif content_type == "line":
        chart_svg = _render_line_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 460", 460
    else:
        chart_svg = _render_donut_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 500", 500

    middle_html = f'''
        <svg width="920" height="{chart_h_css}" viewBox="{chart_viewbox}" xmlns="http://www.w3.org/2000/svg">
            {chart_svg}
        </svg>
    '''
    html = _card_shell(hook, category, middle_html, approx_note=chart.get("is_approx", False))
    _screenshot(html, out_path)


def render_stat_card(hook: str, category: str, stat: dict, out_path: Path):
    """핵심 지표 강조형 카드. stat: {"stat_value", "stat_label", "stat_icon"}."""
    extra_style = f"""
        .stat-box {{ display: flex; flex-direction: column; align-items: center; text-align: center; gap: 20px; }}
        .stat-icon {{ font-size: 160px; line-height: 1; filter: drop-shadow(0 0 40px rgba(201,168,75,0.45)); }}
        .stat-value {{
            font-size: 140px; font-weight: 800; color: {BRAND_GOLD_LIGHT};
            text-shadow: 0 0 50px rgba(201,168,75,0.5); letter-spacing: -2px; line-height: 1;
        }}
        .stat-label {{ font-size: 34px; font-weight: 600; color: rgba(255,255,255,0.75); max-width: 760px; word-break: keep-all; }}
    """
    middle_html = f'''
        <div class="stat-box">
            <div class="stat-icon">{stat["stat_icon"]}</div>
            <div class="stat-value">{stat["stat_value"]}</div>
            <div class="stat-label">{stat["stat_label"]}</div>
        </div>
    '''
    html = _card_shell(hook, category, middle_html, extra_style=extra_style)
    _screenshot(html, out_path)


def render_table_card(hook: str, category: str, table: dict, out_path: Path):
    """비교표 카드. table: {"table_columns": [2개], "table_rows": [{"label","values":[2개]}, ...]}."""
    cols = table["table_columns"]
    rows = table["table_rows"]

    extra_style = f"""
        .cmp-table {{ width: 900px; border-collapse: separate; border-spacing: 0 14px; }}
        .cmp-table th {{
            font-size: 28px; font-weight: 700; color: {BRAND_NAVY_DEEP}; text-align: center;
            background: {BRAND_GOLD}; padding: 18px 0; border-radius: 12px;
        }}
        .cmp-table th:first-child {{ background: transparent; }}
        .cmp-table td {{
            font-size: 30px; font-weight: 600; color: #ffffff; text-align: center;
            background: rgba(255,255,255,0.08); padding: 22px 12px;
        }}
        .cmp-table td:first-child {{
            text-align: left; padding-left: 28px; color: rgba(255,255,255,0.6); font-weight: 500; font-size: 26px;
            background: transparent;
        }}
        .cmp-table tr td:nth-child(2) {{ border-radius: 12px 0 0 12px; }}
        .cmp-table tr td:last-child {{ border-radius: 0 12px 12px 0; }}
    """
    header = f"<th></th><th>{cols[0]}</th><th>{cols[1]}</th>"
    body_rows = "".join(
        f'<tr><td>{r["label"]}</td><td>{r["values"][0]}</td><td>{r["values"][1]}</td></tr>'
        for r in rows
    )
    middle_html = f'''
        <table class="cmp-table">
            <thead><tr>{header}</tr></thead>
            <tbody>{body_rows}</tbody>
        </table>
    '''
    html = _card_shell(hook, category, middle_html, extra_style=extra_style)
    _screenshot(html, out_path)


def render_process_card(hook: str, category: str, process: dict, out_path: Path):
    """순서도/체크리스트 카드. process: {"process_steps": [{"title","desc"}, ...]} (3~5단계)."""
    steps = process["process_steps"]

    extra_style = f"""
        .steps {{ display: flex; flex-direction: column; gap: 0; width: 880px; }}
        .step {{ display: flex; align-items: flex-start; gap: 28px; position: relative; padding-bottom: 44px; }}
        .step:last-child {{ padding-bottom: 0; }}
        .step-num {{
            flex-shrink: 0; width: 64px; height: 64px; border-radius: 50%;
            background: linear-gradient(160deg, {BRAND_GOLD_LIGHT}, {BRAND_GOLD});
            color: {BRAND_NAVY_DEEP}; font-size: 30px; font-weight: 800;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 0 24px rgba(201,168,75,0.4); z-index: 1;
        }}
        .step-connector {{
            position: absolute; left: 31px; top: 64px; width: 2px; bottom: -8px;
            background: rgba(201,168,75,0.35);
        }}
        .step-text {{ padding-top: 10px; }}
        .step-title {{ font-size: 34px; font-weight: 700; color: #ffffff; }}
        .step-desc {{ font-size: 25px; font-weight: 500; color: rgba(255,255,255,0.6); margin-top: 6px; }}
    """
    step_html = []
    for i, s in enumerate(steps):
        is_last = i == len(steps) - 1
        connector = "" if is_last else '<div class="step-connector"></div>'
        step_html.append(f'''
            <div class="step">
                <div class="step-num">{i + 1}</div>
                {connector}
                <div class="step-text">
                    <div class="step-title">{(s.get("emoji") + " ") if s.get("emoji") else ""}{s["title"]}</div>
                    <div class="step-desc">{s["desc"]}</div>
                </div>
            </div>
        ''')
    middle_html = f'<div class="steps">{"".join(step_html)}</div>'
    html = _card_shell(hook, category, middle_html, extra_style=extra_style)
    _screenshot(html, out_path)


def render_card_image(hook: str, category: str, out_path: Path):
    """차트로 만들 데이터가 없을 때 쓰는 기존 방식(후킹 문구 카드) — 폴백용."""
    import random
    random.seed(hook)
    bars = []
    x = 0
    bar_w = 46
    gap = 22
    base_y = 1080
    for i in range(16):
        h = random.randint(90, 420)
        wick_h = h + random.randint(20, 60)
        y = base_y - h
        wick_x = x + bar_w / 2
        bars.append(
            f'<line x1="{wick_x}" y1="{base_y - wick_h}" x2="{wick_x}" y2="{base_y}" '
            f'stroke="{BRAND_GOLD}" stroke-width="2" opacity="0.10"/>'
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" fill="{BRAND_GOLD}" opacity="0.08"/>'
        )
        x += bar_w + gap
    candlesticks_svg = "".join(bars)

    html = f"""
    <html>
    <head>
    <style>
        body {{ margin: 0; }}
        .card {{
            position: relative;
            width: 1080px;
            height: 1080px;
            background: linear-gradient(160deg, #24365c 0%, {BRAND_NAVY} 55%, #16223b 100%);
            overflow: hidden;
            box-sizing: border-box;
            font-family: {FONT};
        }}
        .bg-chart {{
            position: absolute;
            bottom: 0;
            left: 0;
            width: 1080px;
            height: 1080px;
        }}
        .content {{
            position: relative;
            z-index: 2;
            height: 100%;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 80px 76px;
            box-sizing: border-box;
        }}
        .eyebrow {{
            display: flex;
            align-items: center;
            gap: 14px;
            color: {BRAND_GOLD};
            font-size: 30px;
            font-weight: 700;
            letter-spacing: 1px;
        }}
        .eyebrow .dot {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: {BRAND_GOLD};
        }}
        .hook-wrap {{
            display: flex;
            flex-direction: column;
            gap: 28px;
        }}
        .hook {{
            color: #ffffff;
            font-size: 72px;
            font-weight: 800;
            line-height: 1.42;
            word-break: keep-all;
            letter-spacing: -1px;
        }}
        .underline {{
            width: 120px;
            height: 8px;
            background: {BRAND_GOLD};
            border-radius: 4px;
        }}
        .footer {{
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .badge {{
            background: {BRAND_GOLD};
            color: {BRAND_NAVY};
            font-size: 28px;
            font-weight: 700;
            padding: 14px 30px;
            border-radius: 999px;
        }}
        .wordmark {{
            color: rgba(255,255,255,0.55);
            font-size: 26px;
            font-weight: 500;
        }}
    </style>
    </head>
    <body>
        <div class="card">
            <svg class="bg-chart" viewBox="0 0 1080 1080" xmlns="http://www.w3.org/2000/svg">
                {candlesticks_svg}
            </svg>
            <div class="content">
                <div class="eyebrow"><span class="dot"></span>WhiteCoffee · 주식농사</div>
                <div class="hook-wrap">
                    <div class="hook">{hook}</div>
                    <div class="underline"></div>
                </div>
                <div class="footer">
                    <div class="badge">{category}</div>
                    <div class="wordmark">ideas07576.tistory.com</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1080})
        page.set_content(html)
        page.screenshot(path=str(out_path))
        browser.close()


def commit_and_push_image(path: Path) -> bool:
    """생성된 카드 이미지를 리포지토리에 커밋 + 푸시. Threads가 URL로 접근하려면 푸시가 먼저 끝나 있어야 함."""
    try:
        subprocess.run(["git", "add", str(path)], check=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"카드 이미지 추가: {path.name}"],
            capture_output=True, text=True
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            print(f"커밋 실패: {result.stdout}\n{result.stderr}")
            return False
        subprocess.run(["git", "push"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"git 커밋/푸시 실패: {e}")
        return False


def build_raw_url(path: Path) -> str:
    return f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/{path.as_posix()}"


def make_card_image_url(post: dict) -> str | None:
    """카드 이미지 생성 파이프라인.

    1) 글 내용에 맞는 인포그래픽 형태(차트/숫자/비교표/단계) 추출 → 해당 카드로 렌더링
    2) 어떤 형태도 못 만들면 기존 후킹 문구 카드로 폴백
    3) 렌더링 → 커밋/푸시 → 공개 URL 반환 (실패 시 None)
    """
    try:
        IMAGES_DIR.mkdir(exist_ok=True)
        filename = f"{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.png"
        out_path = IMAGES_DIR / filename
        category = post.get("category", "")

        visual = extract_visual_content(post)
        rendered = False
        if visual:
            ctype = visual["content_type"]
            print(f"카드 형태 선택됨: {ctype}")
            try:
                if ctype in ("bar", "line", "donut"):
                    render_data_card(visual["hook"], category, visual, out_path)
                elif ctype == "stat":
                    render_stat_card(visual["hook"], category, visual, out_path)
                elif ctype == "table":
                    render_table_card(visual["hook"], category, visual, out_path)
                elif ctype == "process":
                    render_process_card(visual["hook"], category, visual, out_path)
                rendered = out_path.exists()
            except Exception as e:
                print(f"인포그래픽 렌더링 실패(폴백 예정): {e}")

        if not rendered:
            print("인포그래픽으로 만들 내용 없음 — 후킹 문구 카드로 폴백")
            hook = html.escape(generate_card_hook(post))
            render_card_image(hook, category, out_path)

        if not commit_and_push_image(out_path):
            return None

        return build_raw_url(out_path)
    except Exception as e:
        print(f"카드 이미지 생성 실패: {e}")
        return None


def wait_until_ready(container_id: str, max_wait_sec: int = 60, interval_sec: int = 5) -> bool:
    """컨테이너가 FINISHED 상태가 될 때까지 폴링. 링크 미리보기 생성 등 비동기 처리를 기다림."""
    status_url = f"https://graph.threads.net/v1.0/{container_id}"
    waited = 0
    time.sleep(5)
    waited += 5
    while waited <= max_wait_sec:
        res = requests.get(status_url, params={
            "fields":       "status,error_message",
            "access_token": THREADS_TOKEN,
        })
        if res.status_code == 200:
            status = res.json().get("status")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                print(f"컨테이너 처리 오류: {res.json().get('error_message')}")
                return False
        else:
            print(f"상태 조회 실패: {res.text}")
        time.sleep(interval_sec)
        waited += interval_sec
    print("컨테이너 처리 시간 초과")
    return False


def create_and_publish(text: str, reply_to_id: str = None, image_url: str = None) -> str | None:
    """미디어 컨테이너 생성 → 상태 대기 → 발행. 성공 시 발행된 게시물 id 반환, 실패 시 None."""
    create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    payload = {
        "access_token": THREADS_TOKEN,
    }
    if image_url:
        payload["media_type"] = "IMAGE"
        payload["image_url"] = image_url
        payload["text"] = text
    else:
        payload["media_type"] = "TEXT"
        payload["text"] = text
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id

    res = requests.post(create_url, data=payload)
    if res.status_code != 200:
        print(f"컨테이너 생성 실패: {res.text}")
        return None

    container_id = res.json().get("id")

    if not wait_until_ready(container_id):
        return None

    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    res2 = requests.post(publish_url, data={
        "creation_id":  container_id,
        "access_token": THREADS_TOKEN,
    })
    if res2.status_code != 200:
        print(f"발행 실패: {res2.text}")
        return None

    return res2.json().get("id")


def post_to_threads(text: str, link: str, image_url: str = None) -> bool:
    post_id = create_and_publish(text, image_url=image_url)
    if not post_id:
        return False

    reply_id = create_and_publish(link, reply_to_id=post_id)
    if not reply_id:
        print("본문은 발행됐지만 링크 댓글 발행에 실패했습니다.")

    return True


def publish_one(post: dict) -> bool:
    print(f"\n처리 중: {post['title']} [{post.get('category', '')}]")
    try:
        threads_text = generate_threads_post(post)
        print(f"생성된 홍보글:\n{threads_text}\n")

        image_url = make_card_image_url(post)
        if image_url:
            print(f"카드 이미지 URL: {image_url}")
        else:
            print("카드 이미지 생성/업로드 실패 — 텍스트만 발행합니다.")

        success = post_to_threads(threads_text, post["link"], image_url=image_url)
        if success:
            print(f"발행 완료: {post['title']}")
        else:
            print(f"발행 실패: {post['title']}")
        return success
    except Exception as e:
        print(f"오류: {e}")
        return False


def main():
    data = load_data()
    published_count = 0
    today = datetime.now(KST).date()
    all_posts = fetch_rss()

    short_term_new = [
        p for p in all_posts
        if p["category"] == SHORT_TERM_CATEGORY
        and p["link"] not in data["short_term_done"]
        and p["pub_date"] == today
    ]

    if short_term_new and published_count < POSTS_PER_RUN:
        post = short_term_new[0]
        if publish_one(post):
            data["short_term_done"].add(post["link"])
            published_count += 1

    remaining = POSTS_PER_RUN - published_count
    if remaining > 0:
        cycle_candidates = [
            p for p in reversed(all_posts)
            if p["category"] != SHORT_TERM_CATEGORY
            and p["link"] not in data["cycle_published"]
        ]

        if not cycle_candidates:
            print("순환 대상 글을 모두 발행함. 기록 초기화 후 다시 시작합니다.")
            data["cycle_published"] = set()
            cycle_candidates = [
                p for p in reversed(all_posts)
                if p["category"] != SHORT_TERM_CATEGORY
            ]

        for post in cycle_candidates[:remaining]:
            if publish_one(post):
                data["cycle_published"].add(post["link"])
                published_count += 1

    if published_count == 0:
        print("오늘 발행할 글이 없습니다.")

    save_data(data)
    print(f"\n완료! 총 {published_count}개 발행")


if __name__ == "__main__":
    main()
