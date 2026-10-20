"""Read the model's memory layout from the managed llama.cpp log.

When the server runs with ``-lv 4`` it prints a per-buffer breakdown on load:

    load_tensors:   CPU_Mapped model buffer size =    83.31 MiB
    load_tensors:        CUDA0 model buffer size =  1015.99 MiB
    load_tensors: offloaded 37/37 layers to GPU
    llama_kv_cache:      CUDA0 KV buffer size =  1152.00 MiB
    sched_reserve:      CUDA0 compute buffer size =   112.01 MiB

This tells us how much of the model sits in VRAM vs RAM (partial offload is
normal when the model + KV cache don't fit in the GPU). We parse the LAST load
block so a model switch reflects the current state.
"""

import re
from pathlib import Path

from cuadra import config

# Lines like: "load_tensors:   CPU_Mapped model buffer size =    83.31 MiB"
_RE_BUFFER = re.compile(
    r"(?P<label>[\w./]+)\s+model buffer size\s*=\s*(?P<mib>[\d.]+)\s*MiB"
)
_RE_KV = re.compile(r"(?P<label>[\w./]+)\s+KV buffer size\s*=\s*(?P<mib>[\d.]+)\s*MiB")
_RE_LAYERS = re.compile(r"offloaded\s+(?P<done>\d+)/(?P<total>\d+)\s+layers")
_RE_COMPUTE = re.compile(r"(?P<label>[\w./]+)\s+compute buffer size\s*=\s*(?P<mib>[\d.]+)\s+MiB")


def _is_gpu(label: str) -> bool:
    return label.upper().startswith("CUDA") or label.upper().startswith("GPU")


def _last_load_block(lines: list[str]) -> list[str]:
    """Slice from the last 'loading model' marker to the end."""
    start = 0
    for i, line in enumerate(lines):
        if "load_model" in line and "loading model" in line:
            start = i
    return lines[start:]


def _read_log() -> list[str]:
    path: Path = config.LLM_LOG_FILE
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def parse() -> dict | None:
    """Return {vram_mib, ram_mib, model_mib, kv_mib, offload_pct, layers, ...}.

    ``None`` when the log has no usable load block (server never started, or
    verbosity too low).
    """
    block = _last_load_block(_read_log())
    if not block:
        return None

    vram_model = ram_model = 0.0
    kv_vram = kv_ram = 0.0
    compute_vram = compute_ram = 0.0
    layers = None

    for line in block:
        m = _RE_BUFFER.search(line)
        if m:
            if _is_gpu(m["label"]):
                vram_model += float(m["mib"])
            else:
                ram_model += float(m["mib"])
        m = _RE_KV.search(line)
        if m:
            if _is_gpu(m["label"]):
                kv_vram += float(m["mib"])
            else:
                kv_ram += float(m["mib"])
        m = _RE_COMPUTE.search(line)
        if m:
            if _is_gpu(m["label"]):
                compute_vram += float(m["mib"])
            else:
                compute_ram += float(m["mib"])
        m = _RE_LAYERS.search(line)
        if m:
            layers = {"offloaded": int(m["done"]), "total": int(m["total"])}

    if not layers and vram_model == 0 and ram_model == 0:
        return None

    total = vram_model + ram_model
    return {
        "model_vram_mib": round(vram_model, 1),
        "model_ram_mib": round(ram_model, 1),
        "model_total_mib": round(total, 1),
        "kv_vram_mib": round(kv_vram, 1),
        "kv_ram_mib": round(kv_ram, 1),
        "compute_vram_mib": round(compute_vram, 1),
        "compute_ram_mib": round(compute_ram, 1),
        "vram_total_mib": round(vram_model + kv_vram + compute_vram, 1),
        "ram_total_mib": round(ram_model + kv_ram + compute_ram, 1),
        "offload_pct": round((vram_model / total) * 100, 1) if total else None,
        "layers": layers,
    }
