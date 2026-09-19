# Plataforma web — Maisa / conciliación de facturas

> Documento de diseño para la demo de la hackathon. Define arquitectura,
> stack, contratos de datos y el detalle de cada pantalla. Es la base para
> implementar `ui/` como aplicación React independiente del motor.

---

## 1. Objetivo y contexto

El motor (`motor/`, Python) ya decide `PAGAR` / `NO_PAGAR` / `ESCALAR` sobre
el lote de facturas y deja rastro completo en disco: una línea por factura en
`outputs/outcomes.jsonl` / `outcomes_lote2.jsonl`, y (cuando se activa la
traza completa) un directorio `traces/<file_id>/` con `ocr.json`,
`factura.json`, `evidencia.json` y `decision.json` — ver
`traces/trazabilidad.md`. También existe un `run_summary.json` agregado con
métricas de negocio y rendimiento (`motor/docs/capacidad.md`).

Esta plataforma es la cara visible de ese trabajo para la demo: un perfil
**no técnico** tiene que poder subir facturas, ver cómo se procesan en vivo
(la parte con más impacto en el pitch) y luego revisar/filtrar los
resultados sin tocar una terminal.

**No es** el motor. La plataforma no decide nada: consume lo que el motor
produce. El acoplamiento con el backend real (API + base de datos, hoy en
desarrollo — nombre interno "rapidoc") se deja explícitamente incompleto
detrás de una interfaz, ver §6.

---

## 2. Stack

| Pieza | Elección | Por qué |
|---|---|---|
| Framework | **React 18 + Vite + TypeScript** | Arranque en segundos, sin necesidad de servidor propio (todo habla con una API separada), hot reload instantáneo para iterar la animación durante la demo. |
| Animación | **Framer Motion** | `AnimatePresence` para el feed de logs que aparece/desvanece, `layout` animations para que la tarjeta de factura se desplace de columna sin código de física a mano. |
| Estilos | **Tailwind CSS** | Velocidad para maquetar los 3+1 paneles y el sistema de color por severidad sin escribir CSS a mano. |
| Routing | **react-router-dom** | Dos rutas: `/procesar` y `/facturas`. |
| Estado | **Zustand** (o Context+reducer si se prefiere cero dependencias) | Estado del lote en curso (facturas en cola/procesando/completadas, logs, métricas) compartido entre el monitor y la barra superior. |
| Datos | Capa `src/api/` propia (ver §6), **sin** React Query todavía — YAGNI mientras el backend es un mock; se añade si hace falta caché/reintentos reales. |

No se usa Next.js: no hay SSR ni rutas de servidor que aprovechar, y el
router de servidor añadiría complejidad que esta demo no necesita.

---

## 3. Estructura de carpetas

```
ui/
├── PLATAFORMA.md            # este documento
├── index.html
├── package.json
├── vite.config.ts
├── tailwind.config.ts
├── src/
│   ├── main.tsx
│   ├── App.tsx               # define las rutas
│   ├── api/
│   │   ├── types.ts           # tipos compartidos: Invoice, Decision, Evidencia, Metrics...
│   │   ├── client.ts          # interfaz InvoicesClient (contrato)
│   │   ├── mockClient.ts      # implementación activa hoy
│   │   └── httpClient.ts      # placeholder para la API real (rapidoc + BD)
│   ├── store/
│   │   └── procesamiento.ts   # Zustand: cola, en curso, completadas, logs, métricas en vivo
│   ├── pages/
│   │   ├── ProcesarPage.tsx   # ruta /procesar
│   │   └── FacturasPage.tsx   # ruta /facturas
│   ├── features/
│   │   ├── upload/
│   │   │   └── Dropzone.tsx
│   │   ├── pipeline/
│   │   │   ├── PipelineView.tsx      # las 3 columnas + logs
│   │   │   ├── FacturaActualCard.tsx # panel izquierdo
│   │   │   ├── EtapasPipeline.tsx    # panel central (4 pasos)
│   │   │   ├── LogFeed.tsx           # panel inferior
│   │   │   └── ColumnaCompletadas.tsx # panel derecho
│   │   ├── metrics/
│   │   │   └── MetricsBar.tsx        # barra superior fija con SLO/SLI en vivo
│   │   └── explorer/
│   │       ├── FiltrosFacturas.tsx
│   │       ├── ListaFacturas.tsx
│   │       ├── DetalleFactura.tsx    # PDF a la derecha, datos a la izquierda
│   │       └── PanelMetricasAgregadas.tsx
│   ├── components/            # botones, badges de estado, etc. (design system mínimo)
│   └── lib/
│       └── colorPorResultado.ts
└── public/
```

---

## 4. Modelo de datos (contratos, `src/api/types.ts`)

Se reutilizan **literalmente** los campos que ya emite el motor — no se
inventa un esquema nuevo, así el mock de hoy es intercambiable por la API
real sin tocar componentes.

```ts
export type Resultado = "PAGAR" | "NO_PAGAR" | "ESCALAR";

export interface OutcomeLinea {
  file_id: string;
  result: Resultado;
}

export interface OcrLinea {
  page: number;
  text: string;
  bbox: [number, number, number, number];
  score: number;
}

export interface OcrArtefacto {
  file_id: string;
  engine: string;
  pages: number;
  execution_time_ms: number;
  lines: OcrLinea[];
}

export interface FacturaParseada {
  file_id: string;
  parsed_at: string;
  nif_emisor: string | null;
  cif_cliente: string | null;
  pedido: string | null;
  numero_factura: string | null;
  fecha: string | null;
  base: string | null;
  iva: string | null;
  total: string | null;
  scores: Record<string, number>;
  validaciones: Record<string, boolean>;
}

export interface AsientoErp {
  asiento_id: string;
  proveedor: string;
  nif: string;
  pedido: string;
  importe: string;
  estado: "PENDIENTE" | "PAGADA";
  fecha: string;
}

export interface Evidencia {
  file_id: string;
  match_strategy: "ExactByPedido" | "ByNifYImporte" | "None" | string;
  asiento_erp: AsientoErp | null;
  excel_hits: Array<{ row_index: number; sheet: string; campos: Record<string, string> }>;
  diferencia_importe: string;
  conflictos: string[];
}

export interface Decision {
  file_id: string;
  run_id: string;
  timestamp: string;
  result: Resultado;
  motivo: string;
  regla_aplicada: string;
  reglas_evaluadas: string[];
  timings_ms: Record<string, number>;
  coste_estimado_cents: number;
}

/** Vista combinada que consume la UI: une outcome + trazas de una factura */
export interface FacturaCompleta {
  file_id: string;
  result: Resultado;
  pdfUrl: string;               // ruta o URL del PDF original
  factura: FacturaParseada | null;
  evidencia: Evidencia | null;
  decision: Decision | null;
}

export interface LogEvent {
  timestamp: string;
  level: "INFO" | "WARN" | "ERROR" | "DEBUG";
  component: string;
  event: string;
  file_id: string | null;
  message: string;
}

/** Igual forma que run_summary.json del motor */
export interface RunMetrics {
  run_id: string;
  total_procesadas: number;
  distribucion: Record<Resultado, number>;
  ratio_escalado_pct: number;
  facturas_por_segundo: number;
  latencia_p50_ms: number;
  latencia_p95_ms: number;
  reintentos_ora600: number;
  reautenticaciones_ses401: number;
  coste_total_euros: number;
}
```

---

## 5. Pantalla 1 — `/procesar` (upload + animación en vivo)

Esta es la pantalla que vende la demo. Prioridad: que **se entienda de un
vistazo** qué está pasando, sin leer nada técnico.

### 5.1 Estado vacío

Dropzone centrado, grande, con texto simple ("Arrastra las facturas aquí o
haz clic para elegir archivos"). Sin nada más en pantalla — perfil no
técnico, cero fricción.

Si ya hay resultados de una ejecución previa (detectado vía
`client.hayFacturasProcesadas()`), aparece además un enlace secundario
"Ver facturas ya procesadas →" que lleva directo a `/facturas`, para no
forzar a repetir la animación cada vez (ya confirmado contigo).

### 5.2 Layout en vivo (al soltar archivos)

CSS Grid de 2 filas × 2 columnas, animado con Framer Motion:

```
┌─────────────┬───────────────────────┬──────────────┐
│  IZQUIERDA  │        CENTRO         │   DERECHA    │
│  factura    │   pipeline de 4 pasos │  completadas │
│  actual     │   OCR→Parser→Concilia │  (se acumula │
│             │   →Reglas             │  hacia abajo)│
├─────────────┴───────────────────────┴──────────────┤
│                  LOGS (feed inferior)                │
└───────────────────────────────────────────────────────┘
```

Barra superior fija (`MetricsBar`) por encima de todo, con las métricas en
vivo: `facturas/s`, `% escalado`, `p50/p95 ms`, contador `X / N procesadas`.

**Panel izquierdo — `FacturaActualCard`**
Miniatura del PDF que se está procesando ahora mismo (o el que va a entrar
en cola), nombre de archivo, y en cuanto el parser resuelve el NIF/CIF lo
muestra como badge. Transición suave (`AnimatePresence mode="wait"`) al
cambiar de factura.

**Panel central — `EtapasPipeline`**
4 nodos conectados por una línea (OCR · Parser · Conciliación · Reglas),
mapeados 1:1 a los 4 artefactos que ya escribe el motor
(`ocr.json → factura.json → evidencia.json → decision.json`). El nodo activo
pulsa (animación de opacidad/escala en loop), los completados quedan en
verde con un check, y si un paso falla queda en rojo con un icono de aviso.
Esto es la animación central de la demo: **el pipeline real del motor,
visualizado paso a paso**, no un spinner genérico.

**Panel inferior — `LogFeed`**
Feed tipo terminal. Cada `LogEvent` entra con `initial={{opacity:0, y:8}}` →
`animate={{opacity:1, y:0}}`, y a los ~4s se desvanece
(`animate={{opacity:0.15}}`) pero **no desaparece del DOM**: queda apilado y
sigue siendo legible con opacidad baja, para que quien mira la demo pueda
seguir el hilo sin que la pantalla "salte". El array completo de logs vive
en el store y es consultable después (botón "ver log completo" opcional).

Color por severidad — mapea a los niveles que ya usa el motor
(`traces/trazabilidad.md §4.2`):

| Nivel | Color | Ejemplos reales del motor |
|---|---|---|
| `INFO` / éxito | Verde claro | `invoice_processed`, `erp_snapshot_saved`, "factura escaneada correctamente" |
| `WARN` | Ámbar | `erp_retry_transient`, `reconciliation_conflict`, línea con confianza OCR baja |
| `ERROR` | Rojo | `ocr_fallback_escalate`, excepción no controlada → factura cae a `ESCALAR` |

**Panel derecho — `ColumnaCompletadas`**
Cuando una factura termina (llega `decision.json`), su tarjeta hace un
`layout` transition desde el centro hacia esta columna (Framer Motion
`layoutId` compartido entre `FacturaActualCard` y la entrada de esta lista),
con un toast pequeño y rápido: `✓ factura_0142.pdf · PAGAR` (o el color de
resultado que corresponda), que se desvanece en ~1.5s. La columna crece
hacia abajo con scroll propio.

### 5.3 Fin del lote

Cuando se agota la cola: la barra superior pasa a "Completado — 500/500",
aparece un botón primario "Ver resultados →" (navega a `/facturas`) y uno
secundario "Procesar más facturas" (vuelve al dropzone). No hay navegación
automática forzada — el usuario decide cuándo pasar de pantalla.

---

## 6. Pantalla 2 — `/facturas` (explorador + detalle)

### 6.1 Filtros (`FiltrosFacturas`)

Barra superior con:
- Buscador de texto libre que matchea contra `nif_emisor` **o** `cif_cliente`.
- Selector de fecha (usa `factura.fecha`) — pensado como alternativa cuando
  la factura no trae NIF/CIF legible.
- Toggle **"Solo no pagadas"** → filtra `result !== "NO_PAGAR"` (o el sentido
  inverso, a confirmar con negocio: "no pagadas" = pendientes de pago, es
  decir `PAGAR` + `ESCALAR`).
- Toggle **"Solo escaladas"** → `result === "ESCALAR"`.
- Filtrar por un NIF concreto (clic en un badge de NIF en la lista) acota
  automáticamente a las facturas de ese NIF, como pediste.

### 6.2 Lista (`ListaFacturas`)

Una fila por factura: `file_id`, NIF/CIF, fecha, importe, badge de color de
`result` (mismo esquema verde/ámbar/rojo, pero aquí verde=`PAGAR`,
azul/neutro=`NO_PAGAR`, ámbar=`ESCALAR` — se define paleta exacta en §7).
Clic → abre el detalle.

### 6.3 Detalle (`DetalleFactura`)

Layout de dos columnas, tal como lo describiste:

- **Derecha:** imagen/PDF original embebido (`<iframe>` o visor de PDF vía
  `pdfUrl`), con zoom básico.
- **Izquierda:** panel de datos, de arriba a abajo:
  1. Resultado + motivo (`decision.motivo`), en texto llano.
  2. Datos extraídos (`factura.*`) con su score de confianza junto a cada
     campo — si el score es bajo, se resalta en ámbar.
  3. Evidencia: asiento ERP relacionado (`evidencia.asiento_erp`), filas de
     Excel relacionadas, y **conflictos** en rojo si `evidencia.conflictos`
     no está vacío.
  4. Reglas evaluadas (`decision.reglas_evaluadas`), colapsable — es detalle
     técnico, no lo primero que ve un perfil no técnico pero debe poder
     abrirlo para auditoría/defensa.

### 6.4 Pestaña de métricas agregadas (`PanelMetricasAgregadas`)

Vista adicional, barata de construir y con alto impacto para el tribunal:
reproduce `run_summary.json` (distribución PAGAR/NO_PAGAR/ESCALAR,
facturas/s, p50/p95, reintentos ERP, coste). Mismos datos que ya se
documentan en `motor/docs/capacidad.md`, mostrados como panel en vez de
markdown.

---

## 7. Sistema de color por estado

Un único mapeo, usado tanto en logs como en badges de resultado y en el
pipeline central — para que el usuario no tenga que aprender dos paletas.

| Semántica | Color | Uso |
|---|---|---|
| Éxito / `PAGAR` / `INFO` | Verde claro (`#dcfce7` fondo, `#166534` texto) | logs OK, badge de PAGAR, nodo de pipeline completado |
| Aviso / baja confianza / `ESCALAR` / `WARN` | Ámbar (`#fef9c3` fondo, `#854d0e` texto) | logs de warning, badge de ESCALAR, campos con score bajo |
| Error / `ERROR` | Rojo (`#fee2e2` fondo, `#991b1b` texto) | logs de error, conflictos de evidencia, nodo de pipeline fallido |
| Neutro / `NO_PAGAR` | Gris/azul neutro (`#e2e8f0` fondo, `#334155` texto) | badge de NO_PAGAR (ya liquidada, no es ni error ni pendiente) |

Definido en `src/lib/colorPorResultado.ts` como única fuente de verdad, para
no repetir clases de Tailwind sueltas por los componentes.

---

## 8. Lo que se deja incompleto a propósito (para conectar el backend real)

El backend (API + base de datos + servicio OCR en vivo, nombre interno
"rapidoc") está en desarrollo. La UI se construye contra una interfaz
estable para no bloquear el trabajo de frontend ni tener que reescribir
componentes cuando el backend esté listo.

```ts
// src/api/client.ts
export interface InvoicesClient {
  subirFacturas(files: File[]): Promise<void>;
  suscribirseAProcesamiento(onEvent: (e: LogEvent | DecisionEvent) => void): () => void; // devuelve unsubscribe
  getMetricasEnVivo(): Promise<RunMetrics>;
  hayFacturasProcesadas(): Promise<boolean>;
  listarFacturas(filtros: FiltrosFacturas): Promise<OutcomeLinea[]>;
  getFacturaCompleta(fileId: string): Promise<FacturaCompleta>;
  getMetricasAgregadas(): Promise<RunMetrics>;
}
```

- **`mockClient.ts`** (implementación activa hoy): lee `outcomes.jsonl` /
  `outcomes_lote2.jsonl` y trazas de ejemplo ya existentes en el repo,
  simula el streaming de eventos con `setInterval`/timers para reproducir el
  ritmo real del pipeline (usando los `timings_ms` reales de `decision.json`
  como referencia de velocidad, para que la demo no se sienta artificial).
- **`httpClient.ts`** (placeholder): mismas firmas, cuerpo con
  `// TODO: reemplazar por fetch a <endpoint real de rapidoc>` en cada
  método. `suscribirseAProcesamiento` queda preparado para WebSocket o
  Server-Sent Events (a decidir cuando el backend defina el contrato) en vez
  de polling.
- Selección de implementación por variable de entorno
  (`VITE_API_MODE=mock|http`), sin lógica condicional dentro de los
  componentes.

---

## 9. Fases de implementación sugeridas

1. **Esqueleto**: Vite + Tailwind + rutas + `mockClient` leyendo un
   `outcomes.jsonl` de ejemplo estático (sin animación todavía).
2. **Explorador (`/facturas`)** primero: es el que tiene forma de datos más
   estable y valida los tipos de §4 cuanto antes.
3. **Pipeline en vivo (`/procesar`)**: layout de 4 paneles, luego animación
   de logs, luego animación de tarjeta moviéndose a "completadas".
4. **Pulido de demo**: velocidad de reproducción del mock ajustada a lo que
   se vea bien en pantalla (no necesariamente la velocidad real de 3.7s para
   500 facturas — para la demo puede interesar ralentizar artificialmente
   los primeros N archivos y acelerar el resto).
5. **Conectar `httpClient`** cuando el backend esté listo — cambio de una
   variable de entorno, sin tocar componentes si el contrato de §4 se
   respetó.

---

## 10. Fuera de alcance (por ahora)

- Autenticación/roles — no mencionado como requisito, se asume acceso
  directo para la demo.
- Edición manual de decisiones desde la UI (aprobar/rechazar un `ESCALAR`)
  — interesante como bonus futuro, pero no pedido; anotarlo aquí para no
  perderlo de vista.
- Subida de facturas en formatos distintos de PDF.
