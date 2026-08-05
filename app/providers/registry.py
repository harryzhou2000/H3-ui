from __future__ import annotations

import re
from collections.abc import Callable

from app.config import Settings
from app.providers.base import VideoProvider

ProviderFactory = Callable[[Settings], VideoProvider]
PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ProviderRegistry:
    """Code-owned registry for trusted provider factories."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if not PROVIDER_NAME_PATTERN.fullmatch(name):
            raise ValueError("Provider names must be lowercase identifiers")
        if name in self._factories:
            raise ValueError(f"Provider is already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str, settings: Settings) -> VideoProvider:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(self.names) or "none"
            raise ValueError(
                f"Unknown provider {name!r}; registered providers: {available}"
            ) from exc
        provider = factory(settings)
        if not isinstance(provider, VideoProvider):
            raise TypeError(f"Provider factory {name!r} returned an incompatible object")
        return provider

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
