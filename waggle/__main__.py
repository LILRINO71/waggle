"""python -m waggle off [--for 2h] [--all] | on | status | ...  (the same commands as the `waggle` CLI)"""
import sys

from waggle.cli import main

if __name__ == "__main__":
    sys.exit(main())
