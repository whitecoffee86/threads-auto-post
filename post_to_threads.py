import os
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


def extract_chart_data(post: dict) -> dict | None:
    """글 속에서 카드에 그대로 시각화할 수 있는 실제 수치 데이터를 추출.

    반환 형식 (데이터가 없으면 None):
    {
        "chart_type": "bar" | "line" | "donut",
        "hook": "차트 제목 역할을 하는 한 줄 후킹 문구 (25자 이내)",
        "labels": ["라벨1", "라벨2", ...],   # 2~6개
        "values": [12.3, 45.6, ...],          # labels와 같은 길이, 숫자만
        "unit": "%" | "억" | "만원" | "" 등
    }

    - bar: 서로 다른 대상(지역/상품/연도 등)의 수치 비교
    - line: 시간 흐름에 따른 추이 (3개 이상 시점)
    - donut: 구성비/배분 (합이 의미 있는 비율 데이터)
    실제로 글에 명시된 숫자가 없으면 지어내지 말고 반드시 None을 반환.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글에서 SNS 카드 이미지에 '차트'로 그대로 그릴 수 있는 실제 수치 데이터를 찾아줘.

글 제목: {post['title']}
내용 요약: {post['summary']}

규칙:
1. 글에 실제로 언급된 숫자만 사용해. 절대로 숫자를 지어내거나 추정하지 마.
2. 비교 가능한 숫자 세트(예: 지역별 가격, 연도별 수익률, 항목별 비중 등)가 2개 이상 있어야 차트로 만들 수 있어.
3. 그런 데이터가 없으면 (예: 숫자가 거의 없거나 서로 비교 불가능한 경우) 반드시 has_data를 false로 해.
4. chart_type 선택 기준:
   - "bar": 서로 다른 대상 간의 수치 비교 (지역, 상품, 항목 등)
   - "line": 시간 흐름에 따른 추이 (최소 3개 시점)
   - "donut": 전체 대비 구성비/배분 (합쳐서 의미 있는 비율)
5. labels는 2~6개, values는 labels와 개수가 같아야 함.
6. hook은 차트 제목처럼 쓰일 한 줄 문구, 25자 이내, 이모지 1개 포함.

아래 JSON 형식으로만 답해. 다른 설명 없이 JSON만 출력.

{{"has_data": true, "chart_type": "bar", "hook": "...", "labels": ["...", "..."], "values": [0, 0], "unit": "억"}}

또는 데이터가 없으면:
{{"has_data": false}}"""

    try:
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
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

        if not data.get("has_data"):
            return None
        if data.get("chart_type") not in ("bar", "line", "donut"):
            return None
        labels = data.get("labels") or []
        values = data.get("values") or []
        if len(labels) < 2 or len(labels) != len(values):
            return None
        values = [float(v) for v in values]

        return {
            "chart_type": data["chart_type"],
            "hook": str(data.get("hook", ""))[:40],
            "labels": [str(l) for l in labels],
            "values": values,
            "unit": str(data.get("unit", "")),
        }
    except Exception as e:
        print(f"차트 데이터 추출 실패(폴백 예정): {e}")
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


def render_data_card(hook: str, category: str, chart: dict, out_path: Path):
    """후킹 문구 + 실제 데이터 차트를 담은 카드 (데이터가 있는 경우)."""
    chart_type = chart["chart_type"]
    labels, values, unit = chart["labels"], chart["values"], chart["unit"]

    if chart_type == "bar":
        chart_svg = _render_bar_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 460", 460
    elif chart_type == "line":
        chart_svg = _render_line_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 460", 460
    else:
        chart_svg = _render_donut_chart_svg(labels, values, unit)
        chart_viewbox, chart_h_css = "0 0 920 500", 500

    html = f"""
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
        .chart-wrap {{
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
    </style>
    </head>
    <body>
        <div class="card">
            <div class="content">
                <div class="eyebrow"><span class="dot"></span>WhiteCoffee · 주식농사</div>
                <div class="hook">{hook}</div>
                <div class="chart-wrap">
                    <svg width="920" height="{chart_h_css}" viewBox="{chart_viewbox}" xmlns="http://www.w3.org/2000/svg">
                        {chart_svg}
                    </svg>
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

    1) 글에서 실제 수치 데이터 추출 시도 → 있으면 차트 카드
    2) 없으면 기존 후킹 문구 카드로 폴백
    3) 렌더링 → 커밋/푸시 → 공개 URL 반환 (실패 시 None)
    """
    try:
        IMAGES_DIR.mkdir(exist_ok=True)
        filename = f"{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.png"
        out_path = IMAGES_DIR / filename

        chart = extract_chart_data(post)
        if chart:
            print(f"차트 데이터 추출됨 ({chart['chart_type']}): {chart['labels']}")
            render_data_card(chart["hook"], post.get("category", ""), chart, out_path)
        else:
            print("차트로 만들 데이터 없음 — 후킹 문구 카드로 폴백")
            hook = generate_card_hook(post)
            render_card_image(hook, post.get("category", ""), out_path)

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
