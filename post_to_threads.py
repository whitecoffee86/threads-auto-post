import os
import time
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
                "post_counter": data.get("post_counter", 0),
            }
    return {"cycle_published": set(), "short_term_done": set(), "post_counter": 0}
 
 
def save_data(data: dict):
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "cycle_published": list(data["cycle_published"]),
            "short_term_done": list(data["short_term_done"]),
            "post_counter": data["post_counter"],
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
 
 
def generate_threads_post(post: dict, include_link: bool = True) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
    if include_link:
        format_guide = f"""형식:
1. 첫 줄: 공감 또는 궁금증을 유발하는 후킹 문장 (이모지 1개 포함)
2. 본문: 핵심 내용을 이야기하듯 3~5문장으로 풀어서 설명
3. 마지막: "자세한 내용은 블로그에 정리해뒀어요 👇" 또는 비슷한 자연스러운 유도 문구
4. 링크: {post['link']}
5. 해시태그: 2~3개 (맨 마지막)
 
조건:
- 전체 400~500자
- 재테크/투자 관심 직장인 타깃
- 절대 광고처럼 보이지 않게"""
    else:
        format_guide = """형식:
1. 첫 줄: 공감 또는 궁금증을 유발하는 후킹 문장 (이모지 1개 포함)
2. 본문: 핵심 인사이트를 이야기하듯 3~5문장으로 풀어서 설명하고 그대로 마무리
3. 링크나 "블로그에서 자세히" 같은 유도 문구는 절대 넣지 말 것 — 인사이트 자체로 완결되는 글
4. 해시태그: 2~3개 (맨 마지막)
 
조건:
- 전체 300~400자
- 재테크/투자 관심 직장인 타깃
- 절대 광고처럼 보이지 않게, 순수하게 생각을 나누는 글처럼"""
 
    prompt = f"""아래 블로그 글의 핵심 내용을 바탕으로 스레드(Threads)에 올릴 글을 작성해줘.
 
글 제목: {post['title']}
내용 요약: {post['summary']}
 
스타일 가이드:
- 광고글처럼 보이면 안 됨. 직장인이 퇴근 후 자연스럽게 생각을 공유하는 느낌으로
- "나도 처음엔 몰랐는데", "알고 보니", "생각보다" 같은 자연스러운 구어체 표현 활용
- 독자가 "어? 이거 나 얘기네" 싶게 공감 포인트를 첫 문장에 넣기
- 핵심 인사이트를 2~4문장으로 풀어서 설명 (단순 나열 금지)
 
{format_guide}
 
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
    if not container_id:
        print(f"컨테이너 ID를 받지 못함: {res.text}")
        return False
 
    # TEXT 컨테이너는 Meta의 status 필드(FINISHED 등)가 비디오 전용이라 신뢰할 수 없음.
    # 대신 컨테이너 생성 리전 -> 발행 리전 간 리플리케이션 지연을 흡수하기 위해
    # 고정 대기 후, 실패 시 재시도한다.
    time.sleep(10)
 
    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    retry_delays = [10, 20, 30]  # 재시도 간 대기 시간(초), 총 3회 재시도
 
    for attempt in range(1 + len(retry_delays)):
        res2 = requests.post(publish_url, data={
            "creation_id":  container_id,
            "access_token": THREADS_TOKEN,
        })
        if res2.status_code == 200:
            return True
 
        # "Media Not Found" (subcode 4279009)는 리플리케이션 지연으로 인한
        # 일시적 오류인 경우가 많아 재시도 대상으로 취급
        try:
            err = res2.json().get("error", {})
        except ValueError:
            err = {}
        is_media_not_found = err.get("error_subcode") == 4279009
 
        if is_media_not_found and attempt < len(retry_delays):
            wait = retry_delays[attempt]
            print(f"발행 재시도 {attempt + 1}/{len(retry_delays)} — {wait}초 후 재시도: {res2.text}")
            time.sleep(wait)
            continue
 
        print(f"발행 실패: {res2.text}")
        return False
 
    return False
 
 
def publish_one(post: dict, post_counter: int) -> bool:
    # 누적 발행 횟수 기준 3번째마다 링크 포함 (0,1,2 -> 링크는 인덱스 2번째)
    include_link = (post_counter % 3 == 2)
    link_status = "링크 포함" if include_link else "링크 없음"
    print(f"\n처리 중: {post['title']} [{post.get('category', '')}] ({link_status})")
    try:
        threads_text = generate_threads_post(post, include_link=include_link)
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
        if publish_one(post, data["post_counter"]):
            data["short_term_done"].add(post["link"])
            data["post_counter"] += 1
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
            if publish_one(post, data["post_counter"]):
                data["cycle_published"].add(post["link"])
                data["post_counter"] += 1
                published_count += 1
 
    if published_count == 0:
        print("오늘 발행할 글이 없습니다.")
 
    save_data(data)
    print(f"\n완료! 총 {published_count}개 발행")
 
 
if __name__ == "__main__":
    main()
