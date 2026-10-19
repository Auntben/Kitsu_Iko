"""Entry script for the packaged .exe (PyInstaller needs a file, not -m)."""

import sys

from kitsu_iko.app import main

if __name__ == "__main__":
    sys.exit(main())
