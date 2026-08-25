"""
webapp_main.py
Entry point for the browser-based UI (replaces `python main.py`, the old
PySide6 desktop app). Starts the local Flask server and opens your
default browser to it.

Usage:
    python webapp_main.py
Then visit http://127.0.0.1:5000 if the browser doesn't open automatically.

Everything runs locally on your machine -- no data leaves your computer
except the outbound calls to Yahoo Finance / news sources that the
pipeline itself makes.
"""

import logging
import threading
import webbrowser

from webapp.server import app

HOST = "127.0.0.1"
PORT = 5000


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    threading.Timer(1.0, _open_browser).start()
    print(f"\nStarting AI Stock Desk at http://{HOST}:{PORT}  (Ctrl+C to stop)\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
