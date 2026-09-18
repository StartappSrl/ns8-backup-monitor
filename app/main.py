#!/usr/bin/env python3
"""
Combined entrypoint for the backup-monitor container: runs the IMAP poller
in a background thread and serves the Flask web app (login + dashboard)
in the foreground via waitress.

Using a single process (rather than e.g. multiple gunicorn workers) is
deliberate: it guarantees exactly one poller loop runs, avoiding duplicate
IMAP connections / redundant processing that separate worker processes
would cause.
"""

import logging
import os
import threading

from waitress import serve

import poller
import webapp

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backup-monitor.main")


def start_poller_thread():
    t = threading.Thread(target=poller.main, name="imap-poller", daemon=True)
    t.start()
    return t


def main():
    start_poller_thread()
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("Starting web app on 0.0.0.0:%s", port)
    serve(webapp.app, host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
