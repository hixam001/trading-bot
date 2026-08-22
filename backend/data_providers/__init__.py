from data_providers.base import MarketDataProvider, ProviderError, RateLimitedError
from data_providers.mock import MockProvider


def build_provider():
    """Single selection point (A9) — implemented in data_providers.live."""
    from data_providers.live import build_provider as _build
    return _build()


__all__ = [
    "MarketDataProvider", "ProviderError", "RateLimitedError",
    "MockProvider", "build_provider",
]
