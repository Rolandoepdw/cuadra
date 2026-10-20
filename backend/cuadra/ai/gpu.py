"""NVIDIA GPU probe (best-effort).

Reads VRAM from ``nvidia-smi`` so the dashboard can show the real effect of
loading/unloading the model. Never raises: returns ``None`` when there is no
NVIDIA GPU, no driver, or nvidia-smi is unavailable.
"""

import logging
import subprocess

log = logging.getLogger("cuadra.gpu")

_SMI = "nvidia-smi"
_QUERY = "name,memory.total,memory.used,memory.free"


def query(timeout: float = 4.0) -> dict | None:
    """Return {name, total_mib, used_mib, free_mib} or None if unavailable."""
    try:
        out = subprocess.run(
            [_SMI, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        log.debug("nvidia-smi unavailable: %s", exc)
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None

    # First GPU, "name, total, used, free"; name may contain commas.
    line = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.rsplit(",", 3)]
    if len(parts) != 4:
        return None
    name, total, used, free = parts
    try:
        return {
            "name": name,
            "total_mib": int(total),
            "used_mib": int(used),
            "free_mib": int(free),
        }
    except ValueError:
        return None
