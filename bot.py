"""
DataNest File Bot
------------------
A free Telegram bot that organizes your files (photos, PDFs, docs, screenshots)
into custom folders/categories, without ever downloading them again.
Files stay on Telegram's servers — the bot only stores a reference (file_id).

HOW TO USE (in Telegram, after you run this bot):
  /start              -> shows main menu
  /newfolder Name      -> creates a new top-level folder (e.g. /newfolder Study Material)
  /newtag Folder>Tag    -> creates a tag/subfolder inside a folder (e.g. /newtag Study Material>English)
  Just send a photo/file with a caption like: #English #Notes
        -> bot saves it under those tags automatically
  /menu                -> browse folders with buttons
  /search keyword       -> search files by tag
  /mytags               -> list all your tags

SETUP:
  1. pip install aiogram==3.* --break-system-packages
  2. Put your bot token below (from BotFather) or set env var BOT_TOKEN
  3. Run: python3 bot.py
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
FILES_BOT_TOKEN = os.environ.get("FILES_BOT_TOKEN", "")  # second bot, delivers files only
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # e.g. https://your-app.up.railway.app
DB_PATH = os.path.join(os.path.dirname(__file__), "datanest.db")

logging.basicConfig(level=logging.INFO)

BOT_USERNAME = None        # main (menu) bot's username, filled at startup
FILES_BOT_USERNAME = None  # second (file-delivery) bot's username, filled at startup

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

files_bot = Bot(token=FILES_BOT_TOKEN) if FILES_BOT_TOKEN else None
files_dp = Dispatcher() if files_bot else None

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        UNIQUE(user_id, name)
    );
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        folder_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        FOREIGN KEY(folder_id) REFERENCES folders(id),
        UNIQUE(folder_id, name)
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        file_type TEXT NOT NULL,     -- photo, document, video
        file_name TEXT,
        caption TEXT,
        tag_id INTEGER,
        created_at TEXT,
        FOREIGN KEY(tag_id) REFERENCES tags(id)
    );
    CREATE TABLE IF NOT EXISTS active_session (
        user_id INTEGER PRIMARY KEY,
        tag_id INTEGER NOT NULL
    );
    """)
    conn.commit()
    conn.close()
    # migrate: add file_name column if this DB was created before it existed
    conn = db()
    try:
        conn.execute("ALTER TABLE files ADD COLUMN file_name TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # already exists
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def get_or_create_folder(user_id: int, name: str) -> int:
    conn = db()
    cur = conn.execute(
        "SELECT id FROM folders WHERE user_id=? AND name=?", (user_id, name)
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO folders(user_id, name) VALUES (?,?)", (user_id, name)
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid

def get_or_create_tag(folder_id: int, name: str) -> int:
    conn = db()
    cur = conn.execute(
        "SELECT id FROM tags WHERE folder_id=? AND name=?", (folder_id, name)
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tags(folder_id, name) VALUES (?,?)", (folder_id, name)
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid

def list_folders(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, name FROM folders WHERE user_id=? ORDER BY name", (user_id,)
    ).fetchall()
    conn.close()
    return rows

def list_tags(folder_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, name FROM tags WHERE folder_id=? ORDER BY name", (folder_id,)
    ).fetchall()
    conn.close()
    return rows

def list_files_in_tag(tag_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM files WHERE tag_id=? ORDER BY created_at DESC", (tag_id,)
    ).fetchall()
    conn.close()
    return rows

def save_file(user_id, file_id, file_type, caption, tag_id, file_name=None):
    conn = db()
    conn.execute(
        "INSERT INTO files(user_id,file_id,file_type,file_name,caption,tag_id,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, file_id, file_type, file_name, caption, tag_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def search_files(user_id: int, keyword: str):
    conn = db()
    kw = f"%{keyword}%"
    rows = conn.execute(
        """
        SELECT f.*, t.name as tag_name, fo.name as folder_name
        FROM files f
        JOIN tags t ON f.tag_id = t.id
        JOIN folders fo ON t.folder_id = fo.id
        WHERE f.user_id=? AND (t.name LIKE ? OR fo.name LIKE ? OR f.caption LIKE ?)
        ORDER BY f.created_at DESC
        """,
        (user_id, kw, kw, kw),
    ).fetchall()
    conn.close()
    return rows

def set_active_tag(user_id: int, tag_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO active_session(user_id, tag_id) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET tag_id=excluded.tag_id",
        (user_id, tag_id),
    )
    conn.commit()
    conn.close()

def get_active_tag(user_id: int):
    conn = db()
    row = conn.execute(
        """
        SELECT t.id, t.name FROM active_session s
        JOIN tags t ON s.tag_id = t.id
        WHERE s.user_id=?
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    return row

def clear_active_tag(user_id: int):
    conn = db()
    conn.execute("DELETE FROM active_session WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def delete_file_by_id(file_db_id: int):
    conn = db()
    conn.execute("DELETE FROM files WHERE id=?", (file_db_id,))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    # Deep-link support: t.me/YourBot?start=tag_<id> opens straight to that tag's files
    if payload.startswith("tag_"):
        try:
            tag_id = int(payload.replace("tag_", "", 1))
        except ValueError:
            tag_id = None
        if tag_id:
            conn = db()
            tag = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
            conn.close()
            if tag:
                files = list_files_in_tag(tag_id)
                if not files:
                    await message.answer(f"📂 \"{tag['name']}\" mein koi file nahi hai.")
                    return
                await message.answer(f"📂 \"{tag['name']}\" — {len(files)} file(s):")
                for f in files:
                    await send_saved_file(message.chat.id, f, show_delete=True)
                return

    await message.answer(
        "Assalam o Alaikum! 👋\n\n"
        "Main aapki files (notes, PDFs, screenshots, pictures) folders/tags "
        "mein organize karta hoon — bina dobara download kiye instantly nikaal "
        "ke deta hoon.\n\n"
        "*Setup karne ka tareeqa:*\n"
        "1️⃣ Folder banao: `/newfolder Study Material`\n"
        "2️⃣ Tag banao: `/newtag Study Material>English`\n"
        "3️⃣ Koi bhi photo/file bhejo caption mein `#English` likh kar — "
        "wo automatically us tag mein save ho jayegi\n\n"
        "*Files dekhne ke liye:* /menu\n"
        "*Proper board (gallery view):* /board\n"
        "*Search karne ke liye:* `/search english`\n\n"
        "*Ek sath bohat saare tags banane ke liye* `/bulkadd` use karo "
        "(type /bulkadd for details).\n"
        "*Delete karne ke liye:* `/deletetag Folder>Tag` ya `/deletefolder Folder`",
        parse_mode="Markdown",
    )

@dp.message(Command("newfolder"))
async def cmd_newfolder(message: Message):
    name = message.text.replace("/newfolder", "", 1).strip()
    if not name:
        await message.answer("Folder ka naam likho. Misal: /newfolder Study Material")
        return
    get_or_create_folder(message.from_user.id, name)
    await message.answer(f"✅ Folder \"{name}\" ban gaya.")

@dp.message(Command("newtag"))
async def cmd_newtag(message: Message):
    raw = message.text.replace("/newtag", "", 1).strip()
    if ">" not in raw:
        await message.answer(
            "Format yeh hai: /newtag FolderName>TagName\n"
            "Misal: /newtag Study Material>English"
        )
        return
    folder_name, tag_name = [p.strip() for p in raw.split(">", 1)]
    folder_id = get_or_create_folder(message.from_user.id, folder_name)
    get_or_create_tag(folder_id, tag_name)
    await message.answer(f"✅ Tag \"{tag_name}\" folder \"{folder_name}\" mein ban gaya.")

@dp.message(Command("bulkadd"))
async def cmd_bulkadd(message: Message):
    """
    Paste multiple /newfolder and /newtag lines at once, e.g.:

    /bulkadd
    /newfolder Study Material
    /newtag Study Material>OOP Theory
    /newtag Study Material>OOP Lab
    """
    text = message.text.replace("/bulkadd", "", 1).strip()
    if not text:
        await message.answer(
            "Sara list ek message mein bhejo, har line pe ek command.\n\n"
            "Misal:\n"
            "/bulkadd\n"
            "/newfolder Study Material\n"
            "/newtag Study Material>OOP Theory\n"
            "/newtag Study Material>OOP Lab"
        )
        return

    created_folders = []
    created_tags = []
    errors = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("/newfolder"):
            name = line.replace("/newfolder", "", 1).strip()
            if name:
                get_or_create_folder(message.from_user.id, name)
                created_folders.append(name)
            else:
                errors.append(f"⚠️ Khali folder name: {line}")
        elif line.startswith("/newtag"):
            raw = line.replace("/newtag", "", 1).strip()
            if ">" in raw:
                folder_name, tag_name = [p.strip() for p in raw.split(">", 1)]
                folder_id = get_or_create_folder(message.from_user.id, folder_name)
                get_or_create_tag(folder_id, tag_name)
                created_tags.append(f"{folder_name} > {tag_name}")
            else:
                errors.append(f"⚠️ Galat format: {line}")
        else:
            errors.append(f"⚠️ Samajh nahi aaya: {line}")

    reply = []
    if created_folders:
        reply.append(f"✅ {len(created_folders)} folder(s) ban gaye: {', '.join(created_folders)}")
    if created_tags:
        reply.append(f"✅ {len(created_tags)} tag(s) ban gaye:\n" + "\n".join(f"  • {t}" for t in created_tags))
    if errors:
        reply.append("\n".join(errors))
    if not reply:
        reply.append("Kuch bhi process nahi hua. Format check karo.")

    await message.answer("\n\n".join(reply))

@dp.message(Command("deletetag"))
async def cmd_deletetag(message: Message):
    """Usage: /deletetag Study Material>OOP Theory"""
    raw = message.text.replace("/deletetag", "", 1).strip()
    if ">" not in raw:
        await message.answer(
            "Format: /deletetag FolderName>TagName\n"
            "Misal: /deletetag Study Material>OPP Theory"
        )
        return
    folder_name, tag_name = [p.strip() for p in raw.split(">", 1)]
    conn = db()
    row = conn.execute(
        """
        SELECT t.id FROM tags t
        JOIN folders f ON t.folder_id = f.id
        WHERE f.user_id=? AND f.name=? AND t.name=?
        """,
        (message.from_user.id, folder_name, tag_name),
    ).fetchone()
    if not row:
        conn.close()
        await message.answer(f"⚠️ Tag \"{tag_name}\" nahi mila folder \"{folder_name}\" mein.")
        return
    tag_id = row["id"]
    conn.execute("DELETE FROM files WHERE tag_id=?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    conn.commit()
    conn.close()
    await message.answer(f"🗑 Tag \"{tag_name}\" (aur uski files) delete ho gaya.")

@dp.message(Command("deletefolder"))
async def cmd_deletefolder(message: Message):
    """Usage: /deletefolder Study Material"""
    name = message.text.replace("/deletefolder", "", 1).strip()
    if not name:
        await message.answer("Format: /deletefolder FolderName")
        return
    conn = db()
    row = conn.execute(
        "SELECT id FROM folders WHERE user_id=? AND name=?",
        (message.from_user.id, name),
    ).fetchone()
    if not row:
        conn.close()
        await message.answer(f"⚠️ Folder \"{name}\" nahi mila.")
        return
    folder_id = row["id"]
    tag_ids = [r["id"] for r in conn.execute("SELECT id FROM tags WHERE folder_id=?", (folder_id,))]
    for tid in tag_ids:
        conn.execute("DELETE FROM files WHERE tag_id=?", (tid,))
    conn.execute("DELETE FROM tags WHERE folder_id=?", (folder_id,))
    conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()
    conn.close()
    await message.answer(f"🗑 Folder \"{name}\" (sab tags aur files sameet) delete ho gaya.")

@dp.message(Command("done"))
async def cmd_done(message: Message):
    active = get_active_tag(message.from_user.id)
    if not active:
        await message.answer("Koi active tag set nahi hai.")
        return
    clear_active_tag(message.from_user.id)
    await message.answer(f"✅ \"{active['name']}\" session band ho gaya.")

# ---------------------------------------------------------------------------
# ACTIVE TAG (plain "#TagName" text message sets it as active)
# ---------------------------------------------------------------------------
@dp.message(F.text.startswith("#"))
async def handle_set_active_tag(message: Message):
    tagname = message.text.lstrip("#").strip()
    conn = db()
    row = conn.execute(
        """
        SELECT t.id, t.name FROM tags t
        JOIN folders f ON t.folder_id = f.id
        WHERE f.user_id=? AND LOWER(t.name)=LOWER(?)
        """,
        (message.from_user.id, tagname),
    ).fetchone()
    conn.close()
    if not row:
        await message.answer(
            f"⚠️ Tag \"{tagname}\" nahi mila. /mytags se list dekho ya /newtag se pehle bana lo."
        )
        return
    set_active_tag(message.from_user.id, row["id"])
    await message.answer(
        f"📌 Active tag set: \"{row['name']}\"\n"
        f"Ab jitni files bhejni hain bina caption ke bhejo — sab isi tag mein save hongi.\n"
        f"Khatam hone pe /done bhej dena."
    )

@dp.message(Command("links"))
async def cmd_links(message: Message):
    if not BOT_USERNAME:
        await message.answer("⚠️ Thora wait karo, bot abhi start ho raha hai.")
        return
    conn = db()
    rows = conn.execute(
        """
        SELECT t.id, t.name as tag_name, f.name as folder_name
        FROM tags t JOIN folders f ON t.folder_id = f.id
        WHERE f.user_id=?
        ORDER BY f.name, t.name
        """,
        (message.from_user.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Abhi koi tag nahi hai. /newtag se banao.")
        return

    lines = []
    current_folder = None
    for r in rows:
        if r["folder_name"] != current_folder:
            current_folder = r["folder_name"]
            lines.append(f"\n📁 *{current_folder}*")
        link = f"https://t.me/{FILES_BOT_USERNAME or BOT_USERNAME}?start=tag_{r['id']}"
        lines.append(f"[🏷 {r['tag_name']}]({link})")

    await message.answer("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(Command("mytags"))
async def cmd_mytags(message: Message):
    folders = list_folders(message.from_user.id)
    if not folders:
        await message.answer("Abhi tak koi folder nahi bana. /newfolder se banao.")
        return
    lines = []
    for f in folders:
        tags = list_tags(f["id"])
        tag_names = ", ".join(t["name"] for t in tags) if tags else "(koi tag nahi)"
        lines.append(f"📁 {f['name']}: {tag_names}")
    await message.answer("\n".join(lines))

@dp.message(Command("search"))
async def cmd_search(message: Message):
    keyword = message.text.replace("/search", "", 1).strip()
    if not keyword:
        await message.answer("Search karne ke liye keyword likho. Misal: /search english")
        return
    results = search_files(message.from_user.id, keyword)
    if not results:
        await message.answer("Kuch nahi mila.")
        return
    await message.answer(f"🔎 {len(results)} file(s) mili \"{keyword}\" ke liye:")
    for r in results[:20]:  # limit to avoid flooding
        await send_saved_file(message.chat.id, r)

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await show_folders(message.chat.id, message.from_user.id)

@dp.message(Command("board"))
async def cmd_board(message: Message):
    if not PUBLIC_URL:
        await message.answer(
            "⚠️ Board abhi setup nahi hua. Railway pe service ka public URL "
            "generate karo (Settings → Networking → Generate Domain), phir "
            "usay PUBLIC_URL environment variable mein daalo."
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📊 Board Kholo",
            web_app=WebAppInfo(url=f"{PUBLIC_URL}/app"),
        )]
    ])
    await message.answer("Neeche button se apna board kholo:", reply_markup=kb)

# ---------------------------------------------------------------------------
# FILE UPLOAD HANDLER (photo / document / video with caption #tag)
# ---------------------------------------------------------------------------
@dp.message(F.photo | F.document | F.video)
async def handle_file(message: Message):
    caption = message.caption or ""
    file_name = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
        file_name = message.document.file_name
    else:
        file_id = message.video.file_id
        file_type = "video"

    # Case 1: caption given -> match against tag names in caption
    if caption.strip():
        conn = db()
        all_tags = conn.execute(
            """
            SELECT t.id, t.name FROM tags t
            JOIN folders f ON t.folder_id = f.id
            WHERE f.user_id=?
            """,
            (message.from_user.id,),
        ).fetchall()
        conn.close()

        caption_clean = caption.replace("#", "").strip().lower()
        saved_tags = []
        for t in all_tags:
            if t["name"].strip().lower() in caption_clean:
                save_file(message.from_user.id, file_id, file_type, caption, t["id"], file_name)
                saved_tags.append(t["name"])

        if saved_tags:
            await message.answer(f"✅ Save ho gaya tag(s) mein: {', '.join(saved_tags)}")
        else:
            await message.answer(
                "⚠️ Ye tag exist nahi karta. /mytags se sahi spelling dekh lo, "
                "ya pehle #TagName bhej kar active tag set kar lo."
            )
        return

    # Case 2: no caption -> use active tag session if set
    active = get_active_tag(message.from_user.id)
    if active:
        save_file(message.from_user.id, file_id, file_type, "", active["id"], file_name)
        await message.answer(f"✅ Saved: \"{active['name']}\"")
        return

    # Case 3: nothing to go on
    await message.answer(
        "⚠️ Is file ko save karne ke liye pehle ek tag active karo:\n"
        "#TagName bhejo (misal #Expository Writing), phir files bhejo — "
        "sab isi tag mein save hongi. Khatam hone pe /done bhej dena.\n\n"
        "Ya seedha caption mein tag ka naam likh kar file bhej sakte ho."
    )

# ---------------------------------------------------------------------------
# INLINE MENU NAVIGATION
# ---------------------------------------------------------------------------
async def show_folders(chat_id: int, user_id: int):
    folders = list_folders(user_id)
    if not folders:
        await bot.send_message(
            chat_id, "Abhi koi folder nahi hai. /newfolder Name se banao."
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📁 {f['name']}", callback_data=f"folder:{f['id']}")]
        for f in folders
    ])
    await bot.send_message(chat_id, "Apna folder chuno:", reply_markup=kb)

@dp.callback_query(F.data.startswith("folder:"))
async def cb_folder(callback: CallbackQuery):
    folder_id = int(callback.data.split(":")[1])
    tags = list_tags(folder_id)
    if not tags:
        await callback.message.answer("Is folder mein koi tag nahi hai.")
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🏷 {t['name']}", callback_data=f"tag:{t['id']}")]
        for t in tags
    ] + [[InlineKeyboardButton(text="⬅️ Wapis", callback_data="back:folders")]])
    await callback.message.edit_text("Tag chuno:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "back:folders")
async def cb_back(callback: CallbackQuery):
    await show_folders(callback.message.chat.id, callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data.startswith("tag:"))
async def cb_tag(callback: CallbackQuery):
    tag_id = int(callback.data.split(":")[1])
    files = list_files_in_tag(tag_id)
    if not files:
        await callback.message.answer("Is tag mein koi file nahi hai.")
        await callback.answer()
        return
    if BOT_USERNAME:
        target_username = FILES_BOT_USERNAME or BOT_USERNAME
        link = f"https://t.me/{target_username}?start=tag_{tag_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Files bot mein kholo", url=link)]
        ])
        await callback.message.answer(
            "Ye link kisi ko bhejo ya khud dabao — alag files-bot mein "
            "seedha yehi files mil jayengi:", reply_markup=kb
        )
    await callback.message.answer(f"📂 {len(files)} file(s) mil gayi:")
    for f in files:
        await send_saved_file(callback.message.chat.id, f, show_delete=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("delfile:"))
async def cb_delete_file(callback: CallbackQuery):
    file_db_id = int(callback.data.split(":")[1])
    delete_file_by_id(file_db_id)
    await callback.answer("🗑 File delete ho gayi")
    try:
        await callback.message.delete()
    except Exception:
        pass

async def send_saved_file(chat_id: int, row, show_delete: bool = False, use_bot=None):
    b = use_bot or bot
    caption = row["caption"] or ""
    kb = None
    if show_delete:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete", callback_data=f"delfile:{row['id']}")]
        ])
    if row["file_type"] == "photo":
        await b.send_photo(chat_id, row["file_id"], caption=caption, reply_markup=kb)
    elif row["file_type"] == "video":
        await b.send_video(chat_id, row["file_id"], caption=caption, reply_markup=kb)
    else:
        await b.send_document(chat_id, row["file_id"], caption=caption, reply_markup=kb)

# ---------------------------------------------------------------------------
# SECOND BOT: pure file-delivery bot (kept clean, no menus/names here)
# ---------------------------------------------------------------------------
if files_dp:
    @files_dp.message(CommandStart())
    async def files_bot_start(message: Message):
        args = message.text.split(maxsplit=1)
        payload = args[1] if len(args) > 1 else ""

        if payload.startswith("tag_"):
            try:
                tag_id = int(payload.replace("tag_", "", 1))
            except ValueError:
                tag_id = None
            if tag_id:
                conn = db()
                tag = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
                conn.close()
                if tag:
                    files = list_files_in_tag(tag_id)
                    if not files:
                        await message.answer(f"📂 \"{tag['name']}\" mein koi file nahi hai.")
                        return
                    for f in files:
                        await send_saved_file(message.chat.id, f, use_bot=files_bot)
                    return
            await message.answer("⚠️ Ye link ab valid nahi hai.")
            return

        await message.answer(
            "👋 Ye sirf file-delivery bot hai.\n"
            "Files browse/manage karne ke liye apna asal bot use karo — "
            "wahan se link pe click karoge to files yahan aayengi."
        )

# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
async def main():
    global BOT_USERNAME, FILES_BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    tasks = [dp.start_polling(bot)]

    if files_bot:
        me2 = await files_bot.get_me()
        FILES_BOT_USERNAME = me2.username
        tasks.append(files_dp.start_polling(files_bot))

    await asyncio.gather(*tasks)

# ---------------------------------------------------------------------------
# TELEGRAM MINI APP (the "board") + small JSON API
# ---------------------------------------------------------------------------
def validate_init_data(init_data: str):
    """Validates Telegram WebApp initData and returns the user dict, or None."""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if computed_hash != received_hash:
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

def tg_api_get_file_path(file_id: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={urllib.parse.quote(file_id)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        return None
    return data["result"]["file_path"]

INDEX_HTML = """<!DOCTYPE html>
<html lang="ur">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DataNest Board</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #0e0e12);
    color: var(--tg-theme-text-color, #fff);
    padding: 12px;
  }
  h2 { font-size: 18px; margin: 8px 0 14px; }
  .row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px; margin-bottom: 8px; border-radius: 12px;
    background: var(--tg-theme-secondary-bg-color, #1c1c22);
    cursor: pointer;
  }
  .row span.count {
    font-size: 12px; opacity: 0.6;
  }
  .back { margin-bottom: 10px; opacity: 0.8; cursor: pointer; }
  .grid {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;
  }
  .card {
    background: var(--tg-theme-secondary-bg-color, #1c1c22);
    border-radius: 12px; overflow: hidden; position: relative;
  }
  .card img { width: 100%; height: 110px; object-fit: cover; display: block; }
  .card .doc {
    height: 110px; display: flex; align-items: center; justify-content: center;
    font-size: 34px;
  }
  .card .name {
    font-size: 11px; padding: 6px; opacity: 0.85; word-break: break-word;
  }
  .card .del {
    position: absolute; top: 4px; right: 4px; background: rgba(200,0,0,0.85);
    color: #fff; border: none; border-radius: 8px; padding: 4px 8px; font-size: 11px;
  }
  .empty { opacity: 0.6; padding: 20px; text-align: center; }
  .loading { padding: 20px; text-align: center; opacity: 0.6; }
</style>
</head>
<body>
  <div id="app"><div class="loading">Load ho raha hai...</div></div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
const initData = tg.initData || "";

let state = { view: "folders", folderId: null, folderName: "", tagId: null, tagName: "" };

async function api(path) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(path + sep + "initData=" + encodeURIComponent(initData));
  return res.json();
}

function el(html) {
  const d = document.createElement("div");
  d.innerHTML = html;
  return d.firstElementChild;
}

async function renderFolders() {
  state = { view: "folders", folderId: null, folderName: "", tagId: null, tagName: "" };
  const app = document.getElementById("app");
  app.innerHTML = '<div class="loading">Load ho raha hai...</div>';
  const data = await api("/api/folders");
  app.innerHTML = "";
  app.appendChild(el(`<h2>📁 Apne Folders</h2>`));
  if (!data.folders || data.folders.length === 0) {
    app.appendChild(el(`<div class="empty">Koi folder nahi hai. Bot mein /newfolder se banao.</div>`));
    return;
  }
  data.folders.forEach(f => {
    const row = el(`<div class="row"><span>📁 ${f.name}</span><span class="count">${f.tag_count} tags</span></div>`);
    row.onclick = () => renderTags(f.id, f.name);
    app.appendChild(row);
  });
}

async function renderTags(folderId, folderName) {
  state = { view: "tags", folderId, folderName, tagId: null, tagName: "" };
  const app = document.getElementById("app");
  app.innerHTML = '<div class="loading">Load ho raha hai...</div>';
  const data = await api("/api/tags?folder_id=" + folderId);
  app.innerHTML = "";
  const back = el(`<div class="back">⬅️ Wapis</div>`);
  back.onclick = renderFolders;
  app.appendChild(back);
  app.appendChild(el(`<h2>🏷 ${folderName}</h2>`));
  if (!data.tags || data.tags.length === 0) {
    app.appendChild(el(`<div class="empty">Is folder mein koi tag nahi.</div>`));
    return;
  }
  data.tags.forEach(t => {
    const row = el(`<div class="row"><span>🏷 ${t.name}</span><span class="count">${t.file_count} files</span></div>`);
    row.onclick = () => renderFiles(t.id, t.name);
    app.appendChild(row);
  });
}

async function renderFiles(tagId, tagName) {
  state.view = "files"; state.tagId = tagId; state.tagName = tagName;
  const app = document.getElementById("app");
  app.innerHTML = '<div class="loading">Load ho raha hai...</div>';
  const data = await api("/api/files?tag_id=" + tagId);
  app.innerHTML = "";
  const back = el(`<div class="back">⬅️ Wapis</div>`);
  back.onclick = () => renderTags(state.folderId, state.folderName);
  app.appendChild(back);
  app.appendChild(el(`<h2>📂 ${tagName}</h2>`));
  if (!data.files || data.files.length === 0) {
    app.appendChild(el(`<div class="empty">Koi file nahi hai.</div>`));
    return;
  }
  const grid = el(`<div class="grid"></div>`);
  data.files.forEach(f => {
    let inner;
    if (f.file_type === "photo") {
      inner = `<img src="${f.url}" loading="lazy">`;
    } else {
      inner = `<div class="doc">📄</div>`;
    }
    const card = el(`<div class="card">${inner}
      <div class="name">${f.file_name || f.file_type}</div>
      <button class="del">🗑</button>
    </div>`);
    card.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      await fetch("/api/delete?fid=" + f.id + "&initData=" + encodeURIComponent(initData));
      card.remove();
    };
    if (f.file_type !== "photo") {
      card.onclick = () => tg.openLink(f.url);
    }
    grid.appendChild(card);
  });
  app.appendChild(grid);
}

renderFolders();
</script>
</body>
</html>
"""

def run_web_server():
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silence default access logs

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self, qs):
            init_data = qs.get("initData", [""])[0]
            user = validate_init_data(init_data)
            return user

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path in ("/", "/health"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Bot is running")
                return

            if parsed.path == "/app":
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/folders":
                user = self._auth(qs)
                if not user:
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                conn = db()
                rows = conn.execute(
                    """
                    SELECT f.id, f.name,
                           (SELECT COUNT(*) FROM tags t WHERE t.folder_id=f.id) as tag_count
                    FROM folders f WHERE f.user_id=? ORDER BY f.name
                    """,
                    (user["id"],),
                ).fetchall()
                conn.close()
                self._send_json({"folders": [dict(r) for r in rows]})
                return

            if parsed.path == "/api/tags":
                user = self._auth(qs)
                if not user:
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                folder_id = qs.get("folder_id", [None])[0]
                conn = db()
                rows = conn.execute(
                    """
                    SELECT t.id, t.name,
                           (SELECT COUNT(*) FROM files fl WHERE fl.tag_id=t.id) as file_count
                    FROM tags t
                    JOIN folders f ON t.folder_id=f.id
                    WHERE t.folder_id=? AND f.user_id=?
                    ORDER BY t.name
                    """,
                    (folder_id, user["id"]),
                ).fetchall()
                conn.close()
                self._send_json({"tags": [dict(r) for r in rows]})
                return

            if parsed.path == "/api/files":
                user = self._auth(qs)
                if not user:
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                tag_id = qs.get("tag_id", [None])[0]
                conn = db()
                rows = conn.execute(
                    "SELECT * FROM files WHERE tag_id=? AND user_id=? ORDER BY created_at DESC",
                    (tag_id, user["id"]),
                ).fetchall()
                conn.close()
                files = []
                for r in rows:
                    direct_url = None
                    try:
                        fp = tg_api_get_file_path(r["file_id"])
                        if fp:
                            direct_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}"
                    except Exception:
                        direct_url = None
                    files.append({
                        "id": r["id"],
                        "file_type": r["file_type"],
                        "file_name": r["file_name"],
                        "url": direct_url or f"/api/download?fid={r['id']}&initData={urllib.parse.quote(qs.get('initData',[''])[0])}",
                    })
                self._send_json({"files": files})
                return

            if parsed.path == "/api/download":
                user = self._auth(qs)
                if not user:
                    self.send_response(401)
                    self.end_headers()
                    return
                fid = qs.get("fid", [None])[0]
                conn = db()
                row = conn.execute(
                    "SELECT * FROM files WHERE id=? AND user_id=?", (fid, user["id"])
                ).fetchone()
                conn.close()
                if not row:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    file_path = tg_api_get_file_path(row["file_id"])
                    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                    with urllib.request.urlopen(file_url, timeout=20) as resp:
                        data = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(500)
                    self.end_headers()
                return

            if parsed.path == "/api/delete":
                user = self._auth(qs)
                if not user:
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                fid = qs.get("fid", [None])[0]
                conn = db()
                conn.execute("DELETE FROM files WHERE id=? AND user_id=?", (fid, user["id"]))
                conn.commit()
                conn.close()
                self._send_json({"ok": True})
                return

            self.send_response(404)
            self.end_headers()

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main())
