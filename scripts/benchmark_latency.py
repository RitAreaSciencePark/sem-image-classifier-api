#!/usr/bin/env python3
"""Legacy wrapper — use scripts/benchmark_suite.py for paper evaluation."""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    suite = Path(__file__).resolve().with_name("benchmark_suite.py")
    raise SystemExit(subprocess.call([sys.executable, str(suite), *sys.argv[1:]]))
