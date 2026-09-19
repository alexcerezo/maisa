# Albertitos · Plan de Entrega

**Equipo (teamId):** YEM9Q8TP
**Repositorio GitHub:** https://github.com/alexcerezo/maisa
**Fecha límite:** Domingo 20, 10:30 (hora de Madrid)

---

## 0. Entregables

Tres ficheros en la raíz del repositorio público, **y nada más**:

1. `outcomes.jsonl` — resultados del lote inicial (500 facturas).
2. `outcomes_lote2.jsonl` — resultados del lote adicional del sábado.
3. `albertitos_plan.pdf` — este documento exportado a PDF.

**Contrato JSONL** (una línea por factura, UTF-8 sin BOM):

```
{"file_id":"factura_123.pdf","result":"PAGAR"}
```

Estados permitidos: `PAGAR`, `NO_PAGAR`, `ESCALAR`. `file_id` es el nombre exacto del PDF (extensión y mayúsculas/minúsculas incluidas). Los campos de traza son opcionales; los omitimos en la entrega para minimizar riesgo de fallos de parser.

---

## 1. Semántica de los estados (definición canónica)

Esta es la interpretación que aplicamos y sobre la que se defiende todo el sistema:

- **PAGAR** → la factura corresponde a un asiento del ERP en estado `PENDIENTE`, con importes conciliados. **Procede pagarla.**
- **NO_PAGAR** → la factura corresponde a un asiento del ERP en estado `PAGADA`. **Ya está liquidada; no volver a pagar.** También cubre duplicados y rectificativas explícitas.
- **ESCALAR** → intervención humana obligatoria. Cubre: la factura no consta en el ERP, los importes no cuadran entre PDF y ERP, hay conflicto entre ERP y Excel, la extracción del PDF es de baja confianza, o falta cualquier identificador crítico (NIF, pedido).

**Política ante la duda: conservadora.** En empate o cuando falta información crítica, la decisión por defecto es `ESCALAR`, nunca `PAGAR`. El coste operativo de una revisión manual es asumible; el coste de un pago erróneo no.

---

## 2. Arquitectura del sistema

### 2.1 Flujo de datos

Alberto trabaja con tres fuentes: **PDFs de facturas**, **un Excel caótico**
con contexto financiero, y **un ERP legado de 2009** que expone un bridge HTTP
local con XML en ISO-8859-1. El sistema concilia las tres fuentes.

```
                     ┌──────────────┐
     PDFs ─OCR──────▶│              │
                     │              │        ┌─────────┐      ┌─────────┐
     Excel ─read────▶│  RECONCILER  │──────▶│ REGLAS  │────▶│DECISIÓN│──▶ JSONL + traza
                     │              │        └─────────┘      └─────────┘
     ERP ──cliente──▶│              │
     bridge          └──────────────┘
     (retry)
```

1. **Ingesta ERP**: al arrancar, el cliente descarga *todos* los asientos del
   bridge (paginación 20/pág.), los cachea en disco y construye índices en memoria
   por `pedido` y por `nif`.
2. **Ingesta Excel**: se lee con lector defensivo (esquema flexible, sin asumir
   columnas). Índices por NIF y pedido.
3. **Extracción PDF**: cada factura pasa por RapidOCR (servicio local),
   devolviendo líneas con texto y *confidence score*.
4. **Parser**: extrae de las líneas los campos canónicos: NIF emisor, número de
   pedido, fecha, base, IVA, total.
5. **Reconciliación**: cruza el PDF contra el ERP (fuente de verdad) y el Excel
   (contexto). Genera una `Evidencia` con el asiento asociado, filas Excel
   relacionadas y conflictos detectados.
6. **Motor de reglas**: aplica los gatillos del 2.3 sobre la `Evidencia`.
7. **Persistencia**: genera la línea JSONL y guarda la traza completa por
   factura (OCR crudo, factura parseada, evidencia, decisión con motivo,
   timings y coste).

### 2.2 Reparto Modelo / Agente / Persona

| Actor | Responsabilidad | Qué **NO** hace |
|---|---|---|
| **Modelo (OCR/IA)** | Traducir píxeles a texto estructurado con *confidence score* por campo. | No decide estado. No aplica reglas de negocio. |
| **Agente (backend)** | Normalizar, validar, aplicar reglas de negocio y asignar `PAGAR`/`NO_PAGAR`/`ESCALAR`. | No corrige lectura del modelo. No inventa datos que faltan. |
| **Persona (operador)** | Revisar todo lo que caiga en `ESCALAR` con el PDF y la traza a la vista. | No toca casos ya clasificados en `PAGAR`/`NO_PAGAR` automáticos. |

### 2.3 Reglas de decisión (evaluadas en orden)

El motor recibe una `Evidencia` (factura parseada + asiento ERP encontrado si
existe + filas Excel relacionadas + conflictos detectados) y evalúa:

1. **Sin identificadores extraíbles** (ni NIF ni pedido en el PDF) → `ESCALAR`.
2. **Asiento ERP en estado `PAGADA`** → `NO_PAGAR`, motivo `"asiento AS-xxxxx PAGADA"`.
3. **Asiento ERP en `PENDIENTE` + importes conciliados** (PDF total ≈ ERP importe,
   tolerancia ±0.02 €) → `PAGAR`, motivo `"asiento AS-xxxxx PENDIENTE, importes conciliados"`.
4. **Asiento ERP en `PENDIENTE` pero descuadre de importes** → `ESCALAR`,
   motivo `"descuadre: PDF 1234,50 vs ERP 1200,00"`.
5. **No hay asiento en ERP pero sí filas relacionadas en Excel** → `ESCALAR`,
   motivo `"no consta en ERP; ver filas Excel <índices>"`.
6. **Sin match en ERP ni en Excel** → `ESCALAR`, motivo `"no localizada en
   ninguna fuente"`.
7. **Cualquier conflicto ERP↔Excel** → `ESCALAR` enumerando conflictos.

**Umbrales parametrizables** en `config/reglas.toml`:

```toml
tolerancia_importe = 0.02       # euros
score_minimo       = 0.85       # confianza OCR mínima por campo crítico
# reglas que Alberto puede añadir el sábado:
# prohibido_pagar_proveedor = ["B12345678"]
# retener_iva = true
```

> Las claves de este bloque son las que existen **realmente** en `config/reglas.toml`.
> Si el motor de reglas introduce un umbral nuevo, hay que añadirlo **en los dos
> sitios a la vez**: un nombre que no exista en el fichero no da error, simplemente
> no se aplica, y eso es un fallo silencioso.

Este archivo es el punto de inyección para la **regla nueva del sábado**.

### 2.4 Trazabilidad y observabilidad

Por cada factura, tras el pipeline, escribimos en `traces/<file_id>/`:

- `ocr.json` — salida cruda del servicio de OCR.
- `factura.json` — factura parseada.
- `evidencia.json` — asiento del ERP relacionado + filas Excel + conflictos.
- `decision.json` — decisión, motivo, reglas evaluadas, timings por fase,
  coste estimado.
- Enlace al PDF original (para el visor).

Todo el sistema emite logs estructurados JSON con `run_id` común. El motor de
observabilidad agrega contadores globales: total procesado, distribución
PAGAR/NO_PAGAR/ESCALAR, hits/misses en ERP, reintentos por tipo de error del
ERP (ORA-00600, SES-401, ERP-429), latencia p50/p95 por fase, coste total.

Cualquier decisión es reconstruible: partiendo del `file_id`, cada fichero
en `traces/` explica un paso, y `decision.json` referencia exactamente qué
regla la disparó y qué evidencia había disponible.

### 2.5 Resiliencia y recuperación

- **ERP responde `ORA-00600`** (cada 10 consultas, telegrafiado en el manual):
  el cliente lo trata como error transitorio y reintenta con backoff.
- **Sesión ERP caducada** (`SES-401`, cada 15 min o 300 usos): renovación
  transparente del token, la petición original se reintenta.
- **Rate limit del ERP** (`ERP-429`): respetamos `Retry-After` y bajamos
  concurrencia si se repite.
- **Servicio OCR no disponible**: el pipeline sigue; la factura afectada
  aterriza en `ESCALAR` con motivo `"OCR no disponible"`. **Nunca** se pierde
  una factura por un fallo transitorio.
- **Snapshot ERP en disco**: si el bridge cae, arrancamos con la caché local.
- **Interrupción a mitad de lote**: cada factura escribe su `decision.json`
  atómicamente. Al reanudar, saltamos las ya procesadas.
- **Deduplicación entre lotes**: clave `(nif, pedido)` en `LoteContext`.
- **Regla nueva del sábado**: parametrizada en `config/reglas.toml`; cambiar
  el fichero y reejecutar es suficiente.
- **Cambio de dato del domingo**: se detecta al recargar el snapshot del ERP;
  reejecución idempotente reprocesa lo afectado.

---

## 3. ADRs / Trade-offs

### ADR 1 · ERP como fuente de verdad; conciliación a tres bandas PDF ↔ ERP ↔ Excel

- **Contexto:** hay tres fuentes de datos: PDFs de facturas, un Excel caótico y
  un ERP legado con estado `PENDIENTE`/`PAGADA` por asiento. Cada uno puede
  contradecir a los otros.
- **Alternativas:** (a) decidir solo con el PDF y sus reglas internas;
  (b) decidir cruzando solo PDF y Excel; (c) declarar el ERP como fuente de
  verdad y usar Excel como contexto.
- **Decisión:** (c). El ERP manda: `PAGADA` implica `NO_PAGAR`, `PENDIENTE`
  con importes conciliados implica `PAGAR`. El Excel es solo contexto.
  Cualquier discrepancia sale a `ESCALAR`.
- **Consecuencias aceptadas:** si el ERP tiene un dato erróneo, propagamos el
  error. Compensado por el hecho de que el propio manual del cliente declara
  al ERP como referencia contable oficial.
- **Evidencia:** el manual del ERP dice literalmente "Los datos que sirve el
  bridge son la referencia contable oficial para conciliar". Alberto ya opera
  así hoy. Nuestro sistema respeta esa política.

### ADR 2 · Cliente ERP resiliente con retry para ORA-00600, SES-401 y ERP-429

- **Contexto:** el bridge del ERP de 2009 tiene fallos telegrafiados en su
  propio manual: `ORA-00600` cada 10 consultas autenticadas, sesiones que
  caducan a los 15 min o 300 usos, y rate limit de 10 req/s con `Retry-After`.
  Una descarga completa de asientos (26+ páginas) verá al menos dos ORA-00600.
- **Alternativas:** (a) fallar en el primer error y pedir intervención humana;
  (b) reintentar ciegamente en cualquier fallo; (c) manejar cada código de
  error con la política que su propia documentación indica.
- **Decisión:** (c). Cliente HTTP con `with_retry_and_reauth` que distingue
  cada código: ORA-00600 → reintento con backoff (hasta 3); SES-401 → login
  transparente y reintento; ERP-429 → dormir `Retry-After` + 100 ms; otros
  → burbujea.
- **Consecuencias aceptadas:** latencia total mayor por los reintentos.
  Métricas de reintentos suben. Complejidad extra en el cliente.
- **Evidencia:** el manual literalmente dice "Un cliente que no reintenta no
  llega a la página 26". Nuestro cliente sí llega, sin intervención humana, y
  las métricas del *run* muestran los reintentos como cualquier otra señal
  operativa (número por tipo, tiempo perdido en cada uno).

### ADR 3 · Snapshot local del ERP con refetch bajo demanda

- **Contexto:** el ERP responde con latencia real (~120 ms/consulta) y tiene
  rate limit. Golpearlo por cada factura sería lento y desperdiciaría cuota.
- **Alternativas:** (a) consultar el ERP por cada factura;
  (b) descargar todo al arranque y consultarlo solo bajo cambios explícitos;
  (c) cache con TTL corto.
- **Decisión:** (b). Al arrancar, `descargar_todo` construye
  `data/erp_snapshot.json` y los índices por `nif` y `pedido`. El pipeline
  concilia contra memoria. El sábado, cuando llega `--lote2`, borramos el
  snapshot y refetchamos.
- **Consecuencias aceptadas:** si el ERP cambia entre snapshot y decisión,
  quedamos desactualizados hasta el próximo refetch. Manejado con un flag
  `--refetch-erp` explícito y con la política del sábado.
- **Evidencia:** es exactamente lo que sugiere el propio manual del ERP
  ("descargarse los asientos una vez y trabajar en local es lo que haría
  cualquiera que haya conocido este sistema"). Además reduce el coste por
  factura a prácticamente cero y hace el sistema tolerante a un ERP caído.

### ADR 4 · Motor de reglas determinista + parametrizado por TOML

- **Contexto:** el sábado a las 18:00 Alberto añade una regla nueva. Hay que
  poder aplicarla sin recompilar ni redeployar.
- **Alternativas:** (a) LLM como clasificador final; (b) reglas hardcoded en
  el código; (c) motor determinista con reglas parametrizadas en un fichero
  de configuración.
- **Decisión:** (c). El motor es Rust puro, sin ML. Los parámetros
  (`tolerancia_importe`, `score_minimo`, `prohibido_pagar_proveedor`,
  `retener_iva`, etc.) viven en `config/reglas.toml` y se cargan al arrancar.
  La regla nueva se traduce a entradas en ese fichero.
- **Consecuencias aceptadas:** reglas verdaderamente inesperadas (no
  parametrizables) siguen requiriendo cambio de código. Es un límite honesto.
- **Evidencia:** cada decisión referencia por nombre la regla que la disparó,
  lo que la hace auditable y explicable ante Finanzas. Un LLM clasificador
  sería más flexible pero no defendible ante un descuadre contable.

### ADR 5 · RapidOCR (ONNX) local envuelto en microservicio HTTP; degradación en ESCALAR

- **Contexto:** hay que extraer NIF, pedido, importe y fecha de PDFs con
  soporte de español, sin coste por llamada, y con posibilidad de degradación
  si el motor cae. Además, el equipo trabaja en Windows y necesita una
  instalación limpia sin toolchain de C++.
- **Alternativas:** AWS Textract, Azure Document Intelligence, Google Document
  AI, Mistral OCR, modelos multimodales, PaddleOCR local, RapidOCR local.
- **Decisión:** **RapidOCR** (`rapidocr-onnxruntime`) detrás de un microservicio
  FastAPI local. Usa los modelos PP-OCR convertidos a ONNX; misma familia y
  precisión que PaddleOCR pero sin la dependencia de PaddlePaddle. Nunca
  devuelve 500; ante fallo devuelve `lines: []`. Rust lo consume por HTTP en
  el mismo host.
- **Consecuencias aceptadas:** RapidOCR da `(bbox, texto, score)` por línea,
  no campos estructurados; hay una capa propia de parser con regex y anclas
  de layout. Un `lines: []` fuerza `ESCALAR` para esa factura.
- **Evidencia:** cero coste por factura, ejecución 100% local (sin credenciales
  ni red externa), instalación con un solo `pip install rapidocr-onnxruntime`
  (sin `paddlepaddle`, sin ruedas nativas, sin compilar) — importante en la
  ventana de la hackatón sobre Windows. Si Alberto quiere subir precisión:
  cambiar el servicio a Textract (fórmula de coste `$0.010/factura`) sin
  tocar el resto del pipeline; el contrato JSON es el mismo.

### ADR 6 · Política conservadora ante la duda: en empate, ESCALAR

- **Contexto:** un `PAGAR` erróneo cuesta dinero real; un `ESCALAR` erróneo
  solo cuesta tiempo de operador.
- **Alternativas:** balanceada, agresiva o conservadora.
- **Decisión:** conservadora. En cualquier ambigüedad no resuelta por las
  reglas del 2.3, la decisión por defecto es `ESCALAR`.
- **Consecuencias aceptadas:** más volumen de `ESCALAR` del estrictamente
  necesario, y por tanto más carga humana.
- **Evidencia:** asimetría de coste del error. En un sistema recién
  desplegado, sin histórico de calibración, es la única política defendible
  ante Finanzas.

---

## 4. Escalabilidad y coste

**Coste unitario actual:** 0 € por factura. RapidOCR es open-source y el ERP
es local; no hay llamadas de pago en el pipeline.

**Throughput de ingesta del ERP (medido, portátil consumer):** **516 asientos en
4,8 s ≈ 107 asientos/s**, con 31 peticiones HTTP y paginación de 20. La descarga
se hace **en serie a propósito**: hay que respetar el límite de 10 req/s del
bridge. El cuello de botella es la latencia del ERP legado (≈0,12 s por petición),
no el cómputo local; los `ORA-00600` del bridge se reintentan con backoff sin
perder la página.

**Throughput OCR (por medir):** el servicio `ocr_service/` todavía es un
esqueleto, así que **aún no hay una cifra honesta de facturas/segundo**. Hay que
medirlo sobre `data/facturas` y sustituir este párrafo antes de la defensa.
Cuello de botella previsto: RapidOCR por CPU.

**Cómo escala a 50 000 facturas:** el servicio Python es apátrida; se replica
horizontalmente. El binario Rust reparte por sharding sobre el `file_id`. El
snapshot del ERP se comparte entre workers.

**Cómo evoluciona a nuevos tipos de input** (email, imagen suelta, .eml, hojas
de cálculo): un crate `ingestor_<tipo>` que produce `Factura`s canónicas; el
resto del pipeline (reconciler, rules, observability) no cambia. El contrato
`Factura` es el punto de extensión.

**Cambio a OCR comercial** si Alberto quiere más precisión: sustituir el
microservicio Python por un adaptador a AWS Textract AnalyzeExpense
(≈ $0.010/factura) sin tocar el resto. Coste marginal por 500 facturas:
≈ 5 $. Coste anual estimado para 50 000 facturas: ≈ 500 $/año.

---

## 5. Checklist de entrega

- [ ] Repo público con **solo** `outcomes.jsonl`, `outcomes_lote2.jsonl`, `albertitos_plan.pdf` en la raíz.
- [ ] Cada `file_id` coincide **exactamente** con el nombre del PDF (extensión y mayúsculas/minúsculas).
- [ ] Cada `result` es uno de `PAGAR`, `NO_PAGAR`, `ESCALAR`.
- [ ] Una línea por factura, sin duplicados, sin líneas vacías.
- [ ] JSONL en UTF-8 sin BOM.
- [ ] Cero facturas del lote sin línea correspondiente.
- [ ] Umbrales y reglas del lote 1 idénticos a los del lote 2 (o versión final consistente entre ambos).
- [ ] `teamId` y URL del repo comunicados a la organización antes de las 10:30 del domingo.
