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
import base64
import io
import shutil
import socket
import time
import zipfile
from pathlib import Path

import aiohttp
from guessit import guessit
from telethon import Button, TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

VERSION = "2026.11.13-gdrive2"
CHANGES = "☁️ הורדה מקישורי גוגל דרייב ודרופבוקס (קובץ או תיקייה שלמה) לטיולים/אירועים/סרטים"

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
    source = clean_title(source) or source
    g = {}
    try:
        g = dict(guessit(source or "Unknown"))
    except Exception:
        pass
    title = g.get("title") or source or "Unknown"
    if isinstance(title, list):
        title = " ".join(map(str, title))
    if g.get("part") and not isinstance(g.get("part"), list):
        title = f"{title} Part {g['part']}"
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
                              headers={"Authorization": f'MediaBrowser Token="{key}"',
                                       "X-Emby-Token": key},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                return "ok" if r.status in (200, 204) else f"HTTP {r.status}"
    except Exception as e:
        return f"error: {e}"


_refresh_task = None


def schedule_refresh():
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
                await notify("ℹ️ ג'ליפין לא רוענן — חסר API Key. שלח כאן: /jellyfin <KEY>\n(בלי זה הקבצים יופיעו בסריקה המתוזמנת של ג'ליפין)")
            else:
                await notify(f"⚠️ רענון ג'ליפין נכשל: {res}")

    _refresh_task = asyncio.create_task(later())


# ----------------------------------------------------------------- telegram core
USER: TelegramClient = None   # your account (downloads)
BOT: TelegramClient = None    # control bot (UI) — optional
OWNER_ID = None
BOT_ID = None
QUEUE: asyncio.Queue = None
ACTIVE = {}

KIND_BTN = [("🎬 סרטים", "movies"), ("🇮🇱 ישראלי", "israeli"), ("🧸 ילדים", "kids"), ("📺 סדרות", "series")]


async def notify(text, buttons=None, edit=None):
    """Send (or edit) a message to the owner — via the control bot if available, else Saved Messages."""
    try:
        if edit is not None:
            return await edit.edit(text, buttons=buttons)
        if BOT and OWNER_ID:
            try:
                return await BOT.send_message(OWNER_ID, text, buttons=buttons)
            except Exception as e:
                log.warning("bot send failed (did you press Start?): %s", e)
        return await USER.send_message("me", text)
    except Exception as e:
        log.warning("notify failed: %s", e)


def norm(u):
    return str(u or "").lstrip("@").lower()


def is_source(chat, chat_id):
    srcs = {norm(s) for s in STATE["sources"]}
    return norm(getattr(chat, "username", None)) in srcs or str(chat_id) in srcs


def add_source(key, name):
    key = str(key)
    STATE.setdefault("source_names", {})[key] = name
    if norm(key) not in {norm(s) for s in STATE["sources"]}:
        STATE["sources"].append(key)
        persist()
        return True
    persist()
    return False


def source_label(s):
    n = STATE.get("source_names", {}).get(str(s))
    return f"{n} ({s})" if n and n != s else str(s)


async def enqueue(msg, origin=""):
    key = f"{msg.chat_id}:{msg.id}"
    if key in STATE["done"] or key in ACTIVE:
        return
    ACTIVE[key] = origin
    await QUEUE.put((msg, origin))
    log.info("queued %s from %s", key, origin)


async def worker(n):
    while True:
        msg, origin = await QUEUE.get()
        key = f"{msg.chat_id}:{msg.id}"
        try:
            await process(msg, origin)
            STATE["done"].append(key)
            persist()
        except Exception as e:
            log.exception("download failed %s", key)
            await notify(f"❌ הורדה נכשלה ({origin}): {e}")
        finally:
            ACTIVE.pop(key, None)
            QUEUE.task_done()


def size_text(size):
    return f"{size / 1024 ** 3:.2f}GB" if size > 1024 ** 3 else f"{size / 1024 ** 2:.0f}MB"


def kind_buttons(rid, current):
    return [[Button.inline(("✔️ " if k == current else "") + t, f"mv:{rid}:{k}".encode()) for t, k in KIND_BTN[:2]],
            [Button.inline(("✔️ " if k == current else "") + t, f"mv:{rid}:{k}".encode()) for t, k in KIND_BTN[2:]]]


async def process(msg, origin):
    fname = file_name_of(msg) or ""
    info = classify(fname, msg.message or "")
    dest = unique(target_path(info))
    size = msg.file.size if msg.file else 0
    head = f"⬇️ {info['title']} ({size_text(size)}) → {label(info['kind'])}"
    status = await notify(head + "\n⏳ 0%")

    TMP.mkdir(parents=True, exist_ok=True)
    tmp_file = TMP / f"{abs(msg.chat_id)}_{msg.id}{info['ext']}"
    last = {"t": 0, "pct": -1}

    async def progress(cur, total):
        if not total or not status:
            return
        pct = int(cur * 100 / total)
        now = time.time()
        if pct >= last["pct"] + 5 and now - last["t"] > 6:
            last.update(t=now, pct=pct)
            bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
            try:
                await status.edit(f"{head}\n{bar} {pct}%")
            except Exception:
                pass

    t0 = time.time()
    path = await USER.download_media(msg, file=str(tmp_file), progress_callback=progress)
    if not path or not Path(path).exists() or (size and Path(path).stat().st_size < size * 0.99):
        raise RuntimeError("הקובץ לא ירד במלואו")
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, path, dest)
    mins = (time.time() - t0) / 60
    rel = dest.relative_to(media_root())

    STATE["seq"] = STATE.get("seq", 0) + 1
    rid = str(STATE["seq"])
    STATE["saved"][rid] = {"path": str(dest), "kind": info["kind"], "title": info["title"], "year": info["year"],
                           "season": info["season"], "episode": info["episode"], "ext": info["ext"]}
    STATE["recent"].append(f"{time.strftime('%d/%m %H:%M')} {rel}")
    persist()
    asyncio.create_task(jf_after_download(dest))
    text = f"✅ נשמר: {info['title']}\n📁 {rel}\n⏱ {mins:.1f} דק' · {size_text(size)}\n\nקטגוריה לא נכונה? בחר:"
    if BOT and status and getattr(status, "sender_id", None) == BOT_ID:
        await notify(text, buttons=kind_buttons(rid, info["kind"]), edit=status)
    else:
        if status:
            try:
                await status.delete()
            except Exception:
                pass
        await notify(text.replace("בחר:", "השב סרטים/ישראלי/ילדים/סדרות") + f"  #{rid}",
                     buttons=kind_buttons(rid, info["kind"]) if BOT else None)
    schedule_refresh()


async def move_saved(rid, new_kind):
    rec = STATE["saved"].get(str(rid))
    if not rec:
        return "⚠️ הרשומה לא נמצאה"
    src = Path(rec["path"])
    if rec["kind"] == new_kind:
        return None
    if not src.exists():
        return "⚠️ הקובץ כבר לא נמצא במיקום המקורי"
    info = dict(rec, kind=new_kind)
    if new_kind == "series" and not info.get("season"):
        info["season"] = 1
    dest = unique(target_path(info))
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src), dest)
    root = media_root()
    for d in (src.parent, src.parent.parent):
        try:
            if root in d.parents and d.parent != root and not any(d.iterdir()):
                d.rmdir()
            elif root in d.parents and d.parent == root:
                break
        except Exception:
            pass
    rec.update(path=str(dest), kind=new_kind)
    persist()
    schedule_refresh()
    return f"📦 הועבר ל: {dest.relative_to(root)}"


# ----------------------------------------------------------------- proxmox
def pve_ok():
    return bool(CFG.get("pve_url") and CFG.get("pve_token"))


async def pve(method, path, data=None):
    url = CFG["pve_url"].rstrip("/") + "/api2/json" + path
    hdr = {"Authorization": f"PVEAPIToken={CFG['pve_token']}"}
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, headers=hdr, data=data, ssl=False,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            j = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"Proxmox HTTP {r.status}: {j}")
            return j.get("data")


def fmt_bytes(b):
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}PB"


def fmt_up(sec):
    sec = int(sec or 0)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def nas_space():
    try:
        u = shutil.disk_usage(media_root())
        return f"💾 סינולוג'י: פנוי {fmt_bytes(u.free)} מתוך {fmt_bytes(u.total)} ({u.used * 100 // u.total}% בשימוש)"
    except Exception as e:
        return f"💾 סינולוג'י: לא נגיש ({e})"


async def server_view():
    if not pve_ok():
        return "🖥 פרוקסמוקס לא מוגדר עדיין (הרץ את פקודת העדכון)", [[Button.inline("⬅️ תפריט", b"m:main")]]
    lines, rows = [], []
    for n in await pve("GET", "/nodes"):
        st = await pve("GET", f"/nodes/{n['node']}/status")
        mem = st.get("memory", {})
        lines.append(f"🖥 {n['node']} — {'🟢' if n.get('status') == 'online' else '🔴'}  up {fmt_up(st.get('uptime'))}\n"
                     f"   CPU {st.get('cpu', 0) * 100:.0f}% · RAM {fmt_bytes(mem.get('used'))}/{fmt_bytes(mem.get('total'))}"
                     f" ({(mem.get('used', 0) * 100 // max(mem.get('total', 1), 1))}%)")
    lines.append(nas_space())
    res = await pve("GET", "/cluster/resources?type=vm")
    res.sort(key=lambda x: x.get("vmid", 0))
    lines.append("\nמכונות:")
    btns = []
    for v in res:
        on = v.get("status") == "running"
        icon = "🟢" if on else "🔴"
        typ = "📦" if v.get("type") == "lxc" else "💻"
        extra = f" · CPU {v.get('cpu', 0) * 100:.0f}% · {fmt_bytes(v.get('mem'))}" if on else ""
        lines.append(f"{icon}{typ} {v['vmid']} {v.get('name', '')}{extra}")
        btns.append(Button.inline(f"{icon} {v.get('name', v['vmid'])}"[:30],
                                  f"vm:{v['node']}:{v['type']}:{v['vmid']}".encode()))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([Button.inline("🔄 רענן", b"m:srv"), Button.inline("⬅️ תפריט", b"m:main")])
    return "\n".join(lines), rows


async def vm_view(node, typ, vmid):
    st = await pve("GET", f"/nodes/{node}/{typ}/{vmid}/status/current")
    on = st.get("status") == "running"
    txt = (f"{'🟢' if on else '🔴'} {st.get('name', vmid)} ({typ} {vmid})\n"
           f"מצב: {st.get('status')}\n")
    if on:
        txt += (f"Uptime: {fmt_up(st.get('uptime'))}\nCPU: {st.get('cpu', 0) * 100:.0f}% מתוך {st.get('cpus')} ליבות\n"
                f"RAM: {fmt_bytes(st.get('mem'))}/{fmt_bytes(st.get('maxmem'))}")
    acts = [("🔄 ריסטרט", "reboot"), ("⏹ כיבוי", "shutdown"), ("⛔ כיבוי כפוי", "stop")] if on else [("▶️ הפעלה", "start")]
    base = f"{node}:{typ}:{vmid}"
    rows = [[Button.inline(t, f"va:{base}:{a}".encode()) for t, a in acts]]
    rows.append([Button.inline("🔄 רענן", f"vm:{base}".encode()), Button.inline("⬅️ שרת", b"m:srv")])
    return txt, rows


ACT_HE = {"start": "הפעלה", "shutdown": "כיבוי", "reboot": "ריסטרט", "stop": "כיבוי כפוי"}


# ----------------------------------------------------------------- torrents (qBittorrent on localhost)
QBT = os.environ.get("QBT_URL", "http://127.0.0.1:8080")
TOR_DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP"}
TOR_STATE_HE = {"downloading": "⬇️ מוריד", "stalledDL": "⏳ ממתין לשותפים", "metaDL": "🔎 מושך מידע",
                "queuedDL": "🕒 בתור", "pausedDL": "⏸ מושהה", "stoppedDL": "⏸ מושהה", "checkingDL": "🔍 בודק",
                "forcedDL": "⬇️ מוריד", "error": "❌ שגיאה", "missingFiles": "❌ קבצים חסרים", "moving": "📦 מעביר"}
SUB_EXT = {".srt", ".ass", ".sub", ".vtt"}


async def qbt(method, path, data=None, raw=False):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"{QBT}/api/v2{path}", data=data,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status >= 400:
                raise RuntimeError(f"qBittorrent HTTP {r.status}: {(await r.text())[:100]}")
            if raw:
                return await r.text()
            return await r.json(content_type=None)


async def qbt_ok():
    try:
        await qbt("GET", "/app/version", raw=True)
        return True
    except Exception:
        return False


async def tor_add(magnet=None, file_bytes=None, file_name="file.torrent"):
    STATE["seq"] = STATE.get("seq", 0) + 1
    tag = f"tg{STATE['seq']}"
    form = aiohttp.FormData()
    form.add_field("tags", tag)
    form.add_field("savepath", str(TOR_DIR()))
    if magnet:
        form.add_field("urls", magnet)
    else:
        form.add_field("torrents", file_bytes, filename=file_name, content_type="application/x-bittorrent")
    res = await qbt("POST", "/torrents/add", data=form, raw=True)
    if "fail" in res.lower():
        raise RuntimeError("qBittorrent דחה את הטורנט (אולי כבר קיים?)")
    for _ in range(15):
        lst = await qbt("GET", f"/torrents/info?tag={tag}")
        if lst:
            t = lst[0]
            msg = await notify(tor_text(t), buttons=tor_buttons(t))
            STATE.setdefault("torrents", {})[t["hash"]] = {"msg": getattr(msg, "id", None), "tag": tag,
                                                          "last": 0, "pct": -1}
            persist()
            return t
        await asyncio.sleep(1)
    raise RuntimeError("הטורנט נוסף אבל לא הופיע ברשימה")


def TOR_DIR():
    return media_root() / ".downloads"


def tor_text(t):
    pct = int(t.get("progress", 0) * 100)
    bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
    st = TOR_STATE_HE.get(t.get("state"), t.get("state"))
    eta = t.get("eta", 8640000)
    eta_txt = "" if eta >= 8640000 else f" · נותרו {fmt_up(eta)}"
    return (f"🧲 {t.get('name', '')[:80]}\n{st} · {fmt_bytes(t.get('size'))}\n{bar} {pct}%\n"
            f"⚡ {fmt_bytes(t.get('dlspeed'))}/s · 👥 {t.get('num_seeds', 0)} מקורות{eta_txt}")


def tor_buttons(t, named=False):
    h = t["hash"]
    paused = t.get("state") in ("pausedDL", "stoppedDL")
    n = f" {t.get('name', '')[:18]}" if named else ""
    return [[Button.inline(("▶️ המשך" if paused else "⏸ השהה") + n, f"{'tr' if paused else 'tp'}:{h}".encode()),
             Button.inline("🗑 ביטול" + n, f"td:{h}".encode())]]


async def tor_pause(h, resume=False):
    for ep in (("resume", "start") if resume else ("pause", "stop")):
        try:
            await qbt("POST", f"/torrents/{ep}", data={"hashes": h}, raw=True)
            return
        except Exception:
            continue


async def tor_finish(t):
    """Torrent complete: stop seeding, move video files into the library."""
    h = t["hash"]
    files = await qbt("GET", f"/torrents/files?hash={h}")
    save = Path(t.get("save_path") or TOR_DIR())
    await qbt("POST", "/torrents/delete", data={"hashes": h, "deleteFiles": "false"}, raw=True)
    await asyncio.sleep(2)
    paths = [save / f["name"] for f in files]
    videos = [p for p in paths if p.suffix.lower() in VIDEO_EXT and p.exists()]
    big = [p for p in videos if p.stat().st_size > 150 * 1024 ** 2 and "sample" not in p.name.lower()]
    videos = big or videos
    subs = [p for p in paths if p.suffix.lower() in SUB_EXT and p.exists()]
    tname = t.get("name", "")
    results = []
    for v in videos:
        if len(videos) == 1:
            info = classify(tname + v.suffix, "")       # torrent name is usually the cleanest
            vi = classify(v.name, tname)
            if vi["kind"] == "series" or (vi.get("year") and not info.get("year")):
                info = vi
        else:
            info = classify(v.name, tname)
        dest = unique(target_path(info))
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(v), dest)
        # subtitles: same stem, or all subs if single video
        for i, s in enumerate(subs):
            if len(videos) == 1 or s.stem.lower().startswith(v.stem.lower()):
                lang = ".he" if HEB_RE.search(s.name) or re.search(r"heb|hebrew|\.he\b", s.name, re.I) else ""
                sd = dest.with_name(f"{dest.stem}{lang}{'.' + str(i) if i and not lang else ''}{s.suffix.lower()}")
                try:
                    await asyncio.to_thread(shutil.move, str(s), unique(sd))
                except Exception:
                    pass
        STATE["seq"] = STATE.get("seq", 0) + 1
        rid = str(STATE["seq"])
        STATE["saved"][rid] = {"path": str(dest), "kind": info["kind"], "title": info["title"], "year": info["year"],
                               "season": info["season"], "episode": info["episode"], "ext": info["ext"]}
        STATE["recent"].append(f"{time.strftime('%d/%m %H:%M')} {dest.relative_to(media_root())}")
        results.append((rid, info, dest))
    # clean leftovers (nfo, txt, samples, empty dirs)
    top = save / Path(files[0]["name"]).parts[0] if files and len(Path(files[0]["name"]).parts) > 1 else None
    if top and top.exists() and TOR_DIR() in top.parents:
        await asyncio.to_thread(shutil.rmtree, top, True)
    persist()
    rec = STATE.get("torrents", {}).pop(h, {})
    persist()
    if not results:
        return await notify(f"⚠️ הטורנט הסתיים אבל לא נמצאו בו קבצי וידאו: {tname}")
    if len(results) == 1:
        rid, info, dest = results[0]
        asyncio.create_task(jf_after_download(dest))
        text = f"✅ נשמר: {info['title']}\n📁 {dest.relative_to(media_root())}\n\nקטגוריה לא נכונה? בחר:"
        btn = kind_buttons(rid, info["kind"])
    else:
        folders = sorted({str(d.parent.relative_to(media_root())) for _, _, d in results})
        text = f"✅ נשמרו {len(results)} קבצים מ: {tname[:60]}\n📁 " + "\n📁 ".join(folders[:5])
        btn = None
    if rec.get("msg") and BOT:
        try:
            await BOT.edit_message(OWNER_ID, rec["msg"], text, buttons=btn)
        except Exception:
            await notify(text, buttons=btn)
    else:
        await notify(text, buttons=btn)
    schedule_refresh()


async def tor_monitor():
    while True:
        await asyncio.sleep(10)
        tracked = STATE.get("torrents", {})
        if not tracked:
            continue
        try:
            lst = {t["hash"]: t for t in await qbt("GET", "/torrents/info")}
        except Exception as e:
            log.warning("qbt poll: %s", e)
            continue
        for h, rec in list(tracked.items()):
            t = lst.get(h)
            if not t:
                tracked.pop(h, None)
                persist()
                continue
            try:
                if t.get("state") in TOR_DONE_STATES or t.get("progress", 0) >= 1:
                    await tor_finish(t)
                    continue
                pct = int(t.get("progress", 0) * 100)
                now = time.time()
                if rec.get("msg") and BOT and (pct != rec.get("pct")) and now - rec.get("last", 0) > 20:
                    rec.update(pct=pct, last=now)
                    await BOT.edit_message(OWNER_ID, rec["msg"], tor_text(t), buttons=tor_buttons(t))
            except Exception as e:
                log.warning("torrent %s: %s", h, e)


async def torrents_view():
    if not await qbt_ok():
        return "🧲 qBittorrent לא זמין (הרץ את פקודת העדכון)", [[Button.inline("⬅️ תפריט", b"m:main")]]
    lst = await qbt("GET", "/torrents/info")
    if not lst:
        txt = "🧲 אין טורנטים פעילים.\nשלח לי קישור magnet או קובץ ‎.torrent כדי להתחיל."
    else:
        txt = "🧲 טורנטים:\n\n" + "\n\n".join(tor_text(t) for t in lst[:8])
    rows = []
    for t in lst[:8]:
        rows += tor_buttons(t, named=len(lst) > 1)
    rows.append([Button.inline("🔄 רענן", b"m:tor"), Button.inline("⬅️ תפריט", b"m:main")])
    return txt, rows


# ----------------------------------------------------------------- YouTube (yt-dlp)
YT_RE = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?\S+|playlist\?\S+|shorts/\S+|@\S+|live/\S+)|youtu\.be/\S+)")
YT_PENDING = {}
YT_LOCK = asyncio.Lock()


def yt_root(kids=False):
    r = media_root() / "YouTube"
    return r / "ילדים" if kids else r


def yt_run(url, outdir, prog):
    from yt_dlp import YoutubeDL

    def hook(d):
        info = d.get("info_dict") or {}
        prog["title"] = info.get("title") or prog.get("title")
        prog["idx"] = info.get("playlist_index") or prog.get("idx")
        prog["n"] = info.get("n_entries") or info.get("playlist_count") or prog.get("n")
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            prog["pct"] = int(d.get("downloaded_bytes", 0) * 100 / tot) if tot else prog.get("pct", 0)
            prog["speed"] = d.get("speed") or 0
        elif d.get("status") == "finished":
            prog["files"] = prog.get("files", 0) + 1

    def pp_hook(d):
        if d.get("status") == "finished" and d.get("postprocessor") in ("MoveFiles", "MoveFilesAfterDownload"):
            prog["done"] = prog.get("done", 0) + 1
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                prog["last_dir"] = str(Path(fp).parent)

    (TMP / "yt").mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "paths": {"home": str(outdir), "temp": str(TMP / "yt")},
        "outtmpl": "%(playlist_title,channel,uploader|YouTube)s/%(title).120B [%(id)s].%(ext)s",
        "windowsfilenames": True,
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                           {"key": "FFmpegMetadata"}],
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "concurrent_fragment_downloads": 4,
        "download_archive": str(BASE / "yt_archive.txt"),
    }
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    prog["finished"] = True
    return info


async def yt_job(url, kids):
    async with YT_LOCK:
        outdir = yt_root(kids)
        outdir.mkdir(parents=True, exist_ok=True)
        prog = {"pct": 0}
        status = await notify(f"▶️ YouTube: מתחיל…\n{url}")
        task = asyncio.create_task(asyncio.to_thread(yt_run, url, outdir, prog))
        last = ""
        while not task.done():
            await asyncio.sleep(8)
            pl = f" ({prog.get('idx')}/{prog.get('n')})" if prog.get("n") else ""
            txt = (f"▶️ YouTube{pl}\n{(prog.get('title') or '')[:80]}\n"
                   f"{'▓' * (prog.get('pct', 0) // 10)}{'░' * (10 - prog.get('pct', 0) // 10)} {prog.get('pct', 0)}%"
                   f" · ⚡ {fmt_bytes(prog.get('speed'))}/s")
            if txt != last and status:
                last = txt
                try:
                    await status.edit(txt)
                except Exception:
                    pass
        try:
            info = task.result()
        except Exception as e:
            await notify(f"❌ YouTube נכשל: {e}"[:400], edit=status)
            return
        n = prog.get("done") or (1 if info else 0)
        where = prog.get("last_dir")
        rel = Path(where).relative_to(media_root()) if where and media_root() in Path(where).parents else outdir.name
        title = (info or {}).get("title", "")
        txt = (f"✅ YouTube: ירדו {n} סרטונים\n🎞 {title[:80]}\n📁 {rel}" if n else
               f"ℹ️ לא ירד שום דבר חדש (אולי כבר הורד בעבר)\n🎞 {title[:80]}")
        await notify(txt, edit=status)
        if n:
            schedule_refresh()


# ----------------------------------------------------------------- network scanner
NET_PORTS = [21, 22, 23, 53, 80, 139, 443, 445, 515, 548, 554, 631, 1883, 3000, 3389, 5000, 5001, 5357, 5555,
             5900, 6053, 6668, 7000, 8000, 8001, 8006, 8008, 8009, 8080, 8096, 8123, 8443, 8899, 9000, 9100,
             32400, 34567, 37777, 49152, 62078]
PORT_NAMES = {22: "SSH", 23: "Telnet", 53: "DNS", 80: "HTTP", 139: "NetBIOS", 443: "HTTPS", 445: "SMB", 515: "LPD",
              548: "AFP", 554: "RTSP", 631: "IPP", 1883: "MQTT", 3389: "RDP", 5000: "DSM", 5001: "DSM-SSL",
              5357: "WSD", 5555: "ADB", 5900: "VNC", 6053: "ESPHome", 6668: "Tuya", 7000: "AirPlay",
              8000: "Hik-SDK", 8001: "TV-API", 8006: "Proxmox", 8008: "Cast", 8009: "Cast", 8080: "HTTP-alt",
              8096: "Jellyfin", 8123: "HA", 8443: "HTTPS-alt", 9100: "Printer", 32400: "Plex", 34567: "DVR",
              37777: "Dahua", 62078: "iOS"}


def mac_random(mac):
    try:
        return int(mac.split(":")[0], 16) & 0x02 == 2
    except Exception:
        return False


def guess_type(d):
    v = (d.get("vendor") or "").lower()
    p = set(d.get("ports") or [])
    h = f"{d.get('hostname') or ''} {d.get('title') or ''} {d.get('server') or ''}".lower()
    has = lambda *ws: any(w in v or w in h for w in ws)
    if 8006 in p or "proxmox" in h:
        return "🖥 שרת Proxmox"
    if has("synology") or {5000, 5001} & p and "synology" in h:
        return "💾 Synology NAS"
    if 8123 in p or "home assistant" in h:
        return "🏠 Home Assistant"
    if has("hikvision", "dahua", "uniview", "reolink", "hangzhou", "ezviz", "imou", "axis", "hanwha", "tiandy") \
            or {554, 8000, 37777, 34567} & p and not {445, 3389} & p:
        return "📹 מצלמה / NVR"
    if {9100, 631, 515} & p or has("printer", "laserjet", "epson", "canon", "brother", "xerox", "kyocera"):
        return "🖨 מדפסת"
    if 8096 in p:
        return "🎬 Jellyfin"
    if {8008, 8009} & p or has("chromecast", "google home", "nest"):
        return "📺 Chromecast / Google"
    if 62078 in p:
        return "📱 iPhone / iPad"
    if has("apple"):
        return "🍎 מכשיר Apple" + (" (Apple TV/Mac)" if 7000 in p else "")
    if has("samsung", "lg electronics", "lge", "sony", "tcl", "hisense", "vizio", "philips", "xiaomi tv") and \
            ({8001, 8002, 3000, 7000, 9000} & p or "tv" in h):
        return "📺 טלוויזיה חכמה"
    if has("espressif", "tuya", "shelly", "itead", "sonoff", "broadlink", "yeelight", "lumi", "aqara", "tasmota",
           "esphome", "switchbot", "meross", "wiz") or {6053, 6668, 1883} & p:
        return "💡 בית חכם (IoT)"
    if has("amazon", "echo"):
        return "🔊 Amazon Echo / Fire"
    if has("raspberry"):
        return "🍓 Raspberry Pi"
    if has("proxmox server solutions") or (d.get("mac", "").lower().startswith("bc:24:11")):
        return "📦 מכונה וירטואלית"
    if {3389, 445, 139, 5357} & p or has("windows", "desktop-", "laptop-"):
        return "💻 מחשב Windows"
    if has("playstation", "nintendo", "xbox", "valve"):
        return "🎮 קונסולה"
    if has("tp-link", "tplink", "ubiquiti", "mikrotik", "netgear", "asus", "zyxel", "huawei", "d-link", "cisco",
           "ruckus", "aruba", "tenda", "mercusys", "router", "access point", "deco"):
        return "📡 ציוד רשת"
    if has("samsung", "xiaomi", "oneplus", "oppo", "vivo", "realme", "motorola", "huawei device", "honor"):
        return "📱 טלפון / טאבלט"
    if mac_random(d.get("mac", "")):
        return "📱 טלפון/טאבלט (MAC פרטי)"
    if 22 in p:
        return "🐧 מכשיר לינוקס"
    return "❓ לא מזוהה"


async def port_open(ip, port, timeout=0.8):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def http_hint(ip, ports):
    for port, scheme in ((80, "http"), (443, "https"), (8080, "http"), (8443, "https"), (5000, "http")):
        if port not in ports:
            continue
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{scheme}://{ip}:{port}/", ssl=False, allow_redirects=True,
                                 timeout=aiohttp.ClientTimeout(total=4)) as r:
                    body = (await r.content.read(30000)).decode("utf-8", "ignore")
                    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                    return (m.group(1).strip()[:60] if m else ""), r.headers.get("Server", "")[:40]
        except Exception:
            continue
    return "", ""


async def probe(ip, mac, vendor):
    ports = [p for p, ok in zip(NET_PORTS, await asyncio.gather(*(port_open(ip, p) for p in NET_PORTS))) if ok]
    try:
        hostname = (await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, ip), 3))[0]
    except Exception:
        hostname = ""
    title, server = await http_hint(ip, ports)
    d = {"ip": ip, "mac": mac, "vendor": vendor, "ports": ports, "hostname": hostname,
         "title": title, "server": server}
    d["type"] = guess_type(d)
    return d


async def arp_scan():
    proc = await asyncio.create_subprocess_exec(
        "arp-scan", "--localnet", "--plain", "--retry=2", "--interface", CFG.get("net_iface", "eth0"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    if proc.returncode != 0:
        raise RuntimeError(err.decode()[:200])
    res = {}
    for line in out.decode("utf-8", "ignore").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and re.match(r"\d+\.\d+\.\d+\.\d+$", parts[0]):
            res[parts[1].lower()] = (parts[0], parts[2].strip() if len(parts) > 2 else "")
    return res


def dev_name(d):
    return d.get("name") or d.get("hostname") or d.get("title") or d.get("vendor") or d.get("mac")


def dev_text(d, full=True):
    st = {"new": "🆕 חדש", "known": "✅ מוכר", "flagged": "⚠️ חשוד"}.get(d.get("status"), "")
    online = time.time() - d.get("last", 0) < 600
    lines = [f"{d.get('type', '❓')} — {dev_name(d)}",
             f"{'🟢 מחובר' if online else '⚪ לא מחובר'} · {st}",
             f"🌐 {d.get('ip')} · 🔖 {d.get('mac')}"]
    if full:
        if d.get("vendor"):
            lines.append(f"🏭 יצרן: {d['vendor']}")
        if d.get("hostname"):
            lines.append(f"🏷 שם ברשת: {d['hostname']}")
        if d.get("title") or d.get("server"):
            lines.append(f"🖥 דף ניהול: {d.get('title') or ''} {('(' + d['server'] + ')') if d.get('server') else ''}")
        if d.get("ports"):
            lines.append("🔌 פורטים: " + ", ".join(f"{p}/{PORT_NAMES.get(p, '?')}" for p in d["ports"]))
        if mac_random(d.get("mac", "")):
            lines.append("🔒 כתובת MAC אקראית (פרטיות של טלפון) — היצרן מוסתר")
        lines.append(f"📅 נראה לראשונה: {time.strftime('%d/%m %H:%M', time.localtime(d.get('first', 0)))}"
                     f" · אחרון: {time.strftime('%d/%m %H:%M', time.localtime(d.get('last', 0)))}")
    return "\n".join(lines)


def dev_buttons(mac):
    k = mac.replace(":", "")
    return [[Button.inline("✏️ תן שם", f"nn:{k}".encode()), Button.inline("✅ מוכר", f"nk:{k}".encode()),
             Button.inline("⚠️ חשוד", f"nf:{k}".encode())],
            [Button.inline("🔄 זיהוי מחדש", f"nr:{k}".encode()), Button.inline("🗑 שכח", f"nx:{k}".encode()),
             Button.inline("⬅️ רשת", b"m:net:0")]]


def mac_from_key(k):
    return ":".join(k[i:i + 2] for i in range(0, 12, 2))


async def net_scan_once(alert=True):
    devs = STATE.setdefault("devices", {})
    found = await arp_scan()
    now = time.time()
    new = []
    for mac, (ip, vendor) in found.items():
        d = devs.get(mac)
        if d is None:
            d = await probe(ip, mac, vendor)
            d.update(first=now, last=now, status="new" if STATE.get("net_baselined") else "known")
            devs[mac] = d
            new.append(d)
        else:
            if d.get("ip") != ip:
                d["ip"] = ip
            if d.get("status") == "flagged" and now - d.get("last", 0) > 900 and alert:
                await notify("⚠️ מכשיר שסימנת כחשוד חזר לרשת:\n\n" + dev_text(d, False), buttons=dev_buttons(mac))
            d["last"] = now
    persist()
    if not STATE.get("net_baselined"):
        STATE["net_baselined"] = True
        persist()
        types = {}
        for d in devs.values():
            types[d["type"]] = types.get(d["type"], 0) + 1
        summary = "\n".join(f"{t}: {n}" for t, n in sorted(types.items(), key=lambda x: -x[1]))
        await notify(f"🌐 סריקת רשת ראשונה הושלמה — נמצאו {len(devs)} מכשירים:\n\n{summary}\n\n"
                     f"מעכשיו כל מכשיר חדש יקפיץ התראה.", buttons=[[Button.inline("📋 לרשימה", b"m:net:0")]])
    elif alert:
        for d in new:
            await notify("🆕 מכשיר חדש התחבר לרשת!\n\n" + dev_text(d), buttons=dev_buttons(d["mac"]))
    return len(found), len(new)


async def net_monitor():
    await asyncio.sleep(20)
    while True:
        try:
            await net_scan_once()
        except Exception as e:
            log.warning("net scan: %s", e)
        await asyncio.sleep(int(CFG.get("net_interval", 180)))


def net_view(page=0):
    devs = list(STATE.get("devices", {}).values())
    now = time.time()
    devs.sort(key=lambda d: (now - d.get("last", 0) > 600, d.get("type", ""), dev_name(d).lower()))
    online = sum(1 for d in devs if now - d.get("last", 0) < 600)
    per = 14
    chunk = devs[page * per:(page + 1) * per]
    lines = [f"🌐 רשת ביתית — {online} מחוברים מתוך {len(devs)} מוכרים", ""]
    rows = []
    for d in chunk:
        on = "🟢" if now - d.get("last", 0) < 600 else "⚪"
        flag = " 🆕" if d.get("status") == "new" else (" ⚠️" if d.get("status") == "flagged" else "")
        lines.append(f"{on} {d['type'].split(' ')[0]} {dev_name(d)[:28]} · {d.get('ip')}{flag}")
        rows.append(Button.inline(f"{d['type'].split(' ')[0]} {dev_name(d)[:22]}", f"nd:{d['mac'].replace(':', '')}".encode()))
    rows = [rows[i:i + 2] for i in range(0, len(rows), 2)]
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ הקודם", f"m:net:{page - 1}".encode()))
    if (page + 1) * per < len(devs):
        nav.append(Button.inline("הבא ➡️", f"m:net:{page + 1}".encode()))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("🔍 סרוק עכשיו", b"m:scan"), Button.inline("⬅️ תפריט", b"m:main")])
    return "\n".join(lines), rows


PENDING = {}


# ----------------------------------------------------------------- Jellyfin identify / fix
JUNK_HE = ["נתי מדיה", "סטאר מדיה", "סטארמדיה", "מדובב לעברית", "מדובב", "דיבוב", "תרגום מובנה", "כתוביות מובנות",
           "כתוביות", "תרגום", "איכות גבוהה", "באיכות", "HD", "FHD", "מלא", "סרט מלא", "לצפייה ישירה", "להורדה",
           "ערוץ", "קבוצת", "HebDub", "HebSub", "Heb Dub", "Heb Sub", "Hebrew"]


def clean_title(s):
    s = str(s or "")
    s = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", s)
    for w in sorted(set(JUNK_HE + CFG.get("strip_words", []) + STATE.get("strip_words", [])), key=len, reverse=True):
        s = re.sub(re.escape(w), " ", s, flags=re.I)
    s = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", " ", s)   # emoji
    s = re.sub(r"(?<!\d)[._]|[._](?!\d)", " ", s)
    s = re.sub(r"[\-–|]+", " ", s)
    s = re.sub(r"\b(1080p|720p|480p|2160p|4k|web[- ]?dl|webrip|bluray|brrip|hdrip|x26[45]|h26[45]|hevc|aac|dd5 1)\b", " ", s, flags=re.I)
    s = re.sub(r"[\[\](){}]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


JF_CAND = {}


async def jf(method, path, params=None, body=None, raw=False):
    url = (CFG.get("jellyfin_url") or "").rstrip("/")
    key = STATE.get("jellyfin_key")
    if not url or not key:
        raise RuntimeError("חסר מפתח ג'ליפין (/jellyfin KEY)")
    hdr = {"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key}
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url + path, params=params, json=body, headers=hdr,
                             timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 400:
                raise RuntimeError(f"Jellyfin HTTP {r.status}: {(await r.text())[:120]}")
            if raw:
                return await r.read()
            if r.status == 204:
                return None
            return await r.json(content_type=None)


async def jf_user():
    if not STATE.get("jf_user"):
        users = await jf("GET", "/Users")
        admin = next((u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
        STATE["jf_user"] = admin["Id"]
        persist()
    return STATE["jf_user"]


async def jf_get(item_id):
    uid = await jf_user()
    return await jf("GET", f"/Users/{uid}/Items/{item_id}")


async def jf_find_path(dest: Path):
    items = await jf("GET", "/Items", params={"Recursive": "true", "SortBy": "DateCreated", "SortOrder": "Descending",
                                              "Limit": "80", "Fields": "Path,ProductionYear",
                                              "IncludeItemTypes": "Movie,Episode,Video"})
    for it in items.get("Items", []):
        p = it.get("Path") or ""
        if Path(p).name == dest.name and Path(p).parent.name == dest.parent.name:
            if it.get("Type") == "Episode" and it.get("SeriesId"):
                return {"Id": it["SeriesId"], "Type": "Series", "Name": it.get("SeriesName")}
            return it
    return None


async def jf_search(term):
    res = await jf("GET", "/Items", params={"Recursive": "true", "SearchTerm": term, "Limit": "12",
                                            "IncludeItemTypes": "Movie,Series,Video", "Fields": "ProductionYear,Path"})
    return res.get("Items", [])


def jf_type(item):
    return "Series" if item.get("Type") == "Series" else "Movie"


async def jf_panel(item_id, note=""):
    it = await jf_get(item_id)
    yr = f" ({it.get('ProductionYear')})" if it.get("ProductionYear") else ""
    path = Path(it.get("Path") or "")
    cap = (f"{note}\n" if note else "") + f"🎬 {it.get('Name')}{yr}\n📁 {path.parent.name}/{path.name}"[:300]
    btn = [[Button.inline("✏️ שנה שם", f"jn:{item_id}".encode()), Button.inline("🖼 החלף תמונה", f"jp:{item_id}".encode())],
           [Button.inline("⬅️ לרשימה", b"m:fix"), Button.inline("✅ סגור", b"jok")]]
    img = None
    try:
        img = await jf("GET", f"/Items/{item_id}/Images/Primary", params={"maxHeight": "700"}, raw=True)
    except Exception:
        pass
    return cap, btn, img


async def jf_send_panel(item_id, note=""):
    cap, btn, img = await jf_panel(item_id, note)
    if img and BOT:
        f = io.BytesIO(img)
        f.name = "poster.jpg"
        return await BOT.send_file(OWNER_ID, f, caption=cap, buttons=btn)
    return await notify(cap, buttons=btn)


async def jf_candidates(item_id, query=None):
    it = await jf_get(item_id)
    if query:
        q, year = query, None
    else:
        g = classify(Path(it.get("Path") or "x").name, "")
        q = clean_title(g["title"]) or clean_title(it.get("Name"))
        year = g.get("year")
    res = await jf("POST", f"/Items/RemoteSearch/{jf_type(it)}",
                   body={"SearchInfo": {"Name": q, "Year": year}, "ItemId": item_id,
                         "IncludeDisabledProviders": False})
    if not res and year:
        res = await jf("POST", f"/Items/RemoteSearch/{jf_type(it)}",
                       body={"SearchInfo": {"Name": q}, "ItemId": item_id, "IncludeDisabledProviders": False})
    STATE["seq"] = STATE.get("seq", 0) + 1
    k = str(STATE["seq"])
    JF_CAND[k] = (item_id, (res or [])[:8])
    rows = [[Button.inline(f"{r.get('Name', '')[:30]} ({r.get('ProductionYear') or '?'})", f"ja:{k}:{i}".encode())]
            for i, r in enumerate(JF_CAND[k][1])]
    rows.append([Button.inline("🔎 חפש בשם אחר", f"js:{item_id}".encode()), Button.inline("❌ סגור", b"jok")])
    txt = f"🔄 חיפוש: \"{q}\"" + (f" ({year})" if year else "") + ("\nבחר את הנכון:" if res else "\nלא נמצאו תוצאות — נסה חיפוש בשם אחר (גם באנגלית).")
    return txt, rows


async def jf_apply(k, i):
    item_id, res = JF_CAND.get(k, (None, []))
    if not item_id or i >= len(res):
        raise RuntimeError("הבחירה פגה — נסה שוב")
    await jf("POST", f"/Items/RemoteSearch/Apply/{item_id}", params={"ReplaceAllImages": "true"}, body=res[i])
    return item_id


async def jf_rename(item_id, name):
    it = await jf_get(item_id)
    it["Name"] = name
    locked = set(it.get("LockedFields") or [])
    locked.add("Name")
    it["LockedFields"] = sorted(locked)
    await jf("POST", f"/Items/{item_id}", body=it)


async def jf_image(item_id, url):
    await jf("POST", f"/Items/{item_id}/RemoteImages/Download", params={"type": "Primary", "imageUrl": url})


async def jf_upload_image(item_id, data: bytes):
    url = (CFG.get("jellyfin_url") or "").rstrip("/")
    key = STATE.get("jellyfin_key")
    hdr = {"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key, "Content-Type": "image/jpeg"}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/Items/{item_id}/Images/Primary", data=base64.b64encode(data), headers=hdr,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 400:
                raise RuntimeError(f"Jellyfin HTTP {r.status}: {(await r.text())[:120]}")


async def jf_libraries_view():
    libs = await jf("GET", "/Library/VirtualFolders")
    rows = [[Button.inline("🆕 נוספו לאחרונה", b"jl:new:0")]]
    for lib in libs:
        if lib.get("ItemId"):
            rows.append([Button.inline(f"📚 {lib.get('Name')}", f"jl:{lib['ItemId']}:0".encode())])
    rows.append([Button.inline("🔎 חיפוש לפי שם", b"jq"), Button.inline("🗂 סידור ספריות", b"lb:plan")])
    rows.append([Button.inline("⬅️ תפריט", b"m:main")])
    return "🔧 תיקון שמות ותמונות — בחר ספרייה:", rows


async def jf_list_view(lib, page):
    per = 12
    params = {"Recursive": "true", "IncludeItemTypes": "Movie,Series,Video", "Fields": "ProductionYear",
              "SortBy": "DateCreated", "SortOrder": "Descending", "StartIndex": str(page * per), "Limit": str(per)}
    if lib != "new":
        params["ParentId"] = lib
    res = await jf("GET", "/Items", params=params)
    items, total = res.get("Items", []), res.get("TotalRecordCount", 0)
    rows = [[Button.inline(f"{it.get('Name', '')[:34]}" + (f" ({it['ProductionYear']})" if it.get("ProductionYear") else ""),
                           f"ji:{it['Id']}".encode())] for it in items]
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ הקודם", f"jl:{lib}:{page - 1}".encode()))
    if (page + 1) * per < total:
        nav.append(Button.inline("הבא ➡️", f"jl:{lib}:{page + 1}".encode()))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("⬅️ ספריות", b"m:fix")])
    return f"🎬 בחר פריט ({page * per + 1}-{min((page + 1) * per, total)} מתוך {total}):", rows


async def jf_after_download(dest: Path, delay=150):
    """Wait for Jellyfin to scan the new file, then show what it matched with fix buttons."""
    if not STATE.get("jellyfin_key") or not CFG.get("identify_after_download", False):
        return
    await asyncio.sleep(delay)
    for _ in range(4):
        try:
            it = await jf_find_path(dest)
            if it:
                await jf_send_panel(it["Id"], "🔍 ג'ליפין זיהה את ההורדה כך:")
                return
        except Exception as e:
            log.warning("jf identify: %s", e)
        await asyncio.sleep(90)


async def jf_menu_find(term):
    items = await jf_search(term)
    if not items:
        return f"לא נמצא בג'ליפין: {term}\nנסה מילה אחרת מהשם שמופיע במסך.", None
    rows = [[Button.inline(f"{it.get('Name', '')[:30]} ({it.get('ProductionYear') or '?'})", f"ji:{it['Id']}".encode())]
            for it in items]
    return f"🔧 בחר פריט לתיקון ({len(items)}):", rows


# ----------------------------------------------------------------- Jellyfin library layout
LIB_LAYOUT = {  # top-level folder on the share -> (library name, collection type)
    "סרטים": ("סרטים", "movies"),
    "סרטים ישראלים": ("סרטים ישראלים", "movies"),
    "ילדים": ("ילדים", "movies"),
    "סדרות": ("סדרות", "tvshows"),
    "טיולים": ("טיולים", "homevideos"),
    "YouTube": ("YouTube", "homevideos"),
}
OTHER_LIB = ("אירועים", "homevideos")   # any other folder (weddings, brit, ...) goes here
LIB_PLAN = {}


async def lib_plan():
    libs = await jf("GET", "/Library/VirtualFolders")
    locs = [l for lib in libs for l in (lib.get("Locations") or [])]
    base = None
    for l in locs:
        p = l.rstrip("/").rsplit("/", 1)
        if len(p) == 2 and p[1] in LIB_LAYOUT:
            base = p[0]
            break
    if not base:
        raise RuntimeError("לא הצלחתי לזהות את נתיב התיקייה video בצד של ג'ליפין")
    folders = sorted(d.name for d in media_root().iterdir() if d.is_dir() and not d.name.startswith((".", "#", "@")))
    desired = {}
    for f in folders:
        name, ctype = LIB_LAYOUT.get(f, OTHER_LIB)
        desired.setdefault(name, {"type": ctype, "paths": []})["paths"].append(f"{base}/{f}")
    owner = {p: n for n, d in desired.items() for p in d["paths"]}
    actions, lines = [], []
    remaining = {lib["Name"]: list(lib.get("Locations") or []) for lib in libs}
    for lib in libs:
        for l in lib.get("Locations") or []:
            tgt = owner.get(l.rstrip("/"))
            if tgt and tgt != lib["Name"]:
                actions.append(("rmpath", lib["Name"], l))
                remaining[lib["Name"]].remove(l)
    existing = {lib["Name"]: lib for lib in libs}
    for name, d in desired.items():
        if name in existing:
            have = {x.rstrip("/") for x in existing[name].get("Locations") or []}
            for p in d["paths"]:
                if p not in have:
                    actions.append(("addpath", name, p))
                    remaining.setdefault(name, []).append(p)
        else:
            actions.append(("create", name, d))
            remaining[name] = list(d["paths"])
    for name, left in remaining.items():
        if name in existing and not left and name not in desired:
            actions.append(("delete", name, None))
    # human readable
    lines.append("📚 הסידור החדש:")
    for name, d in desired.items():
        icon = {"movies": "🎬", "tvshows": "📺", "homevideos": "📷"}[d["type"]]
        lines.append(f"{icon} {name} ← " + ", ".join(p.rsplit('/', 1)[1] for p in d["paths"]))
    if actions:
        lines.append("\n🔧 שינויים:")
        for a in actions:
            if a[0] == "create":
                lines.append(f"➕ ספרייה חדשה: {a[1]}")
            elif a[0] == "addpath":
                lines.append(f"📁 {a[1]}: מוסיף {a[2].rsplit('/', 1)[1]}")
            elif a[0] == "rmpath":
                lines.append(f"↪️ {a[1]}: מוציא את {a[2].rsplit('/', 1)[1]}")
            elif a[0] == "delete":
                lines.append(f"🗑 ספרייה ריקה תוסר: {a[1]} (הקבצים לא נמחקים)")
        lines.append("\n⚠️ שים לב: תיקייה שעוברת לספרייה אחרת נסרקת מחדש — שמות ותמונות שתיקנת בה ידנית יתאפסו "
                     "(אפשר לשחזר עם /restore).")
    else:
        lines.append("\n✔️ הכל כבר מסודר — אין מה לשנות.")
    return actions, "\n".join(lines), libs


async def lib_apply(actions, libs):
    opts_by_type = {}
    for lib in libs:
        o = dict(lib.get("LibraryOptions") or {})
        o.pop("PathInfos", None)
        opts_by_type.setdefault(lib.get("CollectionType") or "mixed", o)
        opts_by_type.setdefault("any", o)
    for a in actions:
        if a[0] == "rmpath":
            await jf("DELETE", "/Library/VirtualFolders/Paths",
                     params={"name": a[1], "path": a[2], "refreshLibrary": "false"})
    for a in actions:
        if a[0] == "create":
            d = a[2]
            opts = dict(opts_by_type.get(d["type"]) or opts_by_type.get("any") or {})
            opts.setdefault("PreferredMetadataLanguage", "he")
            opts.setdefault("MetadataCountryCode", "IL")
            params = [("name", a[1]), ("collectionType", d["type"]), ("refreshLibrary", "false")] + \
                     [("paths", p) for p in d["paths"]]
            await jf("POST", "/Library/VirtualFolders", params=params, body={"LibraryOptions": opts})
        elif a[0] == "addpath":
            await jf("POST", "/Library/VirtualFolders/Paths", params={"refreshLibrary": "false"},
                     body={"Name": a[1], "PathInfo": {"Path": a[2]}})
    for a in actions:
        if a[0] == "delete":
            await jf("DELETE", "/Library/VirtualFolders", params={"name": a[1], "refreshLibrary": "false"})
    await jellyfin_refresh()


# ----------------------------------------------------------------- restore manual fixes from chat history
async def jf_protect_libraries():
    """Make Jellyfin save names/images next to the files (NFO + artwork) so they survive library changes."""
    libs = await jf("GET", "/Library/VirtualFolders")
    n = 0
    for lib in libs:
        opts = dict(lib.get("LibraryOptions") or {})
        if not lib.get("ItemId"):
            continue
        opts["SaveLocalMetadata"] = True
        savers = set(opts.get("MetadataSavers") or [])
        savers.add("Nfo")
        opts["MetadataSavers"] = sorted(savers)
        await jf("POST", "/Library/VirtualFolders/LibraryOptions", body={"Id": lib["ItemId"], "LibraryOptions": opts})
        n += 1
    return n


async def collect_fixes():
    """Walk the control-bot chat (from the user's side) and rebuild {(folder, file): {name, img_msg, img_url}}."""
    msgs = []
    async for m in USER.iter_messages(BOT_ID, limit=5000):
        msgs.append(m)
    msgs.reverse()
    fixes = {}
    want, pending = None, None
    for m in msgs:
        txt = m.message or ""
        from_bot = not m.out
        if from_bot and txt.startswith("✏️ שלח את השם החדש"):
            want, pending = "name", None
            continue
        if from_bot and txt.startswith("🖼 שלח תמונה"):
            want, pending = "img", None
            continue
        if not from_bot and want:
            if want == "name" and txt and not txt.startswith("/"):
                pending = ("name", txt.strip()[:120])
            elif want == "img":
                if m.photo:
                    pending = ("img_msg", m)
                else:
                    u = re.search(r"https?://\S+", txt)
                    if u:
                        pending = ("img_url", u.group(0))
            want = None
            continue
        if from_bot and pending and (txt.startswith("✏️ השם עודכן") or txt.startswith("🖼 התמונה עודכנה")):
            pm = re.search(r"📁 (.+)", txt)
            if pm and "/" in pm.group(1):
                folder, fname = pm.group(1).strip().rsplit("/", 1)
                rec = fixes.setdefault((folder, fname), {})
                kind, val = pending
                if kind == "name":
                    rec["name"] = val
                else:
                    rec.pop("img_msg", None)
                    rec.pop("img_url", None)
                    rec[kind] = val
            pending = None
    return fixes


async def restore_fixes(progress=None):
    fixes = await collect_fixes()
    if not fixes:
        return "לא נמצאו תיקונים בהיסטוריה."
    res = await jf("GET", "/Items", params={"Recursive": "true", "Fields": "Path",
                                            "IncludeItemTypes": "Movie,Series,Video,Episode"})
    index = {}
    for it in res.get("Items", []):
        p = it.get("Path") or ""
        if p:
            index[(Path(p).parent.name, Path(p).name)] = it["Id"]
    ok_n, ok_i, missing, errors = 0, 0, [], []
    for (folder, fname), rec in fixes.items():
        item_id = index.get((folder, fname))
        if not item_id:
            missing.append(rec.get("name") or fname)
            continue
        try:
            if rec.get("name"):
                await jf_rename(item_id, rec["name"])
                ok_n += 1
            if rec.get("img_msg"):
                await jf_upload_image(item_id, await USER.download_media(rec["img_msg"], bytes))
                ok_i += 1
            elif rec.get("img_url"):
                await jf_image(item_id, rec["img_url"])
                ok_i += 1
        except Exception as e:
            errors.append(f"{rec.get('name') or fname}: {e}"[:80])
    lines = [f"♻️ שוחזרו {ok_n} שמות ו-{ok_i} תמונות (מתוך {len(fixes)} פריטים שתיקנת)."]
    if missing:
        lines.append(f"\n⏳ לא נמצאו עדיין בג'ליפין ({len(missing)}) — כנראה הסריקה עוד רצה, הרץ /restore שוב בעוד כמה דקות:")
        lines += [f"• {x}" for x in missing[:15]]
    if errors:
        lines.append("\n❌ שגיאות:")
        lines += errors[:10]
    return "\n".join(lines)


# ----------------------------------------------------------------- automatic library cleanup
def _tokens(s):
    s = re.sub(r"[^\w֐-׿ ]+", " ", str(s or "").lower())
    return {w for w in s.split() if len(w) > 1 and not w.isdigit()}


def _sim(a, b):
    import difflib
    a, b = clean_title(a).lower(), clean_title(b).lower()
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def file_guess(path):
    """Best title/year guess from the file AND its folder name."""
    p = Path(path or "x")
    cands = [classify(p.parent.name + p.suffix, ""), classify(p.name, "")]
    best = max(cands, key=lambda g: (bool(g.get("year")), len(g.get("title") or "")))
    return clean_title(best["title"]), best.get("year")


def looks_wrong(it):
    if "Name" in (it.get("LockedFields") or []):
        return False                       # you fixed it by hand — never touch
    if not (it.get("ProviderIds") or {}).get("Tmdb") and not (it.get("ProviderIds") or {}).get("Imdb"):
        return True
    title, year = file_guess(it.get("Path"))
    if year and it.get("ProductionYear") and abs(int(year) - int(it["ProductionYear"])) > 1:
        return True
    ft = _tokens(title)
    if ft and not (ft & (_tokens(it.get("Name")) | _tokens(it.get("OriginalTitle")))):
        return True
    return False


async def autofix_library(report):
    res = await jf("GET", "/Items", params={"Recursive": "true", "IncludeItemTypes": "Movie,Series",
                                            "Fields": "Path,ProviderIds,ProductionYear,OriginalTitle,LockedFields"})
    items = [it for it in res.get("Items", []) if (it.get("Path") or "") and "/YouTube/" not in it["Path"]]
    # skip home-video libraries (events/trips)
    suspects = [it for it in items if looks_wrong(it)]
    await report(f"🔍 עברתי על {len(items)} סרטים וסדרות — {len(suspects)} נראים לא מזוהים או מזוהים לא נכון. מתקן…")
    fixed, review = [], []
    for it in suspects:
        title, year = file_guess(it.get("Path"))
        if not title:
            continue
        typ = jf_type(it)
        try:
            cands = await jf("POST", f"/Items/RemoteSearch/{typ}",
                             body={"SearchInfo": {"Name": title, "Year": year}, "ItemId": it["Id"]}) or []
            if not cands and year:
                cands = await jf("POST", f"/Items/RemoteSearch/{typ}",
                                 body={"SearchInfo": {"Name": title}, "ItemId": it["Id"]}) or []
        except Exception as e:
            log.warning("autofix search %s: %s", title, e)
            continue
        if not cands:
            review.append((it, title, year, []))
            continue
        scored = sorted(cands[:10], key=lambda c: (_sim(title, c.get("Name")) +
                        (0.5 if year and c.get("ProductionYear") and abs(c["ProductionYear"] - int(year)) <= 1 else 0)),
                        reverse=True)
        top = scored[0]
        s = _sim(title, top.get("Name"))
        year_ok = year and top.get("ProductionYear") and abs(top["ProductionYear"] - int(year)) <= 1
        confident = (year_ok and s >= 0.45) or (not year and s >= 0.8 and
                                                  (len(scored) == 1 or _sim(title, scored[1].get("Name")) < s - 0.2))
        if confident:
            try:
                await jf("POST", f"/Items/RemoteSearch/Apply/{it['Id']}", params={"ReplaceAllImages": "true"}, body=top)
                fixed.append(f"{top.get('Name')} ({top.get('ProductionYear') or '?'})")
                await asyncio.sleep(1)
                continue
            except Exception as e:
                log.warning("autofix apply %s: %s", title, e)
        review.append((it, title, year, scored[:4]))
    return fixed, review


async def autofix_run():
    async def report(t):
        await notify(t)
    try:
        fixed, review = await autofix_library(report)
    except Exception as e:
        await notify(f"❌ הניקוי נכשל: {e}"[:300])
        return
    txt = f"✅ תוקנו אוטומטית {len(fixed)}:\n" + "\n".join(f"• {x}" for x in fixed[:40])
    if len(fixed) > 40:
        txt += f"\n…ועוד {len(fixed) - 40}"
    txt += f"\n\n🟡 צריך את ההחלטה שלך: {len(review)}" if review else "\n\n🎉 הכל מסודר."
    await notify(txt)
    for it, title, year, cands in review[:30]:
        STATE["seq"] = STATE.get("seq", 0) + 1
        k = str(STATE["seq"])
        JF_CAND[k] = (it["Id"], cands)
        rows = [[Button.inline(f"{c.get('Name', '')[:30]} ({c.get('ProductionYear') or '?'})", f"ja:{k}:{i}".encode())]
                for i, c in enumerate(cands)]
        rows.append([Button.inline("🔎 חפש בשם", f"js:{it['Id']}".encode()), Button.inline("⏭ דלג", b"jok")])
        p = Path(it.get("Path") or "")
        await notify(f"🎬 {p.parent.name}\nכרגע בג'ליפין: {it.get('Name')} ({it.get('ProductionYear') or '?'})\n"
                     f"{'בחר את הנכון:' if cands else 'לא מצאתי — חפש בשם (אפשר באנגלית):'}", buttons=rows)
        await asyncio.sleep(0.5)
    if len(review) > 30:
        await notify(f"…ועוד {len(review) - 30}. אחרי שתסיים, הרץ /autofix שוב.")


# ----------------------------------------------------------------- self update
REPO_RAW = os.environ.get("TGMB_REPO", "https://raw.githubusercontent.com/morbriga/morbriga/tg-media-bot")
APT_DEPS = {"ffmpeg": "ffmpeg", "arp-scan": "arp-scan", "qbittorrent-nox": "qbittorrent-nox"}


async def self_update(force=False):
    """Pull bot.py from GitHub; if changed and idle, swap and restart (systemd brings us back)."""
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{REPO_RAW}/bot.py?t={int(time.time())}", timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                return f"GitHub HTTP {r.status}"
            new = await r.read()
        async with s.get(f"{REPO_RAW}/requirements.txt?t={int(time.time())}", timeout=aiohttp.ClientTimeout(total=30)) as r:
            reqs = await r.read() if r.status == 200 else None
    cur = Path(__file__).read_bytes()
    if new == cur:
        return "up-to-date"
    compile(new, "bot.py", "exec")                       # refuse broken code
    busy = ACTIVE or any(True for _ in STATE.get("torrents", {})) or YT_LOCK.locked()
    if busy and not force:
        return "busy"
    if reqs and (BASE / "requirements.txt").read_bytes() != reqs:
        (BASE / "requirements.txt").write_bytes(reqs)
        p = await asyncio.create_subprocess_exec(str(BASE / "venv/bin/pip"), "install", "-q", "-r",
                                                 str(BASE / "requirements.txt"))
        await p.wait()
    tmp = Path(__file__).with_suffix(".new")
    tmp.write_bytes(new)
    tmp.replace(Path(__file__))
    log.info("updated — restarting")
    await asyncio.sleep(1)
    os._exit(0)


async def ensure_apt():
    missing = [pkg for cmd, pkg in APT_DEPS.items() if not shutil.which(cmd)]
    if not missing:
        return
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    p = await asyncio.create_subprocess_exec("apt-get", "update", "-qq", env=env)
    await p.wait()
    p = await asyncio.create_subprocess_exec("apt-get", "install", "-y", "-qq", *missing, env=env)
    await p.wait()


async def updater_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await self_update()
        except Exception as e:
            log.warning("self update: %s", e)
        await asyncio.sleep(600)


# ----------------------------------------------------------------- Dropbox / direct links
DBX_RE = re.compile(r"https?://(?:www\.)?dropbox\.com/\S+")
DBX_PENDING = {}
MEDIA_KEEP = VIDEO_EXT | {".jpg", ".jpeg", ".png", ".heic", ".gif"}


def dbx_direct(url):
    url = re.sub(r"([?&])dl=0", r"\1dl=1", url)
    if "dl=1" not in url:
        url += ("&" if "?" in url else "?") + "dl=1"
    return url


async def dbx_job(url, dest_kind):
    status = await notify("☁️ דרופבוקס: מתחיל הורדה…")
    TMP.mkdir(parents=True, exist_ok=True)
    tmpf = TMP / f"dbx_{int(time.time())}"
    name, total, got, last = "Dropbox", 0, 0, 0
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(dbx_direct(url), timeout=aiohttp.ClientTimeout(total=None, sock_read=300)) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} — ודא שהקישור משותף לצפייה")
                cd = r.headers.get("Content-Disposition", "")
                m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    from urllib.parse import unquote
                    name = unquote(m.group(1))
                total = int(r.headers.get("Content-Length") or 0)
                with open(tmpf, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if time.time() - last > 8 and status:
                            last = time.time()
                            pct = f" {got * 100 // total}%" if total else ""
                            try:
                                await status.edit(f"☁️ דרופבוקס: {name}\n⬇️ {fmt_bytes(got)}" +
                                                  (f" / {fmt_bytes(total)}" if total else "") + pct)
                            except Exception:
                                pass
        stem = clean_title(Path(name).stem) or "Dropbox"
        root = {"trip": media_root() / "טיולים" / stem,
                "event": media_root() / stem,
                "movie": None}[dest_kind]
        moved = 0
        if zipfile.is_zipfile(tmpf):
            with zipfile.ZipFile(tmpf) as z:
                for zi in z.infolist():
                    if zi.is_dir() or Path(zi.filename).suffix.lower() not in MEDIA_KEEP:
                        continue
                    if dest_kind == "movie":
                        if Path(zi.filename).suffix.lower() not in VIDEO_EXT:
                            continue
                        out = unique(target_path(classify(Path(zi.filename).name, "")))
                    else:
                        out = unique(root / Path(zi.filename).name)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(zi) as src, open(out, "wb") as dst:
                        await asyncio.to_thread(shutil.copyfileobj, src, dst, 1 << 20)
                    moved += 1
        else:
            if dest_kind == "movie":
                out = unique(target_path(classify(name, "")))
            else:
                out = unique(root / name)
            out.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(tmpf), out)
            moved = 1
        where = root.relative_to(media_root()) if root else "סרטים"
        await notify(f"✅ דרופבוקס: נשמרו {moved} קבצים\n📁 {where}", edit=status)
        if moved:
            schedule_refresh()
    except Exception as e:
        await notify(f"❌ דרופבוקס נכשל: {e}"[:300], edit=status)
    finally:
        try:
            tmpf.unlink()
        except Exception:
            pass


# ----------------------------------------------------------------- Google Drive links (gdown)
GD_RE = re.compile(r"https?://(?:drive|docs)\.google\.com/\S+")


def _dir_size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())


async def gdrive_job(url, dest_kind):
    import gdown
    status = await notify("☁️ גוגל דרייב: מתחיל הורדה…")
    work = TMP / f"gd_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    is_folder = "/folders/" in url
    try:
        def run():
            if is_folder:
                return gdown.download_folder(url, output=str(work), quiet=True, use_cookies=False, retries=3)
            m = re.search(r"/d/([\w-]+)|[?&]id=([\w-]+)", url)
            fid = (m.group(1) or m.group(2)) if m else None
            return gdown.download(id=fid, url=None if fid else url, output=str(work) + "/", quiet=True,
                                  use_cookies=False, retries=3)
        task = asyncio.create_task(asyncio.to_thread(run))
        while not task.done():
            await asyncio.sleep(8)
            try:
                await status.edit(f"☁️ גוגל דרייב: {'תיקייה' if is_folder else 'קובץ'}\n⬇️ {fmt_bytes(_dir_size(work))}")
            except Exception:
                pass
        res = task.result()
        if not res:
            raise RuntimeError("ההורדה נכשלה — ודא שהקישור משותף ל'כל מי שיש לו את הקישור'")
        files = [f for f in work.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA_KEEP]
        if not files:
            raise RuntimeError("לא נמצאו סרטונים או תמונות בקישור")
        subdirs = [d for d in work.iterdir() if d.is_dir()]
        name = subdirs[0].name if is_folder and subdirs else files[0].stem
        stem = clean_title(name) or "Google Drive"
        root = {"trip": media_root() / "טיולים" / stem, "event": media_root() / stem, "movie": None}[dest_kind]
        moved = 0
        for f in files:
            if dest_kind == "movie":
                if f.suffix.lower() not in VIDEO_EXT:
                    continue
                out = unique(target_path(classify(f.name, "")))
            else:
                out = unique(root / f.name)
            out.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(f), out)
            moved += 1
        where = root.relative_to(media_root()) if root else "סרטים"
        await notify(f"✅ גוגל דרייב: נשמרו {moved} קבצים\n📁 {where}", edit=status)
        if moved:
            schedule_refresh()
    except Exception as e:
        await notify(f"❌ גוגל דרייב נכשל: {e}"[:300], edit=status)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------- bot UI
def main_menu():
    return ("🏠 לוח בקרה", [
        [Button.inline("🎬 מדיה", b"m:media"), Button.inline("🖥 שרת", b"m:srv")],
        [Button.inline("➕ הוסף מקור", b"m:add:0"), Button.inline("📋 מקורות", b"m:src")],
        [Button.inline("🧲 טורנטים", b"m:tor"), Button.inline("📊 הורדות", b"m:st")],
        [Button.inline("🌐 רשת ביתית", b"m:net:0"), Button.inline("🔧 תיקון ג'ליפין", b"m:fix")],
        [Button.inline("🪄 סדר לי את הספרייה", b"m:auto"), Button.inline("🔄 רענון ג'ליפין", b"m:jf")],
    ])


def media_view():
    lines = ["🎬 מדיה", f"⏳ בתור/בהורדה: {len(ACTIVE)}", nas_space(),
             f"🔑 ג'ליפין: {'מוגדר' if STATE.get('jellyfin_key') else 'חסר מפתח — שלח /jellyfin KEY'}"]
    if STATE["recent"]:
        lines += ["", "🕘 אחרונים:"] + STATE["recent"][-8:]
    return "\n".join(lines), [[Button.inline("➕ הוסף מקור", b"m:add:0"), Button.inline("📋 מקורות", b"m:src")],
                              [Button.inline("🔄 רענון ג'ליפין", b"m:jf"), Button.inline("⬅️ תפריט", b"m:main")]]


def sources_view():
    rows = [[Button.inline(f"❌ {source_label(s)}"[:40], f"rm:{i}".encode())] for i, s in enumerate(STATE["sources"])]
    rows.append([Button.inline("➕ הוסף מקור", b"m:add:0"), Button.inline("⬅️ תפריט", b"m:main")])
    return "📋 מקורות פעילים (לחיצה = הסרה):", rows


async def add_view(page):
    items = []
    async for d in USER.iter_dialogs(limit=200):
        ent = d.entity
        is_bot = getattr(ent, "bot", False)
        if not (d.is_channel or d.is_group or is_bot):
            continue
        if is_source(ent, d.id):
            continue
        icon = "🤖" if is_bot else ("📢" if d.is_channel and not d.is_group else "👥")
        items.append((d.id, f"{icon} {d.name}"))
    per = 12
    chunk = items[page * per:(page + 1) * per]
    rows = [[Button.inline(n[:40], f"ad:{i}".encode())] for i, n in chunk]
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ הקודם", f"m:add:{page - 1}".encode()))
    if (page + 1) * per < len(items):
        nav.append(Button.inline("הבא ➡️", f"m:add:{page + 1}".encode()))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("⬅️ תפריט", b"m:main")])
    return ("➕ בחר ערוץ / קבוצה / בוט להוספה כמקור.\n"
            "אפשר גם לשלוח לי @username או קישור t.me, או להעביר אליי הודעה ממנו."), rows


async def resolve_and_add(text):
    t = text.strip()
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_+]+)", t)
    ref = m.group(1) if m else t.lstrip("@")
    ent = await USER.get_entity(ref if not ref.lstrip("-").isdigit() else int(ref))
    pid = await USER.get_peer_id(ent)
    name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or ref
    key = getattr(ent, "username", None) or str(pid)
    new = add_source(key, name)
    return f"{'➕ נוסף' if new else '✔️ כבר קיים'}: {name}"


async def bot_text(event):
    text = (event.raw_text or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    if PENDING.get("rename") and text and not text.startswith("/"):
        mac = PENDING.pop("rename")
        d = STATE.get("devices", {}).get(mac)
        if d:
            d["name"] = text[:40]
            d["status"] = "known"
            persist()
            await event.respond("✏️ נשמר.\n\n" + dev_text(d), buttons=dev_buttons(mac))
        return
    PENDING.pop("rename", None)
    if PENDING.get("jimg") and event.message.photo:
        arg_id = PENDING.pop("jimg")
        try:
            await jf_upload_image(arg_id, await event.message.download_media(bytes))
            await jf_send_panel(arg_id, "🖼 התמונה עודכנה:")
        except Exception as e:
            await event.respond(f"❌ {e}"[:300])
        return
    for key in ("jfind", "jsearch", "jname", "jimg"):
        if PENDING.get(key) and text and not text.startswith("/"):
            arg_id = PENDING.pop(key)
            try:
                if key == "jfind":
                    t, b = await jf_menu_find(text); await event.respond(t, buttons=b)
                elif key == "jsearch":
                    t, b = await jf_candidates(arg_id, text); await event.respond(t, buttons=b)
                elif key == "jname":
                    await jf_rename(arg_id, text[:120]); await jf_send_panel(arg_id, "✏️ השם עודכן:")
                elif key == "jimg":
                    url = re.search(r"https?://\S+", text)
                    if not url:
                        await event.respond("⚠️ זה לא קישור. שלח קישור לתמונה (מתחיל ב-http)")
                        return
                    await jf_image(arg_id, url.group(0)); await jf_send_panel(arg_id, "🖼 התמונה עודכנה:")
            except Exception as e:
                await event.respond(f"❌ {e}"[:300])
            return
        PENDING.pop(key, None)
    if cmd == "/autofix":
        await event.respond("🪄 מתחיל לעבור על כל הספרייה. זה יכול לקחת כמה דקות — אעדכן כאן.")
        asyncio.create_task(autofix_run())
        return
    if cmd == "/update":
        await event.respond("🔄 בודק עדכון…")
        r = await self_update(force=True)
        await event.respond({"up-to-date": "✔️ כבר בגרסה האחרונה", "busy": "⏳ יש הורדות פעילות"}.get(r, f"⚠️ {r}"))
        return
    if cmd == "/restore":
        msg = await event.respond("♻️ קורא את היסטוריית התיקונים ומשחזר… (יכול לקחת דקה)")
        try:
            prot = await jf_protect_libraries()
            out = await restore_fixes()
            out += f"\n\n🛡 מעכשיו ג'ליפין שומר שמות ותמונות ליד הקבצים ב-{prot} ספריות — הם ישרדו שינויי ספריות."
        except Exception as e:
            out = f"❌ {e}"[:400]
        await msg.edit(out)
        return
    if cmd == "/libraries":
        actions, txt, libs = await lib_plan()
        LIB_PLAN["p"] = (actions, libs)
        await event.respond(txt, buttons=[[Button.inline("✅ בצע", b"lb:go"), Button.inline("❌ ביטול", b"jok")]] if actions else None)
        return
    if cmd == "/fix":
        if arg.strip():
            t, b = await jf_menu_find(arg.strip()); await event.respond(t, buttons=b)
        else:
            t, b = await jf_libraries_view(); await event.respond(t, buttons=b)
        return
    cloud = DBX_RE.search(text) or GD_RE.search(text)
    if cloud and not cmd.startswith("/"):
        STATE["seq"] = STATE.get("seq", 0) + 1
        k = str(STATE["seq"])
        src = "dbx" if DBX_RE.search(text) else "gd"
        DBX_PENDING[k] = (src, cloud.group(0))
        await event.respond(f"☁️ קישור {'דרופבוקס' if src == 'dbx' else 'גוגל דרייב'} — לאן לשמור?", buttons=[[
            Button.inline("🏝 טיולים", f"dbx:{k}:trip".encode()), Button.inline("🎉 אירועים", f"dbx:{k}:event".encode())],
            [Button.inline("🎬 סרטים", f"dbx:{k}:movie".encode()), Button.inline("❌ ביטול", f"dbx:{k}:x".encode())]])
        return
    if YT_RE.search(text) and not cmd.startswith("/"):
        url = YT_RE.search(text).group(0)
        STATE["seq"] = STATE.get("seq", 0) + 1
        k = str(STATE["seq"])
        YT_PENDING[k] = url
        kind = "פלייליסט" if "list=" in url or "/playlist" in url or "/@" in url else "סרטון"
        await event.respond(f"▶️ {kind} מיוטיוב — לאן להוריד?", buttons=[[
            Button.inline("📺 YouTube", f"yt:{k}:y".encode()), Button.inline("🧸 YouTube ילדים", f"yt:{k}:k".encode())],
            [Button.inline("❌ ביטול", f"yt:{k}:x".encode())]])
        return
    if cmd == "/network":
        t, b = net_view(0); await event.respond(t, buttons=b)
        return
    if cmd in ("/start", "/menu", "/help"):
        t, b = main_menu()
        await event.respond(t, buttons=b)
    elif cmd == "/media":
        t, b = media_view(); await event.respond(t, buttons=b)
    elif cmd == "/torrents":
        t, b = await torrents_view(); await event.respond(t, buttons=b)
    elif "magnet:?" in text:
        for mg in re.findall(r"magnet:\?\S+", text):
            try:
                await tor_add(magnet=mg)
            except Exception as e:
                await event.respond(f"❌ {e}")
    elif event.message.document and (file_name_of(event.message) or "").lower().endswith(".torrent"):
        data = await event.message.download_media(bytes)
        try:
            await tor_add(file_bytes=data, file_name=file_name_of(event.message))
        except Exception as e:
            await event.respond(f"❌ {e}")
    elif cmd == "/server":
        t, b = await server_view(); await event.respond(t, buttons=b)
    elif cmd == "/sources":
        t, b = sources_view(); await event.respond(t, buttons=b)
    elif cmd == "/add":
        if arg:
            await event.respond(await resolve_and_add(arg))
        else:
            t, b = await add_view(0); await event.respond(t, buttons=b)
    elif cmd == "/status":
        t, b = media_view(); await event.respond(t, buttons=b)
    elif cmd == "/refresh":
        await event.respond(f"🔄 רענון ג'ליפין: {await jellyfin_refresh()}")
    elif cmd == "/jellyfin" and arg:
        STATE["jellyfin_key"] = arg.strip(); persist()
        try:
            await event.delete()
        except Exception:
            pass
        r = await jellyfin_refresh()
        await event.respond("✅ מפתח ג'ליפין נשמר ועובד" if r == "ok" else f"⚠️ נשמר, אבל בדיקה נכשלה: {r}")
    elif event.message.fwd_from and not event.message.media:
        peer = event.message.fwd_from.from_id
        if peer is None:
            await event.respond("⚠️ הערוץ הזה מסתיר את המקור. השתמש ב- /add ובחר מהרשימה.")
            return
        ent = await USER.get_entity(peer)
        pid = await USER.get_peer_id(ent)
        name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(pid)
        new = add_source(getattr(ent, "username", None) or str(pid), name)
        await event.respond(f"{'➕ נוסף' if new else '✔️ כבר קיים'}: {name}")
    elif text.startswith("@") or "t.me/" in text:
        await event.respond(await resolve_and_add(text))
    elif not event.message.media:
        t, b = main_menu()
        await event.respond(t, buttons=b)


async def bot_callback(event):
    data = event.data.decode()
    parts = data.split(":")
    edit = event.edit
    if data == "m:main":
        t, b = main_menu(); await edit(t, buttons=b)
    elif data == "m:media" or data == "m:st":
        t, b = media_view(); await edit(t, buttons=b)
    elif data == "m:srv":
        t, b = await server_view(); await edit(t, buttons=b)
    elif data == "m:src":
        t, b = sources_view(); await edit(t, buttons=b)
    elif data == "m:auto":
        await event.answer("🪄 מתחיל…")
        await event.respond("🪄 מתחיל לעבור על כל הספרייה. זה יכול לקחת כמה דקות — אעדכן כאן.")
        asyncio.create_task(autofix_run())
        return
    elif data == "m:fix":
        t, b = await jf_libraries_view()
        try:
            await edit(t, buttons=b)
        except Exception:
            await event.respond(t, buttons=b)
    elif parts[0] == "jl":
        t, b = await jf_list_view(parts[1], int(parts[2])); await edit(t, buttons=b)
    elif data == "lb:plan":
        await event.answer("🔍 בודק…")
        actions, txt, libs = await lib_plan()
        LIB_PLAN["p"] = (actions, libs)
        await event.respond(txt, buttons=[[Button.inline("✅ בצע", b"lb:go"), Button.inline("❌ ביטול", b"jok")]] if actions else None)
        return
    elif data == "lb:go":
        if "p" not in LIB_PLAN:
            await event.answer("התוכנית פגה — פתח שוב", alert=True)
            return
        actions, libs = LIB_PLAN.pop("p")
        await edit("⏳ מסדר ספריות…", buttons=None)
        try:
            await lib_apply(actions, libs)
            await edit("✅ הספריות סודרו. ג'ליפין סורק עכשיו — זה יכול לקחת כמה דקות עד שהכל יופיע במסך הבית.")
        except Exception as e:
            await edit(f"❌ נכשל באמצע: {e}"[:400] + "\nהרץ /libraries שוב כדי לראות מה נשאר.")
        return
    elif data == "jq":
        PENDING["jfind"] = 1
        await event.answer()
        await event.respond("🔎 שלח מילה מהשם כפי שהוא מופיע בג'ליפין")
        return
    elif data == "jok":
        await event.answer("👍")
        try:
            await edit(buttons=None)
        except Exception:
            pass
        return
    elif parts[0] == "ji":
        await event.answer()
        await jf_send_panel(parts[1])
        return
    elif parts[0] == "jr":
        await event.answer("🔄 מחפש…")
        t, b = await jf_candidates(parts[1]); await event.respond(t, buttons=b)
        return
    elif parts[0] in ("js", "jn", "jp"):
        key = {"js": "jsearch", "jn": "jname", "jp": "jimg"}[parts[0]]
        PENDING[key] = parts[1]
        await event.answer()
        await event.respond({"js": "🔎 שלח שם לחיפוש (עברית או אנגלית, אפשר עם שנה)",
                             "jn": "✏️ שלח את השם החדש",
                             "jp": "🖼 שלח תמונה (מהגלריה) או קישור לתמונה"}[parts[0]])
        return
    elif parts[0] == "ja":
        await event.answer("⏳ מעדכן…")
        try:
            chosen = JF_CAND.get(parts[1], (None, []))[1]
            chosen = chosen[int(parts[2])] if int(parts[2]) < len(chosen) else {}
            await jf_apply(parts[1], int(parts[2]))
            await edit(f"✅ עודכן ל: {chosen.get('Name', '')} ({chosen.get('ProductionYear') or '?'})", buttons=None)
        except Exception as e:
            await event.respond(f"❌ {e}"[:300])
        return
    elif parts[0] == "m" and parts[1] == "net":
        t, b = net_view(int(parts[2])); await edit(t, buttons=b)
    elif data == "m:scan":
        await event.answer("🔍 סורק…")
        try:
            n, new = await net_scan_once()
        except Exception as e:
            await event.answer(f"❌ {e}"[:190], alert=True)
            return
        t, b = net_view(0); await edit(f"✔️ נסרקו {n} מכשירים ({new} חדשים)\n\n" + t, buttons=b)
        return
    elif parts[0] in ("nd", "nk", "nf", "nn", "nr", "nx"):
        mac = mac_from_key(parts[1])
        devs = STATE.get("devices", {})
        d = devs.get(mac)
        if not d:
            await event.answer("המכשיר לא נמצא", alert=True)
            return
        if parts[0] == "nk":
            d["status"] = "known"; persist(); await event.answer("✅ סומן כמוכר")
        elif parts[0] == "nf":
            d["status"] = "flagged"; persist(); await event.answer("⚠️ סומן כחשוד — אתריע כשיחזור לרשת", alert=True)
        elif parts[0] == "nn":
            PENDING["rename"] = mac
            await event.answer()
            await event.respond(f"✏️ שלח עכשיו שם ל: {dev_name(d)}")
            return
        elif parts[0] == "nr":
            await event.answer("🔄 מזהה מחדש…")
            nd = await probe(d["ip"], mac, d.get("vendor", ""))
            for k2 in ("ports", "hostname", "title", "server", "type"):
                d[k2] = nd[k2]
            persist()
        elif parts[0] == "nx":
            devs.pop(mac, None); persist(); await event.answer("🗑 נשכח")
            t, b = net_view(0); await edit(t, buttons=b)
            return
        await edit(dev_text(d), buttons=dev_buttons(mac))
        return
    elif parts[0] == "dbx":
        pend = DBX_PENDING.pop(parts[1], None)
        if parts[2] == "x" or not pend:
            await edit("❌ בוטל" if parts[2] == "x" else "⚠️ הבקשה פגה — שלח את הקישור שוב")
            return
        src, url = pend
        await edit({"trip": "🏝 יורד לטיולים…", "event": "🎉 יורד לאירועים…", "movie": "🎬 יורד לסרטים…"}[parts[2]])
        asyncio.create_task((dbx_job if src == "dbx" else gdrive_job)(url, parts[2]))
        return
    elif parts[0] == "yt":
        url = YT_PENDING.pop(parts[1], None)
        if parts[2] == "x" or not url:
            await edit("❌ בוטל" if parts[2] == "x" else "⚠️ הבקשה פגה — שלח את הקישור שוב")
            return
        await edit(f"⏳ נוסף לתור YouTube ({'ילדים' if parts[2] == 'k' else 'כללי'})\n{url}")
        asyncio.create_task(yt_job(url, parts[2] == "k"))
        return
    elif data == "m:tor":
        t, b = await torrents_view(); await edit(t, buttons=b)
    elif parts[0] in ("tp", "tr"):
        await tor_pause(parts[1], resume=parts[0] == "tr")
        await event.answer("▶️ ממשיך" if parts[0] == "tr" else "⏸ הושהה")
        STATE.get("torrents", {}).get(parts[1], {})["last"] = 0
        return
    elif parts[0] == "td":
        await edit("🗑 לבטל את הטורנט ולמחוק את מה שירד?", buttons=[[
            Button.inline("✅ כן, בטל", f"tx:{parts[1]}".encode()), Button.inline("❌ לא", b"m:tor")]])
    elif parts[0] == "tx":
        await qbt("POST", "/torrents/delete", data={"hashes": parts[1], "deleteFiles": "true"}, raw=True)
        STATE.get("torrents", {}).pop(parts[1], None); persist()
        await event.answer("🗑 בוטל")
        t, b = await torrents_view(); await edit(t, buttons=b)
        return
    elif data == "m:jf":
        await event.answer(f"רענון ג'ליפין: {await jellyfin_refresh()}", alert=True)
        return
    elif parts[0] == "m" and parts[1] == "add":
        t, b = await add_view(int(parts[2])); await edit(t, buttons=b)
    elif parts[0] == "ad":
        ent = await USER.get_entity(int(parts[1]))
        name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or parts[1]
        new = add_source(getattr(ent, "username", None) or parts[1], name)
        await event.answer(("➕ נוסף: " if new else "✔️ כבר קיים: ") + name, alert=True)
        t, b = await add_view(0); await edit(t, buttons=b)
        return
    elif parts[0] == "rm":
        i = int(parts[1])
        if 0 <= i < len(STATE["sources"]):
            removed = STATE["sources"].pop(i); persist()
            await event.answer(f"➖ הוסר: {source_label(removed)}")
        t, b = sources_view(); await edit(t, buttons=b)
        return
    elif parts[0] == "mv":
        rid, kind = parts[1], parts[2]
        res = await move_saved(rid, kind)
        if res:
            rec = STATE["saved"].get(rid, {})
            await edit(f"✅ {rec.get('title', '')}\n{res}\n\nקטגוריה לא נכונה? בחר:", buttons=kind_buttons(rid, kind))
        await event.answer(res or "כבר בקטגוריה הזו")
        return
    elif parts[0] == "vm":
        t, b = await vm_view(parts[1], parts[2], parts[3]); await edit(t, buttons=b)
    elif parts[0] == "va":
        node, typ, vmid, act = parts[1:5]
        await edit(f"⚠️ לבצע {ACT_HE.get(act, act)} ל-{typ} {vmid}?", buttons=[
            [Button.inline("✅ כן", f"vc:{node}:{typ}:{vmid}:{act}".encode()),
             Button.inline("❌ ביטול", f"vm:{node}:{typ}:{vmid}".encode())]])
    elif parts[0] == "vc":
        node, typ, vmid, act = parts[1:5]
        try:
            await pve("POST", f"/nodes/{node}/{typ}/{vmid}/status/{act}")
            await event.answer(f"✅ נשלחה פקודת {ACT_HE.get(act, act)}", alert=True)
        except Exception as e:
            await event.answer(f"❌ {e}"[:190], alert=True)
        await asyncio.sleep(3)
        t, b = await vm_view(node, typ, vmid); await edit(t, buttons=b)
        return
    try:
        await event.answer()
    except Exception:
        pass


async def saved_control(event):
    """Fallback control from Saved Messages."""
    msg = event.message
    text = (msg.message or "").strip()
    if msg.fwd_from and is_video(msg):
        await enqueue(msg, "העברה ידנית")
        return
    if msg.is_reply and text:
        m = re.search(r"#(\d+)", (await msg.get_reply_message()).message or "")
        if m and text.lower() in MOVE_WORDS:
            await event.reply(await move_saved(m.group(1), MOVE_WORDS[text.lower()]) or "כבר שם")
            return
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()
    if cmd == "/bot" and arg:
        CFG["bot_token"] = arg
        save_json(CONFIG_FILE, CFG)
        try:
            await msg.delete()
        except Exception:
            pass
        await USER.send_message("me", "🔁 טוקן נשמר — מפעיל מחדש. עכשיו פתח את הבוט שלך ולחץ Start.")
        await asyncio.sleep(1)
        os._exit(0)  # systemd restarts us with the bot enabled
    elif cmd == "/jellyfin" and arg:
        STATE["jellyfin_key"] = arg; persist()
        try:
            await msg.delete()
        except Exception:
            pass
        r = await jellyfin_refresh()
        await USER.send_message("me", "✅ מפתח ג'ליפין נשמר ועובד" if r == "ok" else f"⚠️ נשמר, אבל בדיקה נכשלה: {r}")
    elif cmd == "/add" and arg:
        await event.reply(await resolve_and_add(arg))
    elif cmd == "/list":
        await event.reply("📋 מקורות:\n" + "\n".join(f"• {source_label(s)}" for s in STATE["sources"]))
    elif cmd == "/remove" and arg:
        STATE["sources"] = [s for s in STATE["sources"] if norm(s) != norm(arg)]; persist()
        await event.reply("➖ הוסר")
    elif cmd in ("/help", "/start"):
        await event.reply("פקודות כאן: /bot TOKEN · /jellyfin KEY · /add @x · /list · /remove @x\n"
                          "הכי נוח — דרך הבוט שלך (אחרי /bot TOKEN).")


# ----------------------------------------------------------------- main
async def run():
    global USER, BOT, OWNER_ID, BOT_ID, QUEUE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    USER = TelegramClient(SESSION, int(CFG["api_id"]), CFG["api_hash"])
    await USER.connect()
    if not await USER.is_user_authorized():
        log.error("Not logged in. Run: cd /opt/tgmedia && venv/bin/python bot.py --login")
        raise SystemExit(2)
    me = await USER.get_me()
    OWNER_ID = me.id
    log.info("user: %s", me.username or me.id)

    if CFG.get("bot_token"):
        try:
            BOT = TelegramClient(str(BASE / "bot_session"), int(CFG["api_id"]), CFG["api_hash"])
            await BOT.start(bot_token=CFG["bot_token"])
            bme = await BOT.get_me()
            BOT_ID = bme.id
            log.info("control bot: @%s", bme.username)
            await BOT(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=[
                BotCommand("menu", "🏠 לוח בקרה"),
                BotCommand("media", "🎬 מדיה והורדות"),
                BotCommand("server", "🖥 מצב שרת ומכונות"),
                BotCommand("add", "➕ הוספת מקור"),
                BotCommand("sources", "📋 מקורות"),
                BotCommand("torrents", "🧲 טורנטים"),
                BotCommand("network", "🌐 רשת ביתית"),
                BotCommand("fix", "🔧 תיקון שם/תמונה בג'ליפין"),
                BotCommand("libraries", "🗂 סידור ספריות ג'ליפין"),
                BotCommand("autofix", "🪄 סדר לי את הספרייה"),
                BotCommand("restore", "♻️ שחזור שמות ותמונות שתיקנתי"),
                BotCommand("refresh", "🔄 רענון ג'ליפין"),
            ]))
            owner_only = lambda e: e.sender_id == OWNER_ID

            @BOT.on(events.NewMessage(func=owner_only))
            async def _bt(event):
                try:
                    await bot_text(event)
                except Exception as e:
                    log.exception("bot text")
                    await event.respond(f"⚠️ שגיאה: {e}")

            @BOT.on(events.CallbackQuery(func=owner_only))
            async def _bc(event):
                try:
                    await bot_callback(event)
                except Exception as e:
                    log.exception("bot callback")
                    try:
                        await event.answer(f"⚠️ {e}"[:190], alert=True)
                    except Exception:
                        pass
        except Exception as e:
            log.exception("control bot failed")
            BOT = None
            await USER.send_message("me", f"⚠️ בוט השליטה לא עלה: {e}")

    QUEUE = asyncio.Queue()
    asyncio.create_task(tor_monitor())
    asyncio.create_task(updater_loop())
    asyncio.create_task(ensure_apt())
    if CFG.get("net_enabled", True):
        asyncio.create_task(net_monitor())
    for i in range(int(CFG.get("workers", 2))):
        asyncio.create_task(worker(i))

    @USER.on(events.NewMessage(incoming=True))
    async def on_incoming(event):
        if event.chat_id in (OWNER_ID, BOT_ID):
            return
        if is_video(event.message):
            chat = await event.get_chat()
            if is_source(chat, event.chat_id):
                await enqueue(event.message, getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id))

    @USER.on(events.NewMessage(outgoing=True))
    async def on_outgoing(event):
        if BOT_ID and event.chat_id == BOT_ID and is_video(event.message):
            await enqueue(event.message, "העברה לבוט")
        elif event.chat_id == OWNER_ID:
            try:
                await saved_control(event)
            except Exception as e:
                log.exception("saved control")
                await event.reply(f"⚠️ שגיאה: {e}")

    if not media_root().exists():
        await notify(f"⚠️ תיקיית המדיה {media_root()} לא נגישה — בדוק את החיבור לסינולוג'י")
    if STATE.get("version") != VERSION:
        STATE["version"] = VERSION
        persist()
        await notify(f"🆕 התעדכנתי לגרסה חדשה:\n{CHANGES}")
    elif BOT:
        t, b = main_menu()
        await notify("🟢 המערכת עלתה.\n" + t, buttons=b)
    elif not BOT:
        await notify("🟢 בוט הסרטים פעיל. ליצירת לוח בקרה: צור בוט ב-@BotFather ושלח כאן /bot TOKEN")
    await USER.run_until_disconnected()


async def login():
    client = TelegramClient(SESSION, int(CFG["api_id"]), CFG["api_hash"])
    await client.start()
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
