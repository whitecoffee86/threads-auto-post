import os
import json
import feedparser
import anthropic
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── 설정 ──────────────────────────────────────────
TISTORY_RSS   = "https://ideas07576.tistory.com/rss"
POSTS_PER_RUN = 1
HISTORY_FILE  = "published_history.json"
SHORT_TERM_CATEGORY = "단기 투자"

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
- 마지막에 블로그 링크로 자연스럽게 유도

형식:
1. 첫 줄: 공감 또는 궁금증을 유발하는 후킹 문장 (이모지 1개 포함)
2. 본문: 핵심 내용을 이야기하듯 3~5문장으로 풀어서 설명
3. 마지막: "자세한 내용은 블로그에 정리해뒀어요 👇" 또는 비슷한 자연스러운 유도 문구
4. 링크: {post['link']}
5. 해시태그: 2~3개 (맨 마지막)

조건:
- 전체 400~500자
- 재테크/투자 관심 직장인 타깃
- 절대 광고처럼 보이지 않게

홍보글만 출력해줘. 다른 말 없이."""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=600,
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
    all_posts = fetch_rss()

    # 1순위: 단기 투자 — 오늘 발행된 글만, 한 번 발행되면 영구 제외
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

    # 2순위: 단기 투자 제외한 나머지 전체 글 순환
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
