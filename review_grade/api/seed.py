"""Create the seed user if needed and print one new API key."""

from __future__ import annotations

import argparse

from review_grade.api.store import Store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m review_grade.api.seed")
    parser.add_argument("--db", required=True)
    parser.add_argument("--credits", required=True, type=int)
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        secret = store.seed_account(args.credits)
    finally:
        store.close()
    print(secret)


if __name__ == "__main__":
    main()
