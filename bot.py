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
from telethon import Button, TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
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


# ----------------------------------------------------------------- bot UI
def main_menu():
    return ("🏠 לוח בקרה", [
        [Button.inline("🎬 מדיה", b"m:media"), Button.inline("🖥 שרת", b"m:srv")],
        [Button.inline("➕ הוסף מקור", b"m:add:0"), Button.inline("📋 מקורות", b"m:src")],
        [Button.inline("🧲 טורנטים", b"m:tor"), Button.inline("📊 הורדות", b"m:st")],
        [Button.inline("🔄 רענון ג'ליפין", b"m:jf")],
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
    if BOT:
        t, b = main_menu()
        await notify("🟢 המערכת עלתה.\n" + t, buttons=b)
    else:
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
