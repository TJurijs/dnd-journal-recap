"""Track Gemini API usage and estimate cost."""

class UsageInfo:
    """Token usage from a single API call."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        """Approximate cost in USD (Gemini 3 Flash pricing)."""
        # $0.15 / 1M input tokens, $0.60 / 1M output tokens
        return (self.input_tokens * 0.15 + self.output_tokens * 0.60) / 1_000_000

    def __add__(self, other: "UsageInfo") -> "UsageInfo":
        return UsageInfo(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def format_cost(self) -> str:
        c = self.cost_usd
        if c < 0.001:
            return f"~${c:.6f}"
        if c < 0.01:
            return f"~${c:.4f}"
        return f"~${c:.2f}"


class CostTracker:
    """Aggregate usage across multiple API calls."""

    def __init__(self):
        self._usage = UsageInfo()

    def add(self, usage: UsageInfo | None) -> None:
        if usage:
            self._usage = self._usage + usage

    @property
    def total(self) -> UsageInfo:
        return self._usage

    def format_total(self) -> str:
        return self._usage.format_cost()


def extract_usage(response) -> UsageInfo | None:
    """Extract token counts from a Gemini API response."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    try:
        input_tokens = getattr(meta, "prompt_token_count", 0) or 0
        output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        return UsageInfo(input_tokens=input_tokens, output_tokens=output_tokens)
    except Exception:
        return None
