"""CLI entry — start the bridge server."""
from __future__ import annotations

import argparse

from review_mate.server.app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="review-mate", description="review-mate bridge server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    import uvicorn

    # Cap the graceful-shutdown drain: the server holds long-poll requests (/api/activity ~50s,
    # wait_for_* ~25-30s) and websockets open, and uvicorn otherwise waits for every one of them to
    # finish before exiting — so a single SIGINT appears to hang until a second, forceful one. With a
    # short cap, quick in-flight requests still finish but the long-lived ones are force-closed, so
    # one Ctrl-C exits within ~2s. (manager.shutdown() in the lifespan then cancels the background tasks.)
    uvicorn.run(create_app(), host=args.host, port=args.port, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
