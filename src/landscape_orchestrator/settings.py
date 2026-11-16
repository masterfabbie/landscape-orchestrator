"""Configuration from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./landscape.db"
MIN_API_TOKEN_LENGTH = 16


class SettingsError(Exception):
    """The configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    secret_key: str
    api_token: str
    database_url: str = DEFAULT_DATABASE_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        secret_key = env.get("LO_SECRET_KEY", "")
        api_token = env.get("LO_API_TOKEN", "")
        if not secret_key:
            raise SettingsError("LO_SECRET_KEY is not set. Create one with: lo-keygen")
        if len(api_token) < MIN_API_TOKEN_LENGTH:
            raise SettingsError(
                f"LO_API_TOKEN must have at least {MIN_API_TOKEN_LENGTH} characters. "
                "Create one with: lo-keygen"
            )
        return cls(
            secret_key=secret_key,
            api_token=api_token,
            database_url=env.get("LO_DATABASE_URL", DEFAULT_DATABASE_URL),
        )
