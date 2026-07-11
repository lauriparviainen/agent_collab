"""Command-line runner for credentialed integration tests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import List, Optional
import unittest

from . import harness


def _csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m integration_tests")
    parser.add_argument("backend", nargs="?", choices=sorted(harness.BACKEND_NAMES))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    env_backends = _csv(os.environ.get("AGENT_COLLAB_IT_BACKENDS", ""))
    invalid = sorted(set(env_backends) - harness.BACKEND_NAMES)
    if invalid:
        parser.error(
            f"AGENT_COLLAB_IT_BACKENDS contains unknown backend {invalid[0]!r}; "
            f"expected one of: {', '.join(sorted(harness.BACKEND_NAMES))}"
        )
    backend_names = [args.backend] if args.backend else env_backends or None
    strict = args.strict or os.environ.get("AGENT_COLLAB_IT_STRICT") == "1"
    harness.configure(backend_names, strict=strict)

    suite = unittest.defaultTestLoader.discover(
        str(Path(__file__).parent / "backends"),
        pattern="test_live.py",
        top_level_dir=str(Path(__file__).resolve().parents[1]),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.failures or result.errors:
        return 1
    if strict and any(
        str(reason).startswith("[strict-missing]") for _test, reason in result.skipped
    ):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
