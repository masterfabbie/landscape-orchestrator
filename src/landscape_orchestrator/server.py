"""Entry point of ``lo-server``."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .api import create_app
from .settings import Settings, SettingsError


def main() -> None:
    parser = argparse.ArgumentParser(description="Landscape Orchestrator server")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="connect to the built-in simulated landscape instead of real hosts",
    )
    args = parser.parse_args()

    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        sys.exit(f"Configuration error: {exc}")

    transport = None
    if args.mock:
        from .mock.landscape import MockLandscape, default_landscape_config
        from .mock.transport import MockLandscapeTransport

        transport = MockLandscapeTransport(MockLandscape.from_dict(default_landscape_config()))
        print("Using the simulated landscape. Hosts: sapapp1 (offset 0), sapdb1 (offset 10000).")

    uvicorn.run(create_app(settings, transport=transport), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
