"""스레드 성과 주간 기록 — 팔로워 수, 최근 7일 반응, 글별 성과 상위/하위.

threads_stats.json 에 주간 스냅샷을 쌓고, threads_report.md 에 사람이 읽을 요약을 씀.
토큰에 threads_manage_insights 권한이 없으면 가능한 항목만 기록하고 넘어감.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
API = "https://graph.threads.net/v1.0"
USER_ID = os.environ["THREADS_USER_ID"]
TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
STATS_FILE = Path("threads_stats.json")
REPORT_FILE = Path("threads_report.md")
HISTORY_FILE = Path("published_history.json")

# 링크 재개 판단 기준
LINK_FOLLOWER_GOAL = 100
LINK_REPLIES_PER_POST = 2.0


def get(path: str, **params):
    params["access_token"] = TOKEN
    res = requests.get(f"{API}/{path}", params=params, timeout=30)
    if res.status_code != 200:
        print(f"조회 실패 {path}: {res.text[:300]}")
        return None
    return res.json()


def metric_value(item: dict):
    if "total_value" in item:
        return item["total_value"].get("value")
    vals = item.get("values") or []
    return sum(v.get("value", 0) for v in vals) if vals else None


def account_insights(since: datetime, until: datetime) -> dict:
    out = {}
    data = get(f"{USER_ID}/threads_insights",
               metric="views,likes,replies,reposts,quotes",
               since=int(since.timestamp()), until=int(until.timestamp()))
    for item in (data or {}).get("data", []):
        out[item["name"]] = metric_value(item)
    data = get(f"{USER_ID}/threads_insights", metric="followers_count")
    for item in (data or {}).get("data", []):
        out["followers_count"] = metric_value(item)
    return out


def recent_posts(since: datetime) -> list:
    data = get(f"{USER_ID}/threads", fields="id,text,timestamp,media_type", limit=50) or {}
    posts = []
    for p in data.get("data", []):
        ts = datetime.fromisoformat(p["timestamp"].replace("+0000", "+00:00"))
        if ts < since:
            continue
        ins = get(f"{p['id']}/insights", metric="views,likes,replies,reposts,quotes") or {}
        m = {i["name"]: metric_value(i) or 0 for i in ins.get("data", [])}
        posts.append({
            "id": p["id"],
            "at": ts.astimezone(KST).isoformat(timespec="minutes"),
            "first_line": (p.get("text") or "").split("\n")[0][:60],
            "media_type": p.get("media_type"),
            **m,
        })
    return posts


def kind_of(first_line: str, history: list) -> str:
    for r in history:
        if r.get("text", "").split("\n")[0][:60] == first_line:
            return f"{r.get('kind', 'blog')}/{r.get('format', '?')}"
    return "?"


def main():
    now = datetime.now(KST)
    since = now - timedelta(days=7)
    acct = account_insights(since, now)
    posts = recent_posts(since)

    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text()).get("recent_posts", [])
    for p in posts:
        p["kind"] = kind_of(p["first_line"], history)

    stats = json.loads(STATS_FILE.read_text()) if STATS_FILE.exists() else []
    prev_followers = next((s["account"].get("followers_count") for s in reversed(stats)
                           if s["account"].get("followers_count") is not None), None)
    snapshot = {"week_ending": now.date().isoformat(), "account": acct, "posts": posts}
    stats.append(snapshot)
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    followers = acct.get("followers_count")
    n = len(posts)
    replies_avg = (sum(p.get("replies", 0) for p in posts) / n) if n else 0
    gain = (followers - prev_followers) if (followers is not None and prev_followers is not None) else None
    ready = (followers or 0) >= LINK_FOLLOWER_GOAL or replies_avg >= LINK_REPLIES_PER_POST

    ranked = sorted(posts, key=lambda p: (p.get("views", 0), p.get("replies", 0)), reverse=True)
    lines = [
        f"# 🧵 스레드 주간 성과 ({since.date()} ~ {now.date()})",
        "",
        f"- 👥 팔로워: **{followers if followers is not None else '조회 불가'}**"
        + (f" (지난주 대비 {gain:+d})" if gain is not None else ""),
        f"- 👀 조회 {acct.get('views', '-')} · ❤️ 좋아요 {acct.get('likes', '-')} · "
        f"💬 답글 {acct.get('replies', '-')} · 🔁 리포스트 {acct.get('reposts', '-')}",
        f"- 📝 이번 주 발행 {n}개 · 글당 평균 답글 {replies_avg:.1f}",
        f"- 🔗 링크 재개 기준(팔로워 {LINK_FOLLOWER_GOAL}+ 또는 글당 답글 {LINK_REPLIES_PER_POST}+): "
        + ("**충족** ✅" if ready else "미충족 — 링크 없이 계속"),
        "",
        "## 🏆 잘 된 글 TOP 3",
    ]
    for p in ranked[:3]:
        lines.append(f"- [{p['kind']}] {p['first_line']} — 조회 {p.get('views', 0)}, 답글 {p.get('replies', 0)}")
    lines += ["", "## 🥶 반응 약한 글 3"]
    for p in ranked[-3:][::-1] if n > 3 else []:
        lines.append(f"- [{p['kind']}] {p['first_line']} — 조회 {p.get('views', 0)}, 답글 {p.get('replies', 0)}")
    REPORT_FILE.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
