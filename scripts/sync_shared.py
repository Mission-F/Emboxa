#!/usr/bin/env python3
"""Vendor stable shared sources from the self-hosted reference into the Web build."""
from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = ROOT.parent / "mailbackup"
# The Web catalogue contains auth, administration and public-service strings that
# do not exist in the appliance catalogue, so it is intentionally maintained here.
FILES = ("app/mail_parser.py", "app/imap_adapter.py", "app/static/shared-tokens.css")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    mismatches = []
    for relative in FILES:
        source, target = SELF / relative, ROOT / relative
        if args.check:
            if not target.exists() or digest(source) != digest(target): mismatches.append(relative)
        else:
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
    if mismatches:
        print("Shared sources out of sync: " + ", ".join(mismatches)); return 1
    print("Shared sources verified" if args.check else "Shared sources synchronized"); return 0


if __name__ == "__main__": raise SystemExit(main())
