import fcntl
import logging
import os
import signal
import sys
import time

import config

logger = logging.getLogger("main")

PID_FILE  = config.BASE_DIR / "trading_bot.pid"
LOCK_FILE = config.BASE_DIR / "trading_bot.lock"
_lock_fh  = None  # held open for the lifetime of this process to keep the lock


def acquire_instance_lock():
    """Exit immediately if another bot process is already running.

    Uses an exclusive flock so this is race-condition-free regardless of
    whether the competing process was launched by launchd, start.sh, or
    directly. The lock is held for the lifetime of this process via _lock_fh.
    """
    global _lock_fh
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except IOError:
        print(
            "[trading_bot] Another instance is already running — exiting.",
            flush=True,
        )
        sys.exit(0)


def kill_existing_instance():
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid == os.getpid():
            return
        os.kill(old_pid, 0)  # check if alive
        logger.info(f"Killing existing instance PID {old_pid}…")
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(3)
        try:
            os.kill(old_pid, signal.SIGKILL)  # force-kill if still alive
        except ProcessLookupError:
            pass
        logger.info(f"Previous instance PID {old_pid} terminated.")
    except (ValueError, ProcessLookupError):
        pass  # pid file stale or process already gone


def write_pid():
    PID_FILE.write_text(str(os.getpid()))


def clear_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
