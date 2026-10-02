#!/usr/bin/env python3
"""`mcp`: the same command line as `lotr`, under its plain name (owner, 2026-10-01: both names work).

One implementation, two names. This file only forwards, so the two can never drift apart.
"""
import os
import runpy
import sys

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.argv[0] = os.path.join(here, "lotr.py")
    runpy.run_path(sys.argv[0], run_name="__main__")
