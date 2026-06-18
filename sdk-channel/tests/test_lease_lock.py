#!/usr/bin/env python3
"""
Cross-process lease lock behavior test — Python fcntl.flock side.

Companion to ariake/tests/test_lease_lock.ts (the Bun bun:ffi flock side).
Does NOT import sdk-channel/server.py (would spin up FastAPI + the SDK client).
Uses fcntl.flock directly with the same flags sdk-channel/server.py uses for
_try_acquire_lease_lock / _release_lease_lock.

Standalone holder for the Bun-side driver's Step 4 (Python <-> Bun interlock):
    python3 sdk-channel/tests/test_lease_lock.py --hold <seconds>

Prints "acquired" or "denied" on stdout, then "released" after holding the
lock for <seconds>. Exit 0 in all cases.
"""
import argparse
import fcntl
import os
import sys
import time


TEST_LOCK = "/tmp/ariake-test-lease.lock"


def hold(lock_path: str, hold_sec: float) -> int:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("denied", flush=True)
            return 0
        print("acquired", flush=True)
        time.sleep(hold_sec)
        fcntl.flock(fd, fcntl.LOCK_UN)
        print("released", flush=True)
        return 0
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", type=float, required=True)
    args = parser.parse_args()
    lock_path = os.environ.get("ARIAKE_LEASE_LOCK_PATH", TEST_LOCK)
    return hold(lock_path, args.hold)


if __name__ == "__main__":
    sys.exit(main())
