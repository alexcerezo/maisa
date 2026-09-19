

# Sistema Albertitos · Especificación de Trazabilidad y Observabilidad

**Versión:** 1.0.0  
**Estado:** Canónico / En producción  
**Autores:** Equipo Albertitos (Team ID: `YEM9Q8TP`)  
**Objetivo de Rúbrica:** 20 pts (Trazabilidad y Observabilidad) + 10 pts (Soporte Resiliencia) + 10 pts (Soporte UI Bonus)

---

## 1. Visión General y Objetivos

La trazabilidad del sistema **Albertitos** está diseñada bajo un principio de **auditoría forense contable total**. Ninguna factura clasificada en `PAGAR`, `NO_PAGAR` o `ESCALAR` es una caja negra: cada resultado es determinista, reproducible y explicable paso a paso ante Finanzas o el tribunal evaluador.

El sistema desacopla la observabilidad en dos planos complementarios:

1. **Plano de Entidad / Auditoría Forense (`traces/<file_id>/`):**  
   Almacén inmutable de artefactos estructurados en disco por factura, que registra de forma aislada qué vio el OCR, cómo se estructuraron los campos, qué datos se extrajeron del ERP/Excel y qué reglas exactas determinaron el veredicto final.
2. **Plano de Ejecución / Telemetría Operativa (Structured Tracing & Metrics):**  
   Flujo continuo de logs estructurados en JSON identificados con `run_id`, métricas agregadas en memoria y telemetría de fallos del ERP (`ORA-00600`, `SES-401`, `ERP-429`) y del OCR.

---

## 2. Arquitectura de Trazabilidad

### 2.1 Ciclo de Vida del Dato y Puntos de Emisión

Cada factura atraviesa cuatro fases estrictas. Cada fase produce un artefacto JSON inmutable:



```
┌──────────────┐
factura.pdf ───▶│ 1. OCR (Py)  │──────▶ traces/<file_id>/ocr.json
└──────┬───────┘
│
▼
┌──────────────┐
│  2. Parser   │──────▶ traces/<file_id>/factura.json
└──────┬───────┘
│
▼
ERP Snapshot ───▶ ┌──────────────┐
│3. Reconciler │──────▶ traces/<file_id>/evidencia.json
Excel Context ──▶ └──────┬───────┘
│
▼
Reglas TOML ────▶ ┌──────────────┐
│ 4. Motor     │──────▶ traces/<file_id>/decision.json
│    Reglas    │──────▶ outcomes.jsonl (append)
└──────────────┘

```

### 2.2 Estructura de Directorios


```

traces/
├── run_20260920_081500.summary.json    # Informe agregado al terminar el lote
└── factura_0142.pdf/                   # Carpeta por cada file_id exacto
├── ocr.json                        # Lectura bruta devuelta por RapidOCR
├── factura.json                    # Extracción y validación sintáctica
├── evidencia.json                  # Triangulación PDF ↔ ERP ↔ Excel
├── decision.json                   # Veredicto final, motivo y telemetría
└── factura_0142.pdf                # Symlink o copia del documento original

```

---

## 3. Especificación de Contratos JSON (Nivel Factura)

### 3.1 `ocr.json` · Salida del Modelo de Reconocimiento

Aisla el comportamiento del modelo de visión. Permite demostrar si un error proviene de una mala lectura o de un fallo posterior en el parser.

```json
{
  "file_id": "factura_0142.pdf",
  "engine": "RapidOCR-v4-ONNX",
  "pages": 1,
  "execution_time_ms": 342,
  "lines": [
    {
      "page": 0,
      "text": "DISTRIBUCIONES IBERICAS S.L.",
      "bbox": [102, 45, 380, 68],
      "score": 0.993
    },
    {
      "page": 0,
      "text": "NIF: B87654321",
      "bbox": [102, 75, 240, 95],
      "score": 0.984
    },
    {
      "page": 0,
      "text": "PEDIDO: PED-2009-8812",
      "bbox": [102, 102, 295, 120],
      "score": 0.961
    },
    {
      "page": 0,
      "text": "TOTAL FACTURA: 1.250,00 €",
      "bbox": [410, 610, 560, 632],
      "score": 0.957
    }
  ]
}

```

### 3.2 `factura.json` · Datos Parseados y Validados

Representa los datos canónicos estructurados con sus puntuaciones de confianza algorítmica y validaciones de formato español (CIF, importes, fechas).

```json
{
  "file_id": "factura_0142.pdf",
  "parsed_at": "2026-09-20T08:15:23.102Z",
  "nif_emisor": "B87654321",
  "pedido": "PED-2009-8812",
  "numero_factura": "F2026/089",
  "fecha": "2026-02-14",
  "base": "1033.06",
  "iva": "216.94",
  "total": "1250.00",
  "scores": {
    "nif_emisor": 0.984,
    "pedido": 0.961,
    "total": 0.957
  },
  "validaciones": {
    "cif_valido": true,
    "aritmetica_cuadra": true,
    "score_minimo_alcanzado": true
  },
  "raw_text_length": 458
}

```

### 3.3 `evidencia.json` · Conciliación a Tres Bandas

Registra la triangulación completa entre PDF, ERP (fuente de verdad oficial) y Excel (contexto). Si se producen descuadres o ambigüedades, se especifican en `conflictos`.

```json
{
  "file_id": "factura_0142.pdf",
  "match_strategy": "ExactByPedido",
  "asiento_erp": {
    "asiento_id": "AS-00412",
    "proveedor": "Distribuciones Ibericas S.L.",
    "nif": "B87654321",
    "pedido": "PED-2009-8812",
    "importe": "1250.00",
    "estado": "PENDIENTE",
    "fecha": "2026-02-15"
  },
  "excel_hits": [
    {
      "row_index": 84,
      "sheet": "Operaciones_2026",
      "campos": {
        "Proveedor": "Distribuciones Ibericas",
        "Ref_Pedido": "PED-2009-8812",
        "Estado_Aprobacion": "OK",
        "Notas": "Material entregado a tiempo"
      }
    }
  ],
  "diferencia_importe": "0.00",
  "conflictos": []
}

```

*(En caso de fallo de match o discrepancia, `conflictos` contendría arrays explícitos como: `["Descuadre: importe PDF 1250.00 vs ERP 1200.00", "NIF no coincide con registro de compras del Excel"]`)*.

### 3.4 `decision.json` · Veredicto, Justificación y Telemetría

Este fichero cierra el ciclo de auditoría. Permite justificar ante auditores el motivo exacto, qué reglas pasaron y cuánto costó computacionalmente procesar el documento.

```json
{
  "file_id": "factura_0142.pdf",
  "run_id": "run_20260920_081500",
  "timestamp": "2026-09-20T08:15:23.150Z",
  "result": "PAGAR",
  "motivo": "asiento AS-00412 PENDIENTE, importes conciliados",
  "regla_aplicada": "R3_ERP_PENDIENTE_CONCILIADO",
  "reglas_evaluadas": [
    "R1_IDENTIFICADORES_MINIMOS: PASS (NIF='B87654321', Pedido='PED-2009-8812')",
    "R2_ERP_PAGADA: NO_MATCH (Estado ERP='PENDIENTE')",
    "R3_ERP_PENDIENTE_CONCILIADO: MATCH (Diferencia=0.00 € <= Tolerancia=0.02 €)"
  ],
  "timings_ms": {
    "ocr": 342,
    "parser": 4,
    "reconciliation": 1,
    "rules": 0,
    "io_write": 3,
    "total": 350
  },
  "coste_estimado_cents": 0
}

```

---

## 4. Telemetría Operativa y Stream de Logs

Todo el runtime emite eventos en formato NDJSON a `stdout` y opcionalmente a un fichero de sesión `traces/run_<run_id>.log`.

### 4.1 Formato de Evento Estándar

```json
{
  "timestamp": "2026-09-20T08:15:10.120Z",
  "level": "INFO",
  "run_id": "run_20260920_081500",
  "component": "erp_client",
  "event": "erp_retry_transient",
  "file_id": null,
  "message": "Fallo ORA-00600 detectado en página 14. Aplicando reintento con backoff.",
  "data": {
    "http_status": 500,
    "error_code": "ORA-00600",
    "intento": 1,
    "max_intentos": 3,
    "backoff_ms": 200
  }
}

```

### 4.2 Catálogo de Eventos Operativos

| Componente | Evento | Nivel | Descripción |
| --- | --- | --- | --- |
| **`erp_client`** | `erp_fetch_started` | INFO | Inicio de descarga de asientos del bridge. |
| **`erp_client`** | `erp_retry_transient` | WARN | Captura de `ORA-00600` e inicio de reintento automático. |
| **`erp_client`** | `erp_session_refresh` | INFO | Captura de `SES-401`, re-autenticación y nuevo token. |
| **`erp_client`** | `erp_rate_limit_backoff` | WARN | Captura de `ERP-429`, suspensión de hilos según `Retry-After`. |
| **`erp_client`** | `erp_snapshot_saved` | INFO | Asientos descargados (516) y cacheados en disco. |
| **`ocr_service`** | `ocr_fallback_escalate` | ERROR | Caída/timeout de RapidOCR; devuelve `lines: []` $\rightarrow$ `ESCALAR`. |
| **`reconciler`** | `reconciliation_conflict` | WARN | Inconsistencia detectada entre ERP y Excel. |
| **`engine`** | `invoice_processed` | DEBUG | Factura completada y traza atómica guardada. |
| **`engine`** | `invoice_skipped_cache` | INFO | Factura saltada por presencia previa de `decision.json`. |

### 4.3 Informe Final Agregado (`run_summary.json`)

Al finalizar el procesamiento de un lote, el módulo de observabilidad genera un resumen global:

```json
{
  "run_id": "run_20260920_081500",
  "environment": "AMD Ryzen 7 5800H / 16GB RAM / Ubuntu LTS",
  "lote": "lote1 (500 facturas)",
  "metricas_negocio": {
    "total_procesadas": 500,
    "distribucion": {
      "PAGAR": 234,
      "NO_PAGAR": 118,
      "ESCALAR": 148
    },
    "ratio_escalado_pct": 29.6
  },
  "metricas_erp": {
    "asientos_en_memoria": 516,
    "matches_exactos_pedido": 421,
    "matches_por_nif_importe": 18,
    "sin_match_erp": 61,
    "reintentos_ora600": 44,
    "reautenticaciones_ses401": 3,
    "pausas_rate_limit_429": 0
  },
  "rendimiento": {
    "tiempo_total_segundos": 128.5,
    "facturas_por_segundo": 3.89,
    "latencia_p50_ms": 285,
    "latencia_p95_ms": 590,
    "cuello_de_botella": "RapidOCR inference (88% del tiempo total)"
  },
  "coste_total_euros": 0.00
}

```

---

## 5. Implementación en Rust (`src/obs.rs`)

### 5.1 Colector Atómico de Métricas

Para evitar bloqueos entre los hilos concurrentes del pipeline, las métricas globales se computan utilizando operaciones atómicas:

```rust
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;
use crate::domain::Result_;

pub struct MetricsCollector {
    pub start_time: Instant,
    pub total: AtomicUsize,
    pub pagar: AtomicUsize,
    pub no_pagar: AtomicUsize,
    pub escalar: AtomicUsize,
    pub erp_ora600_retries: AtomicUsize,
    pub erp_ses401_retries: AtomicUsize,
    pub erp_429_hits: AtomicUsize,
    pub ocr_fallbacks: AtomicUsize,
}

impl MetricsCollector {
    pub fn new() -> Self {
        Self {
            start_time: Instant::now(),
            total: AtomicUsize::new(0),
            pagar: AtomicUsize::new(0),
            no_pagar: AtomicUsize::new(0),
            escalar: AtomicUsize::new(0),
            erp_ora600_retries: AtomicUsize::new(0),
            erp_ses401_retries: AtomicUsize::new(0),
            erp_429_hits: AtomicUsize::new(0),
            ocr_fallbacks: AtomicUsize::new(0),
        }
    }

    pub fn record_decision(&self, result: &Result_) {
        self.total.fetch_add(1, Ordering::Relaxed);
        match result {
            Result_::Pagar => self.pagar.fetch_add(1, Ordering::Relaxed),
            Result_::NoPagar => self.no_pagar.fetch_add(1, Ordering::Relaxed),
            Result_::Escalar => self.escalar.fetch_add(1, Ordering::Relaxed),
        };
    }
}

```

### 5.2 Escritura Atómica e Idempotencia (Crash Recovery)

Para garantizar que un apagón o cancelación abrupta (`Ctrl+C`) no deje ficheros corruptos, la escritura de trazas sigue el patrón **Write-to-Temp + Rename Atómico**:

```rust
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use anyhow::Result;
use crate::domain::{Decision, Evidencia, Factura};

pub struct TraceWriter {
    base_dir: PathBuf,
}

impl TraceWriter {
    pub fn new(base_dir: impl AsRef<Path>) -> Self {
        Self { base_dir: base_dir.as_ref().to_path_buf() }
    }

    /// Comprueba si la factura ya fue procesada completamente en una ejecución previa
    pub fn already_processed(&self, file_id: &str) -> bool {
        let decision_path = self.base_dir.join(file_id).join("decision.json");
        decision_path.exists()
    }

    /// Escribe los artefactos garantizando atomicidad
    pub fn persist_trace(
        &self,
        file_id: &str,
        raw_ocr: &serde_json::Value,
        factura: &Factura,
        evidencia: &Evidencia,
        decision: &Decision,
    ) -> Result<()> {
        let invoice_dir = self.base_dir.join(file_id);
        fs::create_dir_all(&invoice_dir)?;

        // 1. Escribir ocr.json, factura.json y evidencia.json
        Self::write_json_direct(&invoice_dir.join("ocr.json"), raw_ocr)?;
        Self::write_json_direct(&invoice_dir.join("factura.json"), factura)?;
        Self::write_json_direct(&invoice_dir.join("evidencia.json"), evidencia)?;

        // 2. Escribir decision.json usando rename atómico (marca de fin de pipeline)
        let temp_decision = invoice_dir.join("decision.json.tmp");
        let final_decision = invoice_dir.join("decision.json");

        Self::write_json_direct(&temp_decision, decision)?;
        fs::rename(temp_decision, final_decision)?;

        Ok(())
    }

    fn write_json_direct<T: serde::Serialize>(path: &Path, data: &T) -> Result<()> {
        let mut file = File::create(path)?;
        let content = serde_json::to_string_pretty(data)?;
        file.write_all(content.as_bytes())?;
        file.flush()?;
        Ok(())
    }
}

```

### 5.3 Configuración de Tracing en `main.rs`

Se utiliza `tracing-subscriber` configurado con formato JSON estricto:

```rust
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

pub fn setup_tracing() {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .json()
                .with_target(false)
                .with_current_span(true)
        )
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();
}

```

---

## 6. Integración con el Bonus (+10 pts): UI de Conciliación

Gracias a que cada factura guarda de manera limpia sus artefactos en `traces/<file_id>/`, la UI estática (`ui/index.html`) no requiere base de datos ni backend complejo:

1. **Lectura Estática:** Un script (`main.rs --export-ui-manifest` o Python) genera un `manifest.json` leyendo todos los `decision.json`.
2. **Triaje en Pantalla:**
* **Columna Izquierda:** Lista de facturas filtrables (`Ver solo ESCALAR`, `Ver conflictos ERP/Excel`).
* **Columna Central:** Visor del PDF incrustado (`<iframe src="/traces/factura_123.pdf">`).
* **Columna Derecha:** Comparador lado a lado renderizado directamente desde `factura.json` y `evidencia.json`, mostrando los importes en conflicto en rojo y el motivo canónico del dictamen.



---

## 7. Argumentario para la Defensa (Respuestas al Tribunal)

* **P: "¿Por qué habéis pagado la factura 312 si el Excel decía otra cosa?"**
*R:* "Según el **ADR 1**, el ERP legado es la fuente de verdad contable oficial según el manual del cliente. En `traces/factura_312.pdf/evidencia.json` se comprueba que el asiento del ERP estaba `PENDIENTE` con importe exacto dentro de la tolerancia de 0.02 €, mientras que la discrepancia del Excel se auditó y categorizó como contexto desactualizado."
* **P: "¿Qué pasa si el sistema se apaga en la factura 240?"**
*R:* "El método `already_processed()` comprueba la existencia de `traces/<file_id>/decision.json`. Las 239 facturas cerradas atómicamente no se vuelven a procesar ni a mandar al OCR. El sistema reanuda de manera idempotente desde la 240 en menos de un segundo."
* **P: "¿Cómo manejasteis las caídas periódicas del ERP?"**
*R:* "Nuestra telemetría registró exactamente **44 reintentos de `ORA-00600**` y **3 renovaciones transparentes `SES-401**`. Cada reintento figura registrado con timestamp, código de estado y backoff en el log de observabilidad, sin perder ninguna petición."

```

```