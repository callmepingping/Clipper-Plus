"""Entry point for the per-user Task Scheduler login trigger."""
import argparse
import sys

from .engine_lifecycle import ensure_engine_ready
from .errors import AppError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start or reuse the Clipper Plus background engine")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        result = ensure_engine_ready(args.config)
        print(f"Clipper Plus engine {result['state']}.")
        return 0
    except AppError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
