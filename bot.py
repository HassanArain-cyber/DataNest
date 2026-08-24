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
from datetime import datetime

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
        "*Search karne ke liye:* `/search english`",
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
    hashtags = [w[1:] for w in caption.split() if w.startswith("#")]

    if not hashtags:
        await message.answer(
            "⚠️ Is file ko save karne ke liye caption mein kam az kam ek "
            "#tag do. Misal: #English #Notes\n\n"
            "Pehle /newtag se tag bana lo agar nahi bana hua."
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

    saved_tags = []
    for tagname in hashtags:
        # find tag anywhere in user's folders (case-insensitive)
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
        if row:
            save_file(message.from_user.id, file_id, file_type, caption, row["id"])
            saved_tags.append(row["name"])

    if saved_tags:
        await message.answer(f"✅ Save ho gaya tag(s) mein: {', '.join(saved_tags)}")
    else:
        await message.answer(
            "⚠️ Ye tag(s) exist nahi karte. Pehle /newtag se bana lo, "
            "phir dobara bhejo (caption ke saath forward kar dena)."
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

if __name__ == "__main__":
    asyncio.run(main())
