"""Entry point for the terminal client: `review-mate-tui`."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress

from review_mate.tui.app import HubScreen
from review_mate.tui.client import ViewClient


async def run(base_url: str) -> None:
    client = ViewClient(base_url)
    screen = HubScreen(client)
    app = screen.build()
    stream = asyncio.create_task(client.run(["hub"], screen.invalidate))
    try:
        await app.run_async()
    finally:
        stream.cancel()
        with suppress(asyncio.CancelledError):
            await stream


def main() -> None:
    parser = argparse.ArgumentParser(prog="review-mate-tui",
                                     description="Terminal client for a running review-mate server.")
    parser.add_argument("--url", default="http://127.0.0.1:8765",
                        help="base URL of the review-mate server (default: %(default)s)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
