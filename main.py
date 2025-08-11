"""
Telegram Reference Bot — v7.1
— Пользователь прикрепляет до 9 медиа: фото / видео / GIF (без скачивания ссылок)
— Меню ▶️ Старт / ⏹ Стоп, автозапуск по ссылке
— Заголовок: yt-dlp (--skip-download, с cookies/impersonation из ENV) -> oEmbed -> og/twitter -> <title>
— Instagram: названием берём ник автора
— Кредиты: dir / dop / color / prod (каждое поле можно пропустить)
— Порядок подписи: Заголовок‑ссылка → кредиты → хэштеги (категория/теги) — без слова «Категории/теги»
— Хэштеги: '-' автоматически меняется на '_'

Зависимости:
  pip install -U aiogram yt-dlp requests
"""

import asyncio
import json
import os
import re
import sqlite3
import textwrap
from contextlib import closing
from datetime import datetime
from typing import List, Optional, Dict, Literal

import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@your_channel_username")

# ---- YT-DLP ENV (для метаданных) ----
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE")  # путь к cookies.txt (опц.)
YTDLP_BROWSER = os.getenv("YTDLP_BROWSER")            # safari | chrome | 'chrome:Default' | ...
YTDLP_IMPERSONATE = os.getenv("YTDLP_IMPERSONATE")    # chrome-120 | safari | edge-120 | ...

def yt_dlp_meta_args() -> List[str]:
    args: List[str] = []
    if YTDLP_COOKIES_FILE:
        args += ["--cookies", YTDLP_COOKIES_FILE]
    elif YTDLP_BROWSER:
        args += ["--cookies-from-browser", YTDLP_BROWSER]
    if YTDLP_IMPERSONATE:
        args += ["--impersonate", YTDLP_IMPERSONATE]
    return args

# Категории (если нужно — пришлёшь новую матрицу, обновлю)
CATEGORIES = [
    "fashion", "auto", "food", "beauty", "sport", "tech",
    "lifestyle", "luxury", "docu", "comedy", "music", "film",
]

# Теги (обновлённые группы)
TAG_GROUPS: Dict[str, List[str]] = {
    "🎥 Техника": [
        "slowmo", "mo-control", "drone", "steadicam", "handheld", "macro", "grip",
    ],
    "🎨 Цветокор": [
        "neon", "bw", "warm", "cold", "natural",
    ],
    "🎭 Настроение": [
        "dreamy", "energetic", "intense", "tender", "epic", "drama", "angry",
    ],
    "🖥️ Монтаж/спецприёмы": [
        "timelapse", "split_screen", "lighttrails", "vfx", "animation",
        "transition-in", "transition-out", "cg", "ai",
    ],
    "💡 Свет": [
        "low_key", "high_key", "dynamic_light",
    ],
    "🔊 Звук": [
        "music", "beat", "soundesign", "sfx",
    ],
}

# ---------- DB ----------
DB_PATH = "work/references.db"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    title TEXT,
    category TEXT,
    tags TEXT,
    dir TEXT,
    dop TEXT,
    color TEXT,
    prod TEXT,
    channel_message_id INTEGER,
    media_json TEXT,            -- JSON list of {"type":"photo|video|animation", "file_id":"..."}
    created_at TEXT NOT NULL
);
"""

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(SCHEMA_SQL)
        # Мягкие миграции со старых версий
        for new_col in ("dir", "dop", "color", "prod", "media_json"):
            try:
                conn.execute(f"ALTER TABLE refs ADD COLUMN {new_col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()

def insert_reference(
    source_url: str,
    title: str,
    category: str,
    tags: List[str],
    media: List[Dict[str, str]],
    channel_message_id: Optional[int],
    dir_: str = "",
    dop: str = "",
    color: str = "",
    prod: str = "",
) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO refs(source_url,title,category,tags,dir,dop,color,prod,channel_message_id,media_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_url,
                title,
                category,
                ",".join(tags),
                dir_,
                dop,
                color,
                prod,
                channel_message_id,
                json.dumps(media),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid

# ---------- FSM ----------
class AddFlow(StatesGroup):
    idle = State()
    collecting_media = State()
    choosing_category = State()
    choosing_tags = State()
    entering_dir = State()
    entering_dop = State()
    entering_color = State()
    entering_prod = State()

# ---------- HELPERS ----------
LINK_RE = re.compile(r"https?://\S+")

def reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⏹ Стоп")]],
        resize_keyboard=True,
        is_persistent=True,
    )

def build_categories_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, c in enumerate(CATEGORIES, 1):
        row.append(InlineKeyboardButton(text=c, callback_data=f"cat:{c}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_tags_kb(selected: List[str]) -> InlineKeyboardMarkup:
    sel = set(selected)
    rows: List[List[InlineKeyboardButton]] = []
    for title, tags in TAG_GROUPS.items():
        rows.append([InlineKeyboardButton(text=title, callback_data="noop")])
        row: List[InlineKeyboardButton] = []
        for i, t in enumerate(tags, 1):
            mark = "✓ " if t in sel else "• "
            row.append(InlineKeyboardButton(text=f"{mark}{t}", callback_data=f"t:{t}"))
            if i % 3 == 0:
                rows.append(row); row = []
        if row: rows.append(row)
    rows.append([
        InlineKeyboardButton(text="Готово", callback_data="done"),
        InlineKeyboardButton(text="Без тегов", callback_data="skip"),
        InlineKeyboardButton(text="Сбросить", callback_data="clr"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def hashtags(category: str, tags: List[str]) -> str:
    parts = []
    if category:
        parts.append(f"#{category.strip().replace('-', '_')}")
    for t in tags:
        parts.append(f"#{t.strip().replace('-', '_')}")
    return " ".join(parts)

def html_link_title(title: str, url: str) -> str:
    safe_url = url.replace('"', "%22").replace("<", "").replace(">", "")
    safe_title = (title or url).replace("<", "").replace(">", "")
    return f'<b><a href="{safe_url}">{safe_title}</a></b>'

async def fetch_title_from_url(url: str) -> str:
    """
    1) yt-dlp --skip-download (+ cookies/impersonate из ENV)
    2) oEmbed (YouTube/Vimeo)
    2.5) Instagram — ник автора
    3) og:title / twitter:title
    4) <title>  + обрезка ' - YouTube' / ' on Vimeo'
    """
    import subprocess, html as htmlmod
    # 1) yt-dlp
    try:
        cmd = ["yt-dlp", "-j", "--skip-download", url] + yt_dlp_meta_args()
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        data = json.loads(out.splitlines()[0])
        t = (data.get("title") or "").strip()
        if t:
            return t
    except Exception:
        pass
    # 2) oEmbed
    try:
        if "youtube.com" in url or "youtu.be" in url:
            r = requests.get("https://www.youtube.com/oembed",
                             params={"url": url, "format": "json"},
                             timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                t = (r.json().get("title") or "").strip()
                if t: return t
        if "vimeo.com" in url:
            r = requests.get("https://vimeo.com/api/oembed.json",
                             params={"url": url},
                             timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                t = (r.json().get("title") or "").strip()
                if t: return t
    except Exception:
        pass
    # 2.5) Instagram — ник автора (из og:title)
    if "instagram.com" in url:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', r.text, re.I)
                if m:
                    raw = m.group(1)
                    raw = re.sub(r"\s*•.*Instagram.*", "", raw, flags=re.I)
                    raw = re.sub(r"\s*on Instagram.*", "", raw, flags=re.I)
                    return raw.strip()
        except Exception:
            pass
    # 3) og:title / twitter:title
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        html_text = r.text
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', html_text, re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']twitter:title["\'][^>]*content=["\'](.*?)["\']', html_text, re.I)
        if m:
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            return htmlmod.unescape(t)
        # 4) <title>
        m = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
        if m:
            t = htmlmod.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
            t = re.sub(r"\s*[-–—]\s*YouTube$", "", t, flags=re.I)
            t = re.sub(r"\s*on\s+Vimeo$", "", t, flags=re.I)
            t = re.sub(r"\s*-\s*Vimeo$", "", t, flags=re.I)
            return t
    except Exception:
        pass
    return ""

# ---------- ROUTER ----------
router = Router()

@router.message(CommandStart())
async def on_cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await state.update_data(enabled=True)
    await state.set_state(AddFlow.idle)
    await msg.answer(
        "Готов! Пришли ссылку — и начнём. Либо пользуйся ▶️ Старт / ⏹ Стоп.",
        reply_markup=reply_menu(),
    )

@router.message(F.text == "▶️ Старт")
async def on_btn_start(msg: Message, state: FSMContext):
    await state.update_data(enabled=True)
    await state.set_state(AddFlow.idle)
    await msg.answer("Я на связи. Пришли ссылку, и начнём ✨", reply_markup=reply_menu())

@router.message(F.text == "⏹ Стоп")
async def on_btn_stop(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddFlow.idle)
    await state.update_data(enabled=False)
    await msg.answer("Остановился. Нажми ▶️ Старт, когда нужно продолжить.", reply_markup=reply_menu())

# --- Автозапуск по ссылке ---
@router.message(AddFlow.idle, F.text.regexp(LINK_RE))
async def on_link_auto(msg: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("enabled", True):
        await msg.answer("Я на паузе. Нажми ▶️ Старт, чтобы продолжить.", reply_markup=reply_menu())
        return

    url = LINK_RE.search(msg.text).group(0)
    title = (await fetch_title_from_url(url)) or url
    await state.update_data(
        source_url=url,
        title=title,
        media=[],
        category=None,
        selected_tags=[],
        dir="",
        dop="",
        color="",
        prod="",
    )

    await state.set_state(AddFlow.collecting_media)
    await msg.answer(
        f"Нашёл заголовок: <b>{title}</b>\n\nПрикрепи до <b>9</b> медиа: фото, видео или GIF. Когда закончишь — нажми «Готово».",
        reply_markup=reply_menu(),
        parse_mode=ParseMode.HTML,
    )
    await msg.answer(
        "Управление вложениями:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Готово", callback_data="media_done"),
             InlineKeyboardButton(text="Сбросить медиа", callback_data="media_clear")]
        ])
    )

# --- Сбор медиа (photo / video / animation-GIF) ---
def _append_media(state_data: dict, kind: Literal["photo","video","animation"], file_id: str) -> int:
    media: List[Dict[str, str]] = state_data.get("media", [])
    if len(media) >= 9:
        return len(media)
    media.append({"type": kind, "file_id": file_id})
    state_data["media"] = media
    return len(media)

@router.message(AddFlow.collecting_media, F.photo)
async def on_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    new_len = _append_media(data, "photo", msg.photo[-1].file_id)
    await state.update_data(**data)
    await msg.answer("Лимит 9. Нажми «Готово»." if new_len > 9 else f"Добавлено: {new_len}/9.")

@router.message(AddFlow.collecting_media, F.video)
async def on_video(msg: Message, state: FSMContext):
    data = await state.get_data()
    new_len = _append_media(data, "video", msg.video.file_id)
    await state.update_data(**data)
    await msg.answer(f"Добавлено: {new_len}/9.")

@router.message(AddFlow.collecting_media, F.animation)
async def on_animation(msg: Message, state: FSMContext):
    data = await state.get_data()
    new_len = _append_media(data, "animation", msg.animation.file_id)
    await state.update_data(**data)
    await msg.answer(f"Добавлено: {new_len}/9.")

@router.callback_query(AddFlow.collecting_media, F.data == "media_clear")
async def on_media_clear(cb: CallbackQuery, state: FSMContext):
    await state.update_data(media=[])
    await cb.message.answer("Список вложений очищен. Пришли новые медиа (до 9).")
    await cb.answer("Очищено")

@router.callback_query(AddFlow.collecting_media, F.data == "media_done")
async def on_media_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    media = data.get("media", [])
    if not media:
        await cb.message.answer("Нужно добавить хотя бы одно вложение.")
        await cb.answer()
        return
    await state.set_state(AddFlow.choosing_category)
    await cb.message.answer("Выбери категорию:", reply_markup=build_categories_kb())
    await cb.answer()

# --- Выбор категории/тегов ---
@router.callback_query(AddFlow.choosing_category, F.data.startswith("cat:"))
async def on_category(cb: CallbackQuery, state: FSMContext):
    category = cb.data.split(":", 1)[1]
    await state.update_data(category=category, selected_tags=[])
    await state.set_state(AddFlow.choosing_tags)
    await cb.message.edit_text(
        f"Категория: <b>{category}</b>\nВыбери теги (можно несколько) или нажми «Без тегов».",
        parse_mode=ParseMode.HTML,
        reply_markup=build_tags_kb([]),
    )
    await cb.answer()

@router.callback_query(AddFlow.choosing_tags, F.data == "noop")
async def on_noop(cb: CallbackQuery):
    await cb.answer()

@router.callback_query(AddFlow.choosing_tags, F.data == "clr")
async def on_clr(cb: CallbackQuery, state: FSMContext):
    await state.update_data(selected_tags=[])
    await cb.message.edit_reply_markup(reply_markup=build_tags_kb([]))
    await cb.answer("Теги сброшены")

@router.callback_query(AddFlow.choosing_tags, F.data.startswith("t:"))
async def on_toggle_tag(cb: CallbackQuery, state: FSMContext):
    tag = cb.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("selected_tags", []))
    if tag in selected:
        selected.remove(tag)
    else:
        selected.add(tag)
    selected_list = sorted(selected)
    await state.update_data(selected_tags=selected_list)
    await cb.message.edit_reply_markup(reply_markup=build_tags_kb(selected_list))
    await cb.answer()

@router.callback_query(AddFlow.choosing_tags, F.data.in_({"done", "skip"}))
async def on_tags_done_or_skip(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddFlow.entering_dir)
    await cb.message.answer("dir? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_dir")]]
    ))
    await cb.answer()

# --- Кредиты: dir / dop / color / prod ---
@router.callback_query(AddFlow.entering_dir, F.data == "skip_dir")
async def skip_dir(cb: CallbackQuery, state: FSMContext):
    await state.update_data(dir="")
    await state.set_state(AddFlow.entering_dop)
    await cb.message.answer("dop? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_dop")]]
    ))
    await cb.answer()

@router.message(AddFlow.entering_dir)
async def got_dir(msg: Message, state: FSMContext):
    await state.update_data(dir=(msg.text or "").strip())
    await state.set_state(AddFlow.entering_dop)
    await msg.answer("dop? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_dop")]]
    ))

@router.callback_query(AddFlow.entering_dop, F.data == "skip_dop")
async def skip_dop(cb: CallbackQuery, state: FSMContext):
    await state.update_data(dop="")
    await state.set_state(AddFlow.entering_color)
    await cb.message.answer("color? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_color")]]
    ))
    await cb.answer()

@router.message(AddFlow.entering_dop)
async def got_dop(msg: Message, state: FSMContext):
    await state.update_data(dop=(msg.text or "").strip())
    await state.set_state(AddFlow.entering_color)
    await msg.answer("color? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_color")]]
    ))

@router.callback_query(AddFlow.entering_color, F.data == "skip_color")
async def skip_color(cb: CallbackQuery, state: FSMContext):
    await state.update_data(color="")
    await state.set_state(AddFlow.entering_prod)
    await cb.message.answer("prod? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_prod")]]
    ))
    await cb.answer()

@router.message(AddFlow.entering_color)
async def got_color(msg: Message, state: FSMContext):
    await state.update_data(color=(msg.text or "").strip())
    await state.set_state(AddFlow.entering_prod)
    await msg.answer("prod? (текст или «Пропустить»)", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_prod")]]
    ))

@router.callback_query(AddFlow.entering_prod, F.data == "skip_prod")
async def skip_prod(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.update_data(prod="")
    await finalize_and_post(cb.message, state, bot)
    await cb.answer()

@router.message(AddFlow.entering_prod)
async def got_prod(msg: Message, state: FSMContext, bot: Bot):
    await state.update_data(prod=(msg.text or "").strip())
    await finalize_and_post(msg, state, bot)

# --- Финал: публикация ---
async def finalize_and_post(msg_or_cb_message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    url: str = data.get("source_url", "")
    title: str = data.get("title") or url
    media: List[Dict[str, str]] = data.get("media", [])
    category: str = data.get("category", "misc")
    tags: List[str] = data.get("selected_tags", [])

    dir_ : str = data.get("dir", "")
    dop  : str = data.get("dop", "")
    color: str = data.get("color", "")
    prod : str = data.get("prod", "")

    if not media:
        await msg_or_cb_message.answer("Нужно добавить хотя бы одно вложение. Нажми ▶️ Старт и начни заново.")
        await state.set_state(AddFlow.idle)
        return

    title_line = html_link_title(title, url)

    credits_lines = []
    if dir_:  credits_lines.append(f"dir: {dir_}")
    if dop:   credits_lines.append(f"dop: {dop}")
    if color: credits_lines.append(f"color: {color}")
    if prod:  credits_lines.append(f"prod: {prod}")

    parts = [title_line]
    if credits_lines:
        parts.append("\n".join(credits_lines))
    if category or tags:
        parts.append(hashtags(category, tags))

    cap = "\n\n".join(parts).strip()

    # Собираем смешанный медиа-альбом
    items = []
    for idx, m in enumerate(media):
        t, fid = m["type"], m["file_id"]
        if idx == 0:
            if t == "photo":
                items.append(InputMediaPhoto(media=fid, caption=cap, parse_mode=ParseMode.HTML))
            elif t == "video":
                items.append(InputMediaVideo(media=fid, caption=cap, parse_mode=ParseMode.HTML))
            else:
                items.append(InputMediaAnimation(media=fid, caption=cap, parse_mode=ParseMode.HTML))
        else:
            if t == "photo":
                items.append(InputMediaPhoto(media=fid))
            elif t == "video":
                items.append(InputMediaVideo(media=fid))
            else:
                items.append(InputMediaAnimation(media=fid))

    try:
        msgs = await bot.send_media_group(chat_id=CHANNEL_ID, media=items)
        first_id = msgs[0].message_id if msgs else None
    except Exception:
        await msg_or_cb_message.answer("Не удалось опубликовать в канал. Проверь права бота и CHANNEL_ID.")
        return

    insert_reference(
        source_url=url,
        title=title,
        category=category,
        tags=tags,
        media=media,
        channel_message_id=first_id,
        dir_=dir_,
        dop=dop,
        color=color,
        prod=prod,
    )

    await msg_or_cb_message.answer("Готово! Пост опубликован в канале ✅", reply_markup=reply_menu())
    await state.clear()
    await state.set_state(AddFlow.idle)

# ---------- MAIN ----------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    init_db()
    dp = Dispatcher()
    dp.include_router(router)
    print("Bot is running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")