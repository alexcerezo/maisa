"""
Comprueba el enrutado de motores sin tocar la API real.

Pensado para ejecutarse DENTRO del contenedor:

    docker compose exec -T ocr python scripts/check_fallback.py

Se apunta la nube a un host inalcanzable (127.0.0.1:9) para verificar:
  1. `auto`  -> falla rapido y cae al motor local, sin perder la peticion.
  2. `cloud` -> responde 503, sin gastar CPU en el motor local.
  3. `local` -> ni siquiera intenta la red.
  4. El cortacircuitos se abre tras N fallos y deja de pagar la sonda.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Antes de importar el servidor, para que la config lo recoja.
os.environ["OCR_CLOUD_BASE_URL"] = "https://127.0.0.1:9/ocr/jobs"

import sys

sys.path.insert(0, "/app")

from fastapi import HTTPException  # noqa: E402

from app.server import _process, get_cloud  # noqa: E402

SAMPLE = Path("/home/ocr/test_files/test.png")


def main() -> int:
    if not SAMPLE.exists():
        print(f"falta {SAMPLE}")
        return 2

    client = get_cloud()
    print("base_url forzada:", client.config.base_url)
    print("sonda TCP:", client.config.probe_timeout, "s")
    print()

    print("=== 1. engine=auto con la nube inalcanzable ===")
    started = time.perf_counter()
    result = _process(SAMPLE, SAMPLE.name, True, None, None, "auto", False)
    elapsed = time.perf_counter() - started
    print(f"  engine={result['engine']} pedido={result.get('requested_engine')}")
    print(f"  fallback={result.get('fallback')}")
    print(f"  latencia total: {elapsed:.2f}s (la sonda debe cortar en ~5s por intento)")
    print(f"  lineas locales: {len(result.get('lines', []))}")
    print(f"  cortacircuitos: {client.state()}")
    print()

    print("=== 2. engine=cloud con la nube caida ===")
    try:
        _process(SAMPLE, SAMPLE.name, True, None, None, "cloud", False)
        print("  NO lanzo excepcion (mal)")
        return 1
    except HTTPException as exc:
        print(f"  HTTPException status={exc.status_code} detail={exc.detail}")
    print()

    print("=== 3. engine=local (no debe tocar la red) ===")
    started = time.perf_counter()
    result = _process(SAMPLE, SAMPLE.name, True, None, None, "local", False)
    print(
        f"  engine={result['engine']} en {time.perf_counter() - started:.2f}s "
        f"fallback={result.get('fallback')} lineas={len(result.get('lines', []))}"
    )
    print()

    print("=== 4. engine invalido ===")
    try:
        _process(SAMPLE, SAMPLE.name, True, None, None, "nube", False)
        print("  NO lanzo excepcion (mal)")
        return 1
    except HTTPException as exc:
        print(f"  HTTPException status={exc.status_code} detail={exc.detail}")
    print()

    print("=== 5. cortacircuitos tras agotar los fallos ===")
    for _ in range(client.config.max_failures):
        try:
            _process(SAMPLE, SAMPLE.name, True, None, None, "auto", False)
        except HTTPException:
            pass
    print("  estado:", client.state())
    available, reason = client.available()
    print(f"  available={available} reason={reason!r}")
    print()

    print("=== 6. con el circuito abierto, `auto` no debe pagar la sonda ===")
    started = time.perf_counter()
    result = _process(SAMPLE, SAMPLE.name, True, None, None, "auto", False)
    elapsed = time.perf_counter() - started
    print(f"  engine={result['engine']} en {elapsed:.2f}s fallback={result.get('fallback')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
