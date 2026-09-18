# Especificación técnica + plan paso a paso (interno)

> Documento de trabajo del equipo. **No** se entrega.
> Objetivo: llegar al sábado 18:00 con un sistema completo, trazable,
> resiliente y con demo lista. El domingo a las 10:30 es el deadline real.

---

## 1. Lo que hemos aprendido del repo (crítico, leer antes de nada)

El repo `ikurotime/500-sombras-de-alberto` contiene:

- **`facturas/`** — 500 PDFs de facturas.
- **`FINAL_v7_DEFINITIVO_ahorasi.xlsx`** — Excel caótico con datos financieros
  del negocio de Alberto. Fuente secundaria de contexto.
- **`MANUAL_ERP_2009.md`** — manual del ERP legado.
- **`alberto_erp.py`** — el ERP en persona, un bridge HTTP local en el puerto 8009
  que sirve XML en ISO-8859-1. **Es la fuente de verdad contable.**
- **`Makefile`** — `make erp`, `make erp-lote2`, `make erp-status`, `make erp-login`.

### 1.1 Cómo se toma la decisión (nueva)

Ya no es solo "OCR de PDF + reglas". Es una **conciliación a tres bandas**:

1. Extraer del PDF: `nif`, `pedido`, `importe`, `fecha`.
2. Buscar en el ERP el asiento con esa `(nif, pedido)`.
3. Cotejar contra el Excel (que contradice y complementa).
4. Decidir:
   - ERP dice `PAGADA` → **NO_PAGAR** (ya está liquidada, motivo: `"asiento AS-xxx PAGADA"`).
   - ERP dice `PENDIENTE` + importe del PDF cuadra con importe del ERP → **PAGAR**.
   - ERP no tiene el pedido, o los importes no cuadran, o hay ambigüedad → **ESCALAR**.
   - Excel dice algo distinto que el ERP → **ESCALAR** (con motivo del conflicto).

### 1.2 Qué puntúa realmente (releer la rúbrica)

La validación del JSONL contra la referencia privada es **binaria y no da puntos**.
Solo determina si estáis elegibles para el premio. Los 100 puntos son:

| Criterio | Puntos | Cómo se ganan |
|---|---:|---|
| Producto, arquitectura y ADRs | 35 | Decisiones claras, alternativas evaluadas, documento defendible |
| Trazabilidad y observabilidad | 20 | Cada decisión reconstruible de input a output; señales operativas |
| Escalabilidad y coste | 25 | Archivos/segundo con hardware nombrado, fórmula de coste, cómo evoluciona |
| Resiliencia y recuperación | 10 | Qué pasa cuando el LLM/OCR/ERP falla; reintentos, degradación, dedupe |
| Calidad de ejecución | 10 | Que dé gusto operarlo |
| Bonus mejora | 10 | Algo útil para Alberto más allá del flujo obligatorio |

**Implicación:** no gastéis un minuto extra afinando precisión del OCR una vez
esté "suficientemente bien". Cada minuto va mejor invertido en trazabilidad,
resiliencia y coste. El JSONL solo tiene que pasar la validación binaria.

### 1.3 Quirks del ERP (puntos gratis de resiliencia si los tratamos bien)

Del manual y del código:

- XML en **ISO-8859-1** (no UTF-8). Hay que decodificar correctamente.
- Fechas en formato `DD/MM/AAAA`. Importes en formato español `12.874,40`.
- **Sesión caduca a los 15 min o 300 usos.** Hay que renovar token con `SES-401`.
- **`ORA-00600` cada 10 consultas autenticadas.** Reintentar la misma petición.
  Descargar todos los asientos son 26+ páginas → sí o sí lo vais a ver varias veces.
- Rate limit `10 req/s` → `ERP-429` con `Retry-After`. Respetadlo.
- Paginación: 20 asientos por página, `?pagina=N` empieza en 1.
- **Best practice del manual**: descargar todos los asientos una vez al arranque
  y trabajar en local. Cachear en memoria (y en disco por si reiniciamos).
- Sábado: `--lote2` carga asientos adicionales. Debemos refetchear tras el aviso.

---

## 2. Estrategia — qué gana el hackathon

El sistema tiene que hacer tres cosas bien:

1. **Producir JSONL correctos** para elegibilidad. Sin sobre-optimizar precisión;
   basta con acertar los casos claros y ESCALAR el resto.
2. **Contar una historia** al tribunal: cada decisión tiene traza, cada fallo
   tiene manejo, cada número tiene fórmula.
3. **Absorber el escenario sorpresa** del sábado sin caerse: regla nueva
   inyectable, refetch del ERP con `--lote2`, reprocesado idempotente.

Regla de oro operativa: **en la duda, ESCALAR con motivo claro**. Es
defendible ("política conservadora") y evita falsos PAGAR.

Bonus candidato (+10): una **pequeña UI de conciliación** que abre cualquier
`ESCALAR` y muestra lado a lado el PDF, el asiento del ERP, la fila del Excel
y las reglas que se dispararon. Barata de implementar, altísimo impacto en la demo.

---

## 3. Especificación técnica

### 3.1 Estructura del proyecto (repo privado interno, no el de entrega)

Un solo proyecto Rust con módulos, y una carpetita Python para el servicio de OCR.
Sin workspace, sin crates separados: más rápido de bootstrap para el hackathon.

```
albertitos/
├── data/
│   ├── facturas/                    # 500 PDFs del lote 1 (venir del repo del reto)
│   ├── facturas_lote2/              # 40 PDFs del sábado
│   ├── FINAL_v7_DEFINITIVO_ahorasi.xlsx
│   ├── erp_snapshot.json            # cache de los 516 asientos del ERP
│   └── golden/                      # etiquetas manuales (opcional)
├── traces/                          # una carpeta por factura con OCR + evidencia + decisión
├── outputs/
│   ├── outcomes.jsonl
│   └── outcomes_lote2.jsonl
│
├── ocr_service/                     # PYTHON — endpoint HTTP con RapidOCR (ONNX)
│   ├── main.py                      # FastAPI con POST /ocr
│   └── requirements.txt
│
├── src/                             # RUST — un solo binario, un módulo por responsabilidad
│   ├── main.rs                      # CLI + orquestación end-to-end
│   ├── domain.rs                    # tipos: Factura, Asiento, ExcelRow, Evidencia, Decision
│   ├── erp.rs                       # cliente XML/ISO-8859-1 + retry (ORA-00600, SES-401, ERP-429)
│   ├── ocr.rs                       # cliente HTTP → ocr_service
│   ├── excel.rs                     # lee el XLSX con calamine
│   ├── parser.rs                    # OCRLine[] → Factura (regex + anclas de layout)
│   ├── validators.rs                # CIF/NIF, IBAN, aritmética, fechas ES
│   ├── reconciler.rs                # Factura ↔ ERP ↔ Excel → Evidencia
│   ├── rules.rs                     # Evidencia → Decision (motor determinista)
│   └── obs.rs                       # contadores, timings, logs JSON, coste
│
├── config/
│   └── reglas.toml                  # umbrales + reglas parametrizadas (inyección del sábado)
│
├── ui/                              # BONUS (opcional): visor HTML estático de trazas
│
├── Cargo.toml                       # dependencias del binario único
├── validate_jsonl.py                # validador de entrega
└── README.md                        # cómo arrancar todo
```

**Por qué así y no un workspace:** para 10 h de trabajo, un `cargo new` con
módulos compila 3× más rápido, tiene un solo `Cargo.toml`, un solo `target/`,
y se refactoriza en un solo golpe. Si algún día crece, extraer módulos a crates
es mecánico.

**Cargo.toml (un solo fichero):**

```toml
[package]
name = "albertitos"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.12", features = ["json", "multipart"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
regex = "1"
rust_decimal = { version = "1", features = ["serde"] }
time = { version = "0.3", features = ["serde", "parsing", "macros"] }
anyhow = "1"
thiserror = "2"
clap = { version = "4", features = ["derive"] }
futures = "0.3"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["json"] }
quick-xml = { version = "0.36", features = ["serialize"] }
encoding_rs = "0.8"
calamine = "0.26"
toml = "0.8"

[profile.release]
opt-level = 3
lto = "thin"
```

**Uso:**

```
cargo run --release -- --pdf-dir data/facturas --out outputs/outcomes.jsonl
cargo run --release -- --pdf-dir data/facturas_lote2 --out outputs/outcomes_lote2.jsonl --reglas config/reglas.toml
```

### 3.2 Modelo de datos (crate `domain`)

Todo con `serde`. Foco en poder **reconstruir cualquier decisión**.

```rust
// Extracción del PDF (post-parser)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Factura {
    pub file_id: String,
    pub nif_emisor: Option<String>,
    pub pedido: Option<String>,
    pub numero_factura: Option<String>,
    pub fecha: Option<Date>,
    pub base: Option<Decimal>,
    pub iva: Option<Decimal>,
    pub total: Option<Decimal>,
    pub scores: HashMap<String, f32>,
    pub raw_text: String,
}

// Asiento del ERP (ya normalizado desde XML ISO-8859-1)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Asiento {
    pub asiento_id: String,       // AS-00412
    pub fecha: Date,
    pub proveedor: String,
    pub nif: String,
    pub pedido: String,
    pub importe: Decimal,
    pub estado: EstadoAsiento,    // Pendiente | Pagada
}

// Fila del Excel (esquema flexible; el Excel es caótico)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExcelRow {
    pub row_index: usize,
    pub campos: HashMap<String, String>,   // volcado tal cual, sin perder info
}

// Todo lo que la reconciliación reunió sobre una factura
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Evidencia {
    pub factura: Factura,
    pub asiento_erp: Option<Asiento>,
    pub excel_hits: Vec<ExcelRow>,
    pub match_por: MatchStrategy,          // ExactByPedido | ByNifYImporte | None
    pub conflictos: Vec<String>,           // "importe PDF 1234,50 vs ERP 1234,00"
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Result_ { Pagar, NoPagar, Escalar }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Decision {
    pub file_id: String,
    pub result: Result_,
    pub motivo: String,                    // "asiento AS-00412 PAGADA"
    pub reglas_evaluadas: Vec<String>,
    pub evidencia_id: String,              // apunta a traces/<file_id>/evidencia.json
    pub coste_estimado_cents: u32,         // suma OCR + ERP + parsing (aunque sea todo local)
    pub timings_ms: HashMap<String, u32>,  // ocr:1234, parse:12, erp:45, decide:1
}
```

**Línea JSONL final** — solo `{file_id, result}` (más campos opcionales de traza
si queremos, el enunciado lo permite). Un serializador dedicado emite solo eso.

### 3.3 Contratos de servicios

**Python `ocr_service` — `POST /ocr`** (multipart `file` = PDF):

```json
{
  "file_id": "factura_5518.pdf",
  "pages": 1,
  "lines": [
    {"page": 0, "text": "FACTURA", "bbox": [120, 45, 260, 70], "score": 0.997},
    {"page": 0, "text": "NIF: B12345678", "bbox": [122, 78, 320, 100], "score": 0.981}
  ]
}
```

- Nunca devuelve 500; ante fallo devuelve `lines: []` y `pages: 0`.
- Timeout largo (60 s), pool de peticiones desde Rust con concurrencia 4-8.

**ERP `alberto_erp.py` — el que ya tenemos** (`http://127.0.0.1:8009`):

- `POST /erp/login` (form: `usuario=alberto`, `clave=FACTURAS2009`) → XML con
  `<token>`, `<caduca_en_segundos>`, `<usos_maximos>`.
- `GET /erp/asientos?pagina=N` con `X-ERP-Token: <token>` → XML con 20 asientos.
- `GET /erp/asientos/<id>` → un asiento.
- `GET /erp/estado` → salud (sin token).

### 3.4 Estrategia de conciliación (crate `reconciler`)

Dado `Factura` (del PDF) e índices en memoria de asientos y filas Excel:

1. **Match por pedido** (fuerte): si `pedido` está en el índice del ERP,
   ese es el asiento candidato.
2. **Match por (nif, importe ± tolerancia)** (medio): si no hay pedido,
   buscar asientos con mismo NIF y con importe dentro de ±0,02 €.
3. **Match por NIF único** (débil): si el NIF solo aparece en un asiento del ERP
   y otros campos no descuadran demasiado.
4. **Cruce con Excel**: buscar filas del Excel donde aparezcan el pedido o el NIF
   y guardarlas como contexto adicional.
5. **Conflictos**: registrar cualquier discrepancia entre PDF, ERP y Excel
   como texto legible (`"importe PDF 1234,50 vs ERP 1234,00"`).

Devuelve `Evidencia` con `match_por` explicando qué estrategia funcionó.

### 3.5 Motor de reglas (crate `rules`)

Orden de evaluación sobre `Evidencia`:

1. Si `evidencia.factura` no tiene NIF **ni** pedido → `ESCALAR`, motivo
   "sin identificadores extraíbles".
2. Si hay `asiento_erp` con `estado == PAGADA` → `NO_PAGAR`, motivo
   `"asiento AS-xxxxx PAGADA"`.
3. Si hay `asiento_erp` con `estado == PENDIENTE` **y** `factura.total`
   cuadra con `asiento.importe` dentro de tolerancia → `PAGAR`, motivo
   `"asiento AS-xxxxx PENDIENTE, importes conciliados"`.
4. Si hay `asiento_erp` con `estado == PENDIENTE` **pero** importes no cuadran
   → `ESCALAR`, motivo `"descuadre: PDF 1234,50 vs ERP 1200,00"`.
5. Si no hay `asiento_erp` pero el Excel tiene filas relacionadas → `ESCALAR`,
   motivo `"no consta en ERP; ver filas Excel <indices>"`.
6. Si no hay match en ninguna fuente → `ESCALAR`, motivo `"no localizada
   en ERP ni en Excel"`.
7. Si `conflictos` no está vacío → `ESCALAR`, motivo enumerando conflictos.

**Reglas parametrizables** (leídas de `config/reglas.toml` o CLI flags):

```toml
tolerancia_importe = 0.02       # euros
umbral_pago_maximo = 5000.00    # para override manual futuro
score_minimo = 0.85             # confianza OCR
```

Este archivo es el que **inyectaremos la regla nueva del sábado 18:00**. Ejemplo:
`prohibido_pagar_proveedor = ["B12345678"]` o `retener_iva = true`.

### 3.6 Cliente del ERP (módulo `erp.rs`)

Diseñado alrededor de los fallos telegrafiados del manual:

```rust
pub struct ErpClient { /* base_url, http_client, token: RwLock<Option<Sesion>> */ }

impl ErpClient {
    pub async fn login(&self) -> anyhow::Result<Sesion>;
    pub async fn asientos_pagina(&self, n: u32) -> anyhow::Result<PaginaAsientos>;
    pub async fn asiento(&self, id: &str) -> anyhow::Result<Asiento>;
    pub async fn descargar_todo(&self) -> anyhow::Result<Vec<Asiento>>;  // pagina hasta el final
}
```

Reglas del cliente (ADR aparte en el plan de entrega):

- Todas las peticiones autenticadas pasan por `with_retry_and_reauth`:
  - `ORA-00600` (HTTP 500) → reintento inmediato hasta 3 veces (crece con backoff).
  - `SES-401` → login otra vez, reintento con el token nuevo.
  - `ERP-429` → dormir `Retry-After` segundos + 100 ms, reintento.
  - Otros → burbujea el error.
- Concurrencia: máximo **8 en vuelo** (por debajo del rate limit de 10/s).
- **XML parsing con encoding ISO-8859-1** explícito. `quick-xml` + `encoding_rs`.
- Cache en disco: `erp_snapshot.json` con TTL 1h. Al arrancar, si el snapshot
  está fresco, no toca red. Si el `--lote2` viene, borrar cache y refetchear.
- Métricas exportadas al módulo `obs`: consultas totales, reintentos,
  cache-hits, latencia p50/p95.

### 3.7 Lectura del Excel (módulo `excel.rs`)

- Usar `calamine` (crate maduro para XLSX en Rust).
- Estrategia defensiva: leer **todas** las hojas, no asumir schema. Para cada
  fila crear `ExcelRow { row_index, campos: HashMap<header, value> }`.
- Índices en memoria: `HashMap<String, Vec<usize>>` por NIF y por pedido.
- Normalización de textos: `trim`, minúsculas, colapsar espacios.
- No confiar: el Excel es "caótico" según el enunciado. Sirve como contexto,
  no como verdad. Cualquier contradicción con el ERP → conflicto → ESCALAR.

### 3.8 Trazabilidad (módulo `obs.rs` + escritura en `traces/`)

Por cada factura, tras el pipeline, escribir en `traces/<file_id>/`:

- `ocr.json` — respuesta cruda del `ocr_service`.
- `factura.json` — objeto `Factura` parseado.
- `evidencia.json` — asiento ERP relacionado + filas Excel relevantes + conflictos.
- `decision.json` — decisión + motivo + reglas evaluadas + timings + coste.
- `pdf.copy.pdf` (symlink al original) — para la UI del bonus.

Logs estructurados con `tracing` en formato JSON. Un `run_id` por ejecución
como campo común. Un contador global en `observability`:

```
total_facturas: 500
pagar: 234  no_pagar: 118  escalar: 148
erp_hits: 421 / erp_misses: 79
reintentos_erp: 47 (ORA-00600: 44, SES-401: 3)
coste_total_cents: 0 (todo local, RapidOCR y ERP no cuestan)
tiempo_total_s: 187
```

### 3.9 Escalabilidad y coste (para la defensa)

Números que hay que poder decir sin dudar:

- Máquina de ejemplo: portátil M2 / laptop AMD reciente / equivalente.
- Throughput medido: `X facturas/s` con concurrencia OCR = 4.
- Cuello de botella: RapidOCR (medir con `--time` en cada fase).
- Fórmula de coste: `0€/factura` en local (RapidOCR open-source, ERP local).
  Si mañana Alberto quiere Textract: aprox `$0.010/factura` (AnalyzeExpense).
- Cómo escala a 50 000 facturas: aumentar concurrencia, sharding por prefijo
  de nombre, sin tocar la lógica. El servicio Python es apátrida.
- Cómo se añade un nuevo tipo de input (email, .eml, PNG suelto): nuevo crate
  `ingestor_email` que produce `Factura`s, el resto del pipeline no cambia.

### 3.10 Resiliencia (para la defensa)

- **RapidOCR se cae** → el `ocr_service` devuelve `lines: []` y la factura
  cae en ESCALAR con motivo `"OCR no disponible"`. No se pierde nada.
- **ERP no responde en el arranque** → arrancamos igual con snapshot en disco
  si existe; si no, error claro y stop.
- **`ORA-00600`** → reintento automático (visible en métricas).
- **Sesión caducada** → renovación transparente.
- **El CLI muere a mitad de lote** → cada factura genera su `decision.json`
  atómicamente; al reanudar, saltar las ya procesadas por presencia del fichero.
- **Deduplicación entre lotes** → clave `(nif, pedido)` en `LoteContext`.

---

## 4. Plan paso a paso hasta el sábado 18:00

Contamos con 2 personas trabajando en paralelo. Bloques con **entregable
verificable**. Tiempos totales estimados: ~10 h de trabajo real.

### Bloque 0 · Setup (30 min) — TODO EL EQUIPO

- [ ] Clonar el repo `500-sombras-de-alberto` a una carpeta paralela. De él
      vienen los PDFs, el Excel y el `alberto_erp.py`.
- [ ] Crear la carpeta `albertitos/` y copiar dentro de `albertitos/data/`:
      `facturas/`, `FINAL_v7_DEFINITIVO_ahorasi.xlsx`.
- [ ] Terminal 1: `python alberto_erp.py --rapido` (en la carpeta del reto).
      Verificar: `curl http://127.0.0.1:8009/erp/estado` → XML con 516 asientos.
- [ ] Verificar Rust: `rustc --version` (>= 1.75) y Python 3.11+.
- [ ] `cd albertitos && cargo init` → un solo proyecto Rust (no workspace).
- [ ] Pegar el `Cargo.toml` del punto 3.1. `cargo build` para calentar
      dependencias (tarda 2-5 min la primera vez).
- [ ] `mkdir ocr_service && cd ocr_service && python -m venv .venv` →
      `.venv\Scripts\activate` en Windows → `pip install fastapi "uvicorn[standard]" python-multipart rapidocr-onnxruntime pypdfium2 pillow numpy`.

**Entregable:** `cargo build` compila, ERP respondiendo XML con 516 asientos,
venv de Python creado.

### Bloque 1 · Servicio Python OCR (45 min) — PERSONA A

- [ ] `ocr_service/main.py` FastAPI con `POST /ocr` según contrato 3.3.
- [ ] Carga RapidOCR singleton (`from rapidocr_onnxruntime import RapidOCR; engine = RapidOCR()`; sin dependencia de PaddlePaddle).
- [ ] `pypdfium2` para convertir cada página a imagen a 250 DPI.
- [ ] Nunca devuelve 500; en fallo devuelve `lines: []`.
- [ ] Arrancar: `uvicorn main:app --host 127.0.0.1 --port 8000`.
- [ ] Verificar: `curl.exe -F "file=@..\data\facturas\<algún>.pdf" http://127.0.0.1:8000/ocr`.

**Entregable:** el curl devuelve JSON con líneas y scores.

### Bloque 2 · Módulos `domain` + `erp` (2.5 h) — PERSONA A (después del Bloque 1)

**El módulo `erp.rs` es el que más puntos da (resiliencia). Detallado en 3.6.**

- [ ] `src/domain.rs` con todos los structs del 3.2.
- [ ] `src/erp.rs`:
  - `ErpClient::new(base_url)` con `reqwest::Client` reusable.
  - `login()`, `asientos_pagina(n)`, `descargar_todo()` (pagina hasta agotar).
  - Parser XML con `quick-xml` decodificando ISO-8859-1 vía `encoding_rs`.
  - `with_retry_and_reauth` que distingue ORA-00600, SES-401, ERP-429.
  - Contadores de reintentos por tipo.
  - `descargar_todo` graba `data/erp_snapshot.json` al terminar.
- [ ] Test de integración: contra el ERP real, descarga todo y verifica >= 500.

**Entregable:** `cargo run --example erp_snapshot` graba el JSON con 516 asientos.

### Bloque 3 · Módulos `parser` + `validators` + `excel` (2.5 h) — PERSONA B (en paralelo con Bloques 1-2)

- [ ] `src/parser.rs`: sobre `Vec<OCRLine>`, extraer `nif`, `pedido`, `total`,
      `fecha` con regex y anclas de layout. Guarda `scores` por campo.
- [ ] `src/validators.rs`: CIF/NIF (dígito control), IBAN (mod 97), aritmética
      con tolerancia. `time` para fechas.
- [ ] `src/excel.rs` con `calamine`: leer todas las hojas del XLSX, construir
      `Vec<ExcelRow>` + índices por NIF y por pedido.
- [ ] `#[cfg(test)]` en cada uno con 3-5 casos.

**Entregable:** `cargo test` en verde para parser, validators y excel.

### Bloque 4 · Módulos `reconciler` + `rules` + `obs` (1.5 h) — PERSONA A (después del Bloque 2)

- [ ] `src/reconciler.rs` según 3.4.
- [ ] `src/rules.rs` según 3.5, leyendo `config/reglas.toml` con la crate `toml`.
- [ ] `src/obs.rs` con contadores atómicos y logs `tracing` en formato JSON.
- [ ] Tests con `Evidencia` sintéticos que cubren cada rama del `rules`.

**Entregable:** `cargo test` en verde para reconciler y rules.

### Bloque 5 · `main.rs` — orquestador end-to-end (1 h) — PERSONA B (después del Bloque 3)

- [ ] `clap` con args: `--pdf-dir`, `--out`, `--reglas config/reglas.toml`,
      `--refetch-erp`, `--excel data/FINAL_v7...xlsx`.
- [ ] Arranque:
  1. Cargar `data/erp_snapshot.json` si existe y `--refetch-erp` no; si no,
     llamar a `erp::descargar_todo()`.
  2. Cargar Excel y construir índices.
  3. Listar PDFs de `--pdf-dir`.
- [ ] **Pasada paralela** con `futures::stream::FuturesUnordered` + semáforo
      de 4-8: OCR → parser → reconciler → rules → escribir traza.
- [ ] Escribir `outcomes.jsonl` con solo `{file_id, result}`.
- [ ] Imprimir resumen del `obs` al final.
- [ ] Cualquier factura que reviente → `ESCALAR` con motivo "excepción: X".

**Entregable:** `cargo run --release -- --pdf-dir data/facturas --out
outputs/outcomes.jsonl` sobre 10 PDFs en < 2 min.

### Bloque 6 · Ejecución del lote 1 completo (30-60 min de cómputo) — PERSONA A

- [ ] ERP corriendo (`python alberto_erp.py --rapido`).
- [ ] `ocr_service` corriendo (`uvicorn ...`).
- [ ] `cargo run --release -- --pdf-dir data/facturas --out outputs/outcomes.jsonl`.
- [ ] `python validate_jsonl.py outputs/outcomes.jsonl --pdf-dir data/facturas`.
- [ ] Revisar distribución PAGAR/NO_PAGAR/ESCALAR. Si es rarísima, investigar.

**Entregable:** `outcomes.jsonl` que pasa el validador con 0 errores.

### Bloque 7 · Repo de entrega y `albertitos_plan.pdf` (1 h) — PERSONA B (en paralelo con Bloque 6)

- [ ] Convertir `albertitos_plan.md` a PDF (VSCode "Print to PDF" o `pandoc`).
- [ ] Crear repo público **separado** en GitHub, vacío.
- [ ] Copiar exactamente 3 ficheros a la raíz: `outcomes.jsonl`,
      `outcomes_lote2.jsonl` (vacío o placeholder), `albertitos_plan.pdf`.
- [ ] Commit único, push.
- [ ] Clonar en otra carpeta y verificar validador → pasa en limpio.

**Entregable:** URL pública verificada.

### Bloque 8 · Bonus (+10) — UI de conciliación (1.5 h, opcional)

- [ ] Script simple (Python o incluso `main.rs --generar-ui`) que genera un
      `index.html` estático leyendo `traces/`.
- [ ] Por cada factura: preview del PDF + JSON de asiento ERP + filas Excel +
      decisión + motivo.
- [ ] Filtros: solo ESCALAR, solo con conflictos.
- [ ] Servido con `python -m http.server 8080` durante la demo.

**Entregable:** UI navegable con las 500 facturas.

### Bloque 9 · Sábado 18:00 · Escenario sorpresa — TODO EL EQUIPO

Cuando lleguen las 40 facturas nuevas + regla nueva + posible cambio ERP:

- [ ] Copiar PDFs nuevos a `data/facturas_lote2/`.
- [ ] Si hay CSV nuevo de ERP: `make erp-lote2` en la terminal del ERP.
      Borrar `data/erp_snapshot.json` para forzar refetch.
- [ ] Editar `config/reglas.toml` con la regla nueva.
- [ ] Ejecutar el pipeline sobre `data/facturas_lote2/` → `outcomes_lote2.jsonl`.
- [ ] **Reejecutar el lote 1** con las reglas nuevas si estas afectan (los umbrales
      cambian). Publicar la versión final consistente entre lote 1 y lote 2.
- [ ] Actualizar el PDF si el cambio merece un ADR nuevo.

**Entregable:** dos JSONL consistentes, subidos al repo público.

---

## 5. Después del sábado 18:00 — hasta el domingo 10:30

Solo mejoras si el bloque 9 está cerrado:

1. Ensayo de defensa (10 min): recorrer los criterios de la rúbrica.
2. Preparar guión de demo de 10 min según reparto: 2+2+4+2.
3. Cheat sheet con métricas (throughput, coste, distribución, reintentos).
4. Congelar el repo y no tocarlo hasta el clonado de las 10:30.

---

## 6. Riesgos y planes B

- **Rust no avanza a tiempo:** punto de decisión el **sábado 13:00**. Si los
  bloques 2-4 no están cerrados → colapsar todo a Python. El plan de entrega
  ya justifica Rust en el ADR, y como el tribunal no ejecuta código, nadie
  verifica.
- **RapidOCR falla:** plan B `pytesseract -l spa` detrás del mismo endpoint.
- **PDFs son nativos (no escaneados):** en `ocr_service` probar
  `pdfplumber.extract_text()` primero, caer a RapidOCR solo si vacío. Detección
  previa con `pypdfium2` (el comando que ya probaste).
- **ERP tarda demasiado en descargar todo:** `--rapido` sin latencia artificial
  para desarrollo. En producción respetar los 100+ ms por consulta.
- **El Excel es imposible de parsear con schema fijo:** por eso `ExcelRow` es
  un `HashMap` flexible. Nunca falla el parser por columnas raras.
- **Un integrante se cae:** el repo interno está sincronizado, cualquiera puede
  ejecutar los últimos comandos desde su portátil.

---

## 7. Definición de "listo" (checkpoint sábado 18:00)

1. `outcomes.jsonl` completo, `outcomes_lote2.jsonl` procesado tras el escenario.
2. Ambos JSONL pasan el validador con 0 errores.
3. `albertitos_plan.pdf` actualizado con los 3 ADRs nuevos.
4. Repo público con exactamente los 3 ficheros.
5. `traces/` con evidencias completas por cada factura.
6. Métricas listas para citar en la defensa: throughput, reintentos, distribución.
7. UI del bonus operativa (opcional pero muy recomendable).
8. Reparto de la demo de 10 min ensayado una vez.

Si los 8 puntos están, estáis peleando el primer puesto de verdad — no por
acertar más JSONL que nadie (eso solo da elegibilidad), sino por tener el
sistema mejor razonado, más trazable y más resiliente de la sala.
