"""Serve the review API on 127.0.0.1."""

from __future__ import annotations

import argparse

from review_grade.api.http import bind
from review_grade.api.worker import Worker, reviewer_for


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m review_grade.api")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Serve without a review worker.",
    )
    parser.add_argument("--fixture", choices=("finding", "error", "bad"))
    args = parser.parse_args(argv)
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be from 0 to 65535")
    httpd = bind(args.db, args.port)
    # bind() leaves the cap off. Accept tests still accept a 7999-line diff.
    httpd.price_cap = True
    worker = None
    if not args.no_worker:
        worker = Worker(httpd.store, reviewer_for(args.fixture))
        worker.start()
    try:
        httpd.serve_forever()
    finally:
        if worker is not None:
            worker.stop()
        httpd.server_close()
        httpd.store.close()


if __name__ == "__main__":
    main()
