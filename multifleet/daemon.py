"""Fleet daemon entrypoint — usable as `python3 -m multifleet.daemon`.

This module is a stable entrypoint that delegates to `nats_client.main`.
Shipped inside the PyPI package so `pip install multifleet && python3 -m multifleet.daemon`
works without requiring the superrepo checkout.

Prefer over `fleet-nerve` console script in install/bootstrap scripts because
the module form is discoverable and version-locked to the installed package.
"""
from __future__ import annotations

import sys


def main() -> int:
    """Start the fleet daemon. Delegates to nats_client.main."""
    try:
        from multifleet.nats_client import main as _nats_main
    except ImportError as exc:
        print(f"[multifleet.daemon] failed to import nats_client: {exc}", file=sys.stderr)
        return 2
    try:
        result = _nats_main()
        return int(result) if isinstance(result, int) else 0
    except KeyboardInterrupt:
        print("[multifleet.daemon] shutdown requested", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level entrypoint
        print(f"[multifleet.daemon] crashed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
