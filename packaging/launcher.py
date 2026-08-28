"""PyInstaller entry point - same as the `tzmrit-display` console script."""

import sys

from tzmrit_display.cli import main

if __name__ == "__main__":
    sys.exit(main())
