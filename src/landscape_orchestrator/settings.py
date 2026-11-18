"""Configuration from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./landscape.db"


class SettingsError(Exception):
    """The configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_url: str = DEFAULT_DATABASE_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        secret_key = env.get("LO_SECRET_KEY", "")
        if not secret_key:
            raise SettingsError("LO_SECRET_KEY is not set. Create one with: lo-keygen")
        return cls(
            secret_key=secret_key,
            database_url=env.get("LO_DATABASE_URL", DEFAULT_DATABASE_URL),
        )
