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

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
