"""Minimal GGUF header reader — enough to get the trained context length.

We only need the KV metadata block (``n_kv`` pairs after the header). Reading it
lets the dashboard cap the context slider at each model's real maximum without
loading the model.
"""

import struct
from pathlib import Path

_GGUF_MAGIC = 0x46554747  # "GGUF" little-endian

_T_UINT8, _T_INT8, _T_UINT16, _T_INT16, _T_UINT32, _T_INT32 = 0, 1, 2, 3, 4, 5
_T_FLOAT32, _T_BOOL, _T_STRING, _T_ARRAY, _T_UINT64, _T_INT64, _T_FLOAT64 = 6, 7, 8, 9, 10, 11, 12

_SIZES = {
    _T_UINT8: 1,
    _T_INT8: 1,
    _T_UINT16: 2,
    _T_INT16: 2,
    _T_UINT32: 4,
    _T_INT32: 4,
    _T_FLOAT32: 4,
    _T_BOOL: 1,
    _T_UINT64: 8,
    _T_INT64: 8,
    _T_FLOAT64: 8,
}


def _read_string(f) -> str:
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def _read_value(f, vtype: int):
    if vtype == _T_STRING:
        return _read_string(f)
    if vtype == _T_ARRAY:
        elem = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        return [_read_value(f, elem) for _ in range(n)]
    if vtype in _SIZES:
        return f.read(_SIZES[vtype])
    raise ValueError(f"unsupported GGUF value type {vtype}")


def read_metadata(path: Path) -> dict:
    """Return the GGUF KV metadata as {key: value} (bytes for numerics)."""
    with open(path, "rb") as f:
        magic = struct.unpack("<I", f.read(4))[0]
        if magic != _GGUF_MAGIC:
            raise ValueError(f"{path.name}: not a GGUF file")
        struct.unpack("<I", f.read(4))[0]  # version
        struct.unpack("<Q", f.read(8))[0]  # n_tensors
        n_kv = struct.unpack("<Q", f.read(8))[0]
        out: dict = {}
        for _ in range(n_kv):
            key = _read_string(f)
            vtype = struct.unpack("<I", f.read(4))[0]
            out[key] = _read_value(f, vtype)
        return out


def context_max(path: Path) -> int | None:
    """Trained maximum context length advertised by the model, if any.

    Looks for ``<arch>.context_length`` and falls back to any ``*.context_length``.
    """
    try:
        meta = read_metadata(path)
    except (OSError, ValueError, struct.error):
        return None

    arch = meta.get("general.architecture")
    if isinstance(arch, bytes):
        arch = arch.decode("utf-8", "replace")

    candidates: list[int] = []
    for key, value in meta.items():
        if key.endswith(".context_length") and isinstance(value, bytes):
            candidates.append(int.from_bytes(value, "little"))
    if not candidates:
        return None

    if arch:
        preferred = meta.get(f"{arch}.context_length")
        if isinstance(preferred, bytes):
            return int.from_bytes(preferred, "little")
    return max(candidates)
