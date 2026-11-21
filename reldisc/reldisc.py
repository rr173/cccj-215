#!/usr/bin/env python3
"""Standalone launcher so the tool runs from any directory:

    python3 reldisc.py prepare --manifest m.json --workdir w
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reldisc.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
