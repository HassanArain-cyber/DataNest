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
import logging
import os
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.path.join(os.path.dirname(__file__), "datanest.db")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
        caption TEXT,
        tag_id INTEGER,
        created_at TEXT,
        FOREIGN KEY(tag_id) REFERENCES tags(id)
    );
    """)
    conn.commit()
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

def save_file(user_id, file_id, file_type, caption, tag_id):
    conn = db()
    conn.execute(
        "INSERT INTO files(user_id,file_id,file_type,caption,tag_id,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, file_id, file_type, caption, tag_id, datetime.utcnow().isoformat()),
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

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message):
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

# ---------------------------------------------------------------------------
# FILE UPLOAD HANDLER (photo / document / video with caption #tag)
# ---------------------------------------------------------------------------
@dp.message(F.photo | F.document | F.video)
async def handle_file(message: Message):
    caption = message.caption or ""

    if not caption.strip():
        await message.answer(
            "⚠️ Is file ko save karne ke liye caption mein tag ka naam likho.\n"
            "Misal: Expository Writing\n"
            "(bilkul wahi naam jo /newtag se banaya tha, # zaroori nahi)"
        )
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        file_id = message.video.file_id
        file_type = "video"

    # Get all of this user's tags, then match any whose name appears in the caption
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
            save_file(message.from_user.id, file_id, file_type, caption, t["id"])
            saved_tags.append(t["name"])

    if saved_tags:
        await message.answer(f"✅ Save ho gaya tag(s) mein: {', '.join(saved_tags)}")
    else:
        await message.answer(
            "⚠️ Ye tag exist nahi karta. Pehle /newtag se bana lo (ya /mytags se "
            "list dekho ke sahi spelling kya hai), phir dobara bhejo."
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
    await callback.message.answer(f"📂 {len(files)} file(s) mil gayi:")
    for f in files:
        await send_saved_file(callback.message.chat.id, f)
    await callback.answer()

async def send_saved_file(chat_id: int, row):
    caption = row["caption"] or ""
    if row["file_type"] == "photo":
        await bot.send_photo(chat_id, row["file_id"], caption=caption)
    elif row["file_type"] == "video":
        await bot.send_video(chat_id, row["file_id"], caption=caption)
    else:
        await bot.send_document(chat_id, row["file_id"], caption=caption)

# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
async def main():
    await dp.start_polling(bot)

def run_fake_webserver():
    """Render's free Web Service needs something listening on a port.
    This tiny server does nothing except say 'OK' so Render is happy."""
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running")

        def log_message(self, *args):
            pass  # silence logs

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_fake_webserver, daemon=True).start()
    asyncio.run(main())
