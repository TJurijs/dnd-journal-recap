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
        usage: "UsageInfo | list[UsageInfo] | None" = None,
        note: Optional[str] = None,
    ) -> None:
        """Log one step event. `usage` is added to the running cost total.

        `usage` may be a single UsageInfo or a list of them (e.g. when a
        single chunk retried on a different model and we want to bill each
        call at its own model's rate). The list is unpacked inside
        CostTracker.add so per-call model tags survive.
        """
        if usage:
            self.cost.add(usage)

        # For the log line's `cost=$X` display, sum tokens to one synthetic
        # UsageInfo. Cost on this synthetic object is approximate when models
        # mix (uses the first model's rate), but the headline `total=$X` from
        # the cost tracker remains accurate.
        if isinstance(usage, list):
            display_usage = UsageInfo()
            for u in usage:
                if u is not None:
                    display_usage = display_usage + u
            if display_usage.total_tokens == 0:
                display_usage = None
        else:
            display_usage = usage

        parts = [f"ctx={self.context}", f"step={name}"]
        if model:
            parts.append(f"model={model}")
        elif tool:
            parts.append(f"tool={tool}")
        if progress:
            parts.append(f"progress={progress}")
        if note:
            parts.append(f"note={note!r}")
        if display_usage:
            parts.append(f"cost={display_usage.format_cost()}")
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
                "cost_usd": display_usage.cost_usd if display_usage else None,
                "total_usd": self.cost.total_cost_usd,
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
                "total_usd": self.cost.total_cost_usd,
            },
        )
