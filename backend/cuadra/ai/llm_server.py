"""Supervisor for Cuadra's own local llama.cpp server.

Cuadra ships a standalone ``llama-server`` (see ``scripts/install_llm.ps1``) and
starts/stops it itself, so the bot needs no external LLM app. The model runs on
the GPU and is released when the app stops, freeing VRAM.

CLI (run from ``backend/``):
    python -m cuadra.ai.llm_server status
    python -m cuadra.ai.llm_server models
    python -m cuadra.ai.llm_server start [--model NAME]
    python -m cuadra.ai.llm_server stop
    python -m cuadra.ai.llm_server restart [--model NAME]
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import httpx

from cuadra import config

log = logging.getLogger("cuadra.llm_server")

# Handle for a server we launched in THIS process (a pid file covers the rest).
_process: "subprocess.Popen | None" = None


def _health_url() -> str:
    return f"http://{config.LLM_HOST}:{config.LLM_PORT}/health"


def is_running(timeout: float = 2.0) -> bool:
    """True when a llama.cpp server answers /health on the configured port."""
    try:
        return httpx.get(_health_url(), timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _read_pid() -> dict | None:
    try:
        return json.loads(config.LLM_PID_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write_pid(info: dict) -> None:
    config.LLM_PID_FILE.write_text(json.dumps(info), encoding="utf-8")


def _clear_pid() -> None:
    try:
        config.LLM_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def list_models() -> list[dict]:
    """GGUF models available locally, sorted by name."""
    if not config.LLM_MODELS_DIR.is_dir():
        return []
    return [
        {"name": path.name, "path": str(path), "size_bytes": path.stat().st_size}
        for path in sorted(config.LLM_MODELS_DIR.glob("*.gguf"))
    ]


def resolve_model(name: str | None = None) -> Path:
    """Resolve a model by path, file name or stem; else the configured default."""
    if name:
        candidate = Path(name)
        if candidate.is_file():
            return candidate
        for model in list_models():
            if name in (model["name"], Path(model["name"]).stem):
                return Path(model["path"])
        raise FileNotFoundError(f"model {name!r} not found in {config.LLM_MODELS_DIR}")
    if config.LLM_MODEL_PATH:
        return Path(config.LLM_MODEL_PATH)
    default = config.LLM_MODELS_DIR / config.LLM_MODEL
    if default.exists():
        return default
    models = list_models()
    if models:
        return Path(models[0]["path"])
    raise FileNotFoundError(
        f"no GGUF model found in {config.LLM_MODELS_DIR} "
        "(copy one with scripts/lab/fetch_model.ps1 or set CUADRA_LLM_MODEL_PATH)"
    )


def _build_cmd(model_path: Path) -> list[str]:
    cmd = [
        str(config.LLM_BIN),
        "-m",
        str(model_path),
        "-c",
        str(config.LLM_CTX),
        "-ngl",
        str(config.LLM_GPU_LAYERS),
        "-np",
        "1",
        "-rea",
        "off",
        "--jinja",
        "--no-webui",
        "--host",
        config.LLM_HOST,
        "--port",
        str(config.LLM_PORT),
    ]
    if config.LLM_API_KEY:
        cmd += ["--api-key", config.LLM_API_KEY]
    if config.LLM_EXTRA_ARGS:
        cmd += config.LLM_EXTRA_ARGS.split()
    return cmd


def _spawn(model_path: Path) -> subprocess.Popen:
    config.ensure_dirs()
    log_file = open(config.LLM_LOG_FILE, "a", encoding="utf-8", errors="replace")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        _build_cmd(model_path),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def _wait_ready(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.5)
    return False


def start(model: str | None = None, *, wait: bool = True, timeout: float = 180) -> dict:
    """Start the managed server (idempotent). Returns the status dict."""
    global _process
    if is_running():
        return status()
    if not config.LLM_MANAGE:
        raise RuntimeError("managed LLM is disabled (CUADRA_LLM_MANAGE=0)")
    if not Path(config.LLM_BIN).exists():
        raise FileNotFoundError(
            f"llama-server not found at {config.LLM_BIN} "
            "(run scripts/install_llm.ps1; check your antivirus didn't quarantine it)"
        )
    model_path = resolve_model(model)
    log.info("starting llama-server: %s", model_path.name)
    _process = _spawn(model_path)
    _write_pid(
        {
            "pid": _process.pid,
            "model": model_path.name,
            "path": str(model_path),
            "port": config.LLM_PORT,
            "started_at": time.time(),
        }
    )
    if wait and not _wait_ready(timeout):
        stop()
        raise TimeoutError(
            f"llama-server did not become ready in {timeout:.0f}s "
            f"(see {config.LLM_LOG_FILE})"
        )
    return status()


def stop() -> dict:
    """Stop the managed server and free VRAM (handles orphans from past runs)."""
    global _process
    info = _read_pid()
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _process.kill()
        _process = None
    elif info and info.get("pid"):
        subprocess.run(
            ["taskkill", "/PID", str(info["pid"]), "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    _clear_pid()
    return status()


def restart(model: str | None = None) -> dict:
    stop()
    return start(model)


def ensure_running() -> dict:
    """Start on demand (used at boot). No-op when disabled or already up."""
    if not config.LLM_MANAGE or not config.LLM_AUTOSTART:
        return status()
    if is_running():
        return status()
    try:
        return start()
    except Exception as exc:  # noqa: BLE001 — never block app startup on the model
        log.error("could not start local LLM: %s", exc)
        return status()


def status() -> dict:
    running = is_running()
    info = _read_pid() or {}
    return {
        "provider": config.LLM_PROVIDER,
        "managed": config.LLM_MANAGE,
        "autostart": config.LLM_AUTOSTART,
        "running": running,
        "url": config.LLM_BASE_URL,
        "model": info.get("model") if running else None,
        "pid": info.get("pid") if running else None,
        "bin": str(config.LLM_BIN),
        "bin_present": Path(config.LLM_BIN).exists(),
        "ctx": config.LLM_CTX,
        "gpu_layers": config.LLM_GPU_LAYERS,
        "port": config.LLM_PORT,
    }


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = argv[0] if argv else "status"
    model = None
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]

    actions = {"start": lambda: start(model), "stop": stop, "restart": lambda: restart(model)}
    if command == "models":
        print(json.dumps(list_models(), ensure_ascii=False, indent=2))
        return 0
    if command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if command not in actions:
        print(__doc__)
        return 2
    print(json.dumps(actions[command](), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
