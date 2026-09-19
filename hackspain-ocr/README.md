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
python alberto_erp.py --rapido          # ERP en :8009 (repo del reto)
cd ocr_service && uvicorn main:app --host 127.0.0.1 --port 8000

cargo run --release -- --pdf-dir data/facturas --out outputs/outcomes.jsonl
```

Ver `spec_y_plan.md` para el detalle completo (modelo de datos, contratos,
motor de reglas, resiliencia y plan por bloques).
