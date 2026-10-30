import os
import tempfile
from pathlib import Path

import requests
import yt_dlp
from supabase import create_client

USERNAME = os.environ["TIKTOK_USERNAME"].lstrip("@")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

db = create_client(SUPABASE_URL, SUPABASE_KEY)
TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
PROFILE_URL = f"https://www.tiktok.com/@{USERNAME}"
MAX_SCAN = 20


def telegram(method, **kwargs):
    response = requests.post(f"{TG_API}/{method}", **kwargs, timeout=180)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def get_recent_videos():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": MAX_SCAN,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(PROFILE_URL, download=False)
    entries = (info or {}).get("entries") or []
    videos = []
    for item in entries:
        if not item:
            continue
        video_id = item.get("id")
        url = item.get("webpage_url") or item.get("url")
        if video_id and url:
            if not str(url).startswith("http"):
                url = f"https://www.tiktok.com/@{USERNAME}/video/{video_id}"
            videos.append({"video_id": str(video_id), "video_url": str(url)})
    return videos


def get_state(key):
    rows = db.table("bot_state").select("value").eq("key", key).limit(1).execute().data
    return rows[0]["value"] if rows else None


def set_state(key, value):
    db.table("bot_state").upsert({
        "key": key, "value": value
    }, on_conflict="key").execute()


def save_baseline(videos):
    for video in videos:
        db.table("videos").upsert({
            **video,
            "status": "baseline",
            "video_sent": False,
            "link_sent": False,
        }, on_conflict="video_id").execute()
    set_state("initialized", "true")
    print(f"Initial setup complete. Saved {len(videos)} existing videos; none were sent.")


def deliver(video):
    video_id = video["video_id"]
    url = video["video_url"]

    rows = db.table("videos").select("*").eq("video_id", video_id).limit(1).execute().data
    if rows and rows[0].get("status") in ("sent", "baseline"):
        return

    if not rows:
        db.table("videos").insert({
            **video, "status": "pending",
            "video_sent": False, "link_sent": False,
        }).execute()
        rows = db.table("videos").select("*").eq("video_id", video_id).limit(1).execute().data

    record = rows[0]

    if not record.get("video_sent"):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "tiktok.%(ext)s")
            opts = {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": output,
                "format": "mp4/best",
                "merge_output_format": "mp4",
                "max_filesize": 49 * 1024 * 1024,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = list(Path(tmp).glob("tiktok.*"))
            if not files:
                raise RuntimeError(f"Could not download video {video_id}")
            file_path = files[0]
            with file_path.open("rb") as f:
                telegram("sendVideo", data={"chat_id": CHAT_ID}, files={"video": f})
        db.table("videos").update({"video_sent": True}).eq("video_id", video_id).execute()

    if not record.get("link_sent"):
        telegram("sendMessage", data={
            "chat_id": CHAT_ID,
            "text": f"New TikTok video from @{USERNAME}:\n{url}",
            "disable_web_page_preview": False,
        })
        db.table("videos").update({"link_sent": True}).eq("video_id", video_id).execute()

    db.table("videos").update({"status": "sent"}).eq("video_id", video_id).execute()
    print(f"Delivered {video_id}")


def main():
    videos = get_recent_videos()
    if not videos:
        raise RuntimeError("No videos were returned. TikTok may be blocking the request.")

    if get_state("initialized") != "true":
        save_baseline(videos)
        return

    # Oldest first, so Telegram receives new posts in chronological order.
    for video in reversed(videos):
        deliver(video)


if __name__ == "__main__":
    main()
