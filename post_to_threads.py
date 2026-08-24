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
BRAND_GOLD = "#c9a84b"

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
3. 마지막: "자세한 내용은 댓글에 남겨둘게요 👇" 또는 비슷한 자연스러운 유도 문구 (URL 자체는 절대 쓰지 말 것)
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
    # 500자 초과 시 강제 자르기
    if len(text) > 490:
        text = text[:490]
    return text


def generate_card_hook(post: dict) -> str:
    """카드 이미지에 크게 들어갈 한 줄 후킹 문구 생성 (이모지 없이, 짧고 임팩트 있게)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 SNS 카드 이미지에 큼직하게 넣을 한 줄 후킹 문구로 요약해줘.

글 제목: {post['title']}
내용 요약: {post['summary']}

조건:
- 반드시 한 문장, 25자 이내 (띄어쓰기 포함)
- 이모지, 해시태그, 따옴표 없이 텍스트만
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


def render_card_image(hook: str, category: str, out_path: Path):
    """WhiteCoffee 브랜드 스타일(네이비/골드, WC 모노그램)의 정사각 카드 이미지를 HTML → PNG로 렌더링."""
    html = f"""
    <html>
    <head>
    <style>
        body {{ margin: 0; }}
        .card {{
            width: 1080px;
            height: 1080px;
            background: {BRAND_NAVY};
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            box-sizing: border-box;
            padding: 70px;
            font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif;
        }}
        .monogram {{
            color: {BRAND_GOLD};
            font-size: 40px;
            font-weight: 800;
            letter-spacing: 2px;
        }}
        .hook {{
            color: #ffffff;
            font-size: 68px;
            font-weight: 800;
            line-height: 1.4;
            word-break: keep-all;
        }}
        .badge {{
            align-self: flex-start;
            background: {BRAND_GOLD};
            color: {BRAND_NAVY};
            font-size: 30px;
            font-weight: 700;
            padding: 14px 28px;
            border-radius: 999px;
        }}
    </style>
    </head>
    <body>
        <div class="card">
            <div class="monogram">WC</div>
            <div class="hook">{hook}</div>
            <div class="badge">{category}</div>
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
    """후킹 문구 생성 → 카드 렌더링 → 커밋/푸시 → 공개 URL 반환. 실패 시 None."""
    try:
        hook = generate_card_hook(post)
        IMAGES_DIR.mkdir(exist_ok=True)
        filename = f"{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.png"
        out_path = IMAGES_DIR / filename
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
    # Meta 권장: 첫 체크 전 최소 몇 초 대기
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
            # IN_PROGRESS 등이면 계속 대기
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
        payload["text"] = text  # 이미지 캡션
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

    # 링크는 도달률 저하를 피하기 위해 본문이 아닌 첫 댓글로 게시
    reply_id = create_and_publish(link, reply_to_id=post_id)
    if not reply_id:
        print("본문은 발행됐지만 링크 댓글 발행에 실패했습니다.")
        # 본문 발행 자체는 성공했으므로 전체 결과는 성공으로 처리

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
