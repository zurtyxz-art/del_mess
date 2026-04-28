import asyncio
import os
import json
import time
import datetime
from io import BytesIO
from typing import List, Dict, Any, Optional

import telebot
from telebot import types
import sqlite3

BOT_TOKEN = "8605542203:AAFPiTQ-OUSwI1X1d4BDWTjlF6fEvRFoqCY"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

DB_FILE = "bot_database.db"
TEMP_DIR = "temp_media"
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_FILE_SIZE = 300 * 1024 * 1024

ADMIN_IDS = [5254779646]

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        is_banned INTEGER DEFAULT 0,
        ban_date TEXT,
        unban_date TEXT,
        first_seen TEXT,
        last_active TEXT,
        total_messages INTEGER DEFAULT 0,
        total_deleted_tracked INTEGER DEFAULT 0,
        total_edited_tracked INTEGER DEFAULT 0,
        total_media_tracked INTEGER DEFAULT 0,
        total_selfdestruct_tracked INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS tracked_deleted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        message_text TEXT,
        media_path TEXT,
        media_type TEXT,
        deleted_time TEXT,
        is_self_destruct INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS tracked_edited (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        old_text TEXT,
        new_text TEXT,
        edited_time TEXT
    );

    CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER,
        channel_username TEXT,
        channel_title TEXT,
        added_date TEXT
    );

    CREATE TABLE IF NOT EXISTS banned_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        banned_date TEXT,
        unbanned_date TEXT,
        is_active INTEGER DEFAULT 1
    );
""")
conn.commit()

message_cache = {}
pending_self_destruct = {}
admin_states = {}


def get_current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def update_user_activity(user_id, username=None, first_name=None, last_name=None):
    current_time = get_current_time()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone():
        update_fields = ["last_active = ?"]
        params = [current_time]
        if username is not None:
            update_fields.append("username = ?")
            params.append(username)
        if first_name is not None:
            update_fields.append("first_name = ?")
            params.append(first_name)
        if last_name is not None:
            update_fields.append("last_name = ?")
            params.append(last_name)
        params.append(user_id)
        cursor.execute(f"UPDATE users SET {', '.join(update_fields)} WHERE user_id = ?", params)
    else:
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_active) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, last_name, current_time, current_time)
        )
    conn.commit()


def increment_user_stat(user_id, stat_type):
    column_map = {
        "deleted": "total_deleted_tracked",
        "edited": "total_edited_tracked",
        "media": "total_media_tracked",
        "selfdestruct": "total_selfdestruct_tracked"
    }
    col = column_map.get(stat_type)
    if col:
        cursor.execute(f"UPDATE users SET {col} = {col} + 1 WHERE user_id = ?", (user_id,))
    cursor.execute("UPDATE users SET total_messages = total_messages + 1 WHERE user_id = ?", (user_id,))
    conn.commit()


def is_user_banned(user_id):
    cursor.execute(
        "SELECT is_active FROM banned_users WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    return cursor.fetchone() is not None


def ban_user(user_id):
    current_time = get_current_time()
    cursor.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, username, banned_date, is_active) VALUES (?, (SELECT username FROM users WHERE user_id = ?), ?, 1)",
        (user_id, user_id, current_time)
    )
    cursor.execute("UPDATE users SET is_banned = 1, ban_date = ? WHERE user_id = ?", (current_time, user_id))
    conn.commit()


def unban_user(user_id):
    current_time = get_current_time()
    cursor.execute(
        "UPDATE banned_users SET is_active = 0, unbanned_date = ? WHERE user_id = ? AND is_active = 1",
        (current_time, user_id)
    )
    cursor.execute("UPDATE users SET is_banned = 0, unban_date = ? WHERE user_id = ?", (current_time, user_id))
    conn.commit()


def get_banned_list():
    cursor.execute("""
        SELECT b.user_id, u.username, b.banned_date, b.unbanned_date 
        FROM banned_users b 
        LEFT JOIN users u ON b.user_id = u.user_id 
        WHERE b.is_active = 1
    """)
    return cursor.fetchall()


def get_unbanned_list():
    cursor.execute("""
        SELECT b.user_id, u.username, b.banned_date, b.unbanned_date 
        FROM banned_users b 
        LEFT JOIN users u ON b.user_id = u.user_id 
        WHERE b.is_active = 0
    """)
    return cursor.fetchall()


def get_user_info(identifier):
    if identifier.isdigit():
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (int(identifier),))
    else:
        cursor.execute("SELECT * FROM users WHERE username = ?", (identifier.lstrip('@'),))
    return cursor.fetchone()


def get_required_channels():
    cursor.execute("SELECT channel_id, channel_username, channel_title FROM required_channels")
    return cursor.fetchall()


def add_required_channel(channel_id, channel_username, channel_title):
    current_time = get_current_time()
    cursor.execute(
        "INSERT OR IGNORE INTO required_channels (channel_id, channel_username, channel_title, added_date) VALUES (?, ?, ?, ?)",
        (channel_id, channel_username, channel_title, current_time)
    )
    conn.commit()


def remove_required_channel(channel_id):
    cursor.execute("DELETE FROM required_channels WHERE channel_id = ?", (channel_id,))
    conn.commit()


def save_deleted_message(user_id, chat_id, message_id, text, media_path, media_type):
    current_time = get_current_time()
    cursor.execute(
        "INSERT INTO tracked_deleted (user_id, chat_id, message_id, message_text, media_path, media_type, deleted_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, chat_id, message_id, text, media_path, media_type, current_time)
    )
    conn.commit()
    increment_user_stat(user_id, "deleted")
    if media_path:
        increment_user_stat(user_id, "media")


def save_edited_message(user_id, chat_id, message_id, old_text, new_text):
    current_time = get_current_time()
    cursor.execute(
        "INSERT INTO tracked_edited (user_id, chat_id, message_id, old_text, new_text, edited_time) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, chat_id, message_id, old_text, new_text, current_time)
    )
    conn.commit()
    increment_user_stat(user_id, "edited")


def save_self_destruct_message(user_id, chat_id, message_id, text, media_path, media_type):
    current_time = get_current_time()
    cursor.execute(
        "INSERT INTO tracked_deleted (user_id, chat_id, message_id, message_text, media_path, media_type, deleted_time, is_self_destruct) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (user_id, chat_id, message_id, text, media_path, media_type, current_time)
    )
    conn.commit()
    increment_user_stat(user_id, "selfdestruct")
    if media_path:
        increment_user_stat(user_id, "media")


def is_admin(user_id):
    return user_id in ADMIN_IDS


def check_subscription(user_id):
    channels = get_required_channels()
    if not channels:
        return True
    for channel_id, channel_username, channel_title in channels:
        try:
            member = bot.get_chat_member(channel_id, user_id)
            if member.status in ['left', 'kicked', 'restricted']:
                return False
        except Exception:
            return False
    return True


def get_subscription_keyboard():
    channels = get_required_channels()
    keyboard = types.InlineKeyboardMarkup()
    for channel_id, channel_username, channel_title in channels:
        if channel_username:
            keyboard.add(types.InlineKeyboardButton(f"📢 {channel_title}", url=f"https://t.me/{channel_username}"))
    keyboard.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub"))
    return keyboard


def get_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("👤 Проверить пользователя", callback_data="admin_check_user")
    )
    keyboard.add(
        types.InlineKeyboardButton("🚫 Бан пользователя", callback_data="admin_ban_user"),
        types.InlineKeyboardButton("✅ Разбан пользователя", callback_data="admin_unban_user")
    )
    keyboard.add(
        types.InlineKeyboardButton("📋 Список банов", callback_data="admin_banlist"),
        types.InlineKeyboardButton("📋 История разбанов", callback_data="admin_unbanlist")
    )
    keyboard.add(
        types.InlineKeyboardButton("📢 Обязательная подписка", callback_data="admin_channels"),
        types.InlineKeyboardButton("📨 Рассылка", callback_data="admin_broadcast")
    )
    return keyboard


def get_back_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin"))
    return keyboard


@bot.message_handler(commands=['start'])
def start_handler(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.send_message(message.chat.id, "🚫 Вы забанены и не можете пользоваться ботом.")
        return
    update_user_activity(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    channels = get_required_channels()
    if channels:
        bot.send_message(
            message.chat.id,
            "👋 Привет! Для использования бота необходимо подписаться на каналы:",
            reply_markup=get_subscription_keyboard()
        )
    else:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("📖 Как использовать?", callback_data="help_info"))
        bot.send_message(
            message.chat.id,
            "👋 Привет! Я чат-бот, который отслеживает удаленные и измененные сообщения.\n\n"
            "Просто общайся со мной в личных сообщениях, и я буду запоминать все.\n"
            "Если собеседник удалит или изменит сообщение - я сразу тебе покажу!\n\n"
            "📌 Для отслеживания одноразовых медиа - ответь на него любым сообщением.",
            reply_markup=keyboard
        )


@bot.callback_query_handler(func=lambda call: call.data == "help_info")
def help_callback(call):
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📖 <b>Как использовать бота:</b>\n\n"
        "• Все сообщения в этом чате автоматически сохраняются\n"
        "• При удалении сообщения - бот покажет что было удалено\n"
        "• При изменении сообщения - бот покажет старую и новую версии\n"
        "• Для сохранения одноразового медиа - ответьте на него любым сообщением\n\n"
        "⚠️ Ограничение на медиафайлы: до 300 МБ",
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
    user_id = call.from_user.id
    if check_subscription(user_id):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        bot.edit_message_text(
            "✅ Подписка подтверждена! Теперь ты можешь пользоваться ботом.\n\n"
            "Просто общайся со мной в личных сообщениях, и я буду запоминать все.\n"
            "Если собеседник удалит или изменит сообщение - я сразу тебе покажу!\n\n"
            "📌 Для отслеживания одноразовых медиа - ответь на него любым сообщением.",
            call.message.chat.id,
            call.message.message_id
        )
    else:
        bot.answer_callback_query(call.id, "❌ Вы не подписались на все каналы!", show_alert=True)


@bot.message_handler(
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'sticker', 'video_note', 'animation'])
def handle_all_messages(message):
    if message.chat.type != 'private':
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    if is_user_banned(user_id):
        return

    update_user_activity(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)

    required_channels = get_required_channels()
    if required_channels:
        if not check_subscription(user_id):
            bot.send_message(
                chat_id,
                "⚠️ Для использования бота необходима подписка на каналы.",
                reply_markup=get_subscription_keyboard()
            )
            return

    if is_admin(user_id) and message.text:
        handle_admin_commands(message)
        return

    message_cache[message.message_id] = {
        'user_id': user_id,
        'chat_id': chat_id,
        'message': message,
        'time': get_current_time()
    }

    cursor.execute("UPDATE users SET total_messages = total_messages + 1 WHERE user_id = ?", (user_id,))
    conn.commit()


def handle_admin_commands(message):
    user_id = message.from_user.id
    text = message.text

    if user_id in admin_states:
        state = admin_states[user_id]
        if state == 'awaiting_user_check':
            check_user_by_admin(message)
            return
        elif state == 'awaiting_user_ban':
            ban_user_by_admin(message)
            return
        elif state == 'awaiting_user_unban':
            unban_user_by_admin(message)
            return
        elif state == 'awaiting_channel_manage':
            manage_channel(message)
            return
        elif state == 'awaiting_broadcast':
            handle_broadcast(message)
            return


def check_user_by_admin(message):
    identifier = message.text.strip()
    user = get_user_info(identifier)
    del admin_states[message.from_user.id]

    if user:
        info_text = (
            f"👤 <b>Информация о пользователе</b>\n"
            f"⏰ {get_current_time()}\n\n"
            f"🆔 ID: <code>{user[0]}</code>\n"
            f"👤 Username: @{user[1] if user[1] else 'Нет'}\n"
            f"📝 Имя: {user[2] or 'Нет'}\n"
            f"📝 Фамилия: {user[3] or 'Нет'}\n"
            f"🚫 Забанен: {'Да' if user[5] else 'Нет'}\n"
            f"📅 Дата бана: {user[6] or 'Нет'}\n"
            f"📅 Дата разбана: {user[7] or 'Нет'}\n"
            f"📅 Первое появление: {user[8]}\n"
            f"📅 Последняя активность: {user[9]}\n"
            f"💬 Всего сообщений: {user[10]}\n"
            f"🗑 Отслежено удалений: {user[11]}\n"
            f"✏️ Отслежено изменений: {user[12]}\n"
            f"📁 Отслежено медиа: {user[13]}\n"
            f"💥 Одноразовых медиа: {user[14]}"
        )
    else:
        info_text = f"❌ Пользователь не найден в базе\n⏰ {get_current_time()}"

    bot.send_message(message.chat.id, info_text, parse_mode="HTML", reply_markup=get_admin_keyboard())


def ban_user_by_admin(message):
    identifier = message.text.strip()
    user = get_user_info(identifier)
    del admin_states[message.from_user.id]

    if user:
        ban_user(user[0])
        bot.send_message(
            message.chat.id,
            f"✅ Пользователь {identifier} забанен\n⏰ {get_current_time()}",
            reply_markup=get_admin_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Пользователь {identifier} не найден в базе\n⏰ {get_current_time()}",
            reply_markup=get_admin_keyboard()
        )


def unban_user_by_admin(message):
    identifier = message.text.strip()
    user = get_user_info(identifier)
    del admin_states[message.from_user.id]

    if user:
        unban_user(user[0])
        bot.send_message(
            message.chat.id,
            f"✅ Пользователь {identifier} разбанен\n⏰ {get_current_time()}",
            reply_markup=get_admin_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Пользователь {identifier} не найден в базе\n⏰ {get_current_time()}",
            reply_markup=get_admin_keyboard()
        )


def manage_channel(message):
    text = message.text.strip()
    del admin_states[message.from_user.id]

    if text.lower().startswith("удалить "):
        channel_identifier = text[8:].strip().lstrip('@')
        channels = get_required_channels()
        found = False
        for ch in channels:
            if ch[1] == channel_identifier or str(ch[0]) == channel_identifier:
                remove_required_channel(ch[0])
                bot.send_message(
                    message.chat.id,
                    f"✅ Канал {ch[2]} удален из обязательной подписки\n⏰ {get_current_time()}",
                    reply_markup=get_admin_keyboard()
                )
                found = True
                break
        if not found:
            bot.send_message(
                message.chat.id,
                f"❌ Канал не найден\n⏰ {get_current_time()}",
                reply_markup=get_admin_keyboard()
            )
    else:
        try:
            chat = bot.get_chat(text)
            if chat.type == 'channel':
                add_required_channel(chat.id, chat.username, chat.title)
                bot.send_message(
                    message.chat.id,
                    f"✅ Канал {chat.title} добавлен в обязательную подписку\n⏰ {get_current_time()}",
                    reply_markup=get_admin_keyboard()
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "❌ Это не канал\n⏰ {get_current_time()}",
                    reply_markup=get_admin_keyboard()
                )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                f"❌ Ошибка: канал не найден или бот не имеет доступа\n{str(e)}\n⏰ {get_current_time()}",
                reply_markup=get_admin_keyboard()
            )


def handle_broadcast(message):
    content = message.text
    del admin_states[message.from_user.id]

    lines = content.split('\n')
    text_lines = []
    buttons_list = []
    reading_buttons = False

    for line in lines:
        if line.strip() == '---':
            reading_buttons = True
            continue
        if reading_buttons:
            if '|' in line:
                parts = line.split('|', 1)
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                buttons_list.append((btn_text, btn_url))
        else:
            text_lines.append(line)

    broadcast_text = '\n'.join(text_lines)

    keyboard = types.InlineKeyboardMarkup()
    for btn_text, btn_url in buttons_list:
        keyboard.add(types.InlineKeyboardButton(btn_text, url=btn_url))

    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = cursor.fetchall()

    success = 0
    failed = 0

    for user in users:
        try:
            if keyboard.keyboard:
                bot.send_message(user[0], broadcast_text, reply_markup=keyboard)
            else:
                bot.send_message(user[0], broadcast_text)
            success += 1
        except Exception:
            failed += 1

    bot.send_message(
        message.chat.id,
        f"📨 Рассылка завершена\n"
        f"⏰ {get_current_time()}\n"
        f"✅ Успешно: {success}\n"
        f"❌ Не удалось: {failed}",
        reply_markup=get_admin_keyboard()
    )


@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.type != 'private':
        return
    if not is_admin(message.from_user.id):
        return
    bot.send_message(message.chat.id, "🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=get_admin_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return
    bot.edit_message_text(
        "🔧 <b>Админ-панель</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=get_admin_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def admin_stats(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM banned_users WHERE is_active = 1")
    active_bans = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(total_messages) FROM users")
    total_messages = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(total_deleted_tracked) FROM users")
    total_deleted = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(total_edited_tracked) FROM users")
    total_edited = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(total_media_tracked) FROM users")
    total_media = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(total_selfdestruct_tracked) FROM users")
    total_selfdestruct = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COUNT(*) FROM tracked_deleted")
    total_tracked_deleted = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tracked_edited")
    total_tracked_edited = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM required_channels")
    total_channels = cursor.fetchone()[0]

    cursor.execute(
        "SELECT user_id, username, first_name, last_name, total_messages, last_active FROM users ORDER BY total_messages DESC LIMIT 10")
    top_users = cursor.fetchall()

    stats_text = (
        f"📊 <b>Статистика бота</b>\n"
        f"⏰ {get_current_time()}\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🚫 Забанено: {banned_users}\n"
        f"🔒 Активных банов: {active_bans}\n"
        f"💬 Всего сообщений: {total_messages}\n"
        f"🗑 Отслежено удалений: {total_deleted}\n"
        f"✏️ Отслежено изменений: {total_edited}\n"
        f"📁 Отслежено медиа: {total_media}\n"
        f"💥 Одноразовых медиа: {total_selfdestruct}\n"
        f"📋 Записей удаленных: {total_tracked_deleted}\n"
        f"📋 Записей измененных: {total_tracked_edited}\n"
        f"📢 Обязательных каналов: {total_channels}\n\n"
        f"🏆 <b>Топ-10 активных пользователей:</b>\n"
    )

    for i, user in enumerate(top_users, 1):
        name = user[1] or f"{user[2] or ''} {user[3] or ''}".strip() or f"ID:{user[0]}"
        stats_text += f"{i}. {name} - {user[4]} сообщ.\n"
        stats_text += f"   Последняя активность: {user[5]}\n"

    bot.edit_message_text(
        stats_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_check_user")
def admin_check_user(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    admin_states[call.from_user.id] = 'awaiting_user_check'

    bot.edit_message_text(
        "👤 Отправьте ID или @username пользователя для проверки:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_ban_user")
def admin_ban_user(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    admin_states[call.from_user.id] = 'awaiting_user_ban'

    bot.edit_message_text(
        "🚫 Отправьте ID или @username пользователя для бана:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_unban_user")
def admin_unban_user(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    admin_states[call.from_user.id] = 'awaiting_user_unban'

    bot.edit_message_text(
        "✅ Отправьте ID или @username пользователя для разбана:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_banlist")
def admin_banlist(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    banned = get_banned_list()

    if banned:
        text = f"📋 <b>Список забаненных пользователей</b>\n⏰ {get_current_time()}\n\n"
        for i, ban in enumerate(banned, 1):
            text += f"{i}. 🆔 <code>{ban[0]}</code> | @{ban[1] or 'Нет'} | 📅 {ban[2]}\n"
    else:
        text = f"📋 Нет забаненных пользователей\n⏰ {get_current_time()}"

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_unbanlist")
def admin_unbanlist(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    unbanned = get_unbanned_list()

    if unbanned:
        text = f"📋 <b>История разбанов</b>\n⏰ {get_current_time()}\n\n"
        for i, ban in enumerate(unbanned, 1):
            text += f"{i}. 🆔 <code>{ban[0]}</code> | @{ban[1] or 'Нет'} | Бан: {ban[2]} | Разбан: {ban[3]}\n"
    else:
        text = f"📋 Нет разбаненных пользователей\n⏰ {get_current_time()}"

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_channels")
def admin_channels(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    admin_states[call.from_user.id] = 'awaiting_channel_manage'

    channels = get_required_channels()

    text = f"📢 <b>Управление обязательной подпиской</b>\n⏰ {get_current_time()}\n\n"
    if channels:
        text += "Текущие каналы:\n"
        for ch in channels:
            text += f"• {ch[2]} (@{ch[1]})\n"
    else:
        text += "Нет обязательных каналов\n"

    text += "\nОтправьте @username или ID канала для добавления\nИли отправьте 'удалить @username' для удаления"

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=get_back_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    admin_states[call.from_user.id] = 'awaiting_broadcast'

    bot.edit_message_text(
        "📨 Отправьте сообщение для рассылки.\n"
        "Формат:\n"
        "Текст сообщения\n"
        "---\n"
        "Кнопка1 | https://link1.com\n"
        "Кнопка2 | https://link2.com\n\n"
        "Можно без кнопок, просто текст",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_back_keyboard()
    )


@bot.edited_message_handler(func=lambda message: message.chat.type == 'private')
def handle_edited_message(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    if is_user_banned(user_id):
        return

    old_message_data = message_cache.get(message.message_id, {})
    old_text = ""

    if old_message_data and 'message' in old_message_data:
        old_msg = old_message_data['message']
        old_text = old_msg.text or old_msg.caption or ""

    new_text = message.text or message.caption or ""

    save_edited_message(user_id, chat_id, message.message_id, old_text, new_text)

    sender_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    if message.from_user.username:
        sender_name += f" (@{message.from_user.username})"

    notify_text = (
        f"✏️ <b>Сообщение изменено</b> от {sender_name}\n"
        f"⏰ Время: {get_current_time()}\n\n"
    )

    if old_text:
        notify_text += f"📝 <b>Старое сообщение:</b>\n{old_text}\n\n"

    notify_text += f"📝 <b>Новое сообщение:</b>\n{new_text}"

    keyboard = types.InlineKeyboardMarkup()

    bot.send_message(chat_id, notify_text, parse_mode="HTML", reply_markup=keyboard)


print("Бот запущен...")
bot.infinity_polling()