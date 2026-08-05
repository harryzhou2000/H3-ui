from __future__ import annotations

from app.config import Settings
from app.provider import MiniMaxClient
from app.providers.base import VideoProvider
from app.providers.registry import ProviderRegistry


def default_provider_registry() -> ProviderRegistry:
    """Build the trusted providers shipped with this application."""

    registry = ProviderRegistry()
    registry.register("minimax", MiniMaxClient)
    return registry


def build_provider(
    settings: Settings,
    *,
    name: str = "minimax",
    registry: ProviderRegistry | None = None,
) -> VideoProvider:
    return (registry or default_provider_registry()).create(name, settings)
