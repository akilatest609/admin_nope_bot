# batch_handlers.py
import asyncio
import html
import time
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,            # Added missing importa
    BotCommandScopeChat,   # Added missing import
    BotCommandScopeDefault,# Added default scope for clearing global commands
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import ContextTypes

from config import ADMIN_ID, GROUP_DELAY_SECONDS, ITEM_RETRY_DELAY_SECONDS, MAX_ITEM_RETRIES, COUNTER_DEBOUNCE_SECONDS, PENDING_SESSION_TTL_SECONDS, DURATION_OPTIONS, log
from database import db, gen_code, encode_list, decode_list, register_user
import json

pending = {}
broadcast_pending = {}
video_locks = {}

def get_lock(uid: int) -> asyncio.Lock:
    if uid not in video_locks:
        video_locks[uid] = asyncio.Lock()
    return video_locks[uid]

def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ADMIN_ID

def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if secs and not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0"

def build_menu_keyboard(total_seconds: int) -> InlineKeyboardMarkup:
    duration_row = [
        InlineKeyboardButton(label, callback_data=f"add_{secs}")
        for label, secs in DURATION_OPTIONS
    ]
    confirm_row = [
        InlineKeyboardButton(f"✅ Confirm ({format_duration(total_seconds)})", callback_data="confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]
    return InlineKeyboardMarkup([duration_row, confirm_row])

def menu_text(count: int, total_seconds: int) -> str:
    return (
        f"{count} item(s) added (posts with media/text/buttons, in order).\n"
        f"Set auto-delete timer (tap to stack):\n"
        f"Current: {format_duration(total_seconds)}"
    )

def cleanup_stale_sessions():
    now = int(time.time())
    stale = [
        uid for uid, s in pending.items()
        if now - s.get("last_active", now) > PENDING_SESSION_TTL_SECONDS
    ]
    for uid in stale:
        pending.pop(uid, None)
        video_locks.pop(uid, None)

    stale_b = [
        uid for uid, s in broadcast_pending.items()
        if now - s.get("last_active", now) > PENDING_SESSION_TTL_SECONDS
    ]
    for uid in stale_b:
        broadcast_pending.pop(uid, None)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        register_user(user.id, user.first_name or "", user.username or "")

    # Clear global commands so regular users don't inherit them by default
    try:
        await context.bot.set_my_commands([], scope=BotCommandScopeDefault())
    except Exception:
        pass

    # Force-set admin commands strictly for the admin chat
    if is_admin(update):
        try:
            await context.bot.set_my_commands(
                [
                    BotCommand("start", "Start / redeem a code"),
                    BotCommand("get_link", "Create a new media batch link"),
                    BotCommand("link_stats", "View, rotate, or delete your links"),
                    BotCommand("broadcast", "Interactive multi-media broadcast to all users"),
                ],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
        except Exception as e:
            log.warning(f"Failed to set admin commands: {e}")
    else:
        # Clear commands specifically for regular user chats
        try:
            await context.bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=user.id))
        except Exception:
            pass

    args = context.args
    if args:
        await deliver_batch(update, context, args[0])
        return
        
    if is_admin(update):
        await update.message.reply_text(
            "Commands:\n"
            "/get_link — start a new batch post link\n"
            "/link_stats — view active links and views\n"
            "/broadcast — interactive broadcast session for posts with buttons"
        )
    else:
        await update.message.reply_text("සාදරයෙන් පිලිගන්නවා 💕, ඇඩ්මින් ට එවන්න ඔනෙ මැසෙජ් එක ටයිප් කරලා එවන්න,")

async def cmd_get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cleanup_stale_sessions()
    uid = update.effective_user.id
    pending[uid] = {
        "items": [],
        "pending_seconds": 0,
        "menu_msg_id": None,
        "edit_task": None,
        "last_active": int(time.time()),
    }
    await update.message.reply_text("Send or forward your posts (media/text with buttons) now.")

get_link = cmd_get_link

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cleanup_stale_sessions()
    uid = update.effective_user.id
    broadcast_pending[uid] = {
        "items": [],
        "menu_msg_id": None,
        "last_active": int(time.time()),
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Send Broadcast", callback_data="bc_send")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]
    ])
    sent = await update.message.reply_text(
        "📢 **Broadcast Mode Active**\n\n"
        "Send or forward all posts (including captions and inline buttons) you want to broadcast. "
        "When you are finished collecting, click **🚀 Send Broadcast** below.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    broadcast_pending[uid]["menu_msg_id"] = sent.message_id

async def _add_item_to_batch(uid: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str, m_type: str, markup=None, caption=None):
    async with get_lock(uid):
        if uid not in pending:
            pending[uid] = {
                "items": [],
                "pending_seconds": 0,
                "menu_msg_id": None,
                "edit_task": None,
                "last_active": int(time.time()),
            }

        pending[uid]["items"].append((content, m_type, markup, caption))
        pending[uid]["last_active"] = int(time.time())

        old_task = pending[uid].get("edit_task")
        if old_task and not old_task.done():
            old_task.cancel()
        pending[uid]["edit_task"] = asyncio.create_task(_update_menu_message(uid, chat_id, context))

async def _add_item_to_broadcast(uid: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str, m_type: str, markup=None, caption=None):
    if uid not in broadcast_pending:
        return
    broadcast_pending[uid]["items"].append((content, m_type, markup, caption))
    broadcast_pending[uid]["last_active"] = int(time.time())
    
    count = len(broadcast_pending[uid]["items"])
    msg_id = broadcast_pending[uid]["menu_msg_id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Send Broadcast ({count} items queued)", callback_data="bc_send")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]
    ])
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=msg_id, 
            text=f"📢 **Broadcast Mode Active**\n\nCollected **{count}** post(s).\nSend more or click **🚀 Send Broadcast**.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass

async def _update_menu_message(uid: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.sleep(COUNTER_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    state = pending.get(uid)
    if not state:
        return

    count = len(state["items"])
    total = state["pending_seconds"]
    text = menu_text(count, total)
    kb = build_menu_keyboard(total)
    msg_id = state["menu_msg_id"]

    try:
        if msg_id is None:
            sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            state["menu_msg_id"] = sent.message_id
        else:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb)
    except Exception as e:
        log.info(f"Could not update menu message for {uid}: {e}")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id

    if uid != ADMIN_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    data = query.data

    if data.startswith("bc_"):
        if uid not in broadcast_pending:
            await query.answer()
            await query.edit_message_text("This broadcast session expired. Use /broadcast to start again.")
            return

        if data == "bc_cancel":
            broadcast_pending.pop(uid, None)
            await query.answer()
            await query.edit_message_text("Broadcast cancelled.")
            return

        if data == "bc_send":
            state = broadcast_pending.pop(uid)
            items = state["items"]
            if not items:
                await query.answer("No posts added yet.", show_alert=True)
                broadcast_pending[uid] = state
                return

            conn = db()
            try:
                users = conn.execute("SELECT user_id FROM users").fetchall()
            finally:
                conn.close()

            if not users:
                await query.answer("No users found in database.", show_alert=True)
                await query.edit_message_text("Broadcast cancelled: No users found.")
                return

            await query.answer()
            await query.edit_message_text(f"📢 Broadcasting {len(items)} item(s) to {len(users)} user(s)... Please wait.")

            success = 0
            failed = 0

            for row in users:
                target_uid = row["user_id"]
                try:
                    for content, m_type, markup, caption in items:
                        if m_type == "text":
                            await context.bot.send_message(chat_id=target_uid, text=content, reply_markup=markup, parse_mode=ParseMode.HTML)
                        else:
                            await _send_single_item(context, target_uid, content, m_type, markup=markup, caption=caption)
                        await asyncio.sleep(0.1)
                    success += 1
                except Exception as e:
                    failed += 1
                    log.warning(f"Failed to broadcast to {target_uid}: {e}")

                await asyncio.sleep(0.04)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Broadcast complete ✅\nSuccessful: {success}\nFailed: {failed}"
            )
            return

    if uid not in pending:
        await query.answer()
        await query.edit_message_text("This batch session expired. Use /get_link to start again.")
        return

    pending[uid]["last_active"] = int(time.time())

    if data == "cancel":
        pending.pop(uid, None)
        await query.answer()
        await query.edit_message_text("Cancelled.")
        return

    if data == "confirm":
        state = pending.pop(uid)
        if not state["items"]:
            await query.answer("No items added yet.", show_alert=True)
            pending[uid] = state
            return

        auto_delete_seconds = state["pending_seconds"] or 3600
        code = gen_code()
        now = int(time.time())

        file_ids = [item[0] for item in state["items"]]
        types = [item[1] for item in state["items"]]
        captions = [item[3] for item in state["items"]]

        conn = db()
        try:
            cursor = conn.execute("PRAGMA table_info(batches)")
            columns = [col["name"] for col in cursor.fetchall()]

            has_markups = "markups" in columns
            has_captions = "captions" in columns

            if has_markups and has_captions:
                markups = [json.dumps(item[2].to_dict()) if item[2] else None for item in state["items"]]
                conn.execute(
                    "INSERT INTO batches (code, owner_id, file_ids, types, markups, captions, created_at, auto_delete_seconds, revoked, deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                    (code, uid, encode_list(file_ids), encode_list(types), encode_list(markups), encode_list(captions), now, auto_delete_seconds),
                )
            elif has_captions:
                conn.execute(
                    "INSERT INTO batches (code, owner_id, file_ids, types, captions, created_at, auto_delete_seconds, revoked, deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                    (code, uid, encode_list(file_ids), encode_list(types), encode_list(captions), now, auto_delete_seconds),
                )
            else:
                conn.execute(
                    "INSERT INTO batches (code, owner_id, file_ids, types, created_at, auto_delete_seconds, revoked, deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                    (code, uid, encode_list(file_ids), encode_list(types), now, auto_delete_seconds),
                )
            conn.commit()
        finally:
            conn.close()

        bot_username = context.bot.username or (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={code}"
        await query.answer()
        await query.edit_message_text(
            f"<code>{html.escape(link)}</code>\n"
            f"{len(file_ids)} items · auto-delete {format_duration(auto_delete_seconds)} after delivery\n\n"
            f"Tap the link above to copy it.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data.startswith("add_"):
        secs = int(data.split("_")[1])
        pending[uid]["pending_seconds"] += secs
        count = len(pending[uid]["items"])
        total = pending[uid]["pending_seconds"]
        await query.answer(f"+{format_duration(secs)}")
        await query.edit_message_text(menu_text(count, total), reply_markup=build_menu_keyboard(total))

async def _send_single_item(context, chat_id, fid, mtype, markup=None, caption=None):
    for attempt in range(1, MAX_ITEM_RETRIES + 1):
        try:
            if mtype == "photo":
                return await context.bot.send_photo(chat_id=chat_id, photo=fid, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif mtype == "animation":
                return await context.bot.send_animation(chat_id=chat_id, animation=fid, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif mtype == "audio":
                return await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif mtype == "voice":
                return await context.bot.send_voice(chat_id=chat_id, voice=fid, caption=caption, reply_markup=markup)
            elif mtype == "document":
                return await context.bot.send_document(chat_id=chat_id, document=fid, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif mtype == "video":
                return await context.bot.send_video(chat_id=chat_id, video=fid, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                return await context.bot.send_document(chat_id=chat_id, document=fid, caption=caption, reply_markup=markup)
        except RetryAfter as e:
            log.warning(f"Flood wait, retrying in {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError) as e:
            log.warning(f"Transient error (attempt {attempt}): {e}")
            await asyncio.sleep(ITEM_RETRY_DELAY_SECONDS)
        except Exception as e:
            log.error(f"Item send failed (attempt {attempt}): {e}")
            await asyncio.sleep(ITEM_RETRY_DELAY_SECONDS)
    return None

async def _send_text_item(context, chat_id, text, markup=None):
    for attempt in range(1, MAX_ITEM_RETRIES + 1):
        try:
            return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except RetryAfter as e:
            log.warning(f"Flood wait on text item, retrying in {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError) as e:
            log.warning(f"Transient error on text item (attempt {attempt}): {e}")
            await asyncio.sleep(ITEM_RETRY_DELAY_SECONDS)
        except Exception as e:
            log.error(f"Text send failed: {e}")
            await asyncio.sleep(ITEM_RETRY_DELAY_SECONDS)
    return None

async def deliver_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    user = update.effective_user
    uid = user.id
    chat_id = update.effective_chat.id
    register_user(uid, user.first_name or "", user.username or "")

    if uid != ADMIN_ID:
        conn = db()
        try:
            wl_count = conn.execute("SELECT COUNT(*) as c FROM whitelist").fetchone()["c"]
            if wl_count > 0:
                allowed = conn.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (uid,)).fetchone()
                if not allowed:
                    await update.message.reply_text("⛔ You are not authorized.")
                    return
        finally:
            conn.close()

    conn = db()
    try:
        row = conn.execute("SELECT * FROM batches WHERE code = ?", (code,)).fetchone()
        now = int(time.time())

        if row is None or row["revoked"] or row["deleted"]:
            await update.message.reply_text("This link is no longer available.")
            return

        file_ids = decode_list(row["file_ids"])
        types = decode_list(row["types"]) if row["types"] else ["video"] * len(file_ids)
        
        try:
            row_keys = row.keys()
        except Exception:
            row_keys = []

        if "markups" in row_keys and row["markups"]:
            try:
                markups = [InlineKeyboardMarkup.de_json(json.loads(m), context.bot) if m else None for m in decode_list(row["markups"])]
            except Exception:
                markups = [None] * len(file_ids)
        else:
            markups = [None] * len(file_ids)

        if "captions" in row_keys and row["captions"]:
            try:
                captions = decode_list(row["captions"])
            except Exception:
                captions = [None] * len(file_ids)
        else:
            captions = [None] * len(file_ids)

        items = list(zip(file_ids, types, markups, captions))
        auto_delete_seconds = row["auto_delete_seconds"] or 0

        conn.execute("INSERT INTO views (code, user_id, ts) VALUES (?, ?, ?)", (code, uid, now))
        conn.commit()

        await update.message.reply_text(f"Sending {len(items)} item(s)...")

        delivered = 0
        sent_message_ids = []

        i = 0
        total_items = len(items)

        while i < total_items:
            current_type = items[i][1]

            if current_type in ["photo", "video"]:
                album_chunk = []
                chunk_indices = []

                while i < total_items and items[i][1] in ["photo", "video"] and len(album_chunk) < 10:
                    fid, mtype, markup, cap = items[i]
                    formatted_cap = cap if cap else None
                    if mtype == "photo":
                        album_chunk.append(InputMediaPhoto(media=fid, caption=formatted_cap, parse_mode=ParseMode.HTML))
                    else:
                        album_chunk.append(InputMediaVideo(media=fid, caption=formatted_cap, parse_mode=ParseMode.HTML))
                    chunk_indices.append(i)
                    i += 1

                if len(album_chunk) > 1:
                    try:
                        sent_msgs = await context.bot.send_media_group(chat_id=chat_id, media=album_chunk)
                        for m in sent_msgs:
                            delivered += 1
                            sent_message_ids.append(m.message_id)
                    except Exception as e:
                        log.error(f"Failed to send media group album: {e}")
                        for idx in chunk_indices:
                            fid, mtype, markup, cap = items[idx]
                            sent = await _send_single_item(context, chat_id, fid, mtype, markup=markup, caption=cap)
                            if sent:
                                delivered += 1
                                sent_message_ids.append(sent.message_id)
                else:
                    idx = chunk_indices[0]
                    fid, mtype, markup, cap = items[idx]
                    sent = await _send_single_item(context, chat_id, fid, mtype, markup=markup, caption=cap)
                    if sent:
                        delivered += 1
                        sent_message_ids.append(sent.message_id)
            else:
                fid, mtype, markup, cap = items[i]
                if mtype == "text":
                    sent = await _send_text_item(context, chat_id, fid, markup=markup)
                else:
                    sent = await _send_single_item(context, chat_id, fid, mtype, markup=markup, caption=cap)

                if sent is not None:
                    delivered += 1
                    sent_message_ids.append(sent.message_id)
                i += 1

            if i < total_items:
                await asyncio.sleep(GROUP_DELAY_SECONDS)

        if auto_delete_seconds > 0 and sent_message_ids:
            for mid in sent_message_ids:
                conn.execute(
                    "INSERT INTO deliveries (code, user_id, chat_id, message_id, created_at, delete_after_seconds, deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (code, uid, chat_id, mid, now, auto_delete_seconds),
                )
            conn.commit()

        summary = f"Done ✅ Delivered {delivered}/{len(items)} item(s)."
        if auto_delete_seconds > 0:
            summary += f"\nThese will auto-delete in {format_duration(auto_delete_seconds)}."
        await update.message.reply_text(summary)
    finally:
        conn.close()

async def cleanup_deliveries_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE deleted = 0 AND delete_after_seconds > 0 "
            "AND (created_at + delete_after_seconds) <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            try:
                await context.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
            except Exception:
                pass
            conn.execute("UPDATE deliveries SET deleted = 1 WHERE id = ?", (row["id"],))
        if rows:
            conn.commit()
    finally:
        conn.close()

async def link_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    uid = update.effective_user.id
    bot_username = context.bot.username or (await context.bot.get_me()).username

    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM batches WHERE owner_id = ? AND revoked = 0 AND deleted = 0 ORDER BY created_at DESC LIMIT 20",
            (uid,),
        ).fetchall()

        if not rows:
            await update.message.reply_text("No active links.")
            return

        for row in rows:
            views = conn.execute("SELECT COUNT(*) as c FROM views WHERE code = ?", (row["code"],)).fetchone()["c"]
            link = f"https://t.me/{bot_username}?start={row['code']}"
            text = f"<code>{html.escape(link)}</code>\n{len(decode_list(row['file_ids']))} posts · {views} views"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 Revoke", callback_data=f"rotate_{row['code']}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"del_{row['code']}"),
            ]])
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
    finally:
        conn.close()

async def on_rotate_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid != ADMIN_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return
    old_code = query.data.split("_", 1)[1]

    conn = db()
    try:
        row = conn.execute("SELECT * FROM batches WHERE code = ? AND owner_id = ?", (old_code, uid)).fetchone()
        if row is None:
            await query.answer("Not found.", show_alert=True)
            return

        conn.execute("UPDATE batches SET revoked = 1 WHERE code = ?", (old_code,))
        new_code = gen_code()
        now = int(time.time())

        row_keys = row.keys()
        has_markups = "markups" in row_keys
        has_captions = "captions" in row_keys

        if has_markups and has_captions:
            conn.execute(
                "INSERT INTO batches (code, owner_id, file_ids, types, markups, captions, created_at, auto_delete_seconds, revoked, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (new_code, uid, row["file_ids"], row["types"], row["markups"], row["captions"], now, row["auto_delete_seconds"]),
            )
        elif has_captions:
            conn.execute(
                "INSERT INTO batches (code, owner_id, file_ids, types, captions, created_at, auto_delete_seconds, revoked, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (new_code, uid, row["file_ids"], row["types"], row["captions"], now, row["auto_delete_seconds"]),
            )
        else:
            conn.execute(
                "INSERT INTO batches (code, owner_id, file_ids, types, created_at, auto_delete_seconds, revoked, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                (new_code, uid, row["file_ids"], row["types"], now, row["auto_delete_seconds"]),
            )
        conn.commit()
    finally:
        conn.close()

    bot_username = context.bot.username or (await context.bot.get_me()).username
    new_link = f"https://t.me/{bot_username}?start={new_code}"
    await query.answer("Regenerated.")
    await query.edit_message_text(f"New link:\n<code>{html.escape(new_link)}</code>", parse_mode=ParseMode.HTML)

async def on_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid != ADMIN_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return
    code = query.data.split("_", 1)[1]

    conn = db()
    try:
        conn.execute("UPDATE batches SET deleted = 1, revoked = 1 WHERE code = ?", (code,))
        conn.commit()
    finally:
        conn.close()

    await query.answer("Deleted.")
    await query.edit_message_text("Link and posts deleted.")
