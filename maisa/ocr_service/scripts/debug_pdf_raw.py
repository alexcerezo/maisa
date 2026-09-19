"""Diagnostico crudo de un PDF pequeno: que hay dentro de sus streams."""

import re
import sys
import zlib
from pathlib import Path

path = Path(sys.argv[1])
data = path.read_bytes()
print(f"fichero: {path.name}  {len(data)} B")
print()
print("--- filtros declarados ---")
for match in re.finditer(rb"/Filter\s*(/\w+|\[[^\]]*\])", data):
    print("  ", match.group(0).decode("latin-1"))
print()
print("--- posiciones de 'stream' ---")
for match in re.finditer(rb"stream(\r\n|\r|\n)", data):
    start = match.end()
    end = data.find(b"endstream", start)
    chunk = data[start:end]
    ok_z = ok_r = False
    try:
        zlib.decompress(chunk, zlib.MAX_WBITS)
        ok_z = True
    except Exception:
        pass
    try:
        zlib.decompress(chunk, -zlib.MAX_WBITS)
        ok_r = True
    except Exception:
        pass
    print(f"  pos={match.start():6d} len={len(chunk):7d} zlib={ok_z} raw_deflate={ok_r}")
    print(f"    primeros 120 bytes: {chunk[:120]!r}")
print()
print("--- operadores de texto en el fichero CRUDO ---")
print("  Tj    :", len(re.findall(rb"\bTj\b", data)))
print("  TJ    :", len(re.findall(rb"\bTJ\b", data)))
print("  BT/ET :", len(re.findall(rb"\bBT\b", data)), len(re.findall(rb"\bET\b", data)))
print("  Tf    :", len(re.findall(rb"\bTf\b", data)))
print()
print("--- cadenas entre parentesis (texto literal) ---")
literals = re.findall(rb"\(((?:[^()\\]|\\.){1,60})\)", data)
print(f"  encontradas: {len(literals)}")
for lit in literals[:12]:
    print("   ", lit[:70])
