"""인스타 성과 주간 기록 — 팔로워, 최근 7일 게시물별 도달·저장·공유, 표지(남색/크림) 비교.

ig_stats.json 에 주간 스냅샷을 쌓고 ig_report.md 에 요약을 씀.
토큰은 post_to_instagram.py 와 같은 방식(ig_token.enc, 없으면 시크릿)으로 읽음.
"""
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KST = timezone(timedelta(hours=9))
GRAPH = "https://graph.instagram.com"
SECRET = os.environ.get("IG_ACCESS_TOKEN", "")
TOKEN_FILE = Path("ig_token.enc")
HISTORY_FILE = Path("instagram_history.json")
STATS_FILE = Path("ig_stats.json")
REPORT_FILE = Path("ig_report.md")


def load_token() -> str:
    if TOKEN_FILE.exists():
        try:
            key = hashlib.sha256(("wc-ig|" + SECRET).encode()).digest()
            raw = base64.b64decode(TOKEN_FILE.read_text().strip())
            return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
        except Exception:
            print("저장 토큰 복호화 실패 → 시크릿 사용")
    return SECRET


TOKEN = load_token()


def get(path: str, **params):
    params["access_token"] = TOKEN
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    if not r.ok:
        print(f"조회 실패 {path} {params.get('metric', '')}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()


def media_insights(mid: str, product: str) -> dict:
    for metrics in ("reach,saved,shares,views,total_interactions", "reach,saved,shares,total_interactions", "reach,saved"):
        d = get(f"{mid}/insights", metric=metrics)
        if d:
            out = {}
            for i in d.get("data", []):
                vals = i.get("values") or [{}]
                out[i["name"]] = i.get("total_value", {}).get("value", vals[0].get("value", 0))
            return out
    return {}


def main():
    now = datetime.now(KST)
    since = now - timedelta(days=7)
    me = get("me", fields="username,followers_count,media_count") or {}
    media = (get("me/media", fields="id,caption,media_product_type,timestamp,like_count,comments_count", limit=30) or {}).get("data", [])

    log = json.loads(HISTORY_FILE.read_text(encoding="utf-8")).get("log", []) if HISTORY_FILE.exists() else []
    by_id = {x.get("media_id"): x for x in log if x.get("media_id")}

    posts = []
    for m in media:
        ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
        if ts < since:
            continue
        meta = by_id.get(m["id"], {})
        posts.append({
            "id": m["id"],
            "at": ts.astimezone(KST).isoformat(timespec="minutes"),
            "type": m.get("media_product_type"),
            "cover": meta.get("cover"),
            "title": meta.get("title") or (m.get("caption") or "").split("\n")[0][:50],
            "likes": m.get("like_count", 0),
            "comments": m.get("comments_count", 0),
            **media_insights(m["id"], m.get("media_product_type", "")),
        })

    stats = json.loads(STATS_FILE.read_text()) if STATS_FILE.exists() else []
    prev = next((s["account"].get("followers_count") for s in reversed(stats)
                 if s["account"].get("followers_count") is not None), None)
    stats.append({"week_ending": now.date().isoformat(), "account": me, "posts": posts})
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    f = me.get("followers_count")
    gain = f"{f - prev:+d}" if (f is not None and prev is not None) else "-"

    def avg(rows, k):
        return sum(r.get(k, 0) or 0 for r in rows) / len(rows) if rows else 0

    lines = [f"# 📸 인스타 주간 성과 ({since.date()} ~ {now.date()})", "",
             f"- 👥 팔로워 **{f if f is not None else '조회 불가'}** (지난주 대비 {gain}) · 게시물 {me.get('media_count', '-')}",
             f"- 이번 주 게시물 {len(posts)}개 · 평균 도달 {avg(posts, 'reach'):.0f} · 평균 저장 {avg(posts, 'saved'):.1f} · 평균 공유 {avg(posts, 'shares'):.1f}"]
    for kind in ("FEED", "REELS"):
        rows = [p for p in posts if p["type"] == kind]
        if rows:
            lines.append(f"- {'🗂️ 캐러셀' if kind == 'FEED' else '🎬 릴스'} {len(rows)}개: 도달 {avg(rows, 'reach'):.0f}, 저장 {avg(rows, 'saved'):.1f}, 공유 {avg(rows, 'shares'):.1f}")
    for cv in ("navy", "cream"):
        rows = [p for p in posts if p.get("cover") == cv]
        if rows:
            lines.append(f"- 🎨 표지 {cv}: {len(rows)}개, 도달 {avg(rows, 'reach'):.0f}, 저장 {avg(rows, 'saved'):.1f}")
    lines += ["", "## 게시물별"]
    for p in sorted(posts, key=lambda p: p.get("reach", 0), reverse=True):
        lines.append(f"- [{p['type']}/{p.get('cover') or '-'}] {p['title']} — 도달 {p.get('reach', 0)}, 저장 {p.get('saved', 0)}, 공유 {p.get('shares', 0)}, ❤️ {p['likes']}")
    REPORT_FILE.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
