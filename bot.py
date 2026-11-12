#!/usr/bin/env python3
"""
tg-media-bot — Telegram userbot that auto-downloads videos sent by movie bots
(e.g. @searchgram_bbot) into a Synology share, sorted into category folders,
then asks Jellyfin to refresh its libraries.

Control is done from your own "Saved Messages" chat:
  /help                 show commands
  /list                 list sources
  /add @username        add a source (bot / channel / group)
  /remove @username     remove a source
  /jellyfin <API_KEY>   set Jellyfin API key
  /status               queue + recent downloads
  forward a message from a new bot/channel  -> adds it as a source
  forward a video                           -> downloads it now
  reply to a "✅ saved" message with one of:
      סרטים / ישראלי / ילדים / סדרות        -> moves the file there
"""
import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import aiohttp
from guessit import guessit
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

BASE = Path(os.environ.get("TGMB_HOME", "/opt/tgmedia"))
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
SESSION = str(BASE / "session")
TMP = BASE / "tmp"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".webm", ".mpg", ".mpeg"}

# category key -> folder name under the media root
DEFAULT_FOLDERS = {
    "movies": "סרטים",
    "israeli": "סרטים ישראלים",
    "kids": "ילדים",
    "series": "סדרות",
}
# words a user can reply with to move a file
MOVE_WORDS = {
    "סרטים": "movies", "סרט": "movies", "movies": "movies",
    "ישראלי": "israeli", "ישראלים": "israeli", "סרטים ישראלים": "israeli", "israeli": "israeli",
    "ילדים": "kids", "kids": "kids",
    "סדרות": "series", "סדרה": "series", "series": "series",
}
KIDS_HINTS = ["מדובב", "דיבוב", "ילדים", "אנימציה", "מצויר", "dubbed", "hebdub", "heb dub", "disney", "pixar", "dreamworks"]
HEB_RE = re.compile(r"[֐-׿]")
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

log = logging.getLogger("tgmb")


# ----------------------------------------------------------------- storage
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


CFG = load_json(CONFIG_FILE, {})
STATE = load_json(STATE_FILE, {})
STATE.setdefault("sources", CFG.get("default_sources", ["searchgram_bbot"]))
STATE.setdefault("done", [])          # "chat_id:msg_id" already downloaded
STATE.setdefault("saved", {})         # control-msg-id -> {"path":..., "kind":..., "name":...}
STATE.setdefault("recent", [])
STATE.setdefault("jellyfin_key", CFG.get("jellyfin_key", ""))


def persist():
    STATE["done"] = STATE["done"][-5000:]
    STATE["recent"] = STATE["recent"][-20:]
    if len(STATE["saved"]) > 500:
        for k in list(STATE["saved"])[:-500]:
            STATE["saved"].pop(k, None)
    save_json(STATE_FILE, STATE)


def media_root():
    return Path(CFG.get("media_root", "/media/video"))


def folder_for(kind):
    return media_root() / CFG.get("folders", DEFAULT_FOLDERS).get(kind, DEFAULT_FOLDERS[kind])


# ----------------------------------------------------------------- naming
def clean(s):
    s = BAD_CHARS.sub(" ", str(s))
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s[:150] or "Unknown"


def file_name_of(msg):
    doc = msg.document
    if doc:
        for a in doc.attributes:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                return a.file_name
    return None


def is_video(msg):
    if not msg.media:
        return False
    if msg.video:
        return True
    doc = msg.document
    if not doc:
        return False
    if (doc.mime_type or "").startswith("video/"):
        return True
    name = file_name_of(msg) or ""
    if Path(name).suffix.lower() in VIDEO_EXT:
        return True
    return any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes)


def classify(filename, caption=""):
    """Return dict: kind, title, year, season, episode, ext."""
    filename = filename or ""
    caption = caption or ""
    ext = Path(filename).suffix.lower() if Path(filename).suffix.lower() in VIDEO_EXT else ".mp4"
    stem = Path(filename).stem if filename else ""
    # if no usable filename, use first caption line
    source = stem if stem and not re.fullmatch(r"(video|file|movie)?[_\- ]?\d*", stem, re.I) else caption.split("\n")[0]
    g = {}
    try:
        g = dict(guessit(source or "Unknown"))
    except Exception:
        pass
    title = g.get("title") or source or "Unknown"
    if isinstance(title, list):
        title = " ".join(map(str, title))
    year = g.get("year")
    season = g.get("season")
    episode = g.get("episode")
    if isinstance(season, list):
        season = season[0]
    if isinstance(episode, list):
        episode = episode[0]
    # Hebrew series notation: עונה 2 פרק 5
    blob = f"{filename} {caption}"
    m = re.search(r"עונה\s*(\d+)", blob)
    if m and season is None:
        season = int(m.group(1))
    m = re.search(r"פרק\s*(\d+)", blob)
    if m and episode is None:
        episode = int(m.group(1))

    if g.get("type") == "episode" or episode is not None:
        kind = "series"
        season = season or 1
        title = re.split(r"\s*(?:עונה\s*\d+|פרק\s*\d+|[Ss]\d{1,2}[Ee]\d{1,3})", str(title))[0] or title
    else:
        low = blob.lower()
        if any(h in low for h in KIDS_HINTS):
            kind = "kids"
        elif HEB_RE.search(str(title)):
            kind = "israeli"
        else:
            kind = "movies"
    return {"kind": kind, "title": clean(title), "year": year, "season": season,
            "episode": episode, "ext": ext}


def target_path(info):
    t, y = info["title"], info["year"]
    if info["kind"] == "series":
        s = int(info["season"] or 1)
        e = info["episode"]
        name = f"{t} S{s:02d}E{int(e):02d}" if e is not None else t
        return folder_for("series") / t / f"Season {s:02d}" / f"{name}{info['ext']}"
    base = f"{t} ({y})" if y else t
    return folder_for(info["kind"]) / base / f"{base}{info['ext']}"


def unique(path: Path):
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem} [{i}]{path.suffix}")
        if not p.exists():
            return p
        i += 1


def label(kind):
    return CFG.get("folders", DEFAULT_FOLDERS).get(kind, kind)


# ----------------------------------------------------------------- jellyfin
async def jellyfin_refresh():
    url = (CFG.get("jellyfin_url") or "").rstrip("/")
    key = STATE.get("jellyfin_key")
    if not url or not key:
        return "no-key"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/Library/Refresh",
                              headers={"X-Emby-Token": key},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                return "ok" if r.status in (200, 204) else f"HTTP {r.status}"
    except Exception as e:
        return f"error: {e}"


_refresh_task = None


def schedule_refresh(client):
    """Debounced: refresh 60s after the last completed download."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()

    async def later():
        await asyncio.sleep(60)
        res = await jellyfin_refresh()
        log.info("jellyfin refresh: %s", res)
        if res not in ("ok",):
            if res == "no-key":
                await notify(client, "ℹ️ ג'ליפין לא רוענן — חסר API Key. שלח כאן: /jellyfin <KEY>\n(בלי זה הקבצים יופיעו בסריקה המתוזמנת של ג'ליפין)")
            else:
                await notify(client, f"⚠️ רענון ג'ליפין נכשל: {res}")

    _refresh_task = asyncio.create_task(later())


# ----------------------------------------------------------------- telegram
async def notify(client, text, reply_to=None):
    try:
        return await client.send_message("me", text, reply_to=reply_to)
    except Exception as e:
        log.warning("notify failed: %s", e)


QUEUE: asyncio.Queue = None
ACTIVE = {}


def norm(u):
    return (u or "").lstrip("@").lower()


async def chat_matches_source(event):
    chat = await event.get_chat()
    uname = norm(getattr(chat, "username", None))
    cid = str(event.chat_id)
    srcs = [norm(s) for s in STATE["sources"]]
    return uname in srcs or cid in srcs


async def enqueue(client, msg, origin=""):
    key = f"{msg.chat_id}:{msg.id}"
    if key in STATE["done"] or key in ACTIVE:
        return
    ACTIVE[key] = "queued"
    await QUEUE.put((msg, origin))
    log.info("queued %s from %s", key, origin)


async def worker(client, n):
    while True:
        msg, origin = await QUEUE.get()
        key = f"{msg.chat_id}:{msg.id}"
        try:
            await process(client, msg, origin)
            STATE["done"].append(key)
            persist()
        except Exception as e:
            log.exception("download failed %s", key)
            await notify(client, f"❌ הורדה נכשלה ({origin}): {e}")
        finally:
            ACTIVE.pop(key, None)
            QUEUE.task_done()


async def process(client, msg, origin):
    fname = file_name_of(msg) or ""
    caption = msg.message or ""
    info = classify(fname, caption)
    dest = unique(target_path(info))
    size = msg.file.size if msg.file else 0
    size_txt = f"{size / 1024 / 1024 / 1024:.2f}GB" if size > 1024 ** 3 else f"{size / 1024 / 1024:.0f}MB"
    status = await notify(client, f"⬇️ מוריד: {info['title']} ({size_txt}) → {label(info['kind'])}")

    TMP.mkdir(parents=True, exist_ok=True)
    tmp_file = TMP / f"{msg.chat_id}_{msg.id}{info['ext']}"
    last = {"t": 0, "pct": -1}

    async def progress(cur, total):
        if not total or not status:
            return
        pct = int(cur * 100 / total)
        now = time.time()
        if pct >= last["pct"] + 10 and now - last["t"] > 8:
            last.update(t=now, pct=pct)
            try:
                await status.edit(f"⬇️ מוריד: {info['title']} ({size_txt}) → {label(info['kind'])}  {pct}%")
            except Exception:
                pass

    t0 = time.time()
    path = await client.download_media(msg, file=str(tmp_file), progress_callback=progress)
    if not path or not Path(path).exists() or (size and Path(path).stat().st_size < size * 0.99):
        raise RuntimeError("הקובץ לא ירד במלואו")

    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, path, dest)
    mins = (time.time() - t0) / 60
    rel = dest.relative_to(media_root())
    if status:
        try:
            await status.delete()
        except Exception:
            pass
    done = await notify(
        client,
        f"✅ נשמר: {info['title']}\n📁 {rel}\n⏱ {mins:.1f} דק'\n\n"
        f"קטגוריה לא נכונה? השב להודעה הזו: סרטים / ישראלי / ילדים / סדרות",
    )
    if done:
        STATE["saved"][str(done.id)] = {"path": str(dest), "kind": info["kind"], "title": info["title"],
                                        "year": info["year"], "season": info["season"],
                                        "episode": info["episode"], "ext": info["ext"]}
    STATE["recent"].append(f"{time.strftime('%d/%m %H:%M')} {rel}")
    persist()
    schedule_refresh(client)


async def move_saved(client, event, rec, new_kind):
    src = Path(rec["path"])
    if not src.exists():
        await event.reply("⚠️ הקובץ כבר לא נמצא במיקום המקורי")
        return
    info = dict(rec)
    info["kind"] = new_kind
    if new_kind == "series" and not info.get("season"):
        info["season"] = 1
    dest = unique(target_path(info))
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src), dest)
    # remove empty old folders
    for d in (src.parent, src.parent.parent):
        try:
            if d != media_root() and d.parent != media_root().parent and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass
    rec.update(path=str(dest), kind=new_kind)
    persist()
    await event.reply(f"📦 הועבר ל: {dest.relative_to(media_root())}")
    schedule_refresh(client)


HELP = (
    "🤖 בוט הסרטים — פקודות (כאן בהודעות שמורות):\n"
    "/list — רשימת מקורות\n"
    "/add @שם — הוספת מקור\n"
    "/remove @שם — הסרת מקור\n"
    "/status — תור והורדות אחרונות\n"
    "/jellyfin KEY — הגדרת מפתח ג'ליפין\n"
    "/refresh — רענון ג'ליפין עכשיו\n\n"
    "העבר (Forward) לכאן הודעה מבוט/ערוץ חדש ← הוא נוסף כמקור.\n"
    "העבר לכאן סרטון ← יורד מיד.\n"
    "השב להודעת ✅ עם סרטים/ישראלי/ילדים/סדרות ← העברת תיקייה."
)


async def handle_control(client, event):
    msg = event.message
    text = (msg.message or "").strip()

    # forwarded into Saved Messages
    if msg.fwd_from:
        if is_video(msg):
            await enqueue(client, msg, "העברה ידנית")
            return
        peer = msg.fwd_from.from_id
        if peer is not None:
            try:
                ent = await client.get_entity(peer)
                name = getattr(ent, "username", None) or str(await client.get_peer_id(ent))
                if norm(name) not in [norm(s) for s in STATE["sources"]]:
                    STATE["sources"].append(name)
                    persist()
                    await event.reply(f"➕ נוסף מקור: {name}")
                else:
                    await event.reply(f"✔️ {name} כבר ברשימת המקורות")
            except Exception as e:
                await event.reply(f"⚠️ לא הצלחתי לזהות את המקור: {e}")
        return

    # reply to a ✅ message with a category word
    if msg.is_reply and text:
        rid = str(msg.reply_to_msg_id)
        word = text.strip().lower()
        if rid in STATE["saved"] and word in MOVE_WORDS:
            await move_saved(client, event, STATE["saved"][rid], MOVE_WORDS[word])
            return

    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()
    if cmd in ("/help", "/start"):
        await event.reply(HELP)
    elif cmd == "/list":
        await event.reply("📋 מקורות:\n" + "\n".join(f"• {s}" for s in STATE["sources"]) or "אין מקורות")
    elif cmd == "/add" and arg:
        name = arg.lstrip("@")
        if norm(name) not in [norm(s) for s in STATE["sources"]]:
            STATE["sources"].append(name)
            persist()
        await event.reply(f"➕ נוסף מקור: {name}")
    elif cmd == "/remove" and arg:
        before = len(STATE["sources"])
        STATE["sources"] = [s for s in STATE["sources"] if norm(s) != norm(arg)]
        persist()
        await event.reply("➖ הוסר" if len(STATE["sources"]) < before else "לא נמצא ברשימה")
    elif cmd == "/jellyfin" and arg:
        STATE["jellyfin_key"] = arg
        persist()
        try:
            await msg.delete()
        except Exception:
            pass
        res = await jellyfin_refresh()
        await notify(client, "✅ מפתח ג'ליפין נשמר והרענון עובד" if res == "ok" else f"⚠️ נשמר, אבל בדיקה נכשלה: {res}")
    elif cmd == "/refresh":
        await event.reply(f"🔄 רענון ג'ליפין: {await jellyfin_refresh()}")
    elif cmd == "/status":
        lines = [f"⏳ בתור/בהורדה: {len(ACTIVE)}"]
        if STATE["recent"]:
            lines.append("\n🕘 אחרונים:")
            lines += STATE["recent"][-8:]
        await event.reply("\n".join(lines))


async def run():
    global QUEUE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = TelegramClient(SESSION, int(CFG["api_id"]), CFG["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Not logged in. Run: cd /opt/tgmedia && venv/bin/python bot.py --login")
        await client.disconnect()
        raise SystemExit(2)
    me = await client.get_me()
    log.info("logged in as %s", me.username or me.id)
    QUEUE = asyncio.Queue()
    for i in range(int(CFG.get("workers", 2))):
        asyncio.create_task(worker(client, i))

    @client.on(events.NewMessage(incoming=True))
    async def on_incoming(event):
        if event.is_private and event.chat_id == me.id:
            return
        if is_video(event.message) and await chat_matches_source(event):
            chat = await event.get_chat()
            await enqueue(client, event.message, getattr(chat, "username", None) or str(event.chat_id))

    @client.on(events.NewMessage(chats="me"))
    async def on_saved(event):
        try:
            await handle_control(client, event)
        except Exception as e:
            log.exception("control error")
            await event.reply(f"⚠️ שגיאה: {e}")

    if not media_root().exists():
        await notify(client, f"⚠️ תיקיית המדיה {media_root()} לא נגישה — בדוק את החיבור לסינולוג'י")
    await notify(client, "🟢 בוט הסרטים פעיל. שלח /help לרשימת פקודות.")
    await client.run_until_disconnected()


async def login():
    client = TelegramClient(SESSION, int(CFG["api_id"]), CFG["api_hash"])
    await client.start()  # interactive: phone, code, 2FA password
    me = await client.get_me()
    print(f"\n✅ מחובר בתור {me.first_name} (@{me.username})")
    await client.disconnect()


async def check():
    client = TelegramClient(SESSION, int(CFG["api_id"]), CFG["api_hash"])
    await client.connect()
    ok = await client.is_user_authorized()
    await client.disconnect()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    asyncio.run(login() if a.login else check() if a.check else run())
