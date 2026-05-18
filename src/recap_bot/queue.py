import asyncio
import logging

from recap_bot.pipeline.orchestrator import run_job

logger = logging.getLogger(__name__)


class JobQueue:
    """FIFO queue of channel_ids waiting to run their recap pipeline."""

    def __init__(self, bot):
        self.bot = bot
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.current_channel_id: int | None = None
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())
            logger.info("Job queue worker started")

    async def enqueue(self, channel_id: int):
        await self.queue.put(channel_id)
        logger.info("Enqueued recap for channel %s", channel_id)

    async def _worker(self):
        while True:
            channel_id = await self.queue.get()
            self.current_channel_id = channel_id
            try:
                await run_job(self.bot, channel_id)
            except Exception:
                logger.exception("Recap on channel %s crashed", channel_id)
            finally:
                self.current_channel_id = None
                self.queue.task_done()
