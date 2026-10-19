"""Bake-off harness for candidate GGUFs.

Runs each candidate through a local llama-server with Cuadra's *real* prompts
(cuadra.ai.prompts) and scores: NLU JSON validity, intent correctness, Cuban
Spanish quality signals, latency and tokens/s, plus VRAM usage.

Usage:
    python scripts/bakeoff.py --model ../models/foo.gguf [--ctx 8192] [--ngl 99]
    python scripts/bakeoff.py --model ../models/foo.gguf --no-server   # reuse :8080
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

import httpx

BACKEND = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

from cuadra.ai import prompts  # noqa: E402  (reuse the real persona/task text)
from cuadra.ai.llm import Intent  # noqa: E402

SERVER_EXE = BACKEND / "tools" / "llamacpp" / "llama-server.exe"
LOGS_DIR = BACKEND / "data" / "logs"

NLU_SCHEMA = {
    "type": "object",
    "properties": {
        "intencion": {
            "type": "string",
            "enum": [
                "consulta_precio",
                "consulta_stock",
                "consulta_dato",
                "comparar",
                "comprar",
                "queja",
                "otro",
            ],
        },
        "producto": {"type": ["string", "null"]},
        "datos_faltantes": {"type": "array", "items": {"type": "string"}},
        "confianza": {"type": "number"},
    },
    "required": ["intencion", "producto", "datos_faltantes", "confianza"],
    "additionalProperties": False,
}

PRODUCTS = {
    "cargador": {
        "id": 1,
        "nombre": "Cargador 20W USB-C",
        "descripcion": "Cargador de pared 20W con puerto USB-C, carga rapida",
        "precio": 12.5,
        "moneda": "USD",
        "comision": 0.5,
        "moneda_comision": "USD",
        "stock": 5,
        "estado": "ACTIVE",
        "zona_entrega": "La Habana",
        "especificaciones": {"potencia_w": 20, "conector": "USB-C"},
    },
    "cable": {
        "id": 2,
        "nombre": "Cable USB-C 1m",
        "descripcion": "Cable USB-C de 1 metro para carga y datos",
        "precio": 3.0,
        "moneda": "USD",
        "comision": 0.5,
        "moneda_comision": "USD",
        "stock": 2,
        "estado": "ACTIVE",
        "zona_entrega": "La Habana",
        "especificaciones": {"largo_m": 1, "conector": "USB-C"},
    },
    "powerbank": {
        "id": 3,
        "nombre": "Power Bank 10000mAh",
        "descripcion": "Bateria portatil 10000mAh con dos puertos",
        "precio": 20.0,
        "moneda": "USD",
        "comision": 1.0,
        "moneda_comision": "USD",
        "stock": 0,
        "estado": "OUT_OF_STOCK",
        "zona_entrega": "La Habana",
        "especificaciones": {"capacidad_mah": 10000},
    },
}

NLU_CASES = [
    {"id": "price_cable", "text": "cuanto vale el cable usb-c?", "product": "cable", "expect": "consulta_precio"},
    {"id": "price_typo", "text": "kuanto esta el kargador d 20w??", "product": "cargador", "expect": "consulta_precio"},
    {"id": "stock_powerbank", "text": "tienen el power bank ese de 10000?", "product": "powerbank", "expect": "consulta_stock"},
    {"id": "buy_cargador", "text": "ok, me llevo dos cargadores. te transfiero ahora", "product": "cargador", "expect": "comprar"},
    {"id": "data_garantia", "text": "el cargador ese trae garantia?", "product": "cargador", "expect": "consulta_dato"},
    {"id": "greeting", "text": "hola buenas, que tal todo", "product": None, "expect": "otro"},
    {
        "id": "compare",
        "text": "cual me conviene mas, el cargador de 20w o el cable?",
        "product": None,
        "candidates": ["cargador", "cable"],
        "expect": "comparar",
    },
]

COMPOSE_CASES = [
    {
        "id": "compose_compare",
        "text": "cual me conviene mas, el cargador de 20w o el cable?",
        "candidates": ["cargador", "cable"],
    },
    {
        "id": "compose_complaint",
        "text": "llevo 3 dias esperando y nadie me responde, esto no es serio",
        "product": "cargador",
    },
]

KNOWN_PRICES = {3.0, 12.5, 20.0}
BOT_WORDS = ("soy una ia", "asistente", "inteligencia artificial", "chatbot", " soy un bot")


def build_messages(case: dict) -> list[dict]:
    parts = [prompts.SYSTEM_PROMPT]
    if case.get("product"):
        parts.append(
            "PRODUCTO EN JUEGO (datos reales; no inventes otros):\n"
            + json.dumps(PRODUCTS[case["product"]], ensure_ascii=False)
        )
    if case.get("candidates"):
        parts.append(
            "PRODUCTOS CANDIDATOS:\n"
            + json.dumps([PRODUCTS[c] for c in case["candidates"]], ensure_ascii=False)
        )
    history = []
    for turn in case.get("history", []):
        history.append({"role": turn["role"], "content": turn["text"]})
    return [{"role": "system", "content": "\n\n".join(parts)}, *history]


def gpu_memory_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def wait_ready(client: httpx.Client, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = client.get("/health")
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def start_server(model: pathlib.Path, ctx: int, ngl: int, port: int, reasoning: str):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"llama-server-{model.stem}.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    cmd = [
        str(SERVER_EXE),
        "-m",
        str(model),
        "-c",
        str(ctx),
        "-ngl",
        str(ngl),
        "-np",
        "1",
        "-rea",
        reasoning,
        "--jinja",
        "--no-webui",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    print("server:", " ".join(cmd))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, creationflags=creationflags)
    return process, log_file, log_path


def chat(client: httpx.Client, messages: list[dict], *, json_schema: bool, max_tokens: int) -> dict:
    payload: dict = {
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "intent", "strict": True, "schema": NLU_SCHEMA},
        }
    started = time.perf_counter()
    response = client.post("/v1/chat/completions", json=payload)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code == 400 and json_schema:
        payload.pop("response_format")
        started = time.perf_counter()
        response = client.post("/v1/chat/completions", json=payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        result = response.json()
        result["_schema_unsupported"] = True
        result["_elapsed_ms"] = elapsed_ms
        return result
    response.raise_for_status()
    result = response.json()
    result["_elapsed_ms"] = elapsed_ms
    return result


def number_tokens(text: str) -> set[float]:
    return {float(n) for n in re.findall(r"\d+(?:[.,]\d+)?", text.replace(",", "."))}


def run_case(client: httpx.Client, case: dict, *, compose: bool) -> dict:
    messages = build_messages(case)
    task = prompts.COMPOSE_INSTRUCTION if compose else prompts.NLU_TASK
    messages = [*messages, {"role": "user", "content": f"{task}\n\nMENSAJE DEL CLIENTE:\n{case['text']}"}]
    data = chat(client, messages, json_schema=not compose, max_tokens=400 if compose else 200)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    timings = data.get("timings") or {}
    record = {
        "id": case["id"],
        "elapsed_ms": data["_elapsed_ms"],
        "tok_s": round(timings.get("predicted_per_second") or 0.0, 1),
        "completion_tokens": timings.get("predicted_n"),
        "schema_unsupported": data.get("_schema_unsupported", False),
        "raw": content,
    }
    if compose:
        text = content.strip()
        verdict = {
            "has_json": "{" in text and "}" in text,
            "bot_word": any(word in text.lower() for word in BOT_WORDS),
            "chars": len(text),
            "prices_mentioned": sorted(number_tokens(text) & KNOWN_PRICES),
            "unknown_prices": sorted(n for n in number_tokens(text) if n not in KNOWN_PRICES and n > 50),
            "text": text,
        }
        record["compose"] = verdict
    else:
        try:
            parsed = json.loads(content)
            parsed_ok = True
        except json.JSONDecodeError:
            parsed, parsed_ok = None, False
        schema_ok = False
        if parsed_ok:
            try:
                Intent.model_validate(parsed)
                schema_ok = True
            except Exception:  # noqa: BLE001
                schema_ok = False
        record["nlu"] = {
            "json_ok": parsed_ok,
            "schema_ok": schema_ok,
            "intent": (parsed or {}).get("intencion"),
            "expected": case["expect"],
            "intent_ok": (parsed or {}).get("intencion") == case["expect"],
            "producto": (parsed or {}).get("producto"),
            "confianza": (parsed or {}).get("confianza"),
        }
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--ctx", type=int, default=8192)
    parser.add_argument("--ngl", type=int, default=99)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reasoning", default="off")
    parser.add_argument("--no-server", action="store_true", help="reuse an already running server")
    parser.add_argument("--wait", type=float, default=300.0)
    args = parser.parse_args()

    model = pathlib.Path(args.model)
    if not model.is_absolute():
        model = (pathlib.Path.cwd() / model).resolve()
    if not model.exists():
        print(f"model not found: {model}")
        return 2

    base_url = f"http://127.0.0.1:{args.port}"
    process = None
    log_file = None
    vram_before = gpu_memory_mib()

    try:
        started_at = time.perf_counter()
        if not args.no_server:
            process, log_file, log_path = start_server(model, args.ctx, args.ngl, args.port, args.reasoning)
        with httpx.Client(base_url=base_url, timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            if not wait_ready(client, args.wait):
                print(f"server did not become ready; see {log_path if process else 'server log'}")
                return 3
            load_ms = int((time.perf_counter() - started_at) * 1000)
            vram_after = gpu_memory_mib()
            print(f"ready in {load_ms} ms. vram used: {vram_before} -> {vram_after} MiB")
            results = {
                "model": model.name,
                "ctx": args.ctx,
                "ngl": args.ngl,
                "reasoning": args.reasoning,
                "vram_before_mib": vram_before,
                "vram_after_mib": vram_after,
                "cases": [],
            }
            for case in NLU_CASES:
                try:
                    record = run_case(client, case, compose=False)
                except (httpx.HTTPError, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
                    record = {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
                    results["cases"].append(record)
                    print(f"[ERR ] {record['id']:<16} {record['error']}")
                    break
                results["cases"].append(record)
                nlu = record["nlu"]
                mark = "OK " if (nlu["json_ok"] and nlu["schema_ok"] and nlu["intent_ok"]) else "FAIL"
                print(
                    f"[{mark}] {record['id']:<16} intent={nlu['intent']!s:<16} exp={nlu['expected']:<16} "
                    f"{record['elapsed_ms']:>5} ms  {record['tok_s']:>5} tok/s"
                )
            else:
                for case in COMPOSE_CASES:
                    try:
                        record = run_case(client, case, compose=True)
                    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                        record = {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
                        results["cases"].append(record)
                        print(f"[ERR ] {record['id']:<16} {record['error']}")
                        break
                    results["cases"].append(record)
                    print(f"[TXT ] {record['id']:<16} {record['elapsed_ms']:>5} ms  {record['tok_s']:>5} tok/s")
                    print(f"        {record['compose']['text'][:280]}")
            results["_load_ms"] = load_ms
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
        if log_file is not None:
            log_file.close()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOGS_DIR / f"bakeoff_{model.stem}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    nlu_records = [c["nlu"] for c in results["cases"] if "nlu" in c]
    print("")
    print(
        f"SUMMARY {model.name}: json_ok={sum(r['json_ok'] for r in nlu_records)}/{len(nlu_records)} "
        f"schema_ok={sum(r['schema_ok'] for r in nlu_records)}/{len(nlu_records)} "
        f"intent_ok={sum(r['intent_ok'] for r in nlu_records)}/{len(nlu_records)}"
    )
    print(f"results -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
