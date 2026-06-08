from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher

from app.bot.handlers import HandlerDeps, setup_handlers
from app.bot.security import AccessControlMiddleware
from app.config import get_settings
from app.database import init_db
from app.health import HealthServer
from app.services.ai_writer import AIWriter
from app.services.autopilot import AutopilotService
from app.services.news_collector import NewsCollector
from app.services.media_manager import MediaManager
from app.services.publisher import Publisher
from app.services.scheduler import create_scheduler
from app.services.source_checker import SourceChecker


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    # Railway-деплой этого проекта работает как worker: бот получает апдейты
    # через Telegram long polling и не обязан быть публичным web-сервисом.
    # Маленький HTTP-сервер оставлен опционально для ручной проверки /health,
    # но railway.json больше не включает обязательный network healthcheck.
    health_server: HealthServer | None = None
    if settings.health_server_enabled:
        health_server = HealthServer(settings.health_host, settings.port, settings.ai_app_name)
        await health_server.start()

    try:
        if not settings.telegram_bot_token or settings.telegram_bot_token == "put_your_telegram_bot_token_here":
            raise RuntimeError("Укажи TELEGRAM_BOT_TOKEN в Railway Variables или .env")
        if settings.require_admin_ids and settings.bot_access_mode in {"admin_only", "private", "closed"} and not settings.admin_ids:
            raise RuntimeError(
                "ADMIN_IDS обязателен для private/personal режима. "
                "Напиши своему боту /start локально/временно, узнай Telegram ID и добавь ADMIN_IDS в Railway Variables."
            )

        logger.info("Initializing PostgreSQL database")
        await init_db()
        logger.info("Database initialized")
    except Exception:
        if health_server is not None:
            await health_server.stop()
        raise

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    access_middleware = AccessControlMiddleware(settings)
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    collector = NewsCollector(settings)
    checker = SourceChecker(settings)
    writer = AIWriter(settings)
    media_manager = MediaManager(settings)
    publisher = Publisher(bot, caption_limit=settings.telegram_caption_limit, media_manager=media_manager, require_channel_verification=settings.require_channel_verification, require_user_admin_on_publish=settings.public_require_user_channel_admin, allowed_channels=settings.allowed_channels, default_format_mode=settings.post_format_default, link_preview_enabled=settings.post_link_preview_enabled)
    autopilot = AutopilotService(settings, bot, collector, checker, writer, publisher)

    dp.include_router(
        setup_handlers(
            HandlerDeps(
                settings=settings,
                bot=bot,
                collector=collector,
                checker=checker,
                writer=writer,
                publisher=publisher,
            )
        )
    )

    scheduler = create_scheduler(publisher, autopilot, settings.app_timezone)
    scheduler.start()

    stop_event = asyncio.Event()

    def _stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    logging.getLogger(__name__).info("ChannelPilot AI bot started")
    polling_task = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
    await stop_event.wait()
    polling_task.cancel()
    scheduler.shutdown(wait=False)
    await bot.session.close()
    if health_server is not None:
        await health_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
