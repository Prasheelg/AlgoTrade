"""Switch the continuous scanner (main.py, what run.bat runs) on and off from the dashboard.

- On: starts `python main.py` as its own process, output in logs/scanner.log, unless a scanner is already
  running (one started with run.bat counts).
- Off: asks the running scanner to quit through the shared database (STOP_KEY). It looks every STOP_POLL_SEC,
  ends a scan in progress at its next step and exits, so a scanner started with run.bat stops too."""
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

from . import health, state

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "scanner.log"
STOP_KEY = "scanner_stop"
STOP_POLL_SEC = 2
START_GRACE_SEC = 60   # a scanner started here counts as "starting" until its first heartbeat, at most this long
_proc: subprocess.Popen | None = None
_started = 0.0
_started_at = ""   # wall clock, to tell this start's heartbeat from an older one
_lock = threading.Lock()


def stop_requested() -> bool:
    return bool(state.get_setting(STOP_KEY, False)[0])


def clear_stop() -> None:
    state.set_setting(STOP_KEY, False)


def _child_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _beat_since_start() -> bool:
    _, at = state.get_setting(health.HEARTBEAT, None)
    return bool(at) and at >= _started_at


def status() -> dict:
    """running: the scanner's heartbeat is fresh; starting: launched here, no heartbeat yet; stopping: asked to
    quit and not gone yet; error: the scanner launched here exited with an error."""
    running = health.scanner_running()
    starting = (not running and _child_alive() and monotonic() - _started < START_GRACE_SEC
                and not _beat_since_start())   # once it has beaten (or said goodbye) it is past starting
    out = {"running": running, "starting": starting, "stopping": running and stop_requested(), "error": None}
    if not running and _proc is not None and _proc.poll() not in (None, 0):
        out["error"] = f"The scanner stopped with an error (exit code {_proc.returncode}); see logs/scanner.log"
    return out


def start() -> dict:
    """Start the scanner unless one is running already."""
    global _proc, _started, _started_at
    with _lock:
        if health.scanner_running() or _child_alive():
            return status()
        clear_stop()
        LOG_FILE.parent.mkdir(exist_ok=True)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        with open(LOG_FILE, "a", encoding="utf-8") as out:
            args = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
            try:   # outlive the dashboard even when it runs inside a job that would end its children with it
                _proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], **args,
                                         creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
            except OSError:
                _proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], **args, creationflags=flags)
        _started, _started_at = monotonic(), datetime.now().isoformat(timespec="seconds")
        log.info("Scanner started from the dashboard (pid %s)", _proc.pid)
    return status()


def stop() -> dict:
    """Ask the running scanner to quit; it does within STOP_POLL_SEC, or at the end of the step it is on."""
    state.set_setting(STOP_KEY, True)
    log.info("Scanner asked to stop from the dashboard")
    return status()
