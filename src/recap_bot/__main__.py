import asyncio
import logging

from recap_bot.bot import run_bot
from recap_bot.config import settings
from recap_bot.logging_setup import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("Starting D&D Recap Bot v%s", "0.1.0")
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
