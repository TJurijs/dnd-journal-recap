"""Unified per-step logging for /initialize and /recap.

Every step emits one line like:
    ctx=recap#42 step=transcribe model=gemini-3.1-flash-lite progress=12/20 cost=~$0.0023 total=~$0.0145

At the end of a run, .total() emits a TOTAL line with elapsed time and cumulative cost.
"""

import logging
import time
from typing import Optional

from recap_bot.pipeline.cost import CostTracker, UsageInfo

logger = logging.getLogger("recap_bot.steps")


class StepLog:
    """Emit unified per-step log lines and a final total.

    One instance per top-level run (one per /initialize call, one per /recap job).
    """

    def __init__(self, context: str, cost_tracker: Optional[CostTracker] = None):
        self.context = context
        self.cost = cost_tracker or CostTracker()
        self._started = time.monotonic()

    def step(
        self,
        name: str,
        *,
        model: Optional[str] = None,
        tool: Optional[str] = None,
        progress: Optional[str] = None,
        usage: Optional[UsageInfo] = None,
        note: Optional[str] = None,
    ) -> None:
        """Log one step event. `usage` is added to the running cost total."""
        if usage:
            self.cost.add(usage)

        parts = [f"ctx={self.context}", f"step={name}"]
        if model:
            parts.append(f"model={model}")
        elif tool:
            parts.append(f"tool={tool}")
        if progress:
            parts.append(f"progress={progress}")
        if note:
            parts.append(f"note={note!r}")
        if usage:
            parts.append(f"cost={usage.format_cost()}")
        parts.append(f"total={self.cost.format_total()}")

        logger.info(
            " ".join(parts),
            extra={
                "ctx": self.context,
                "step": name,
                "model": model,
                "tool": tool,
                "progress": progress,
                "note": note,
                "cost_usd": usage.cost_usd if usage else None,
                "total_usd": self.cost.total.cost_usd,
            },
        )

    def total(self, status: str = "done") -> None:
        elapsed = time.monotonic() - self._started
        logger.info(
            f"ctx={self.context} TOTAL status={status} duration={elapsed:.1f}s total={self.cost.format_total()}",
            extra={
                "ctx": self.context,
                "step": "TOTAL",
                "status": status,
                "duration_s": round(elapsed, 1),
                "total_usd": self.cost.total.cost_usd,
            },
        )
