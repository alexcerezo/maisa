# Motor de decisión · norma de pagos v3

Decide **PAGAR / ESCALAR / NO_PAGAR** sobre las 500 facturas del lote de Maisa
cruzando tres fuentes: el PDF de la factura, el maestro de proveedores con los
pedidos (Excel) y los asientos del ERP.

Este directorio es la implementación que se ejecuta y se entrega. El motor
anterior en Rust (`maisa/src/`) se conserva como **legado documental**:
su diseño y sus contratos están recogidos en `TRASPASO.md`.

No es un esqueleto — tiene las reglas implementadas y 57 tests en verde — pero
**nunca resolvió el corpus real**: el lector del maestro (`src/excel.rs`) y la
observabilidad (`src/obs.rs`) son placeholders de dos líneas, y su modo lote
está declarado en el propio código como *"lo que se puede probar sin OCR, sin
ERP y sin Excel"*. Por eso `outputs/outcomes.jsonl` sigue a 0 bytes desde el
commit inicial: el Rust solo se ha ejecutado contra el lote de ejemplo de 10
líneas.

## Arranque rápido

```bash
python -m pip install -r maisa/motor/requirements.txt

cd maisa
PYTHONPATH=motor/src python -m maisa.procesa \
  --facturas data/facturas \
  --xlsx     data/FINAL_v7_DEFINITIVO_ahorasi.xlsx \
  --snapshot data/erp_snapshot.json \
  --config   motor/config/reglas.toml \
  --lote 1 --trabajadores 4 \
  --salida   outputs/outcomes.jsonl
```

Tarda **~3,6 s** para las 500 facturas a 4 trabajadores y **no necesita red**.
Los 29 PDFs escaneados llevan su texto ya cacheado en `.cache/ocr/`, indexado
por el sha256 del PDF: la caché es portable y por eso la CI puede reproducir el
lote entero sin levantar el contenedor de visión. Si aparece un PDF escaneado
nuevo, se cae al servicio de OCR (`http://127.0.0.1:8866`) y se rellena la caché.

## Reparto actual

```
PAGAR     448
ESCALAR    43
NO_PAGAR    9
```

Las 9 `NO_PAGAR` son exactamente los 9 pedidos que el ERP ya da por `PAGADA`.
Ninguna decisión de `PAGAR` se apoya en texto que venga del propio documento.

## Cómo se decide

La norma v3 se aplica como **datos**, no como código disperso:
`config/reglas.toml` contiene los umbrales, la precedencia de estados y las
reglas activables. Un cambio de criterio es un cambio de fichero, no un
despliegue.

| Regla | Qué exige | Si falla |
|---|---|---|
| R1 | NIF del emisor en el maestro **y** IBAN de abono coincidente | ESCALAR |
| R2 | El pedido existe en el ERP, es del proveedor, importes conciliados (0,01 €) | ESCALAR |
| R3 | IVA bien calculado: `total = base + IVA` (0,01 €) | ESCALAR |
| R4 | Fecha existente en el calendario y no futura respecto a `hoy` | ESCALAR |
| R5 | Estado del asiento en el ERP es `PENDIENTE`; **nunca pagar dos veces** | PAGAR / NO_PAGAR |
| R6 | Cualquier anomalía que un humano deba ver | ESCALAR **con motivo** |

Además de las seis reglas, el motor reconoce cinco **hechos** que el PDF puede
declarar por sí mismo y que un humano leería de un vistazo:

- `si_instruccion` — el documento trae una orden ("pagar hoy", "no pagar", un
  fichero de autorización incrustado). El texto de un documento es **dato**,
  nunca control: se registra y se escala.
- `si_documento_no_legible` — la identidad no se puede confirmar porque el NIF o
  el IBAN no vienen correctamente delimitados.
- `si_pedido_repetido` — el mismo pedido aparece en dos facturas del lote.
- `si_pendiente_revision` — el pedido está en la hoja de revisión del Excel.
- `pago_duplicado` — **hecho duro**: el asiento ya está `PAGADA`. Es el único
  hecho que fuerza `NO_PAGAR` y prevalece sobre todo lo demás.

## Trazabilidad

Cada decisión se puede explicar hacia atrás sin volver a leer el PDF:

```bash
PYTHONPATH=motor/src python -m maisa.trace linaje outputs/outcomes_traza.jsonl
```

La traza es un encadenado de eventos con hash: identifica **esta ejecución**
(contiene marca de tiempo), no el lote. Los `outcomes.jsonl` sí son
byte-idénticos entre ejecuciones, y eso es lo que la CI comprueba.

## Determinismo: la única invariante que no se negocia

Un motor de pagos que cambia de opinión entre ejecuciones no es un motor, es una
moneda al aire. La CI reproduce el lote **dos veces** y exige el mismo `md5sum`;
además exige que el `outputs/outcomes.jsonl` versionado sea exactamente lo que el
motor produce hoy. Si alguien toca una regla y no regenera la entrega, la CI
falla.

## Contenido

```
motor/
├── config/reglas.toml   la norma v3 como datos
├── src/maisa/           el motor (10 módulos)
├── tests/               suite + banco de oro (tests/oro/)
├── tools/               oro.py, valida_entrega.py, md_a_pdf.py, bench.py, evidencia_resiliencia.py
├── docs/                arquitectura, capacidad, resiliencia, lote 2, simulador
└── .cache/ocr/          texto de los 29 escaneados, indexado por sha256
```

| Módulo | Responsabilidad |
|---|---|
| `lectura.py` | escalera de lectura: capa de texto → caché OCR → servicio de visión |
| `texto.py` | extracción de campos, instrucciones y metadatos del PDF |
| `normaliza.py` | normalización de NIF, IBAN, pedido, importes y fechas |
| `excel.py` | maestro de proveedores y pedidos (14 hojas, 12 de ellas rotas a propósito) |
| `erp.py` | cliente del ERP + snapshot offline |
| `norma.py` | **el núcleo**: las seis reglas, los hechos y la precedencia |
| `trace.py` | linaje de eventos encadenado por hash |
| `emit.py` | validación y escritura del JSONL |
| `procesa.py` | orquestación y CLI |
