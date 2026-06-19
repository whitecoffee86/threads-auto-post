import os
import json
import re
import feedparser
import anthropic
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── 설정 ──────────────────────────────────────────
TISTORY_RSS   = "https://ideas07576.tistory.com/rss"
BLOG_BASE     = "https://ideas07576.tistory.com"
POSTS_PER_RUN = 1
HISTORY_FILE  = "published_history.json"

SHORT_TERM_CATEGORY = "단기 투자"
CYCLE_CATEGORIES    = ["직장인 투자", "장기 투자"]

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
                "cycle_published":  set(data.get("cycle_published", [])),
                "cycle_all_time":   data.get("cycle_all_time", []),
                "short_term_done":  set(data.get("short_term_done", [])),
            }
    return {"cycle_published": set(), "cycle_all_time": [], "short_term_done": set()}


def save_data(data: dict):
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "cycle_published": list(data["cycle_published"]),
            "cycle_all_time":  data["cycle_all_time"],
            "short_term_done": list(data["short_term_done"]),
        }, f, ensure_ascii=False, indent=2)


def fetch_rss_posts() -> list:
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


def fetch_post_urls_from_sitemap() -> list:
    urls = []
    try:
        res = requests.get(f"{BLOG_BASE}/sitemap.xml", timeout=10)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            all_urls = [loc.text for loc in root.findall(".//sm:loc", ns)]
            urls = [
                u for u in all_urls
                if "/category/" not in u
                and "/tag/" not in u
                and u.rstrip("/") != BLOG_BASE
                and re.search(r"/\d+$", u.rstrip("/"))
            ]
    except Exception as e:
        print(f"사이트맵 가져오기 실패: {e}")
    return urls


def fetch_post_detail(url: str) -> dict:
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        html = res.text

        title_match = re.search(r'<meta property="og:title" content="([^"]*)"', html)
        desc_match  = re.search(r'<meta property="og:description" content="([^"]*)"', html)
        title   = title_match.group(1) if title_match else url
        summary = desc_match.group(1)  if desc_match  else ""

        category = ""
        cat_link = re.search(r'/category/([^"/]+)', html)
        if cat_link:
            category = requests.utils.unquote(cat_link.group(1))

        return {"title": title, "link": url, "summary": summary[:800], "category": category}
    except Exception as e:
        print(f"글 상세 가져오기 실패 ({url}): {e}")
        return {"title": url, "link": url, "summary": "", "category": ""}


def generate_threads_post(post: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 스레드(Threads) 홍보글로 작성해줘.

글 제목: {post['title']}
글 링크: {post['link']}
내용 요약: {post['summary']}

형식:
1. 후킹 문장 1줄 (궁금증 유발, 이모지 포함)
2. 핵심 내용 3줄 요약 (각 줄 앞에 체크 이모지)
3. 마지막 줄에 "전체 내용 보기: [링크]"
4. 해시태그 3개 이내 (맨 마지막)

조건:
- 전체 500자 이내
- 재테크/투자 관심 직장인 타깃
- 자연스러운 구어체

홍보글만 출력해줘. 다른 말 없이."""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


def post_to_threads(text: str) -> bool:
    create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    res = requests.post(create_url, data={
        "media_type":   "TEXT",
        "text":         text,
        "access_token": THREADS_TOKEN,
    })
    if res.status_code != 200:
        print(f"컨테이너 생성 실패: {res.text}")
        return False

    container_id = res.json().get("id")

    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    res2 = requests.post(publish_url, data={
        "creation_id":  container_id,
        "access_token": THREADS_TOKEN,
    })
    if res2.status_code != 200:
        print(f"발행 실패: {res2.text}")
        return False

    return True


def publish_one(post: dict) -> bool:
    print(f"\n처리 중: {post['title']} [{post.get('category', '')}]")
    try:
        threads_text = generate_threads_post(post)
        print(f"생성된 홍보글:\n{threads_text}\n")
        success = post_to_threads(threads_text)
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

    # 1순위: 단기 투자 — 오늘 발행된 글만, 한 번 발행되면 영구 제외
    rss_posts = fetch_rss_posts()
    short_term_new = [
        p for p in rss_posts
        if p["category"] == SHORT_TERM_CATEGORY
        and p["link"] not in data["short_term_done"]
        and p["pub_date"] == today
    ]

    if short_term_new and published_count < POSTS_PER_RUN:
        post = short_term_new[0]
        if publish_one(post):
            data["short_term_done"].add(post["link"])
            published_count += 1

    # 2순위: 직장인 투자 / 장기 투자 — 전체 글 순환
    remaining = POSTS_PER_RUN - published_count
    if remaining > 0:
        urls = fetch_post_urls_from_sitemap()
        cycle_posts = []

        for u in urls:
            if u in data["cycle_published"]:
                continue
            detail = fetch_post_detail(u)
            if detail["category"] in CYCLE_CATEGORIES:
                cycle_posts.append(detail)
            if len(cycle_posts) >= remaining:
                break

        if not cycle_posts:
            print("순환 대상 글을 모두 발행함. 기록 초기화 후 다시 시작합니다.")
            data["cycle_published"] = set()
            for u in urls:
                detail = fetch_post_detail(u)
                if detail["category"] in CYCLE_CATEGORIES:
                    cycle_posts.append(detail)
                if len(cycle_posts) >= remaining:
                    break

        for post in cycle_posts[:remaining]:
            if publish_one(post):
                data["cycle_published"].add(post["link"])
                if post["link"] not in data["cycle_all_time"]:
                    data["cycle_all_time"].append(post["link"])
                published_count += 1

    if published_count == 0:
        print("오늘 발행할 글이 없습니다.")

    save_data(data)
    print(f"\n완료! 총 {published_count}개 발행")


if __name__ == "__main__":
    main()
