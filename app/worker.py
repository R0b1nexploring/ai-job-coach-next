from __future__ import annotations

import logging
import os
import signal
import threading

from app.db import SessionLocal
from app.providers import build_provider
from app.privacy import initialize_journal, replay_deletions
from app.runtime import WorkerHeartbeat, validate_configuration
from app.service import process_batch_once, process_guide_once
from app.worker_v2 import process_batch_v2_once, process_guide_v2_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("coach.worker")


def run_worker(provider, session_factory, stop: threading.Event, *, idle_seconds: float = 1) -> None:
    heartbeat = WorkerHeartbeat(interval=float(os.getenv("COACH_WORKER_HEARTBEAT_SECONDS", "5")))
    heartbeat.start()
    logger.info("worker started provider=%s", provider.name)
    try:
        while not stop.is_set():
            did_work = process_batch_once(session_factory, provider, progress=heartbeat.progressed)
            heartbeat.progressed()
            if stop.is_set():
                break
            did_work = process_guide_once(session_factory, provider) or did_work
            heartbeat.progressed()
            if stop.is_set():
                break
            if os.getenv("COACH_V2_ENABLED") == "1":
                did_work = process_batch_v2_once(session_factory) or did_work
                heartbeat.progressed()
                if stop.is_set():
                    break
                did_work = process_guide_v2_once(session_factory) or did_work
                heartbeat.progressed()
            if not did_work:
                stop.wait(idle_seconds)
    finally:
        heartbeat.close()
        logger.info("worker stopped")


def main() -> None:
    validate_configuration()
    initialize_journal(SessionLocal, bootstrap=True)
    replay_deletions(SessionLocal)
    provider = build_provider()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signal, _frame: stop.set())
    run_worker(provider, SessionLocal, stop)


if __name__ == "__main__":
    main()
