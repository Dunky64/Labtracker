#!/usr/bin/env python3
"""Entry point. Run with `python3 -I run.py` so environment variables and the
current directory cannot inject code (the installer's launcher does this)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labtracker.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
