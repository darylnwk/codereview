"""Serve the review API on 127.0.0.1. There is no worker thread."""

from __future__ import annotations

import argparse

from review_grade.api.http import bind


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m review_grade.api")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Serve without a review worker. This is the only mode.",
    )
    args = parser.parse_args(argv)
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be from 0 to 65535")
    httpd = bind(args.db, args.port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        httpd.store.close()


if __name__ == "__main__":
    main()
