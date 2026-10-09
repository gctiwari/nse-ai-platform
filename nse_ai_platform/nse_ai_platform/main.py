"""
main.py
Entry point for the desktop application.

On startup:
  1. Runs the full pipeline once (fetch -> analyze -> score -> recommend
     -> paper trade) so the UI opens with fresh data.
  2. Launches the PySide6 dashboard.

Usage:
    python main.py
"""

import logging
from ui.main_window import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    main()
