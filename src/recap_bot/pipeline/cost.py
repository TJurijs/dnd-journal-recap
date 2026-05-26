"""Track Gemini API usage and estimate cost — model-aware.

Each API call is tagged with the model that produced it, and cost is computed
from a per-model price table (prices.yaml). This replaces the old behaviour of
applying a single flat (Gemini Flash) rate to every call, which badly
under-counted the pricier Pro calls (summarize, roster/scratchpad build).

prices.yaml is keyed by MODEL NAME, so switching a step's model in models.yaml
automatically picks up that model's price here — as long as it's listed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Fallback price (USD per 1M tokens) for any model absent from prices.yaml.
# Intentionally the historical Gemini Flash rate so an unconfigured model
# degrades to the old behaviour rather than reporting $0.
_DEFAULT_INPUT_PER_M = 0.15
_DEFAULT_OUTPUT_PER_M = 0.60


class PriceTable:
    """Per-model token prices loaded from prices.yaml. USD per 1M tokens."""

    def __init__(self, path: Path = Path("prices.yaml")):
        # Each entry: {"input", "output", optional "long_threshold",
        # "input_long", "output_long"}. The _long rates kick in when a call's
        # input token count exceeds long_threshold (some models, e.g. Gemini
        # Pro, charge more for prompts over 200k tokens).
        self._prices: dict[str, dict] = {}
        self._default = (_DEFAULT_INPUT_PER_M, _DEFAULT_OUTPUT_PER_M)
        self._warned: set[str] = set()
        if not path.exists():
            logger.warning("%s not found — all models priced at default rate", path)
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("Failed to parse %s; using default prices", path)
            return
        for model, p in (data.get("prices") or {}).items():
            try:
                entry = {"input": float(p["input"]), "output": float(p["output"])}
                if p.get("long_threshold") is not None:
                    entry["long_threshold"] = int(p["long_threshold"])
                    entry["input_long"] = float(p.get("input_long", entry["input"]))
                    entry["output_long"] = float(p.get("output_long", entry["output"]))
                self._prices[model] = entry
            except (KeyError, TypeError, ValueError):
                logger.warning("Bad price entry for %r in %s; skipping", model, path)
        d = data.get("default") or {}
        try:
            self._default = (
                float(d.get("input", _DEFAULT_INPUT_PER_M)),
                float(d.get("output", _DEFAULT_OUTPUT_PER_M)),
            )
        except (TypeError, ValueError):
            pass

    def rates(self, model: str, input_tokens: int = 0) -> tuple[float, float]:
        """(input_per_1M, output_per_1M) for `model` at this call's prompt size.

        Applies the model's high-token-count tier when `input_tokens` exceeds
        its `long_threshold`. Falls back to the default rate for unknown models.
        """
        entry = self._prices.get(model)
        if entry is None:
            if model and model not in self._warned:
                self._warned.add(model)
                logger.warning(
                    "No price listed for model %r in prices.yaml — using default rate %s",
                    model, self._default,
                )
            return self._default
        threshold = entry.get("long_threshold")
        if threshold is not None and input_tokens > threshold:
            return (entry["input_long"], entry["output_long"])
        return (entry["input"], entry["output"])


# Module-level table, loaded once at import (like model_config).
price_table = PriceTable()


def _fmt_usd(c: float) -> str:
    if c < 0.001:
        return f"~${c:.6f}"
    if c < 0.01:
        return f"~${c:.4f}"
    return f"~${c:.2f}"


class UsageInfo:
    """Token usage from one API call, tagged with the model that produced it."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0, model: str = ""):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        """Cost in USD using THIS call's model price + prompt-size tier."""
        in_rate, out_rate = price_table.rates(self.model, self.input_tokens)
        return (self.input_tokens * in_rate + self.output_tokens * out_rate) / 1_000_000

    def __add__(self, other: "UsageInfo") -> "UsageInfo":
        # Used to accumulate usage WITHIN one pipeline step (same model). When
        # the models differ we keep the first non-empty one — accurate
        # cross-model totals must go through CostTracker, which sums per-call
        # cost rather than merging token buckets.
        return UsageInfo(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model=self.model or other.model,
        )

    def format_cost(self) -> str:
        return _fmt_usd(self.cost_usd)


class CostTracker:
    """Aggregate cost across calls.

    Sums each call's cost at ITS OWN model's rate, so a mix of Pro +
    Flash-Lite + Flash calls totals correctly. (The previous version merged all
    tokens into one bucket and applied a single Flash rate, under-counting the
    Pro calls by a large multiple.)
    """

    def __init__(self):
        self._total_cost = 0.0
        self._by_model: dict[str, UsageInfo] = {}

    def add(self, usage: UsageInfo | None) -> None:
        if not usage:
            return
        self._total_cost += usage.cost_usd
        key = usage.model or "(unknown)"
        self._by_model[key] = self._by_model.get(key, UsageInfo(model=usage.model)) + usage

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost

    def format_total(self) -> str:
        return _fmt_usd(self._total_cost)

    def format_breakdown(self) -> str:
        """Per-model 'model: $cost' parts for a detailed cost line. Empty if no usage."""
        if not self._by_model:
            return ""
        return " · ".join(
            f"{m}: {u.format_cost()}" for m, u in sorted(self._by_model.items())
        )


def extract_usage(response, model: str = "") -> UsageInfo | None:
    """Extract token counts from a Gemini API response, tagged with `model`."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    try:
        input_tokens = getattr(meta, "prompt_token_count", 0) or 0
        output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        return UsageInfo(input_tokens=input_tokens, output_tokens=output_tokens, model=model)
    except Exception:
        return None
