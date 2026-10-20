"""Single-instance guard for cuadra-core.

Two cores running at once would both long-poll Telegram's `getUpdates` for the
same bot: Telegram delivers each update to only ONE of the connections, so
messages get swallowed unpredictably and never reach the DB. This module makes
a second start fail fast instead of silently stealing updates.

Locking uses an exclusive file handle (``O_EXCL``/``msvcrt.locking``). A stale
lock from a crash is detected by checking whether the recorded PID is alive and
reclaimed automatically.
"""

import logging
import os
import sys
import time

from cuadra import config

log = logging.getLogger("cuadra.instance")

_lock_fh = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock() -> dict:
    try:
        text = config.LOCK_FILE.read_text(encoding="utf-8")
        pid, _, started = text.partition("\n")
        return {"pid": int(pid.strip()), "started_at": started.strip()}
    except Exception:  # noqa: BLE001
        return {}


def acquire() -> dict:
    """Take the single-instance lock.

    Returns ``{"acquired": True, "pid": ...}`` on success. Raises
    ``RuntimeError`` when another live core already holds the lock.
    """
    global _lock_fh
    config.ensure_dirs()

    for attempt in range(2):
        try:
            _lock_fh = open(config.LOCK_FILE, "x", encoding="utf-8")  # create, fail if exists
        except FileExistsError:
            info = _read_lock()
            pid = info.get("pid", 0)
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise RuntimeError(
                    f"another cuadra-core is already running (pid {pid}). "
                    "Stop it first — two cores would both poll Telegram and "
                    "steal each other's messages."
                ) from None
            # Stale lock (dead PID) or unreadable: reclaim it once.
            log.warning("reclaiming stale core lock (pid %s not alive)", pid or "?")
            try:
                config.LOCK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        _lock_fh.write(f"{os.getpid()}\n{time.time()}\n")
        _lock_fh.flush()
        return {"acquired": True, "pid": os.getpid(), "file": str(config.LOCK_FILE)}

    raise RuntimeError(f"could not acquire the core lock at {config.LOCK_FILE}")


def release() -> None:
    """Release the lock (best-effort; called at shutdown)."""
    global _lock_fh
    if _lock_fh is not None:
        try:
            _lock_fh.close()
        except OSError:
            pass
        _lock_fh = None
    try:
        info = _read_lock()
        if info.get("pid") == os.getpid():
            config.LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def holder() -> dict:
    """Diagnostics: the current lock holder (empty when free/stale)."""
    info = _read_lock()
    pid = info.get("pid")
    return {**info, "alive": _pid_alive(pid) if pid else False}
