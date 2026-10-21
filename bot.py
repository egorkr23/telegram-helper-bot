import asyncio
import logging
import sys
import os
import sqlite3
import re
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import quote
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from html import escape
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, 
    InlineKeyboardButton, CallbackQuery, FSInputFile, BotCommand, LabeledPrice, PreCheckoutQuery
)
import yt_dlp

# Импорт библиотек конвертации
try:
    from pdf2docx import Converter
except ImportError:
    Converter = None

try:
    from docx2pdf import convert as convert_docx_to_pdf
except ImportError:
    convert_docx_to_pdf = None

try:
    import pdfplumber
    from docx import Document
except ImportError:
    pdfplumber = None
    Document = None

try:
    import openpyxl
    import pandas as pd
except ImportError:
    openpyxl = None
    pd = None

try:
    from PIL import Image
except ImportError:
    Image = None

TOKEN = "8684428526:AAGaUesYUe4VgIdjLQJ25_B7ICYJ5rKjsos"
ADMIN_ID = 1745630699  # Ваш ID администратора (Вечный PRO / Безлимит)
FREE_LIMIT_PER_DAY = 5  # 5 бесплатных запросов в сутки
PRO_PRICE_STARS = 100   # Стоимость PRO-подписки на 30 дней в Telegram Stars (~100 грн)

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s - %(levelname)s - %(message)s")

# === БАЗА ДАННЫХ С АВТОМАТИЧЕСКОЙ МИГРАЦИЕЙ ВСЕХ КОЛОНОК ===
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
            current_mode TEXT DEFAULT 'idle',
            invited_count INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL
        )
    """)
    
    columns_to_add = [
        ("username", "TEXT"),
        ("is_pro", "INTEGER DEFAULT 0"),
        ("pro_expire_date", "TEXT"),
        ("daily_usage", "INTEGER DEFAULT 0"),
        ("last_usage_date", "TEXT"),
        ("current_mode", "TEXT DEFAULT 'idle'"),
        ("invited_count", "INTEGER DEFAULT 0"),
        ("referred_by", "INTEGER DEFAULT NULL")
    ]
    
    for col_name, col_type in columns_to_add:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass

    conn.commit()
    conn.close()

def check_user_limit(user_id: int, username: str) -> tuple[bool, int, bool]:
    init_db()
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    today_str = str(date.today())
    
    cursor.execute("SELECT is_pro, pro_expire_date, daily_usage, last_usage_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, daily_usage, last_usage_date, current_mode, invited_count) VALUES (?, ?, 0, ?, 'idle', 0)", 
                       (user_id, username, today_str))
        conn.commit()
        is_pro, daily_usage = False, 0
    else:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
        is_pro_db = row[0]
        pro_expire = row[1]
        daily_usage = row[2]
        last_date = row[3]
        
        is_pro = False
        if is_pro_db == 1:
            if pro_expire:
                if datetime.now().date() <= datetime.strptime(pro_expire, "%Y-%m-%d").date():
                    is_pro = True
                else:
                    cursor.execute("UPDATE users SET is_pro = 0, pro_expire_date = NULL WHERE user_id = ?", (user_id,))
                    conn.commit()
            else:
                is_pro = True

        if last_date != today_str:
            daily_usage = 0
            cursor.execute("UPDATE users SET daily_usage = 0, last_usage_date = ? WHERE user_id = ?", (today_str, user_id))
            conn.commit()

    if user_id == ADMIN_ID:
        conn.close()
        return True, 0, True

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
user_modes = {}
prompt_messages = {}
active_tasks = set()

def clean_media_url(raw_url: str) -> str:
    if "youtube.com" in raw_url or "youtu.be" in raw_url:
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", raw_url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"
    return raw_url

# === ГЛАВНОЕ МЕНЮ ===
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Скачать медиа (YT/TT/Insta)"), KeyboardButton(text="📁 Конвертер файлов")],
        [KeyboardButton(text="📖 Инструкция / FAQ"), KeyboardButton(text="🌟 Мой профиль / PRO")],
        [KeyboardButton(text="🎁 Бонус / Поделиться ботом"), KeyboardButton(text="⬅️ Главное меню / Назад")]
    ],
    resize_keyboard=True
)

dp = Dispatcher()

async def clear_prompt(bot: Bot, chat_id: int):
    msg_id = prompt_messages.pop(chat_id, None)
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

# === АДМИН-КОМАНДЫ (СТРОГО ПЕРЕД ОБЩИМИ ТЕКСТОВЫМИ ХЕНДЛЕРАМИ) ===

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    init_db()
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_pro = 1")
    pro_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(invited_count) FROM users")
    total_referrals = cursor.fetchone()[0] or 0
    
    conn.close()
    free_users = total_users - pro_users
    
    stats_text = (
        f"📊 <b>АДМИН-СТАТИСТИКА БОТА</b> 🚀\n\n"
        f"👥 <b>Всего пользователей:</b> <code>{total_users}</code>\n"
        f"👑 <b>PRO-аккаунтов:</b> <code>{pro_users}</code>\n"
        f"⏳ <b>Бесплатный тариф:</b> <code>{free_users}</code>\n"
        f"🎁 <b>Привлечено по рефералке:</b> <code>{total_referrals}</code>\n\n"
        f"💡 <i>Напишите /users чтобы увидеть список всех пользователей и их ID!</i>"
    )
    await message.answer(stats_text)

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    init_db()
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT user_id, username, is_pro, pro_expire_date FROM users")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await message.answer("👥 В базе данных пока нет пользователей.")
        return

    text = "👥 <b>СПИСОК ПОЛЬЗОВАТЕЛЕЙ БОТА:</b>\n\n"
    for idx, (u_id, u_name, is_pro, pro_exp) in enumerate(rows, 1):
        uname_str = f"@{u_name}" if u_name and u_name != "Без юзернейма" else "<i>без юзернейма</i>"
        status = f"<b>PRO</b> (до {pro_exp})" if is_pro and pro_exp else ("<b>PRO (Админ)</b>" if u_id == ADMIN_ID else "<b>Free</b>")
        text += f"{idx}. {uname_str} | ID: <code>{u_id}</code> | {status}\n"

    await message.answer(text)

@dp.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    args = command.args
    if not args:
        await message.answer(
            "⚠️ <b>Неверный формат!</b>\n"
            "Используйте команду так:\n"
            "<code>/grant [ID_пользователя] [количество_дней]</code>\n"
            "Пример: <code>/grant 1745630699 30</code>"
        )
        return

    try:
        parts = args.strip().split()
        target_user_id = int(parts[0])
        days = int(parts[1])
    except (IndexError, ValueError):
        await message.answer("❌ Ошибка в аргументах. ID и дни должны быть числами. Пример: <code>/grant 123456789 30</code>")
        return

    init_db()
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT pro_expire_date FROM users WHERE user_id = ?", (target_user_id,))
    row = cursor.fetchone()
    
    base_date = datetime.now()
    if row and row[0]:
        try:
            exp_dt = datetime.strptime(row[0], "%Y-%m-%d")
            if exp_dt > base_date:
                base_date = exp_dt
        except Exception:
            pass
            
    new_expire_date = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
    
    if row:
        cursor.execute("UPDATE users SET is_pro = 1, pro_expire_date = ? WHERE user_id = ?", (new_expire_date, target_user_id))
    else:
        cursor.execute("INSERT INTO users (user_id, is_pro, pro_expire_date) VALUES (?, 1, ?)", (target_user_id, new_expire_date))
    
    conn.commit()
    conn.close()

    await message.answer(
        f"✅ <b>Успешно!</b>\n"
        f"👑 Пользователю <code>{target_user_id}</code> выдан PRO-доступ на <b>{days} дней</b>.\n"
        f"📅 Новый срок действия: <b>{new_expire_date}</b>"
    )

    # Автоматическое уведомление пользователя с красивым текстом-сопровождением
    try:
        await bot.send_message(
            target_user_id,
            f"🎉 <b>Ух ты, отличные новости!</b> 🌟\n\n"
            f"🎁 Вам только что открыли эксклюзивный <b>PRO-доступ на {days} дней</b>!\n"
            f"⏳ Все суточные лимиты на скачивание и конвертацию файлов сняты.\n"
            f"📅 Подписка активна до: <b>{new_expire_date}</b>\n\n"
            f"🚀 Пользуйтесь всеми возможностями бота на максимум!",
            reply_markup=main_menu_keyboard
        )
    except Exception:
        pass

# === ОБЫЧНЫЕ КОМАНДЫ И МЕНЮ ===

@dp.message(CommandStart())
async def command_start_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    init_db()
    await clear_prompt(bot, message.chat.id)
    user_id = message.from_user.id
    username = message.from_user.username or "Без юзернейма"
    
    args = command.args
    if args and args.startswith("ref_"):
        try:
            referrer_id = int(args.split("_")[1])
            if referrer_id != user_id:
                conn = sqlite3.connect("bot_database.db")
                cursor = conn.cursor()
                cursor.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                
                if not row or row[0] is None:
                    cursor.execute("INSERT OR REPLACE INTO users (user_id, username, daily_usage, last_usage_date, current_mode, referred_by) VALUES (?, ?, 0, ?, 'idle', ?)", 
                                   (user_id, username, str(date.today()), referrer_id))
                    cursor.execute("UPDATE users SET invited_count = COALESCE(invited_count, 0) + 1 WHERE user_id = ?", (referrer_id,))
                    conn.commit()
                    
                    cursor.execute("SELECT invited_count, pro_expire_date, is_pro FROM users WHERE user_id = ?", (referrer_id,))
                    ref_data = cursor.fetchone()
                    if ref_data:
                        invited_count = ref_data[0]
                        current_expire = ref_data[1]
                        
                        if invited_count > 0 and invited_count % 5 == 0:
                            base_date = datetime.now()
                            if current_expire:
                                exp_dt = datetime.strptime(current_expire, "%Y-%m-%d")
                                if exp_dt > base_date:
                                    base_date = exp_dt
                            
                            new_expire_date = (base_date + timedelta(days=30)).strftime("%Y-%m-%d")
                            cursor.execute("UPDATE users SET is_pro = 1, pro_expire_date = ? WHERE user_id = ?", (new_expire_date, referrer_id))
                            conn.commit()
                            
                            try:
                                await bot.send_message(
                                    referrer_id,
                                    f"🎉 <b>Потрясающие новости!</b> Вы пригласили уже <b>{invited_count}-го друга</b>!\n"
                                    f"👑 Вам автоматически добавлено <b>+30 дней PRO-подписки</b> (активна до {new_expire_date})! ⚡"
                                )
                            except Exception:
                                pass
                conn.close()
        except Exception as e:
            logging.error(f"Ошибка реферальной системы: {e}")

    _, left_count, is_pro = check_user_limit(user_id, username)
    
    safe_name = escape(message.from_user.full_name)
    text = (
        f"✨ <b>Добро пожаловать, {safe_name}!</b> 🚀\n\n"
        f"🔥 Ваш универсальный цифровой помощник готов к работе.\n\n"
        f"🎁 <b>Акция:</b> Приглашайте друзей по кнопке ниже и получайте <b>+30 дней PRO-доступа бесплатно</b> за каждые 5 приглашений! ⚡\n\n"
        f"🎯 Выберите нужный инструмент в меню 👇"
    )
    if is_pro:
        text += "\n\n👑 <b>Ваш статус:</b> <code>DEVELOPER / PRO</code> (Безлимит ⚡)"
    else:
        text += f"\n\n📊 <b>Ваш статус:</b> <code>Бесплатный тариф</code> (Осталось запросов: <b>{left_count}/{FREE_LIMIT_PER_DAY}</b> ⏳)"
        
    await message.answer(text, reply_markup=main_menu_keyboard)

@dp.message(Command("faq"))
async def cmd_faq(message: Message, bot: Bot):
    await menu_faq(message, bot)

@dp.message(Command("profile"))
async def cmd_profile(message: Message, bot: Bot):
    await menu_profile(message, bot)

@dp.message(Command("bonus"))
async def cmd_bonus(message: Message, bot: Bot):
    await menu_share_bot(message, bot)

@dp.message(F.text == "⬅️ Главное меню / Назад")
async def menu_back(message: Message, bot: Bot):
    await clear_prompt(bot, message.chat.id)
    user_modes.pop(message.from_user.id, None)
    await message.answer("🏠 <b>Главное меню</b>\nВыберите категорию:", reply_markup=main_menu_keyboard)

@dp.message(F.text == "📖 Инструкция / FAQ")
async def menu_faq(message: Message, bot: Bot):
    await clear_prompt(bot, message.chat.id)
    faq_text = (
        "📖 <b>Справочник и инструкция по боту</b> 💡\n\n"
        "<b>1. 📥 Скачивание медиа (YouTube, TikTok, Instagram, Pinterest)</b>\n"
        "• Нажмите кнопку <i>«📥 Скачать медиа»</i>.\n"
        "• Отправьте ссылку на видео или трек в чат.\n"
        "• Выберите формат: видео (MP4) или аудио (MP3).\n\n"
        "<b>2. 📁 Конвертер файлов</b>\n"
        "• Нажмите кнопку <i>«📁 Конвертер файлов»</i>.\n"
        "• Выберите нужную задачу из меню (например, Word ➡️ PDF или PDF ➡️ Word с таблицами).\n"
        "• Отправьте файл в чат, и бот мгновенно пришлет готовый результат!\n\n"
        "<b>3. 🌟 Реферальная программа и PRO</b>\n"
        "• Приглашайте друзей по личной ссылке из раздела <i>«🎁 Бонус / Поделиться ботом»</i>.\n"
        "• За каждые <b>5 приглашенных друзей</b> вы получаете <b>+30 дней PRO-доступа бесплатно</b> (награда суммируется)!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать пользоваться", callback_data="go_back")]
    ])
    await message.answer(faq_text, reply_markup=kb)

@dp.message(F.text == "🎁 Бонус / Поделиться ботом")
async def menu_share_bot(message: Message, bot: Bot):
    init_db()
    await clear_prompt(bot, message.chat.id)
    bot_info = await bot.get_me()
    user_id = message.from_user.id
    
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    raw_text = (
        "🚀 Пользуюсь отличным цифровым комбайном!\n\n"
        "🎬 Скачивание видео и музыки без водяных знаков из TikTok, YouTube и Instagram.\n"
        "📁 Профессиональный конвертер файлов (Word, PDF, Excel и др.).\n\n"
        f"Присоединяйся по моей ссылке: {ref_link}"
    )
    
    encoded_text = quote(raw_text)
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={encoded_text}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Поделиться с друзьями", url=share_url)]
    ])
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT invited_count FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    invited_count = row[0] if row and row[0] is not None else 0
    left_to_next = 5 - (invited_count % 5)
    
    await message.answer(
        f"🎁 <b>Реферальный бонус и PRO</b> 🌟\n\n"
        f"👥 Приглашено друзей: <b>{invited_count}</b>\n"
        f"🎯 До следующих +30 дней PRO осталось пригласить: <b>{left_to_next}</b> (награда выдается за каждого 5-го друга!)\n\n"
        f"📎 Ваша личная ссылка:\n<code>{ref_link}</code>\n\n"
        "Нажмите кнопку ниже, чтобы выбрать кому из друзей отправить ссылку! 👇",
        reply_markup=kb
    )

@dp.message(F.text == "📥 Скачать медиа (YT/TT/Insta)")
async def menu_download(message: Message, bot: Bot):
    await clear_prompt(bot, message.chat.id)
    prompt_msg = await message.answer("👇 <b>Вставьте ссылку на видео или музыку из YouTube, TikTok, Instagram или Pinterest прямо сюда!</b>")
    prompt_messages[message.chat.id] = prompt_msg.message_id

@dp.message(F.text == "📁 Конвертер файлов")
async def menu_convert(message: Message, bot: Bot):
    await clear_prompt(bot, message.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Word ➡️ PDF", callback_data="conv_docx_pdf"),
         InlineKeyboardButton(text="📑 PDF ➡️ Word (с таблицами)", callback_data="conv_pdf_docx")],
        [InlineKeyboardButton(text="📊 Excel ➡️ PDF", callback_data="conv_xlsx_pdf"),
         InlineKeyboardButton(text="📉 PDF ➡️ Excel (таблицы)", callback_data="conv_pdf_xlsx")],
        [InlineKeyboardButton(text="📽️ PowerPoint ➡️ PDF", callback_data="conv_pptx_pdf"),
         InlineKeyboardButton(text="🖼️ Сжатие фото / JPG", callback_data="conv_compress_img")],
        [InlineKeyboardButton(text="🔄 Картинка ➡️ PDF", callback_data="conv_img_pdf"),
         InlineKeyboardButton(text="📦 Архиватор ZIP", callback_data="conv_zip")]
    ])
    await message.answer(
        "📁 <b>Профессиональный конвертер файлов</b> ⚙️\n\n"
        "🎯 Выберите нужную задачу из списка ниже:",
        reply_markup=kb
    )

@dp.message(F.text == "🌟 Мой профиль / PRO")
async def menu_profile(message: Message, bot: Bot):
    init_db()
    await clear_prompt(bot, message.chat.id)
    user_id = message.from_user.id
    _, left_count, is_pro = check_user_limit(user_id, message.from_user.username or "Без юзернейма")
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT invited_count, pro_expire_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    invited_count = row[0] if row and row[0] is not None else 0
    expire_date = row[1] if row else None
    
    status_text = "👑 АДМИН / PRO (Безлимит ⚡)" if is_pro else f"Бесплатный (Осталось лимита: {left_count}/{FREE_LIMIT_PER_DAY} ⏳)"
    if expire_date and is_pro and user_id != ADMIN_ID:
        status_text += f" (Активен до {expire_date})"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить PRO ({PRO_PRICE_STARS} Звёзд / 30 дней) ✨", callback_data="buy_pro")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="go_back")]
    ])
    
    await message.answer(
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"⭐ План: <b>{status_text}</b>\n"
        f"👥 Приглашено друзей: <b>{invited_count}</b>",
        reply_markup=kb
    )

@dp.callback_query(F.data == "buy_pro")
async def process_buy_pro(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    
    prices = [LabeledPrice(label="PRO-доступ на 30 дней", amount=PRO_PRICE_STARS)]
    
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="👑 PRO-подписка на 30 дней",
        description="Снимает все суточные лимиты, увеличивает максимальный размер загружаемых файлов до 120 МБ и открывает максимальную скорость работы!",
        payload=f"pro_30_days_{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    user_id = message.from_user.id
    init_db()
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT pro_expire_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    current_expire = row[0] if row else None
    base_date = datetime.now()
    if current_expire:
        try:
            exp_dt = datetime.strptime(current_expire, "%Y-%m-%d")
            if exp_dt > base_date:
                base_date = exp_dt
        except Exception:
            pass
            
    new_expire_date = (base_date + timedelta(days=30)).strftime("%Y-%m-%d")
    cursor.execute("UPDATE users SET is_pro = 1, pro_expire_date = ? WHERE user_id = ?", (new_expire_date, user_id))
    conn.commit()
    conn.close()
    
    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b> ✨\n\n"
        f"👑 Вам активирован <b>PRO-доступ на 30 дней</b>!\n"
        f"📅 Подписка активна до: <b>{new_expire_date}</b>\n\n"
        f"Спасибо за поддержку проекта! Все ограничения сняты 🚀",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text.regexp(r"https?://"))
async def handle_url(message: Message, bot: Bot) -> None:
    await clear_prompt(bot, message.chat.id)
    user_id = message.from_user.id

    if user_id in active_tasks:
        await message.answer("⏳ <b>Подождите!</b> Ваш предыдущий запрос еще обрабатывается.")
        return
    
    can_use, _, is_pro = check_user_limit(user_id, message.from_user.username or "Без юзернейма")
    
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"⭐ Купить PRO ({PRO_PRICE_STARS} Звёзд)", callback_data="buy_pro")]])
        await message.answer("⚠️ <b>Лимит исчерпан!</b> Оформите подписку за Звёзды или пригласите друзей в разделе «🎁 Бонус / Поделиться ботом».", reply_markup=kb)
        return

    if not is_pro:
        increment_usage(user_id)

    cleaned_url = clean_media_url(message.text)
    user_links[user_id] = cleaned_url
    
    await message.answer(
        "✅ <b>Ссылка принята!</b>\n🎯 Выберите формат загрузки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Скачать ВИДЕО (MP4)", callback_data="download_type_video")],
            [InlineKeyboardButton(text="🎵 Скачать МУЗЫКУ (MP3)", callback_data="download_type_audio")]
        ])
    )

@dp.callback_query(F.data.startswith("conv_"))
async def set_conversion_mode(callback: CallbackQuery, bot: Bot):
    await clear_prompt(bot, callback.message.chat.id)
    mode = callback.data
    user_id = callback.from_user.id
    user_modes[user_id] = mode
    
    names = {
        "conv_docx_pdf": "📄 Word ➡️ PDF",
        "conv_pdf_docx": "📑 PDF ➡️ Word (с таблицами)",
        "conv_xlsx_pdf": "📊 Excel ➡️ PDF",
        "conv_pdf_xlsx": "📉 PDF ➡️ Excel (таблицы)",
        "conv_pptx_pdf": "📽️ PowerPoint ➡️ PDF",
        "conv_compress_img": "🖼️ Сжатие фото",
        "conv_img_pdf": "🔄 Картинка ➡️ PDF",
        "conv_zip": "📦 Архиватор ZIP"
    }
    
    prompt_msg = await callback.message.answer(
        f"✅ Режим выбран: <b>{names.get(mode, 'Конвертация')}</b>\n\n"
        f"👇 <b>Добавьте и отправьте файл в чат прямо сейчас!</b>"
    )
    prompt_messages[callback.message.chat.id] = prompt_msg.message_id
    await callback.answer()

@dp.message(F.document | F.photo)
async def handle_file(message: Message, bot: Bot) -> None:
    await clear_prompt(bot, message.chat.id)
    user_id = message.from_user.id

    if user_id in active_tasks:
        await message.answer("⏳ <b>Подождите!</b> Предыдущий файл еще в обработке.")
        return

    mode = user_modes.get(user_id)
    
    if not mode:
        await message.answer("⚠️ Сначала выберите нужный инструмент в разделе <b>📁 Конвертер файлов</b>!", reply_markup=main_menu_keyboard)
        return

    can_use, _, is_pro = check_user_limit(user_id, message.from_user.username or "Без юзернейма")
    if not can_use:
        await message.answer("⚠️ <b>Суточный лимит исчерпан!</b> Оформите PRO.")
        return

    free_limits = {
        "conv_docx_pdf": 30 * 1024 * 1024,
        "conv_pdf_docx": 40 * 1024 * 1024,
        "conv_xlsx_pdf": 40 * 1024 * 1024,
        "conv_pdf_xlsx": 40 * 1024 * 1024,
        "conv_pptx_pdf": 30 * 1024 * 1024,
        "conv_compress_img": 30 * 1024 * 1024,
        "conv_img_pdf": 30 * 1024 * 1024,
        "conv_zip": 40 * 1024 * 1024
    }
    pro_limits = {k: 120 * 1024 * 1024 for k in free_limits}

    file_size = message.document.file_size if message.document else 1024 * 1024
    max_limit = pro_limits.get(mode, 120 * 1024 * 1024) if is_pro else free_limits.get(mode, 40 * 1024 * 1024)

    if file_size > max_limit:
        limit_mb = max_limit // (1024 * 1024)
        await message.answer(f"❌ <b>Файл слишком большой!</b> Лимит для вашего тарифа: <code>{limit_mb} МБ</code>.")
        return

    if not is_pro:
        increment_usage(user_id)

    active_tasks.add(user_id)
    status_msg = await message.answer("⚙️ <i>Выполняю точную конвертацию с сохранением структуры и таблиц...</i> 🔄")
    
    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    original_name = message.document.file_name if message.document else "photo.jpg"
    
    input_path = f"input_{user_id}_{original_name}"
    file = await bot.get_file(file_id)
    await bot.download(file, destination=input_path)

    output_path = None

    try:
        if mode == "conv_pdf_docx" and pdfplumber and Document:
            output_path = f"converted_{user_id}_" + os.path.splitext(original_name)[0] + ".docx"
            doc = Document()
            
            with pdfplumber.open(input_path) as pdf:
                for page_idx, page in enumerate(pdf.pages):
                    if page_idx > 0:
                        doc.add_page_break()
                    
                    tables = page.extract_tables({
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                        "snap_tolerance": 3,
                        "join_tolerance": 3
                    })
                    
                    if tables:
                        for table in tables:
                            if table:
                                rows = len(table)
                                cols = max(len(row) for row in table if row)
                                if rows > 0 and cols > 0:
                                    t = doc.add_table(rows=rows, cols=cols)
                                    t.style = 'Table Grid'
                                    for r_idx, row in enumerate(table):
                                        for c_idx, cell_value in enumerate(row):
                                            if c_idx < cols:
                                                t.cell(r_idx, c_idx).text = str(cell_value or '').strip()
                                doc.add_paragraph()
                    
                    text_lines = page.extract_text(layout=False)
                    if text_lines:
                        for line in text_lines.split('\n'):
                            clean_line = line.strip()
                            if clean_line:
                                doc.add_paragraph(clean_line)
                                
            doc.save(output_path)

        elif mode in ["conv_docx_pdf", "conv_pptx_pdf"]:
            output_path = f"converted_{user_id}_" + os.path.splitext(original_name)[0] + ".pdf"
            if convert_docx_to_pdf:
                convert_docx_to_pdf(input_path, output_path)
            else:
                os.rename(input_path, output_path)

        elif mode == "conv_xlsx_pdf" and pd:
            output_path = f"converted_{user_id}_" + os.path.splitext(original_name)[0] + ".pdf"
            excel_data = pd.read_excel(input_path, sheet_name=0)
            doc = Document()
            doc.add_heading(f"Таблица: {original_name}", level=1)
            
            rows, cols = excel_data.shape
            t = doc.add_table(rows=rows + 1, cols=cols)
            t.style = 'Table Grid'
            
            for c_idx, col_name in enumerate(excel_data.columns):
                t.cell(0, c_idx).text = str(col_name)
            
            for r_idx, row in excel_data.iterrows():
                for c_idx, val in enumerate(row):
                    t.cell(r_idx + 1, c_idx).text = str(val if pd.notna(val) else '')
            
            docx_temp = f"temp_{user_id}.docx"
            doc.save(docx_temp)
            if convert_docx_to_pdf:
                convert_docx_to_pdf(docx_temp, output_path)
                os.remove(docx_temp)
            else:
                output_path = docx_temp

        elif mode == "conv_pdf_xlsx" and pdfplumber and pd:
            output_path = f"converted_{user_id}_" + os.path.splitext(original_name)[0] + ".xlsx"
            all_tables = []
            with pdfplumber.open(input_path) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        if table:
                            all_tables.extend(table)
            
            if all_tables:
                df = pd.DataFrame(all_tables[1:], columns=all_tables[0] if len(all_tables) > 0 else None)
                df.to_excel(output_path, index=False)
            else:
                df = pd.DataFrame(["Таблицы не обнаружены в автоматическом режиме"])
                df.to_excel(output_path, index=False)

        elif mode == "conv_compress_img" and Image:
            output_path = f"compressed_{user_id}_{original_name}"
            img = Image.open(input_path)
            img.save(output_path, "JPEG", quality=75)

        elif mode == "conv_img_pdf" and Image:
            output_path = f"converted_{user_id}_" + os.path.splitext(original_name)[0] + ".pdf"
            img = Image.open(input_path)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(output_path, "PDF", resolution=100.0)

        elif mode == "conv_zip":
            output_path = f"archive_{user_id}.zip"
            with zipfile.ZipFile(output_path, 'w') as zipf:
                zipf.write(input_path, arcname=original_name)

        else:
            output_path = f"processed_{original_name}"
            os.rename(input_path, output_path)

        if output_path and os.path.exists(output_path):
            input_file = FSInputFile(output_path)
            await message.answer_document(
                document=input_file,
                caption=f"🎉 <b>Конвертация завершена успешно!</b> ✨\n📁 Файл: <code>{escape(original_name)}</code>"
            )
            os.remove(output_path)

    except Exception as e:
        logging.error(f"Ошибка при обработке файла: {e}")
        await message.answer("❌ <b>Произошла ошибка при конвертации.</b> Убедитесь, что структура файла корректна.")

    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        await status_msg.delete()
        user_modes.pop(user_id, None)
        active_tasks.discard(user_id)

@dp.callback_query(F.data == "go_back")
async def process_go_back(callback: CallbackQuery, bot: Bot):
    await clear_prompt(bot, callback.message.chat.id)
    await callback.message.edit_text("↩️ <b>Действие отменено.</b>")
    await callback.answer()

@dp.callback_query(F.data == "download_type_video")
async def process_video_download(callback: CallbackQuery):
    await callback.answer("⏳ Загрузка...")
    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.message.answer("❌ Ссылка устарела. Отправьте её заново.")
        return

    if user_id in active_tasks:
        await callback.message.answer("⏳ Предыдущая загрузка еще выполняется.")
        return

    active_tasks.add(user_id)
    status_msg = await callback.message.answer("🎬 <b>Скачиваю медиа...</b> Пожалуйста, подождите 🔄")
    
    ydl_opts = {
        'format': 'best[height<=720]/best',
        'outtmpl': '%(title)s.%(ext)s',
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'quiet': True
    }

    downloaded_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        if downloaded_file and os.path.exists(downloaded_file):
            await status_msg.edit_text("📤 Отправляю файл в чат...")
            input_file = FSInputFile(downloaded_file)
            await callback.message.answer_video(video=input_file, caption="📥 <b>Ваше видео успешно скачано!</b> 🚀")
            os.remove(downloaded_file)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не удалось найти готовый файл.")
    except Exception as e:
        logging.error(f"Ошибка загрузки видео: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Ошибка загрузки:</b> <code>{escape(str(e))}</code>")
    finally:
        active_tasks.discard(user_id)

@dp.callback_query(F.data == "download_type_audio")
async def process_audio_download(callback: CallbackQuery):
    await callback.answer("⏳ Загрузка аудио...")
    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.message.answer("❌ Ссылка устарела. Отправьте её заново.")
        return

    if user_id in active_tasks:
        await callback.message.answer("⏳ Предыдущая загрузка еще выполняется.")
        return

    active_tasks.add(user_id)
    status_msg = await callback.message.answer("🎵 <b>Загружаю аудиодорожку...</b> Пожалуйста, подождите 🔄")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(title)s.%(ext)s',
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'quiet': True
    }

    downloaded_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        if downloaded_file and os.path.exists(downloaded_file):
            await status_msg.edit_text("📤 Отправляю трек в чат...")
            input_file = FSInputFile(downloaded_file)
            await callback.message.answer_audio(audio=input_file, caption="🎵 <b>Ваша музыка готова!</b> ✨")
            os.remove(downloaded_file)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не удалось найти аудиофайл.")
    except Exception as e:
        logging.error(f"Ошибка аудио: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Ошибка загрузки аудио:</b> <code>{escape(str(e))}</code>")
    finally:
        active_tasks.discard(user_id)

async def main() -> None:
    init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    commands = [
        BotCommand(command="start", description="Запустить бота и главное меню"),
        BotCommand(command="faq", description="Инструкция и справка"),
        BotCommand(command="profile", description="Мой профиль и лимиты"),
        BotCommand(command="bonus", description="Реферальный бонус и PRO")
    ]
    await bot.set_my_commands(commands)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
