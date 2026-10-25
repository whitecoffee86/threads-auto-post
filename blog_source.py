"""티스토리 글 재료 — RSS(최신 10개) + 사이트맵(전체 글).

2026-10-20: RSS 만 읽어서 스레드·인스타가 최신 글 10개 안에서만 돌던 문제 해결.
사이트맵에는 글 300여 개가 모두 있으므로, 새 글(RSS)을 먼저 쓰고 그다음 과거 글을 섞어 쓴다.
과거 글에는 지금은 바뀌었을 수 있는 수치(세율·금리·한도)가 남아 있을 수 있어
PAST_NOTE 를 본문 앞에 붙여 생성 프롬프트가 그런 숫자를 피하게 한다.
"""
import html
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import feedparser
import requests

BLOG = "https://ideas07576.tistory.com"
RSS_URL = f"{BLOG}/rss"
SITEMAP_URL = f"{BLOG}/sitemap.xml"
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (WhiteCoffee auto-post)"}

PAST_NOTE = ("[주의: 예전에 쓴 글이다. 세율·금리·한도·가격·제도처럼 지금은 바뀌었을 수 있는 수치나 규정은 쓰지 말고, "
             "원리·경험·판단 포인트 위주로 써라. 지난 수익률 같은 과거 데이터는 '당시 기준'임이 드러나게 써라.]\n")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def rss_posts() -> list[dict]:
    """RSS 최신 글(카테고리·요약 포함). 기존 코드와 같은 필드."""
    out = []
    for e in feedparser.parse(RSS_URL).entries:
        tags = [t.term for t in getattr(e, "tags", [])]
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).astimezone(KST).date()
        out.append({"title": html.unescape(e.title), "link": e.link.rstrip("/"),
                    "summary": _clean(e.get("summary", ""))[:800],
                    "category": tags[0] if tags else "", "pub_date": pub, "past": False})
    return out


def sitemap_links() -> list[dict]:
    """사이트맵의 글 주소 전체 [{link, lastmod}] (글 번호 주소만)."""
    try:
        xml = requests.get(SITEMAP_URL, timeout=30, headers=UA).text
    except Exception as e:
        print(f"사이트맵 읽기 실패(RSS 만 사용): {e}")
        return []
    items = re.findall(r"<loc>(https://ideas07576\.tistory\.com/\d+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?", xml)
    return [{"link": u.rstrip("/"), "lastmod": (m or "")[:10]} for u, m in items]


def fetch_detail(link: str) -> dict | None:
    """글 페이지에서 제목·카테고리·본문 앞부분. 실패하면 None."""
    try:
        page = requests.get(link, timeout=30, headers=UA).text
    except Exception as e:
        print(f"글 읽기 실패: {link} ({e})")
        return None
    t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = html.unescape(t.group(1)).strip() if t else ""
    cats = re.findall(r'href="/category/([^"?#]+)"', page)
    category = ""
    if cats:  # 글의 카테고리 링크가 본문 위·아래에 두 번 나와 가장 많이 나온 것이 이 글의 카테고리
        category = unquote(max(set(cats), key=cats.count)).split("/")[0]
    m = re.search(r'<div[^>]+class="[^"]*(tt_article_useless_p_margin|contents_style|entry-content)[^"]*"', page)
    body = page[m.start():] if m else ""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"^반응형\s*", "", _clean(body))
    cut = text.find("함께 보면 좋은 글")
    if cut > 0:
        text = text[:cut]
    if not text:
        d = re.search(r'<meta property="og:description" content="([^"]*)"', page)
        text = html.unescape(d.group(1)) if d else ""
    if not title or not text:
        return None
    return {"title": title, "link": link.rstrip("/"), "category": category, "summary": text[:800],
            "body": text[:14000], "pub_date": None}


RESULT_TITLE = re.compile(r"결산|수익\s*:|매매\s*일지")


def candidates(done: set, skip_categories: set, recent_days: int = 7, k: int = 1,
               rss: list | None = None, fresh_days: int | None = None) -> list[dict]:
    """아직 안 쓴 글 k개. 1순위 최근 RSS 새 글, 2순위 사이트맵 과거 글(무작위 — 주제가 골고루 섞이게).
    fresh_days 를 주면 사이트맵 수정일이 그 안인 과거 글만 쓴다(인스타처럼 숫자가 많은 캐러셀용)."""
    done = {d.rstrip("/") for d in done}
    rss = rss if rss is not None else rss_posts()
    today = datetime.now(KST).date()
    out = []
    for p in rss:
        if p["link"] in done or p["category"] in skip_categories:
            continue
        if p["pub_date"] and (today - p["pub_date"]).days <= recent_days:
            out.append(p)
        if len(out) >= k:
            return out
    rss_links = {p["link"] for p in rss}
    pool = [s for s in sitemap_links() if s["link"] not in done and s["link"] not in rss_links]
    if fresh_days is not None:
        cutoff = (today - timedelta(days=fresh_days)).isoformat()
        pool = [s for s in pool if s["lastmod"] >= cutoff]
    # RSS 에 있지만 일주일 넘은 글도 과거 글 후보로
    pool += [{"link": p["link"], "lastmod": ""} for p in rss
             if p["link"] not in done and p["category"] not in skip_categories and p not in out]
    random.shuffle(pool)
    for s in pool[:k * 6]:  # 카테고리 제외·읽기 실패를 감안해 넉넉히 시도
        d = next((p for p in rss if p["link"] == s["link"]), None) or fetch_detail(s["link"])
        if not d or d["category"] in skip_categories or RESULT_TITLE.search(d.get("title", "")):
            continue  # 2026-10-25: 월 결산·수익 공개 글(계좌 손익 숫자)은 과거 글 재료에서 뺌
        d = dict(d, past=True, summary=PAST_NOTE + d["summary"])
        out.append(d)
        if len(out) >= k:
            break
    return out


def total_posts() -> int:
    return len(sitemap_links())
