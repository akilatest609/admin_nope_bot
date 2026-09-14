# main.py
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
)
from config import BOT_TOKEN, ADMIN_ID, log
from database import init_db
from batch_handlers import (
    start,
    get_link,
    link_stats,
    broadcast_cmd,
    on_rotate_button,
    on_delete_button,
    on_button,
    cleanup_deliveries_job,
)
from chat_handlers import handle_incoming_message, on_message_reaction

async def post_init(app: Application):
    # Register global default commands so they show up universally
    await app.bot.set_my_commands([
        BotCommand("start", "Start / redeem a code"),
        BotCommand("get_link", "Create a new media batch link"),
        BotCommand("link_stats", "View, rotate, or delete your links"),
        BotCommand("broadcast", "Interactive multi-media broadcast to all users"),
    ], scope=BotCommandScopeDefault())
    
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Start / redeem a code"),
                BotCommand("get_link", "Create a new media batch link"),
                BotCommand("link_stats", "View, rotate, or delete your links"),
                BotCommand("broadcast", "Interactive multi-media broadcast to all users"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception as e:
        log.warning(f"Could not set admin commands yet (chat not found): {e}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("link_stats", link_stats))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(MessageHandler(~filters.COMMAND, handle_incoming_message))
    app.add_handler(MessageReactionHandler(on_message_reaction))

    app.add_handler(CallbackQueryHandler(on_rotate_button, pattern=r"^rotate_"))
    app.add_handler(CallbackQueryHandler(on_delete_button, pattern=r"^del_"))
    app.add_handler(CallbackQueryHandler(on_button))

    app.job_queue.run_repeating(cleanup_deliveries_job, interval=30, first=10)

    log.info("Bot starting with modular structure & interactive broadcast session support...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()