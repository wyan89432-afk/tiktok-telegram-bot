import os
import tempfile
import time
from pathlib import Path

import requests
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
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

TIKTOK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

YTDLP_REQUEST_OPTIONS = {
    "http_headers": TIKTOK_HEADERS,
    # yt-dlp's Python API requires an ImpersonateTarget object, not a string.
    "impersonate": ImpersonateTarget.from_str("chrome"),
    "socket_timeout": 30,
    "retries": 5,
    "extractor_retries": 5,
}


def telegram(method, **kwargs):
    response = requests.post(f"{TG_API}/{method}", **kwargs, timeout=180)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        description = data.get("description", "Unknown Telegram API error")
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return data


def check_telegram():
    me = telegram("getMe")["result"]
    chat = telegram("getChat", data={"chat_id": CHAT_ID})["result"]
    print(
        "Telegram connection OK. "
        f"Bot=@{me.get('username', 'unknown')}, "
        f"chat_type={chat.get('type', 'unknown')}."
    )


def get_recent_videos():
    opts = {
        **YTDLP_REQUEST_OPTIONS,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": MAX_SCAN,
        "ignoreerrors": False,
        "sleep_interval_requests": 1,
        "max_sleep_interval_requests": 2,
    }

    last_error = None
    for attempt in range(1, 4):
        try:
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
                    videos.append({
                        "video_id": str(video_id),
                        "video_url": str(url),
                    })

            if videos:
                print(f"Found {len(videos)} recent TikTok videos.")
                return videos

            last_error = RuntimeError(
                "TikTok returned an empty video list. Account privacy, "
                "rate limiting, or runner blocking may be responsible."
            )
        except Exception as exc:
            last_error = exc
            print(
                f"TikTok scan attempt {attempt}/3 failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        if attempt < 3:
            delay = 10 * attempt
            print(f"Retrying TikTok scan in {delay} seconds...", flush=True)
            time.sleep(delay)

    raise RuntimeError(
        "Unable to read TikTok uploads after 3 attempts. "
        "TikTok may be rate-limiting or blocking GitHub Actions. "
        f"Last error: {last_error}"
    ) from last_error


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
    telegram("sendMessage", data={
        "chat_id": CHAT_ID,
        "text": (
            f"✅ TikTok → Telegram bot is connected.\n"
            f"Account: @{USERNAME}\n"
            f"Saved {len(videos)} existing videos as the starting point. "
            "Older videos were not sent. New uploads will be forwarded automatically."
        ),
    })
    print(
        f"Initial setup complete. Saved {len(videos)} existing videos; "
        "sent Telegram initialization notice. Existing videos were not forwarded."
    )


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
                **YTDLP_REQUEST_OPTIONS,
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
    # Verify Telegram credentials/chat access before doing any TikTok work.
    check_telegram()
    videos = get_recent_videos()

    if get_state("initialized") != "true":
        save_baseline(videos)
        return

    # Oldest first, so Telegram receives new posts in chronological order.
    for video in reversed(videos):
        deliver(video)


if __name__ == "__main__":
    main()
