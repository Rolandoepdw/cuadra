"""Speech-to-text via faster-whisper (CPU, int8, bounded threads).

One transcription at a time (doc 01 §5): whisper must not fight the LLM for CPU.
The model is loaded lazily on first use and kept warm.
"""

import asyncio
import logging
import time

from cuadra import config

log = logging.getLogger("cuadra.stt")

_model = None
_lock = asyncio.Lock()


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info(
            "loading whisper '%s' (cpu/int8, %s threads) — first run downloads it",
            config.WHISPER_MODEL,
            config.WHISPER_THREADS,
        )
        _model = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
            cpu_threads=config.WHISPER_THREADS,
        )
    return _model


def _transcribe_blocking(path: str, initial_prompt: str | None, language: str | None) -> dict:
    model = _load_model()
    started = time.perf_counter()
    segments, info = model.transcribe(
        path,
        language=language or None,
        initial_prompt=initial_prompt,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {
        "text": text,
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "duration_s": round(float(info.duration), 2),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


async def transcribe(path, *, initial_prompt: str | None = None, language: str | None = None) -> dict:
    async with _lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _transcribe_blocking,
            str(path),
            initial_prompt,
            language or config.WHISPER_LANGUAGE,
        )


def build_initial_prompt(conn, *, limit: int = 40) -> str | None:
    """Catalog names + currencies improve brand/number recognition (01 §6.2)."""
    names = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM products WHERE status = 'ACTIVE' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    ]
    pieces = ["Productos: " + ", ".join(names)] if names else []
    pieces.append("Monedas: USD, CUP.")
    return " ".join(pieces) if names else None
