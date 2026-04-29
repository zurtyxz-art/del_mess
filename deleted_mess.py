import asyncio
import logging
import sqlite3
import os
import shutil
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

API_TOKEN = "8605542203:AAFPiTQ-OUSwI1X1d4BDWTjlF6fEvRFoqCY"
ADMIN_IDS = [5254779646]

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

logging.basicConfig(level=logging.INFO)

conn = sqlite3.connect('bot_database.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    join_date TEXT,
    is_banned INTEGER DEFAULT 0,
    ban_reason TEXT,
    ban_date TEXT,
    unban_date TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS forced_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    chat_url TEXT,
    added_date TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS deleted_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    target_user_id INTEGER,
    original_text TEXT,
    new_text TEXT,
    file_id TEXT,
    file_type TEXT,
    file_size INTEGER,
    delete_date TEXT,
    edit_date TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS temp_media (
    user_id INTEGER,
    target_user_id INTEGER,
    message_id INTEGER,
    file_id TEXT,
    file_type TEXT,
    file_size INTEGER,
    send_date TEXT
)
''')

conn.commit()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_buttons = State()


def get_user_info(user_id: int) -> Dict:
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    if user:
        return {
            'user_id': user[0],
            'username': user[1],
            'first_name': user[2],
            'last_name': user[3],
            'join_date': user[4],
            'is_banned': user[5],
            'ban_reason': user[6],
            'ban_date': user[7],
            'unban_date': user[8]
        }
    return {}


def add_user(user_id: int, username: str, first_name: str, last_name: str):
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if not cursor.fetchone():
        cursor.execute('''
        INSERT INTO users (user_id, username, first_name, last_name, join_date)
        VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()


def is_user_banned(user_id: int) -> bool:
    cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    return result is not None and result[0] == 1


def check_forced_subs(user_id: int) -> bool:
    cursor.execute('SELECT chat_id, chat_url FROM forced_subs')
    subs = cursor.fetchall()
    return len(subs) == 0


async def send_forced_subs_message(message: Message):
    cursor.execute('SELECT chat_id, chat_url FROM forced_subs')
    subs = cursor.fetchall()
    if subs:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Подписаться {i + 1}", url=sub[1])] for i, sub in enumerate(subs)
        ])
        await message.answer("⚠️ Для использования бота подпишитесь на обязательные каналы/чаты:",
                             reply_markup=keyboard)


async def check_user_access(user_id: int, message: Message) -> bool:
    if is_user_banned(user_id):
        await message.answer("❌ Вы забанены и не можете использовать бота.")
        return False

    cursor.execute('SELECT chat_id FROM forced_subs')
    subs = cursor.fetchall()
    for sub in subs:
        try:
            member = await bot.get_chat_member(chat_id=sub[0], user_id=user_id)
            if member.status in ['left', 'kicked']:
                await send_forced_subs_message(message)
                return False
        except:
            await send_forced_subs_message(message)
            return False

    return True


@dp.message(Command("start"))
async def start_command(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.first_name,
             message.from_user.last_name)

    if not await check_user_access(message.from_user.id, message):
        return

    await message.answer("✅ Бот активирован! Я отслеживаю удаленные и измененные сообщения в личных чатах.")


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещен.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика пользователей", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔍 Проверка пользователя", callback_data="admin_check_user")],
        [InlineKeyboardButton(text="🚫 Бан пользователя", callback_data="admin_ban_user")],
        [InlineKeyboardButton(text="✅ Разбан пользователя", callback_data="admin_unban_user")],
        [InlineKeyboardButton(text="📜 Баны/Разбаны", callback_data="admin_ban_list")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="➕ Добавить обязательную подписку", callback_data="admin_add_sub")],
        [InlineKeyboardButton(text="➖ Удалить обязательную подписку", callback_data="admin_remove_sub")],
        [InlineKeyboardButton(text="📋 Список подписок", callback_data="admin_list_subs")]
    ])
    await message.answer("🔐 Админ-панель", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data.startswith("admin_"))
async def admin_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    if callback.data == "admin_stats":
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1')
        banned_users = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_banned = 0')
        active_users = cursor.fetchone()[0]

        cursor.execute('SELECT join_date FROM users ORDER BY join_date DESC LIMIT 5')
        last_users = cursor.fetchall()
        last_users_text = "\n".join([f"• {u[0]}" for u in last_users]) if last_users else "Нет"

        stats_text = f"📊 **Статистика пользователей**\n\n"
        stats_text += f"👥 Всего: {total_users}\n"
        stats_text += f"✅ Активных: {active_users}\n"
        stats_text += f"❌ Забанено: {banned_users}\n\n"
        stats_text += f"🕐 Последние 5 регистраций:\n{last_users_text}\n"
        stats_text += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        await callback.message.edit_text(stats_text, parse_mode="Markdown")

    elif callback.data == "admin_check_user":
        await state.set_state("waiting_for_user_id")
        await callback.message.edit_text("🔍 Введите ID пользователя или @username:")

    elif callback.data == "admin_ban_user":
        await state.set_state("waiting_for_ban_id")
        await callback.message.edit_text("🚫 Введите ID пользователя или @username для бана:")

    elif callback.data == "admin_unban_user":
        await state.set_state("waiting_for_unban_id")
        await callback.message.edit_text("✅ Введите ID пользователя или @username для разбана:")

    elif callback.data == "admin_ban_list":
        cursor.execute(
            'SELECT user_id, ban_reason, ban_date, unban_date FROM users WHERE is_banned = 1 OR unban_date IS NOT NULL ORDER BY ban_date DESC LIMIT 50')
        bans = cursor.fetchall()
        if bans:
            text = "📜 **История банов/разбанов**\n\n"
            for ban in bans:
                text += f"👤 ID: {ban[0]}\n"
                text += f"📝 Причина: {ban[1] or 'Не указана'}\n"
                text += f"🔨 Бан: {ban[2] or 'Не указано'}\n"
                text += f"🔓 Разбан: {ban[3] or 'Активен'}\n"
                text += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n" + "-" * 30 + "\n"
            await callback.message.edit_text(text, parse_mode="Markdown")
        else:
            await callback.message.edit_text("📜 История банов пуста.")

    elif callback.data == "admin_broadcast":
        await state.set_state(BroadcastStates.waiting_for_message)
        await callback.message.edit_text(
            "📢 Введите текст рассылки. Для добавления кнопок напишите /add_buttons после текста")

    elif callback.data == "admin_add_sub":
        await state.set_state("waiting_for_chat_id")
        await callback.message.edit_text("➕ Введите ID канала/чата (например @channel или -100123456789):")

    elif callback.data == "admin_remove_sub":
        cursor.execute('SELECT id, chat_id FROM forced_subs')
        subs = cursor.fetchall()
        if subs:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Удалить {sub[1]}", callback_data=f"remove_sub_{sub[0]}")] for sub in subs
            ])
            await callback.message.edit_text("➖ Выберите подписку для удаления:", reply_markup=keyboard)
        else:
            await callback.message.edit_text("Нет обязательных подписок.")

    elif callback.data == "admin_list_subs":
        cursor.execute('SELECT chat_id, chat_url, added_date FROM forced_subs')
        subs = cursor.fetchall()
        if subs:
            text = "📋 **Список обязательных подписок**\n\n"
            for sub in subs:
                text += f"🔗 {sub[0]}\n"
                text += f"📅 Добавлено: {sub[2]}\n"
                text += f"📍 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n" + "-" * 30 + "\n"
            await callback.message.edit_text(text, parse_mode="Markdown")
        else:
            await callback.message.edit_text("Нет обязательных подписок.")

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("remove_sub_"))
async def remove_sub_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    sub_id = int(callback.data.split("_")[2])
    cursor.execute('DELETE FROM forced_subs WHERE id = ?', (sub_id,))
    conn.commit()
    await callback.message.edit_text("✅ Подписка удалена!")


@dp.message(Command("add_buttons"))
async def add_buttons_command(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(BroadcastStates.waiting_for_buttons)
    await message.answer(
        "🔘 Введите кнопки в формате:\nтекст_кнопки|ссылка\nДля нескольких кнопок пишите каждую с новой строки.\nДля завершения введите /done")


@dp.message(BroadcastStates.waiting_for_buttons)
async def process_buttons(message: Message, state: FSMContext):
    if message.text == "/done":
        data = await state.get_data()
        text = data.get('broadcast_text')
        buttons = data.get('buttons', [])

        await state.clear()

        cursor.execute('SELECT user_id FROM users WHERE is_banned = 0')
        users = cursor.fetchall()

        keyboard = None
        if buttons:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=btn[0], url=btn[1])] for btn in buttons
            ])

        success = 0
        fail = 0
        for user in users:
            try:
                if keyboard:
                    await bot.send_message(user[0], text, reply_markup=keyboard)
                else:
                    await bot.send_message(user[0], text)
                success += 1
                await asyncio.sleep(0.05)
            except:
                fail += 1

        await message.answer(
            f"✅ Рассылка завершена!\n📨 Отправлено: {success}\n❌ Ошибок: {fail}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return

    buttons = message.text.split('\n')
    button_list = []
    for btn in buttons:
        if '|' in btn:
            text, url = btn.split('|', 1)
            button_list.append((text.strip(), url.strip()))

    await state.update_data(buttons=button_list)
    await message.answer(f"✅ Добавлено {len(button_list)} кнопок!\nДля завершения введите /done")


@dp.message(BroadcastStates.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastStates.waiting_for_buttons)
    await message.answer(
        "📨 Текст получен!\nТеперь добавьте кнопки командой /add_buttons или отправьте /add_buttons без кнопок")


@dp.message(state="waiting_for_user_id")
async def process_check_user(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    input_text = message.text.strip()
    user_id = None

    if input_text.startswith('@'):
        username = input_text[1:]
        cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
        result = cursor.fetchone()
        if result:
            user_id = result[0]
    else:
        try:
            user_id = int(input_text)
        except:
            pass

    if user_id:
        user_info = get_user_info(user_id)
        if user_info:
            text = f"👤 **Информация о пользователе**\n\n"
            text += f"🆔 ID: {user_info['user_id']}\n"
            text += f"📝 Username: @{user_info['username'] or 'Нет'}\n"
            text += f"👶 Имя: {user_info['first_name'] or 'Нет'}\n"
            text += f"👪 Фамилия: {user_info['last_name'] or 'Нет'}\n"
            text += f"📅 Регистрация: {user_info['join_date']}\n"
            text += f"🚫 Забанен: {'Да' if user_info['is_banned'] else 'Нет'}\n"
            if user_info['ban_reason']:
                text += f"📝 Причина бана: {user_info['ban_reason']}\n"
            if user_info['ban_date']:
                text += f"🔨 Дата бана: {user_info['ban_date']}\n"
            if user_info['unban_date']:
                text += f"🔓 Дата разбана: {user_info['unban_date']}\n"
            text += f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await message.answer(text, parse_mode="Markdown")
        else:
            await message.answer("❌ Пользователь не найден в базе данных.")
    else:
        await message.answer("❌ Неверный формат. Введите ID или @username.")

    await state.clear()
    await admin_panel(message)


@dp.message(state="waiting_for_ban_id")
async def process_ban_user(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    input_text = message.text.strip()
    user_id = None

    if input_text.startswith('@'):
        username = input_text[1:]
        cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
        result = cursor.fetchone()
        if result:
            user_id = result[0]
    else:
        try:
            user_id = int(input_text)
        except:
            pass

    if user_id:
        cursor.execute(
            'UPDATE users SET is_banned = 1, ban_reason = ?, ban_date = ?, unban_date = NULL WHERE user_id = ?',
            ("Бан от админа", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
        conn.commit()
        await message.answer(f"✅ Пользователь {user_id} забанен!\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        await message.answer("❌ Пользователь не найден.")

    await state.clear()
    await admin_panel(message)


@dp.message(state="waiting_for_unban_id")
async def process_unban_user(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    input_text = message.text.strip()
    user_id = None

    if input_text.startswith('@'):
        username = input_text[1:]
        cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
        result = cursor.fetchone()
        if result:
            user_id = result[0]
    else:
        try:
            user_id = int(input_text)
        except:
            pass

    if user_id:
        cursor.execute('UPDATE users SET is_banned = 0, unban_date = ? WHERE user_id = ?',
                       (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
        conn.commit()
        await message.answer(f"✅ Пользователь {user_id} разбанен!\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        await message.answer("❌ Пользователь не найден.")

    await state.clear()
    await admin_panel(message)


@dp.message(state="waiting_for_chat_id")
async def process_add_subscription(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    chat_id = message.text.strip()
    chat_url = f"https://t.me/{chat_id.replace('@', '')}" if chat_id.startswith('@') else f"https://t.me/c/{chat_id}"

    cursor.execute('INSERT INTO forced_subs (chat_id, chat_url, added_date) VALUES (?, ?, ?)',
                   (chat_id, chat_url, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()

    await message.answer(
        f"✅ Обязательная подписка добавлена!\n📎 {chat_id}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    await state.clear()
    await admin_panel(message)


@dp.message()
async def track_messages(message: Message):
    if message.chat.type != 'private':
        return

    if not await check_user_access(message.from_user.id, message):
        return

    add_user(message.from_user.id, message.from_user.username, message.from_user.first_name,
             message.from_user.last_name)

    if message.reply_to_message and message.reply_to_message.from_user.id != message.from_user.id:
        cursor.execute(
            'SELECT file_id, file_type FROM temp_media WHERE user_id = ? AND target_user_id = ? AND message_id = ?',
            (message.reply_to_message.from_user.id, message.from_user.id, message.reply_to_message.message_id))
        temp_media = cursor.fetchone()

        if temp_media:
            file_id, file_type = temp_media

            if file_type == 'photo':
                await bot.send_photo(message.from_user.id, file_id)
            elif file_type == 'video':
                await bot.send_video(message.from_user.id, file_id)
            elif file_type == 'document':
                await bot.send_document(message.from_user.id, file_id)
            elif file_type == 'voice':
                await bot.send_voice(message.from_user.id, file_id)

            cursor.execute('DELETE FROM temp_media WHERE user_id = ? AND target_user_id = ? AND message_id = ?',
                           (message.reply_to_message.from_user.id, message.from_user.id,
                            message.reply_to_message.message_id))
            conn.commit()

    if message.photo or message.video or message.document or message.voice:
        file_id = None
        file_type = None
        file_size = 0

        if message.photo:
            file_id = message.photo[-1].file_id
            file_type = 'photo'
            file_size = message.photo[-1].file_size or 0
        elif message.video:
            file_id = message.video.file_id
            file_type = 'video'
            file_size = message.video.file_size or 0
        elif message.document:
            file_id = message.document.file_id
            file_type = 'document'
            file_size = message.document.file_size or 0
        elif message.voice:
            file_id = message.voice.file_id
            file_type = 'voice'
            file_size = message.voice.file_size or 0

        if file_size <= 300 * 1024 * 1024:
            cursor.execute(
                'INSERT INTO temp_media (user_id, target_user_id, message_id, file_id, file_type, file_size, send_date) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (message.from_user.id, message.from_user.id, message.message_id, file_id, file_type, file_size,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()


@dp.message()
async def handle_message(message: Message):
    pass


@dp.message()
async def track_edits(message: Message):
    pass


@dp.edited_message()
async def track_edited_messages(message: Message):
    if message.chat.type != 'private':
        return

    if await check_user_access(message.from_user.id, message):
        if message.reply_to_message:
            cursor.execute(
                'SELECT original_text FROM deleted_messages WHERE user_id = ? AND target_user_id = ? ORDER BY delete_date DESC LIMIT 1',
                (message.reply_to_message.from_user.id, message.from_user.id))
            result = cursor.fetchone()
            if result and result[0] != message.text:
                await bot.send_message(message.from_user.id,
                                       f"✏️ Изменено сообщение:\n\nБыло:\n{result[0]}\n\nСтало:\n{message.text}\n\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                cursor.execute(
                    'INSERT INTO deleted_messages (user_id, target_user_id, original_text, new_text, edit_date) VALUES (?, ?, ?, ?, ?)',
                    (message.reply_to_message.from_user.id, message.from_user.id, result[0], message.text,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()


@dp.message()
async def handle_any_message(message: Message):
    pass


@dp.message_delete()
async def track_deleted_messages(chat_id: int, message_id: int, bot: Bot):
    pass


@dp.message()
async def track_everything(message: Message):
    pass


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())