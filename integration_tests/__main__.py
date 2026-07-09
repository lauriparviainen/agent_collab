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
    parser.add_argument("provider", nargs="?", choices=sorted(harness.PROVIDERS))
    parser.add_argument("backend", nargs="?", choices=sorted(harness.BACKENDS))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    env_providers = _csv(os.environ.get("AGENT_COLLAB_IT_PROVIDERS", ""))
    env_backends = _csv(os.environ.get("AGENT_COLLAB_IT_BACKENDS", ""))
    providers = [args.provider] if args.provider else env_providers or None
    backend_ids = [args.backend] if args.backend else env_backends or None
    explicit = providers or []
    strict = args.strict or os.environ.get("AGENT_COLLAB_IT_STRICT") == "1"
    harness.configure(providers, backend_ids, strict=strict, explicit_providers=explicit)

    suite = unittest.defaultTestLoader.discover(
        str(Path(__file__).parent / "backends"),
        pattern="test_live.py",
        top_level_dir=str(Path(__file__).resolve().parents[1]),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.failures or result.errors:
        return 1
    if strict and any(str(reason).startswith("[strict-missing]") for _test, reason in result.skipped):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
