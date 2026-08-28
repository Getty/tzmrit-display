"""PyInstaller entry point - same as the `display-panel` console script."""

import sys

from display_panel.cli import main

if __name__ == "__main__":
    sys.exit(main())
