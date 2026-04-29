import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any, Optional, List, Union

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, BaseFilter, StateFilter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, FSInputFile, BufferedInputFile
)
from aiogram.enums import ChatType, ParseMode, ChatMemberStatus
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import Text

import aiosqlite

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("8605542203:AAFPiTQ-OUSwI1X1d4BDWTjlF6fEvRFoqCY")
ADMIN_ID = int(os.getenv("5254779646", "0"))

DB_PATH = "bot_data.sqlite"

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class BroadcastState(StatesGroup):
    waiting_for_content = State()
    waiting_for_button_url = State()
    waiting_for_button_text = State()

def retro_terminal_frame(text: str, title: Optional[str] = "TERMINAL_v1.0") -> str:
    lines = text.split("\n")
    max_len = max([len(line) for line in lines] + [len(title) + 4])
    border_top = f"┌─< {title} >─{'─' * (max_len - len(title) - 4)}┐"
    border_bot = f"└{'─' * (max_len + 2)}┘"
    formatted_lines = []
    for line in lines:
        padding = " " * (max_len - len(line))
        formatted_lines.append(f"│ {line}{padding} │")
    return f"<pre>{border_top}\n" + "\n".join(formatted_lines) + f"\n{border_bot}</pre>"

class Database:
    def __init__(self):
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                banned INTEGER DEFAULT 0,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS message_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                message_type TEXT,
                file_id TEXT,
                text_content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS broadcast_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER,
                text TEXT,
                url TEXT
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                channel_name TEXT
            );
        """)
        await self.conn.commit()

    async def add_or_update_user(self, user: types.User):
        await self.conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name
        """, (user.id, user.username, user.first_name, user.last_name))
        await self.conn.commit()

    async def ban_user(self, user_id: int, status: bool):
        await self.conn.execute("UPDATE users SET banned = ? WHERE user_id = ?", (int(status), user_id))
        await self.conn.commit()

    async def is_banned(self, user_id: int) -> bool:
        cursor = await self.conn.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def get_user(self, identifier: Union[int, str]):
        if isinstance(identifier, int) or identifier.isdigit():
            cursor = await self.conn.execute("SELECT * FROM users WHERE user_id = ?", (int(identifier),))
        else:
            cursor = await self.conn.execute("SELECT * FROM users WHERE username = ?", (identifier.lstrip("@"),))
        return await cursor.fetchone()

    async def cache_message(self, user_id: int, message_id: int, msg_type: str, file_id: Optional[str], text: Optional[str]):
        await self.conn.execute("""
            INSERT INTO message_cache (user_id, message_id, message_type, file_id, text_content)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, message_id, msg_type, file_id, text))
        await self.conn.commit()

    async def get_cached_message(self, user_id: int, message_id: int):
        cursor = await self.conn.execute(
            "SELECT * FROM message_cache WHERE user_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, message_id)
        )
        return await cursor.fetchone()

    async def delete_cached_message(self, user_id: int, message_id: int):
        await self.conn.execute("DELETE FROM message_cache WHERE user_id = ? AND message_id = ?", (user_id, message_id))
        await self.conn.commit()

    async def get_all_users(self):
        cursor = await self.conn.execute("SELECT user_id FROM users WHERE banned = 0")
        return [row[0] async for row in cursor]

    async def add_broadcast_buttons(self, broadcast_id: int, buttons: List[Dict]):
        for btn in buttons:
            await self.conn.execute(
                "INSERT INTO broadcast_buttons (broadcast_id, text, url) VALUES (?, ?, ?)",
                (broadcast_id, btn['text'], btn['url'])
            )
        await self.conn.commit()

    async def get_broadcast_buttons(self, broadcast_id: int):
        cursor = await self.conn.execute("SELECT * FROM broadcast_buttons WHERE broadcast_id = ?", (broadcast_id,))
        return await cursor.fetchall()

    async def add_channel(self, channel_id: str, channel_name: str):
        await self.conn.execute(
            "INSERT OR IGNORE INTO required_channels (channel_id, channel_name) VALUES (?, ?)",
            (channel_id, channel_name)
        )
        await self.conn.commit()

    async def remove_channel(self, channel_id: str):
        await self.conn.execute("DELETE FROM required_channels WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()

    async def get_all_channels(self):
        cursor = await self.conn.execute("SELECT * FROM required_channels")
        return await cursor.fetchall()

db = Database()

def is_admin_filter(user_id: int) -> bool:
    return user_id == ADMIN_ID

class AdminFilter(BaseFilter):
    async def __call__(self, obj: Union[Message, CallbackQuery]) -> bool:
        if isinstance(obj, CallbackQuery):
            return obj.from_user.id == ADMIN_ID
        if isinstance(obj, Message):
            return obj.from_user.id == ADMIN_ID
        return False

async def check_subscription(user_id: int) -> bool:
    channels = await db.get_all_channels()
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch['channel_id'], user_id=user_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED]:
                return False
        except Exception:
            return False
    return True

def admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="[ MAILING ]", callback_data="admin_mailing"),
        InlineKeyboardButton(text="[ USERS ]", callback_data="admin_users")
    )
    builder.row(
        InlineKeyboardButton(text="[ CHANNELS ]", callback_data="admin_channels"),
        InlineKeyboardButton(text="[ EXIT ]", callback_data="admin_exit")
    )
    return builder.as_markup()

def back_to_admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="[ RETURN ]", callback_data="admin_back"))
    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if await db.is_banned(message.from_user.id):
        await message.answer(retro_terminal_frame("ACCESS DENIED: USER BANNED", "SECURITY"))
        return
    await db.add_or_update_user(message.from_user)
    sub_status = await check_subscription(message.from_user.id)
    if not sub_status:
        channels = await db.get_all_channels()
        channels_text = "\n".join([f"{ch['channel_name']} ({ch['channel_id']})" for ch in channels]) if channels else "NO CHANNELS CONFIGURED"
        text = f"SUBSCRIPTION REQUIRED\n\nJOIN CHANNELS:\n{channels_text}"
        await message.answer(retro_terminal_frame(text, "AUTH"))
        return
    await message.answer(retro_terminal_frame("SYSTEM READY. AWAITING INPUT.", "STATUS"))

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin_filter(message.from_user.id):
        await message.answer(retro_terminal_frame("ACCESS DENIED", "SECURITY"))
        return
    await db.add_or_update_user(message.from_user)
    await message.answer(retro_terminal_frame("ADMIN PANEL ACTIVATED", "ROOT"), reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_exit")
async def exit_admin(call: CallbackQuery):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    await call.message.edit_text(retro_terminal_frame("SYSTEM TERMINATED", "LOGOUT"))
    await call.answer()

@dp.callback_query(F.data == "admin_back")
async def back_admin(call: CallbackQuery):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    await call.message.edit_text(retro_terminal_frame("ADMIN PANEL ACTIVATED", "ROOT"), reply_markup=admin_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_channels")
async def admin_channels_menu(call: CallbackQuery):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    channels = await db.get_all_channels()
    builder = InlineKeyboardBuilder()
    if channels:
        channels_text = "REQUIRED CHANNELS:\n\n"
        for ch in channels:
            channels_text += f"• {ch['channel_name']} ({ch['channel_id']})\n"
            builder.row(InlineKeyboardButton(
                text=f"[ REMOVE {ch['channel_name']} ]",
                callback_data=f"remove_ch_{ch['id']}"
            ))
    else:
        channels_text = "NO CHANNELS CONFIGURED"
    builder.row(InlineKeyboardButton(text="[ ADD CHANNEL ]", callback_data="add_channel"))
    builder.row(InlineKeyboardButton(text="[ BACK ]", callback_data="admin_back"))
    await call.message.edit_text(
        retro_terminal_frame(channels_text, "CHANNELS"),
        reply_markup=builder.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data == "add_channel")
async def add_channel_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    await call.message.edit_text(
        retro_terminal_frame("SEND CHANNEL ID (e.g. @channelname or -100123456)", "ADD_CHANNEL"),
        reply_markup=back_to_admin_kb()
    )
    await state.set_state("waiting_channel_id")
    await call.answer()

@dp.message(StateFilter("waiting_channel_id"))
async def process_channel_id(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id):
        return
    channel_id = message.text.strip()
    try:
        chat = await bot.get_chat(channel_id)
        await db.add_channel(str(chat.id), chat.title or channel_id)
        await message.answer(
            retro_terminal_frame(f"CHANNEL ADDED: {chat.title} ({chat.id})", "SUCCESS"),
            reply_markup=admin_kb()
        )
    except Exception as e:
        await message.answer(
            retro_terminal_frame(f"ERROR: {str(e)}", "FAILED"),
            reply_markup=admin_kb()
        )
    await state.clear()

@dp.callback_query(F.data.startswith("remove_ch_"))
async def remove_channel(call: CallbackQuery):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    ch_id = int(call.data.split("_")[-1])
    channels = await db.get_all_channels()
    target = next((ch for ch in channels if ch['id'] == ch_id), None)
    if target:
        await db.remove_channel(target['channel_id'])
        await call.answer(f"CHANNEL {target['channel_name']} REMOVED")
    else:
        await call.answer("CHANNEL NOT FOUND")
    await admin_channels_menu(call)

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(call: CallbackQuery, state: FSMContext):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    await call.message.edit_text(
        retro_terminal_frame("SEND POST CONTENT (TEXT, PHOTO, VIDEO, DOCUMENT)\nOR /cancel", "BROADCAST"),
        reply_markup=back_to_admin_kb()
    )
    await state.set_state(BroadcastState.waiting_for_content)
    await call.answer()

@dp.message(BroadcastState.waiting_for_content, F.text != "/cancel")
async def broadcast_content_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    await state.update_data(content_type="text", text=message.text)
    await message.answer(retro_terminal_frame("ADD URL BUTTON?\nSEND URL OR /done", "BROADCAST"), reply_markup=back_to_admin_kb())
    await state.set_state(BroadcastState.waiting_for_button_url)

@dp.message(BroadcastState.waiting_for_content, F.photo)
async def broadcast_photo_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    await state.update_data(content_type="photo", file_id=message.photo[-1].file_id, caption=message.caption)
    await message.answer(retro_terminal_frame("ADD URL BUTTON?\nSEND URL OR /done", "BROADCAST"), reply_markup=back_to_admin_kb())
    await state.set_state(BroadcastState.waiting_for_button_url)

@dp.message(BroadcastState.waiting_for_content, F.video)
async def broadcast_video_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    await state.update_data(content_type="video", file_id=message.video.file_id, caption=message.caption)
    await message.answer(retro_terminal_frame("ADD URL BUTTON?\nSEND URL OR /done", "BROADCAST"), reply_markup=back_to_admin_kb())
    await state.set_state(BroadcastState.waiting_for_button_url)

@dp.message(BroadcastState.waiting_for_content, F.document)
async def broadcast_document_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    await state.update_data(content_type="document", file_id=message.document.file_id, caption=message.caption)
    await message.answer(retro_terminal_frame("ADD URL BUTTON?\nSEND URL OR /done", "BROADCAST"), reply_markup=back_to_admin_kb())
    await state.set_state(BroadcastState.waiting_for_button_url)

@dp.message(BroadcastState.waiting_for_button_url, F.text.regexp(r'^https?://'))
async def broadcast_url_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    await state.update_data(pending_url=message.text)
    await message.answer(retro_terminal_frame("SEND BUTTON TEXT", "BROADCAST"))
    await state.set_state(BroadcastState.waiting_for_button_text)

@dp.message(BroadcastState.waiting_for_button_text)
async def broadcast_button_text_received(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    data = await state.get_data()
    url = data.get("pending_url")
    text = message.text
    buttons = data.get("buttons", [])
    buttons.append({"text": text, "url": url})
    await state.update_data(buttons=buttons, pending_url=None)
    await message.answer(retro_terminal_frame("BUTTON ADDED. SEND MORE URL OR /done", "BROADCAST"), reply_markup=back_to_admin_kb())
    await state.set_state(BroadcastState.waiting_for_button_url)

@dp.message(BroadcastState.waiting_for_button_url, Command("done"))
@dp.message(BroadcastState.waiting_for_content, Command("cancel"))
async def broadcast_done_or_cancel(message: Message, state: FSMContext):
    if not is_admin_filter(message.from_user.id): return
    if message.text == "/cancel":
        await state.clear()
        await message.answer(retro_terminal_frame("BROADCAST CANCELLED", "TERMINATED"), reply_markup=admin_kb())
        return

    data = await state.get_data()
    buttons = data.get("buttons", [])
    content_type = data.get("content_type")
    users = await db.get_all_users()
    success = 0
    failed = 0

    markup = None
    if buttons:
        builder = InlineKeyboardBuilder()
        for btn in buttons:
            builder.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
        markup = builder.as_markup()

    for uid in users:
        try:
            if content_type == "text":
                await bot.send_message(uid, data['text'], reply_markup=markup, disable_web_page_preview=True)
            elif content_type == "photo":
                await bot.send_photo(uid, data['file_id'], caption=data.get('caption'), reply_markup=markup)
            elif content_type == "video":
                await bot.send_video(uid, data['file_id'], caption=data.get('caption'), reply_markup=markup)
            elif content_type == "document":
                await bot.send_document(uid, data['file_id'], caption=data.get('caption'), reply_markup=markup)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await state.clear()
    result = f"BROADCAST COMPLETE\nSUCCESS: {success}\nFAILED: {failed}"
    await message.answer(retro_terminal_frame(result, "REPORT"), reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    if not is_admin_filter(call.from_user.id):
        await call.answer("ACCESS DENIED", show_alert=True)
        return
    await call.message.edit_text(
        retro_terminal_frame("SEND USER ID OR @USERNAME", "SEARCH"),
        reply_markup=back_to_admin_kb()
    )
    await call.answer()

@dp.message(F.text.regexp(r'^(@\w+|\d+)$'), AdminFilter())
async def search_user(message: Message):
    identifier = message.text
    user = await db.get_user(identifier)
    if not user:
        await message.answer(retro_terminal_frame("USER NOT FOUND", "ERROR"), reply_markup=admin_kb())
        return

    ban_status = "YES" if user['banned'] else "NO"
    info = f"ID: {user['user_id']}\nUSERNAME: {user['username']}\nNAME: {user['first_name']} {user['last_name'] or ''}\nBANNED: {ban_status}\nJOINED: {user['joined_date']}"
    builder = InlineKeyboardBuilder()
    action = "UNBAN" if user['banned'] else "BAN"
    builder.row(InlineKeyboardButton(text=f"[ {action} ]", callback_data=f"toggle_ban_{user['user_id']}"))
    builder.row(InlineKeyboardButton(text="[ BACK ]", callback_data="admin_back"))
    await message.answer(retro_terminal_frame(info, "USER_DATA"), reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("toggle_ban_"), AdminFilter())
async def toggle_ban(call: CallbackQuery):
    user_id = int(call.data.split("_")[-1])
    user = await db.get_user(user_id)
    if not user: await call.answer("USER NOT FOUND"); return
    new_status = not user['banned']
    await db.ban_user(user_id, new_status)
    action = "BANNED" if new_status else "UNBANNED"
    await call.answer(f"USER {action}")
    await call.message.delete()
    await call.message.answer(retro_terminal_frame(f"USER {user_id} {action}", "ADMIN"), reply_markup=admin_kb())

@dp.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(message: Message):
    if message.from_user.id == ADMIN_ID: return
    if await db.is_banned(message.from_user.id):
        await message.answer(retro_terminal_frame("ACCESS DENIED: USER BANNED", "SECURITY"))
        return
    if not await check_subscription(message.from_user.id):
        channels = await db.get_all_channels()
        channels_text = "\n".join([f"{ch['channel_name']} ({ch['channel_id']})" for ch in channels]) if channels else "NO CHANNELS CONFIGURED"
        text = f"SUBSCRIPTION REQUIRED\n\nJOIN CHANNELS:\n{channels_text}"
        await message.answer(retro_terminal_frame(text, "AUTH"))
        return

    await db.add_or_update_user(message.from_user)

    msg_type = "text"
    file_id = None
    text_content = message.text or message.caption

    if message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        msg_type = "video"
        file_id = message.video.file_id
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
    elif message.voice:
        msg_type = "voice"
        file_id = message.voice.file_id
    elif message.animation:
        msg_type = "animation"
        file_id = message.animation.file_id
    elif message.sticker:
        msg_type = "sticker"
        file_id = message.sticker.file_id
    elif message.audio:
        msg_type = "audio"
        file_id = message.audio.file_id

    await db.cache_message(message.from_user.id, message.message_id, msg_type, file_id, text_content)

    if message.reply_to_message:
        replied = message.reply_to_message
        if replied.photo and replied.photo[-1].file_id:
            await message.reply_photo(replied.photo[-1].file_id, caption="[RETRIEVED ONE-TIME MEDIA]")
        elif replied.video and replied.video.file_id:
            await message.reply_video(replied.video.file_id, caption="[RETRIEVED ONE-TIME MEDIA]")

@dp.edited_message(F.chat.type == ChatType.PRIVATE)
async def handle_edited_message(message: Message):
    if message.from_user.id == ADMIN_ID: return
    original = await db.get_cached_message(message.from_user.id, message.message_id)
    new_text = message.text or message.caption
    if original:
        orig_text = original['text_content'] or original['message_type']
        report = f"EDITED MESSAGE ID: {message.message_id}\n\n[ORIGINAL]:\n{orig_text}\n\n[EDITED]:\n{new_text}"
        await message.answer(retro_terminal_frame(report, "TRACKING"))
        await db.cache_message(message.from_user.id, message.message_id, original['message_type'], original['file_id'], new_text)

async def main():
    await db.connect()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())