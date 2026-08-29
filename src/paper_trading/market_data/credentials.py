"""Environment-only Kite credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class KiteCredentials:
    api_key: str
    access_token: str | None = None
    api_secret: str | None = None

    @classmethod
    def from_environment(
        cls,
        env_path: str | Path | None = None,
        *,
        require_access_token: bool = True,
        require_api_secret: bool = False,
    ) -> "KiteCredentials":
        load_dotenv(env_path, override=False)
        api_key = os.getenv("KITE_API_KEY", "").strip()
        access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip() or None
        api_secret = os.getenv("KITE_API_SECRET", "").strip() or None
        missing = []
        if not api_key:
            missing.append("KITE_API_KEY")
        if require_access_token and not access_token:
            missing.append("KITE_ACCESS_TOKEN")
        if require_api_secret and not api_secret:
            missing.append("KITE_API_SECRET")
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(missing)}")
        return cls(api_key, access_token, api_secret)
