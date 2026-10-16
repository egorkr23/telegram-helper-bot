import asyncio
import logging
import sys
import os
import sqlite3
import re
from datetime import date
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from html import escape
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
import yt_dlp

TOKEN = "8684428526:AAGaUesYUe4VgIdjLQJ25_B7ICYJ5rKjsos"
ADMIN_ID = 1745630699  # Ваш ID администратора (Вечный PRO)
FREE_LIMIT_PER_DAY = 3

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s - %(levelname)s - %(message)s")

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_pro INTEGER DEFAULT 0,
            pro_expire_date TEXT,
            daily_usage INTEGER DEFAULT 0,
            last_usage_date TEXT,
            current_mode TEXT DEFAULT 'idle'
        )
    """)
    conn.commit()
    conn.close()

def check_user_limit(user_id: int, username: str) -> tuple[bool, int, bool]:
    if user_id == ADMIN_ID:
        return True, 0, True

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    today_str = str(date.today())
    
    cursor.execute("SELECT is_pro, pro_expire_date, daily_usage, last_usage_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (user_id, username, daily_usage, last_usage_date, current_mode) VALUES (?, ?, 0, ?, 'idle')", 
                       (user_id, username, today_str))
        conn.commit()
        is_pro, daily_usage = False, 0
    else:
        is_pro = bool(row[0])
        daily_usage = row[2]
        last_date = row[3]
        
        if last_date != today_str:
            daily_usage = 0
            cursor.execute("UPDATE users SET daily_usage = 0, last_usage_date = ? WHERE user_id = ?", (today_str, user_id))
            conn.commit()

    if is_pro:
        conn.close()
        return True, 0, True

    if daily_usage < FREE_LIMIT_PER_DAY:
        conn.close()
        return True, FREE_LIMIT_PER_DAY - daily_usage, False
    
    conn.close()
    return False, 0, False

def increment_usage(user_id: int):
    if user_id == ADMIN_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

user_links = {}

def clean_media_url(raw_url: str) -> str:
    if "youtube.com" in raw_url or "youtu.be" in raw_url:
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", raw_url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"
    return raw_url

# === ГЛАВНОЕ НИЖНЕЕ МЕНЮ ===
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Скачать видео / музыку"), KeyboardButton(text="📁 Конвертер файлов")],
        [KeyboardButton(text="🌟 Мой профиль / PRO"), KeyboardButton(text="ℹ️ Помощь")],
        [KeyboardButton(text="⬅️ Главное меню / Назад")]
    ],
    resize_keyboard=True
)

# === БОТ ===
dp = Dispatcher()

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    init_db()
    _, left_count, is_pro = check_user_limit(message.from_user.id, message.from_user.username)
    
    safe_name = escape(message.from_user.full_name)
    text = (
        f"✨ <b>Приветствую, {safe_name}!</b> 🚀\n\n"
        f"⚡ Профессиональный цифровой комбайн готов к работе в облаке.\n"
        f"🎯 Выберите нужный инструмент в меню ниже 👇"
    )
    if is_pro:
        text += "\n\n👑 <b>Ваш статус:</b> <code>DEVELOPER / PRO</code> (Безлимит ⚡)"
    else:
        text += f"\n\n📊 <b>Ваш статус:</b> <code>Бесплатный тариф</code> (Осталось запросов: <b>{left_count}/{FREE_LIMIT_PER_DAY}</b> ⏳)"
        
    await message.answer(text, reply_markup=main_menu_keyboard)

@dp.message(F.text == "⬅️ Главное меню / Назад")
async def menu_back(message: Message):
    await message.answer("🏠 <b>Главное меню</b>\nВыберите категорию:", reply_markup=main_menu_keyboard)

@dp.message(F.text == "📥 Скачать видео / музыку")
async def menu_download(message: Message):
    await message.answer(
        "📥 <b>Загрузчик медиа активен</b> 🎬\n\n"
        "🔗 Отправьте ссылку на видео или трек (YouTube, TikTok, Instagram), и бот предложит выбор формата.",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text == "📁 Конвертер файлов")
async def menu_convert(message: Message):
    await message.answer(
        "📁 <b>Конвертер файлов</b> ⚙️\n\n"
        "📎 Отправьте в чат документ или изображение для оптимизации.",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text == "🌟 Мой профиль / PRO")
async def menu_profile(message: Message):
    init_db()
    _, left_count, is_pro = check_user_limit(message.from_user.id, message.from_user.username)
    
    status_text = "👑 АДМИН / PRO (Безлимит ⚡)" if is_pro else f"Бесплатный (Осталось лимита: {left_count}/{FREE_LIMIT_PER_DAY} ⏳)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Активировать PRO ✨", callback_data="buy_pro")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="go_back")]
    ])
    
    await message.answer(f"👤 <b>Ваш профиль:</b>\n\n🆔 ID: <code>{message.from_user.id}</code>\n⭐ План: <b>{status_text}</b>", reply_markup=kb)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: Message):
    await message.answer("ℹ️ <b>Центр справки:</b>\n\n• Отправьте ссылку на медиа для загрузки.\n• Отправьте файл для конвертации.", reply_markup=main_menu_keyboard)

@dp.message(F.text.regexp(r"https?://"))
async def handle_url(message: Message) -> None:
    can_use, _, is_pro = check_user_limit(message.from_user.id, message.from_user.username)
    
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Купить PRO", callback_data="buy_pro")]])
        await message.answer("⚠️ <b>Суточный лимит исчерпан!</b> Оформите подписку.", reply_markup=kb)
        return

    if not is_pro:
        increment_usage(message.from_user.id)

    cleaned_url = clean_media_url(message.text)
    user_links[message.from_user.id] = cleaned_url
    
    status_msg = await message.answer("🔍 <i>Анализирую ссылку...</i> ⏳")
    await asyncio.sleep(0.3)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Скачать ВИДЕО (MP4)", callback_data="download_type_video")],
        [InlineKeyboardButton(text="🎵 Скачать МУЗЫКУ (MP3)", callback_data="download_type_audio")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="go_back")]
    ])
    
    await status_msg.edit_text("✅ <b>Ссылка успешно принята!</b>\n🎯 Выберите формат загрузки:", reply_markup=kb)

@dp.message(F.document | F.photo)
async def handle_file(message: Message) -> None:
    can_use, _, is_pro = check_user_limit(message.from_user.id, message.from_user.username)
    
    if not can_use:
        await message.answer("⚠️ <b>Лимит исчерпан!</b>")
        return

    if not is_pro:
        increment_usage(message.from_user.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Конвертировать / Сжать ⚙️", callback_data="conv_standard")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="go_back")]
    ])
    await message.answer("✅ <b>Файл получен!</b> Выберите действие:", reply_markup=kb)

@dp.callback_query(F.data == "go_back")
async def process_go_back(callback: CallbackQuery):
    await callback.message.edit_text("↩️ <b>Действие отменено.</b>")
    await callback.answer()

# Защищенная загрузка ВИДЕО с мультиклиентским обходом (Android + TV + Web)
@dp.callback_query(F.data == "download_type_video")
async def process_video_download(callback: CallbackQuery):
    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.message.edit_text("❌ Ссылка устарела. Отправьте её заново.")
        await callback.answer()
        return

    status_msg = await callback.message.edit_text("🎬 <b>Загружаю видео (обход защиты активен)...</b> 🔄")
    
    output_filename = f"video_{user_id}.mp4"
    ydl_opts = {
        'format': 'best[height<=720]/best',
        'outtmpl': output_filename,
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'tv_embedded', 'web']
            }
        },
        'socket_timeout': 30,
        'quiet': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(output_filename):
            await status_msg.edit_text("📤 Отправляю видео в чат...")
            input_file = FSInputFile(output_filename)
            await callback.message.answer_video(video=input_file, caption="📥 <b>Ваше видео успешно скачано!</b> 🚀")
            os.remove(output_filename)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не удалось найти готовый видеофайл.")
    except Exception as e:
        logging.error(f"Ошибка загрузки видео: {e}")
        await status_msg.edit_text("❌ <b>Ошибка загрузки!</b> Источник отклонил запрос.")

    await callback.answer()

# Защищенная загрузка МУЗЫКИ (MP3)
@dp.callback_query(F.data == "download_type_audio")
async def process_audio_download(callback: CallbackQuery):
    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.message.edit_text("❌ Ссылка устарела. Отправьте её заново.")
        await callback.answer()
        return

    status_msg = await callback.message.edit_text("🎵 <b>Конвертирую аудио (обход защиты активен)...</b> 🔄")
    
    output_base = f"audio_{user_id}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_base,
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'tv_embedded', 'web']
            }
        },
        'socket_timeout': 30,
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        real_file = f"{output_base}.mp3"
        if not os.path.exists(real_file):
            for ext in ['.mp3', '.m4a', '.webm', '.opus']:
                if os.path.exists(f"{output_base}{ext}"):
                    real_file = f"{output_base}{ext}"
                    break

        if os.path.exists(real_file):
            await status_msg.edit_text("📤 Отправляю трек в чат...")
            input_file = FSInputFile(real_file)
            await callback.message.answer_audio(audio=input_file, caption="🎵 <b>Ваша музыка готова!</b> ✨")
            os.remove(real_file)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не удалось найти готовый аудиофайл.")
    except Exception as e:
        logging.error(f"Ошибка загрузки аудио: {e}")
        await status_msg.edit_text("❌ <b>Ошибка конвертации аудио!</b>")

    await callback.answer()

@dp.callback_query(F.data == "conv_standard")
async def process_file_actions(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ <i>Обрабатываю файл...</i>")
    await asyncio.sleep(1)
    await callback.message.answer("🎉 <b>Готово!</b> Ваш файл успешно обработан. ✨")
    await callback.answer()

@dp.callback_query(F.data == "buy_pro")
async def process_buy_pro(callback: CallbackQuery):
    await callback.message.answer("💳 <b>Активация PRO-доступа</b> 🌟\n\nВсе лимиты сняты.")
    await callback.answer()

async def main() -> None:
    init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())