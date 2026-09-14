# chat_handlers.py
import time
from telegram import Update, ReactionTypeEmoji
from telegram.ext import ContextTypes
from config import ADMIN_ID, log
from database import db, register_user
from batch_handlers import is_admin, deliver_batch, pending, broadcast_pending, _add_item_to_batch, _add_item_to_broadcast

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = update.message
    user = update.effective_user

    if user and not is_admin(update):
        register_user(user.id, user.first_name or "", user.username or "")

    # 1. Admin replying to a user message or thread
    if is_admin(update) and msg.reply_to_message:
        replied_msg_id = msg.reply_to_message.message_id
        conn = db()
        try:
            mapping = conn.execute("SELECT user_id FROM chat_mapping WHERE admin_message_id = ?", (replied_msg_id,)).fetchone()
            if not mapping:
                thread_map = conn.execute("SELECT user_id FROM chat_threads WHERE admin_msg_id = ?", (replied_msg_id,)).fetchone()
                if thread_map:
                    mapping = thread_map
        finally:
            conn.close()

        if mapping:
            target_user_id = mapping["user_id"]
            try:
                await context.bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=chat_id,
                    message_id=msg.message_id
                )
                await context.bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    reaction=[ReactionTypeEmoji("👍")]
                )
            except Exception as e:
                log.error(f"Failed to copy reply to user {target_user_id}: {e}")
                try:
                    await context.bot.set_message_reaction(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        reaction=[ReactionTypeEmoji("⚠️")]
                    )
                except Exception:
                    pass
            return
        else:
            try:
                await context.bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    reaction=[ReactionTypeEmoji("❓")]
                )
            except Exception:
                pass
            return

    # Helper to extract post components (file_id/text, type, inline buttons/markup, and captions)
    def extract_post_data(m):
        file_id, m_type, caption = None, None, m.caption_html or m.caption
        markup = m.reply_markup

        if m.photo:
            file_id = m.photo[-1].file_id
            m_type = "photo"
        elif m.video:
            file_id = m.video.file_id
            m_type = "video"
        elif m.animation:
            file_id = m.animation.file_id
            m_type = "animation"
        elif m.audio:
            file_id = m.audio.file_id
            m_type = "audio"
        elif m.voice:
            file_id = m.voice.file_id
            m_type = "voice"
        elif m.document:
            file_id = m.document.file_id
            m_type = "document"
        elif m.text:
            file_id = m.text_html or m.text
            m_type = "text"
        return file_id, m_type, markup, caption

    # 2. Admin Broadcast input handler (active during /broadcast session)
    if is_admin(update) and uid in broadcast_pending:
        file_id, m_type, markup, caption = extract_post_data(msg)
        if file_id and m_type:
            await _add_item_to_broadcast(uid, chat_id, context, file_id, m_type, markup=markup, caption=caption)
            return

    # 3. Admin Batch Creation input handler (active during /get_link session)
    if is_admin(update) and uid in pending:
        file_id, m_type, markup, caption = extract_post_data(msg)
        if file_id and m_type:
            await _add_item_to_batch(uid, chat_id, context, file_id, m_type, markup=markup, caption=caption)
            return

    # 4. Regular user handling code lookup or direct messaging the admin
    # chat_handlers.py (Inside handle_incoming_message, replace section 4 for regular users)

    # 4. Regular user handling code lookup or forwarding messages/posts with buttons to admin
    if not is_admin(update):
        if msg.text:
            text_stripped = msg.text.strip()
            conn = db()
            try:
                row = conn.execute("SELECT code FROM batches WHERE code = ?", (text_stripped,)).fetchone()
            finally:
                conn.close()

            if row is not None:
                await deliver_batch(update, context, text_stripped)
                return

        username = user.username or ""
        first_name = user.first_name or "User"
        display_name = f"@{username}" if username else first_name

        conn = db()
        try:
            last_thread = conn.execute(
                "SELECT admin_msg_id, last_updated FROM chat_threads WHERE user_id = ?", 
                (uid,)
            ).fetchone()

            now = int(time.time())
            header_sent_id = None

            if not last_thread or (now - last_thread["last_updated"] > 300):
                header_text = f"💬 msg from {display_name} / <code>{uid}</code>"
                header_sent = await context.bot.send_message(chat_id=ADMIN_ID, text=header_text, parse_mode="HTML")
                header_sent_id = header_sent.message_id
                conn.execute(
                    "REPLACE INTO chat_threads (user_id, username, first_name, admin_msg_id, last_updated) VALUES (?, ?, ?, ?, ?)",
                    (uid, username, first_name, header_sent_id, now)
                )
            else:
                header_sent_id = last_thread["admin_msg_id"]

            # Use copy_message to safely forward media, captions, and inline buttons intact
            copied_msg = await context.bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=msg.reply_markup  # Keeps inline buttons attached
            )

            conn.execute("INSERT OR REPLACE INTO chat_mapping (admin_message_id, user_id) VALUES (?, ?)", (header_sent_id, uid))
            conn.execute("INSERT OR REPLACE INTO chat_mapping (admin_message_id, user_id) VALUES (?, ?)", (copied_msg.message_id, uid))
            
            conn.execute(
                "REPLACE INTO user_msg_mapping (user_message_id, user_id, admin_message_id) VALUES (?, ?, ?)",
                (msg.message_id, uid, copied_msg.message_id)
            )
            conn.commit()
        finally:
            conn.close()
        return

async def on_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    if reaction_update.chat.id != ADMIN_ID:
        return

    admin_msg_id = reaction_update.message_id
    new_reactions = reaction_update.new_reaction
    if not new_reactions:
        return

    conn = db()
    try:
        mapping = conn.execute(
            "SELECT user_id, user_message_id FROM user_msg_mapping WHERE admin_message_id = ?",
            (admin_msg_id,)
        ).fetchone()
    finally:
        conn.close()

    if mapping:
        target_user_id = mapping["user_id"]
        target_user_msg_id = mapping["user_message_id"]
        emoji_to_set = None

        for r in new_reactions:
            if hasattr(r, "emoji") and r.emoji:
                emoji_to_set = r.emoji
                break

        if emoji_to_set:
            try:
                await context.bot.set_message_reaction(
                    chat_id=target_user_id,
                    message_id=target_user_msg_id,
                    reaction=[ReactionTypeEmoji(emoji_to_set)]
                )
            except Exception as e:
                log.error(f"Failed to sync reaction to user {target_user_id}: {e}")