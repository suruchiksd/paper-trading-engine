#!/usr/bin/env python3
"""Generate a Kite login URL or exchange an explicitly supplied request token."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kiteconnect import KiteConnect  # noqa: E402

from paper_trading.market_data import KiteCredentials  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-token", help="request token returned after browser login")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args()
    credentials = KiteCredentials.from_environment(
        args.env_file,
        require_access_token=False,
        require_api_secret=bool(args.request_token),
    )
    kite = KiteConnect(api_key=credentials.api_key)
    if not args.request_token:
        print("Open this URL in a browser and complete Zerodha login:")
        print(kite.login_url())
        print("Then rerun with --request-token TOKEN_FROM_REDIRECT")
        return 0
    session = kite.generate_session(args.request_token, api_secret=credentials.api_secret)
    access_token = session.get("access_token")
    if not access_token:
        raise RuntimeError("Zerodha response did not contain an access token")
    print("Access token generated. Copy it into your local .env; it is not stored automatically:")
    print(f"KITE_ACCESS_TOKEN={access_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
