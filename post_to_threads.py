import os
import json
import feedparser
import anthropic
import requests
from pathlib import Path

# ── 설정 ──────────────────────────────────────────
TISTORY_RSS   = "https://ideas07576.tistory.com/rss"
POSTS_PER_RUN = 1
HISTORY_FILE  = "published_history.json"
ALLOWED_CATEGORIES = ["직장인 투자", "장기 투자"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
THREADS_USER_ID   = os.environ["THREADS_USER_ID"]
THREADS_TOKEN     = os.environ["THREADS_ACCESS_TOKEN"]
# ─────────────────────────────────────────────────


def load_history() -> set:
    if Path(HISTORY_FILE).exists():
        with open(HISTORY_FILE) as f:
            data = json.load(f)
            return set(data.get("published", []))
    return set()


def load_all_published() -> list:
    if Path(HISTORY_FILE).exists():
        with open(HISTORY_FILE) as f:
            data = json.load(f)
            return data.get("all_time", [])
    return []


def save_history(history: set, all_time: list):
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "published": list(history),
            "all_time": all_time
        }, f, ensure_ascii=False, indent=2)


def fetch_all_posts() -> list:
    feed = feedparser.parse(TISTORY_RSS)
    posts = []
    for entry in feed.entries:
        tags = [t.term for t in getattr(entry, "tags", [])]
        if not any(cat in tags for cat in ALLOWED_CATEGORIES):
            continue
        posts.append({
            "title":   entry.title,
            "link":    entry.link,
            "summary": entry.get("summary", "")[:800],
            "category": tags[0] if tags else "",
        })
    return posts


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


def main():
    history   = load_history()
    all_time  = load_all_published()
    all_posts = fetch_all_posts()

    # 아직 안 올린 글 필터링
    pending = [p for p in reversed(all_posts) if p["link"] not in history]

    # 다 올렸으면 전체 초기화 후 처음부터 다시
    if not pending:
        print("모든 글 발행 완료! 처음부터 다시 시작합니다.")
        history = set()
        pending = list(reversed(all_posts))

    if not pending:
        print("발행할 글이 없어요.")
        return

    targets = pending[:POSTS_PER_RUN]
    print(f"오늘 발행 대상: {len(targets)}개")

    for post in targets:
        print(f"\n처리 중: {post['title']} [{post['category']}]")
        try:
            threads_text = generate_threads_post(post)
            print(f"생성된 홍보글:\n{threads_text}\n")

            success = post_to_threads(threads_text)
            if success:
                history.add(post["link"])
                if post["link"] not in all_time:
                    all_time.append(post["link"])
                print(f"발행 완료: {post['title']}")
            else:
                print(f"발행 실패: {post['title']}")
        except Exception as e:
            print(f"오류: {e}")

    save_history(history, all_time)
    print("\n완료!")


if __name__ == "__main__":
    main()
