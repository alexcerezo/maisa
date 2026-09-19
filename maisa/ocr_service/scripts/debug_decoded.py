"""Ejecuta decode_streams() de profile_pipeline sobre un PDF y muestra el resultado."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("pp", "/tmp/profile_pipeline.py")
pp = importlib.util.module_from_spec(spec)
sys.modules["pp"] = pp
try:
    spec.loader.exec_module(pp)
except Exception as exc:  # noqa: BLE001
    print("no se pudo cargar el modulo:", exc)
    sys.exit(1)

path = Path(sys.argv[1])
data = path.read_bytes()
decoded, streams, found = pp.decode_streams(data)
print(f"fichero              : {path.name}")
print(f"streams encontrados  : {found}")
print(f"streams descomprimidos: {streams}")
print(f"bytes decodificados  : {len(decoded)}")
print()
print("text_ops :", len(pp.TEXT_OPS.findall(decoded)))
print("curve_ops:", len(pp.CURVE_OPS.findall(decoded)))
print()
print("--- primeros 400 bytes decodificados ---")
print(repr(decoded[:400]))
print()
print("--- contexto de cada coincidencia de operador de texto ---")
for match in list(pp.TEXT_OPS.finditer(decoded))[:10]:
    a = max(0, match.start() - 60)
    print("  ...", repr(decoded[a : match.end() + 20]))
