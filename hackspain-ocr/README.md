# hackspain-ocr

Estructura de proyecto según `spec_y_plan.md`. Conciliación a tres bandas
(PDF ↔ ERP ↔ Excel) para decidir `PAGAR` / `NO_PAGAR` / `ESCALAR`.

## Estructura

- `data/` — PDFs de entrada (`facturas/`, `facturas_lote2/`), Excel de contexto,
  snapshot cacheado del ERP (`erp_snapshot.json`) y etiquetas manuales (`golden/`).
- `traces/` — una carpeta por factura con OCR, evidencia y decisión (trazabilidad).
- `outputs/` — `outcomes.jsonl` y `outcomes_lote2.jsonl` (entregables).
- `ocr_service/` — servicio Python (FastAPI + RapidOCR) con `POST /ocr`.
- `src/` — binario Rust: `main.rs` (orquestación) + módulos `domain`, `erp`,
  `ocr`, `excel`, `parser`, `validators`, `reconciler`, `rules`, `obs`.
- `config/reglas.toml` — umbrales y reglas del motor de decisión (punto de
  inyección de la regla nueva del sábado).
- `ui/` — visor HTML estático de trazas (bonus).
- `validate_jsonl.py` — validador de entrega.

## Arranque

```
# 1. Bridge del ERP del reto (servicio externo, puerto 8009 por defecto)
python alberto_erp.py --rapido
#    Si no esta en esa direccion, apunta la URL sin tocar codigo:
#    $env:ERP_BASE_URL = "http://127.0.0.1:8009"   (Windows PowerShell)
#    export ERP_BASE_URL="http://127.0.0.1:8009"   (bash)

# 2. Servicio de OCR (FastAPI + RapidOCR)
cd ocr_service && uvicorn main:app --host 127.0.0.1 --port 8000

# 3. Binario Rust (rutas relativas a la raiz de `hackspain-ocr/`)
cargo run --release -- --pdf-dir data/facturas --out outputs/outcomes.jsonl
```

## Configuración (variables de entorno)

Todas las direcciones y credenciales se leen del entorno: no hay ninguna ruta
de máquina ni contraseña fija en el código.

| Variable | Para qué | Por defecto |
|---|---|---|
| `ERP_BASE_URL` | dirección del bridge del ERP | `http://127.0.0.1:8009` |
| `ERP_USUARIO` / `ERP_CLAVE` | credenciales del bridge (`descargar_erp.py`) | las del reto |
| `OCR_URL` | endpoint del servicio de OCR | `http://127.0.0.1:8000/ocr` |
| `MONGO_URI` | conexión a MongoDB | `mongodb://localhost:27017` |
| `MONGO_ROOT_USER` / `MONGO_ROOT_PASSWORD` / `MONGO_APP_PASSWORD` | Mongo (ver `.env`) | — |

En producción define `ERP_CLAVE` (y las de Mongo): el script avisa por `stderr`
cuando está usando las credenciales por defecto.

Ver `spec_y_plan.md` para el detalle completo (modelo de datos, contratos,
motor de reglas, resiliencia y plan por bloques).
