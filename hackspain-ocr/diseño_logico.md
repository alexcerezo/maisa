# Diseño Lógico de la Base de Datos — Albertitos (MongoDB)

> **Ámbito:** traducción del modelo conceptual ([`diseño_conceptual.md`](./diseño_conceptual.md))
> a un diseño físico concreto sobre **MongoDB 7**: base de datos, colecciones,
> validadores `$jsonSchema`, índices justificados, estrategia de escritura,
> GridFS, time-series, migraciones, Docker y escalado a 50 000 facturas.
>
> **Motor:** MongoDB 7.x · **Driver:** `mongodb` crate 2.8 (Rust) · **Despliegue:** Docker Compose
> **Base de datos:** `albertitos`

---

## 1. Convenciones generales

### 1.1 Nomenclatura

| Elemento | Convención | Ejemplo |
|---|---|---|
| Base de datos | minúsculas, sin prefijo | `albertitos` |
| Colecciones | plural, `snake_case`, sin prefijo de BD | `expedientes`, `excel_filas` |
| Campos | `snake_case`, español (coherente con el dominio) | `nif_emisor`, `estado_proceso` |
| Subdocumentos | agrupados por fase del pipeline | `documento`, `ocr`, `factura`, `evidencia`, `decision` |
| Enumeraciones | `SCREAMING_SNAKE_CASE` | `PAGAR`, `NO_PAGAR`, `ESCALAR` |
| Fechas | `Date` (BSON UTC) | `creado_en` |
| Importes | `Decimal128` | `NumberDecimal("1234.50")` |
| Identificadores de negocio | `String` | `file_id`, `asiento_id`, `run_id` |

### 1.2 Reglas transversales

1. **Todo documento lleva `esquema_version`** (entero). Permite migraciones
   incrementales sin adivinar la forma del documento.
2. **Todo documento lleva `creado_en` y `actualizado_en`** (`Date`).
3. **Los importes son siempre `Decimal128`** (INV-8). Nunca `double`.
4. **Los `_id` son identificadores de negocio**, no `ObjectId`, salvo donde se
   indique lo contrario. Esto hace la idempotencia trivial y la trazabilidad
   directa.
5. **Los arrays embebidos están acotados** (< 100 KB por documento). El OCR de
   una factura produce decenas de líneas, no millones.

### 1.3 Colecciones

| Colección | Tipo | Propósito | Cardinalidad a 540 |
|---|---|---|---|
| `expedientes` | Documento raíz | Un expediente por factura (agregado completo) | 540 |
| `asientos` | Catálogo | Asientos del ERP, versionados por snapshot | ~516 × nº snapshots |
| `excel_filas` | Catálogo | Filas del Excel caótico, volcadas sin pérdida | ~cientos |
| `reglas_versiones` | Catálogo versionado | Versiones inmutables de `reglas.toml` | ~5 |
| `erp_snapshots` | Catálogo versionado | Metadatos de cada descarga del ERP | ~5 |
| `ejecuciones` | Operativa | Un documento por run del pipeline | ~10 |
| `eventos` | **Time-series** | Señales operativas con TTL 90 días | ~miles |
| `migraciones` | Control | Versiones de esquema aplicadas | ~5 |
| `pdfs.files` / `pdfs.chunks` | **GridFS** | PDFs originales | 540 |

---

## 2. Colección `expedientes`

### 2.1 Propósito

Es el **agregado raíz** del sistema: un documento por factura que contiene todo
lo necesario para reconstruir su decisión. Es la única colección escrita en el
camino crítico, y la única que se lee para generar el JSONL de entrega.

**Patrón:** embedding. Los subdocumentos `documento`, `ocr`, `factura`,
`evidencia`, `decision` y `revision` viven dentro del documento porque:
- se escriben y se leen **juntos** (misma fase del pipeline);
- su cardinalidad es **1:1** con el expediente;
- su tamaño está **acotado** (el OCR de una factura son decenas de líneas).

**Clave:** `_id = file_id` (el nombre exacto del PDF, con extensión y
mayúsculas/minúsculas). Esto da idempotencia natural: reprocesar un lote hace
`upsert` sobre el mismo `_id`.

### 2.2 Validador `$jsonSchema`

```javascript
db.createCollection("expedientes", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      title: "Expediente de conciliación",
      required: ["_id", "lote_id", "estado_proceso", "esquema_version", "creado_en", "actualizado_en"],
      additionalProperties: true,
      properties: {
        _id: { bsonType: "string", description: "file_id: nombre exacto del PDF" },
        lote_id: { enum: ["lote1", "lote2"] },
        estado_proceso: {
          enum: ["PENDIENTE", "OCR", "PARSEADA", "CONCILIADA", "DECIDIDA", "COMPLETADA", "ERROR"]
        },
        huella_negocio: {
          bsonType: ["string", "null"],
          description: "nif|pedido — NO único por diseño (INV-7)"
        },
        esquema_version: { bsonType: "int", minimum: 1 },
        creado_en: { bsonType: "date" },
        actualizado_en: { bsonType: "date" },

        documento: {
          bsonType: "object",
          required: ["nombre_original", "sha256", "tamano_bytes"],
          properties: {
            nombre_original: { bsonType: "string" },
            sha256: { bsonType: "string", pattern: "^[a-f0-9]{64}$" },
            tamano_bytes: { bsonType: "long", minimum: 1 },
            paginas: { bsonType: "int", minimum: 0 },
            gridfs_id: { bsonType: ["objectId", "null"] }
          }
        },

        ocr: {
          bsonType: "object",
          required: ["motor", "lineas"],
          properties: {
            motor: { enum: ["rapidocr", "pytesseract", "pdfplumber", "ninguno"] },
            paginas: { bsonType: "int", minimum: 0 },
            duracion_ms: { bsonType: "int", minimum: 0 },
            disponible: { bsonType: "bool" },
            lineas: {
              bsonType: "array",
              maxItems: 2000,
              items: {
                bsonType: "object",
                required: ["pagina", "texto"],
                properties: {
                  pagina: { bsonType: "int", minimum: 0 },
                  texto: { bsonType: "string" },
                  bbox: {
                    bsonType: "array",
                    minItems: 4, maxItems: 4,
                    items: { bsonType: "double" }
                  },
                  score: { bsonType: "double", minimum: 0, maximum: 1 }
                }
              }
            }
          }
        },

        // Cada campo de `factura` es {estado, valor, rastro}: ver §2.5.
        factura: {
          bsonType: "object",
          properties: {
            nif_emisor:     campoExtraido(["string"]),
            pedido:         campoExtraido(["string"]),
            numero_factura: campoExtraido(["string"]),
            fecha:          campoExtraido(["string"]),
            base:           campoExtraido(["decimal"]),
            iva:            campoExtraido(["decimal"]),
            total:          campoExtraido(["decimal"])
          }
        },

        evidencia: {
          bsonType: ["object", "null"],
          required: ["match_por", "conflictos"],
          properties: {
            asiento_id: { bsonType: ["string", "null"] },
            asiento: {
              bsonType: ["object", "null"],
              description: "Copia embebida del asiento en el momento de decidir",
              properties: {
                asiento_id: { bsonType: "string" },
                fecha: { bsonType: "date" },
                proveedor: { bsonType: "string" },
                nif: { bsonType: "string" },
                pedido: { bsonType: "string" },
                importe: { bsonType: "decimal" },
                estado: { enum: ["PENDIENTE", "PAGADA"] }
              }
            },
            excel_filas: {
              bsonType: "array",
              maxItems: 50,
              items: { bsonType: "string", description: "fila_id: hoja#indice" }
            },
            match_por: {
              enum: ["EXACT_BY_PEDIDO", "BY_NIF_Y_IMPORTE", "BY_NIF_UNICO", "NONE"]
            },
            conflictos: {
              bsonType: "array",
              items: { bsonType: "string" }
            }
          }
        },

        decision: {
          bsonType: ["object", "null"],
          required: ["resultado", "motivo", "reglas_evaluadas", "huellas"],
          properties: {
            resultado: { enum: ["PAGAR", "NO_PAGAR", "ESCALAR"] },
            motivo: { bsonType: "string", minLength: 1 },
            reglas_evaluadas: {
              bsonType: "array",
              items: { bsonType: "string" }
            },
            coste_estimado_cents: { bsonType: "int", minimum: 0 },
            timings_ms: {
              bsonType: "object",
              additionalProperties: { bsonType: "int", minimum: 0 }
            },
            huellas: {
              bsonType: "object",
              required: ["reglas_version", "erp_snapshot_id", "run_id"],
              description: "Bitemporalidad mínima: permite detectar decisiones obsoletas",
              properties: {
                reglas_version: { bsonType: "int", minimum: 1 },
                erp_snapshot_id: { bsonType: "string" },
                run_id: { bsonType: "string" }
              }
            }
          }
        },

        revision: {
          bsonType: ["object", "null"],
          properties: {
            estado: { enum: ["PENDIENTE", "EN_REVISION", "RESUELTA"] },
            revisor: { bsonType: ["string", "null"] },
            resultado_final: { enum: ["PAGAR", "NO_PAGAR", "ESCALAR", null] },
            comentario: { bsonType: ["string", "null"] },
            revisado_en: { bsonType: ["date", "null"] }
          }
        }
      },

      // INV-2 / INV-3: la decisión existe si y solo si el expediente está COMPLETADA
      oneOf: [
        {
          properties: {
            estado_proceso: { enum: ["COMPLETADA"] },
            decision: { bsonType: "object" }
          },
          required: ["decision"]
        },
        {
          properties: {
            estado_proceso: { enum: ["PENDIENTE", "OCR", "PARSEADA", "CONCILIADA", "DECIDIDA", "ERROR"] },
            decision: { bsonType: "null" }
          }
        }
      ]
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 2.3 Documento de ejemplo

```javascript
{
  _id: "factura_5518.pdf",
  lote_id: "lote1",
  estado_proceso: "COMPLETADA",
  huella_negocio: "B12345678|PED-2024-0912",
  esquema_version: 1,
  creado_en: ISODate("2026-09-19T09:14:02.113Z"),
  actualizado_en: ISODate("2026-09-19T09:14:03.871Z"),

  documento: {
    nombre_original: "factura_5518.pdf",
    sha256: "3f786850e387550fdab836ed7e6dc881de23001b1a2b3c4d5e6f708192a3b4c5",
    tamano_bytes: NumberLong(184320),
    paginas: 1,
    gridfs_id: ObjectId("651f2a1c9e4b0d0012ab34cd")
  },

  ocr: {
    motor: "rapidocr",
    paginas: 1,
    duracion_ms: 1187,
    disponible: true,
    lineas: [
      { pagina: 0, texto: "FACTURA", bbox: [120.0, 45.0, 260.0, 70.0], score: 0.997 },
      { pagina: 0, texto: "NIF: B12345678", bbox: [122.0, 78.0, 320.0, 100.0], score: 0.981 },
      { pagina: 0, texto: "Pedido: PED-2024-0912", bbox: [122.0, 104.0, 360.0, 126.0], score: 0.964 },
      { pagina: 0, texto: "TOTAL: 1.234,50 EUR", bbox: [300.0, 402.0, 520.0, 428.0], score: 0.972 }
    ]
  },

  factura: {
    nif_emisor: {
      estado: "ENCONTRADO",
      valor: "B12345678",
      rastro: {
        crudo: "NIF: B12345678",
        score: 0.981,
        origen: { fuente: "OCR", pagina: 0, linea: 1 }
      }
    },
    pedido: {
      estado: "ENCONTRADO",
      valor: "PED-2024-0912",
      rastro: {
        crudo: "Pedido: PED-2024-0912",
        score: 0.964,
        origen: { fuente: "OCR", pagina: 0, linea: 2 }
      }
    },
    numero_factura: {
      estado: "ENCONTRADO",
      valor: "F-2024-5518",
      rastro: { crudo: "Factura nº F-2024-5518", score: 0.976,
                origen: { fuente: "OCR", pagina: 0, linea: 9 } }
    },
    fecha: {
      estado: "ENCONTRADO",
      valor: "2024-09-02",
      rastro: { crudo: "Fecha: 02/09/2024", score: 0.958,
                origen: { fuente: "OCR", pagina: 0, linea: 10 } }
    },
    base: {
      estado: "ENCONTRADO",
      valor: NumberDecimal("1020.25"),
      rastro: { crudo: "BASE IMPONIBLE 1.020,25", score: 0.943,
                origen: { fuente: "OCR", pagina: 0, linea: 27 } }
    },
    iva: {
      estado: "ENCONTRADO",
      valor: NumberDecimal("214.25"),
      rastro: { crudo: "IVA 21% 214,25", score: 0.951,
                origen: { fuente: "OCR", pagina: 0, linea: 28 } }
    },
    total: {
      estado: "ENCONTRADO",
      valor: NumberDecimal("1234.50"),
      rastro: {
        crudo: "TOTAL: 1.234,50 EUR",
        score: 0.972,
        origen: { fuente: "OCR", pagina: 0, linea: 3 }
      }
    }
  },

  evidencia: {
    asiento_id: "AS-00412",
    asiento: {
      asiento_id: "AS-00412",
      fecha: ISODate("2024-09-03T00:00:00Z"),
      proveedor: "Suministros Ibéricos S.L.",
      nif: "B12345678",
      pedido: "PED-2024-0912",
      importe: NumberDecimal("1234.50"),
      estado: "PENDIENTE"
    },
    excel_filas: ["Hoja1#42"],
    match_por: "EXACT_BY_PEDIDO",
    conflictos: []
  },

  decision: {
    resultado: "PAGAR",
    motivo: "asiento AS-00412 PENDIENTE, importes conciliados",
    reglas_evaluadas: ["R1_sin_identificadores", "R2_erp_pagada", "R3_erp_pendiente_conciliado"],
    coste_estimado_cents: 0,
    timings_ms: { ocr: 1187, parse: 12, erp: 0, excel: 3, decide: 1 },
    huellas: {
      reglas_version: 3,
      erp_snapshot_id: "snap-2026-09-19T09-00-00Z",
      run_id: "run-20260919-091402"
    }
  },

  revision: null
}
```

### 2.4 Índices

| Índice | Definición | Patrón de acceso que cubre | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` (implícito) | Idempotencia, lectura por `file_id`, export JSONL ordenado | Clave natural; el export recorre el `_id` en orden |
| `ix_lote_resultado` | `{lote_id: 1, "decision.resultado": 1, _id: 1}` | UI: "todos los ESCALAR del lote 1" | Índice compuesto que sirve filtro + orden sin `SORT` en memoria |
| `ix_nif` | `{"factura.nif_emisor.valor": 1}` **sparse** | "¿qué facturas son de este proveedor?" | Consulta de investigación y detección de duplicados |
| `ix_pedido` | `{"factura.pedido.valor": 1}` **sparse** | "¿qué facturas comparten pedido?" | Detección de duplicados (INV-7) |
| `ix_asiento` | `{"evidencia.asiento_id": 1}` | "¿qué facturas apuntan a este asiento?" | Trazabilidad inversa ERP → facturas |
| `ix_run` | `{"decision.huellas.run_id": 1}` | "¿qué se decidió en este run?" | Reproducibilidad de una ejecución |
| `ix_huella_negocio` | `{huella_negocio: 1}` | Detección de duplicados `(nif, pedido)` | **No único** por diseño (INV-7) |
| `ix_escalar_pendiente` | `{"decision.resultado": 1, "revision.estado": 1}` **parcial** `{"decision.resultado": "ESCALAR"}` | Cola de trabajo del operador (bonus UI) | Índice parcial: solo indexa los `ESCALAR`, mucho más pequeño |
| `ix_snapshot_obsoleto` | `{"decision.huellas.erp_snapshot_id": 1}` | "¿qué decisiones usaron un snapshot viejo?" | Reprocesado tras `--lote2` |
| `ix_reglas_version` | `{"decision.huellas.reglas_version": 1}` | "¿qué decisiones se tomaron con reglas antiguas?" | Reprocesado tras cambiar `reglas.toml`. Simétrico de `ix_snapshot_obsoleto` |

> **Nota sobre el barrido por versión de reglas.** El nombre del campo es
> `decision.huellas.reglas_version`, **no** `run_id`: un `run_id` identifica una
> *ejecución* concreta, mientras que `reglas_version` identifica la *versión de
> las reglas* con la que se decidió. Sin `ix_reglas_version`, el barrido
> "¿qué decisiones quedaron obsoletas tras cambiar las reglas?" hace `COLLSCAN`
> (verificado sobre 1 000 documentos: `docsExamined = 1 000`, `nReturned = 1 000`).
>
> En la práctica, ese barrido **siempre se acota por `lote_id`**, y en ese caso
> el planificador elige `ix_lote_resultado` (prefijo `lote_id`), que es mejor
> plan porque acota primero por lote. `ix_reglas_version` cubre el caso no
> acotado —auditoría global— donde no hay `lote_id` por el que empezar.

### 2.5 La forma de un campo extraído

`factura` no guarda siete valores, guarda **siete campos extraídos**, y los siete
comparten la misma forma. El `$jsonSchema` de §2.2 la escribe con un ayudante
`campoExtraido(tiposValor)` para no repetirla (y para que no puedan divergir):

```javascript
function campoExtraido(tiposValor) {
  return {
    bsonType: ["object", "null"],
    required: ["estado"],
    properties: {
      estado: { enum: ["ENCONTRADO", "NO_APARECE", "ILEGIBLE"] },
      valor: { bsonType: tiposValor },
      rastro: {
        bsonType: ["object", "null"],
        required: ["crudo", "score"],
        properties: {
          crudo: { bsonType: "string" },
          score: { bsonType: "double", minimum: 0, maximum: 1 },
          origen: {
            bsonType: ["object", "null"],
            required: ["fuente"],
            properties: {
              fuente: { enum: ["OCR", "EXCEL", "ERP"] },
              pagina: { bsonType: "int", minimum: 0 },
              linea:  { bsonType: "int", minimum: 0 },
              fila:   { bsonType: "string" },
              columna:{ bsonType: "string" },
              asiento_id: { bsonType: "string" }
            }
          }
        }
      }
    }
  };
}
```

**Por qué el valor y el crudo van juntos y no en dos campos paralelos.** Sin el
crudo, una decisión mala es indistinguible de un OCR malo, de una normalización
mala o de una regla mala: los tres caminos terminan en el mismo valor canónico.
Con el crudo, la traza dice *en qué paso* se torció la cosa, que es lo que
convierte la auditoría en diagnóstico. Guardarlos en el mismo subdocumento (en
vez de `nif_emisor` + `nif_emisor_bruto`) hace **imposible** tener uno sin el
otro; un mapa `scores: {campo: double}` aparte era justo lo contrario, dos
estructuras que hay que sincronizar a mano y que se desincronizan.

**Por qué `estado` es un tri-estado.** Un `null` confunde dos casos que llevan a
decisiones distintas:

| `estado` | Significa | Consecuencia |
|---|---|---|
| `NO_APARECE` | El PDF no trae el dato | Caso normal: si hay pedido, se concilia por pedido |
| `ILEGIBLE` | Hay texto, pero no se pudo canonizar | Se **escala**: no se puede garantizar *a quién* se paga |
| `ENCONTRADO` | Hay valor canónico y `rastro` lo respalda | Se usa el valor; el `score` entra en R5 |

**Invariantes que Mongo NO comprueba (las garantiza el tipo Rust
`Identificador<T>` en el borde de entrada):** `ENCONTRADO` exige `valor` y
`rastro`; `ILEGIBLE` exige `rastro` y prohíbe `valor`; `NO_APARECE` prohíbe
ambos. Replicarlo en `$jsonSchema` con `oneOf`/`not` daría un validador frágil
que rechazaría escrituras legítimas, así que el validador solo comprueba formas
y tipos. **El escritor es Rust y es el único**, y valida antes de escribir.

**`valor` no existe ≠ `valor: null`.** Lo que no se leyó **se omite** (el
`Option` de Rust no se serializa). Es lo que hace que `ix_nif` / `ix_pedido`
puedan ser `sparse`: un expediente sin NIF no entra en `ix_nif`, en vez de
entrar con un `null` que después hay que descartar en cada consulta.

**Ojo con la proyección de escritura.** En el dominio, `base`/`iva`/`total` son
`Decimal` y `fecha` es texto ISO-8601 (`"2024-09-02"`). `rust_decimal` serializa
a **string** vía serde, así que un `bson::to_bson(&Factura)` directo escribiría
strings donde el validador espera `decimal` (y fallaría la escritura, que es la
forma correcta de fallar). La conversión a `Decimal128` / `ISODate` es
responsabilidad explícita de la capa de persistencia, no del serde del dominio.

---

## 3. Colección `asientos`

### 3.1 Propósito

Catálogo de asientos del ERP. **Es la fuente de verdad contable.** Se versiona
por `snapshot_id` para que una decisión antigua siga siendo explicable aunque el
ERP cambie.

**Patrón:** referencia. El expediente embebe una **copia** del asiento usado
(`evidencia.asiento`) y referencia su `asiento_id`. La copia garantiza
auditabilidad; la referencia permite consultas cruzadas.

**Clave:** `_id = "<snapshot_id>#<asiento_id>"`. Un mismo `AS-00412` puede
existir en varios snapshots con estados distintos (el sábado pasa de `PENDIENTE`
a `PAGADA`); el `_id` compuesto los distingue.

### 3.2 Validador `$jsonSchema`

```javascript
db.createCollection("asientos", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "asiento_id", "snapshot_id", "nif", "pedido", "importe", "estado", "vigente"],
      properties: {
        _id: { bsonType: "string", description: "<snapshot_id>#<asiento_id>" },
        asiento_id: { bsonType: "string", pattern: "^AS-[0-9]{5}$" },
        snapshot_id: { bsonType: "string" },
        fecha: { bsonType: ["date", "null"] },
        proveedor: { bsonType: ["string", "null"] },
        nif: { bsonType: "string" },
        pedido: { bsonType: "string" },
        importe: { bsonType: "decimal" },
        estado: { enum: ["PENDIENTE", "PAGADA"] },
        vigente: { bsonType: "bool", description: "true solo en el snapshot activo" },
        esquema_version: { bsonType: "int", minimum: 1 }
      }
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 3.3 Documento de ejemplo

```javascript
{
  _id: "snap-2026-09-19T09-00-00Z#AS-00412",
  asiento_id: "AS-00412",
  snapshot_id: "snap-2026-09-19T09-00-00Z",
  fecha: ISODate("2024-09-03T00:00:00Z"),
  proveedor: "Suministros Ibéricos S.L.",
  nif: "B12345678",
  pedido: "PED-2024-0912",
  importe: NumberDecimal("1234.50"),
  estado: "PENDIENTE",
  vigente: true,
  esquema_version: 1
}
```

### 3.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` | Lectura directa por asiento + snapshot | Clave compuesta natural |
| `ix_pedido_vigente` | `{pedido: 1, vigente: 1}` | **Match por pedido** (estrategia fuerte) | El reconciler busca por pedido en el snapshot activo |
| `ix_nif_vigente` | `{nif: 1, vigente: 1}` | **Match por NIF único** (estrategia débil) | Fallback cuando no hay pedido |
| `ix_nif_importe_vigente` | `{nif: 1, importe: 1, vigente: 1}` | **Match por (nif, importe ± tolerancia)** | Rango sobre `importe` con prefijo `nif` fijo |
| `ix_snapshot` | `{snapshot_id: 1}` | Marcar/desmarcar `vigente` al refetchear | Actualización masiva por snapshot |
| `ix_vigente_parcial` | `{vigente: 1}` **parcial** `{vigente: true}` | Cualquier consulta del snapshot activo | Índice parcial: solo ~516 entradas de los ~2 500 totales |

---

## 4. Colección `excel_filas`

### 4.1 Propósito

Volcado **sin pérdida** del Excel caótico. El enunciado lo describe como
"caótico" y el plan advierte de no asumir esquema: por eso `campos` es un mapa
libre y `indices` contiene solo las claves normalizadas que el sistema usa para
buscar.

**Patrón:** referencia. El expediente guarda los `fila_id` relevantes en
`evidencia.excel_filas`.

**Clave:** `_id = "<excel_id>#<hoja>#<indice>"`.

### 4.2 Validador `$jsonSchema`

```javascript
db.createCollection("excel_filas", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "excel_id", "hoja", "indice", "campos"],
      properties: {
        _id: { bsonType: "string", description: "<excel_id>#<hoja>#<indice>" },
        excel_id: { bsonType: "string" },
        hoja: { bsonType: "string" },
        indice: { bsonType: "int", minimum: 0 },
        campos: {
          bsonType: "object",
          description: "Volcado flexible: cabecera -> valor, tal cual",
          additionalProperties: { bsonType: ["string", "double", "int", "long", "decimal", "bool", "null"] }
        },
        indices: {
          bsonType: "object",
          description: "Claves normalizadas para búsqueda",
          properties: {
            nif: { bsonType: ["string", "null"] },
            pedido: { bsonType: ["string", "null"] }
          }
        },
        esquema_version: { bsonType: "int", minimum: 1 }
      }
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 4.3 Documento de ejemplo

```javascript
{
  _id: "FINAL_v7#Hoja1#42",
  excel_id: "FINAL_v7_DEFINITIVO_ahorasi.xlsx",
  hoja: "Hoja1",
  indice: 42,
  campos: {
    "Proveedor": "Suministros Ibéricos S.L.",
    "CIF": "B12345678",
    "Nº Pedido": "PED-2024-0912",
    "Importe": "1.234,50",
    "Observaciones": "pendiente de confirmar"
  },
  indices: { nif: "B12345678", pedido: "PED-2024-0912" },
  esquema_version: 1
}
```

### 4.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` | Lectura directa | Clave natural |
| `ix_indices_nif` | `{"indices.nif": 1}` | **Cruce Excel por NIF** | Búsqueda de contexto por proveedor |
| `ix_indices_pedido` | `{"indices.pedido": 1}` | **Cruce Excel por pedido** | Búsqueda de contexto por pedido |
| `ix_excel_hoja_fila` | `{excel_id: 1, hoja: 1, indice: 1}` | Reconstruir el orden original | Recarga y verificación del volcado |

---

## 5. Colección `reglas_versiones`

### 5.1 Propósito

Versionado **inmutable** de `config/reglas.toml`. Es la pieza que hace que la
regla nueva del sábado 18:00 sea auditable: cada decisión apunta a la versión
exacta de las reglas con la que se tomó.

**Decisión de diseño:** se guarda el **TOML completo** además de los campos
estructurados. Motivo: una decisión debe ser 100 % reconstruible, y una regla
futura no parametrizable (p. ej. `prohibido_pagar_proveedor`) no tendría campo
propio. Los campos estructurados se derivan por proyección para consultas
rápidas.

**Clave:** `_id = version` (entero incremental).

### 5.2 Validador `$jsonSchema`

```javascript
db.createCollection("reglas_versiones", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "hash", "contenido_toml", "vigente", "creado_en"],
      properties: {
        _id: { bsonType: "int", minimum: 1 },
        hash: { bsonType: "string", pattern: "^[a-f0-9]{64}$" },
        contenido_toml: { bsonType: "string", minLength: 1 },
        vigente: { bsonType: "bool" },
        creado_en: { bsonType: "date" },
        descripcion: { bsonType: ["string", "null"] },
        parametros: {
          bsonType: "object",
          description: "Proyección estructurada del TOML para consultas",
          properties: {
            tolerancia_importe: { bsonType: ["decimal", "null"] },
            umbral_pago_maximo: { bsonType: ["decimal", "null"] },
            score_minimo: { bsonType: ["double", "null"] },
            prohibido_pagar_proveedor: {
              bsonType: ["array", "null"],
              items: { bsonType: "string" }
            },
            retener_iva: { bsonType: ["bool", "null"] }
          }
        },
        esquema_version: { bsonType: "int", minimum: 1 }
      }
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 5.3 Documento de ejemplo

```javascript
{
  _id: 3,
  hash: "9f2c1a7b4e8d3f6a0b5c9e2d7f4a1b8c3e6d9f2a5b8c1e4d7f0a3b6c9e2d5f8a",
  contenido_toml: "tolerancia_importe = 0.02\numbral_pago_maximo = 5000.00\nscore_minimo = 0.85\n",
  vigente: true,
  creado_en: ISODate("2026-09-19T09:00:00Z"),
  descripcion: "Reglas iniciales del lote 1",
  parametros: {
    tolerancia_importe: NumberDecimal("0.02"),
    umbral_pago_maximo: NumberDecimal("5000.00"),
    score_minimo: 0.85,
    prohibido_pagar_proveedor: null,
    retener_iva: null
  },
  esquema_version: 1
}
```

### 5.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` | Lectura por versión (desde `decision.huellas`) | Clave natural |
| `ix_vigente_parcial` | `{vigente: 1}` **parcial** `{vigente: true}` | "¿cuál es la versión activa?" | Índice parcial: solo 1 documento |
| `ix_hash` | `{hash: 1}` | Evitar duplicar una versión idéntica | Idempotencia de la carga de reglas |

---

## 6. Colección `erp_snapshots`

### 6.1 Propósito

Metadatos de cada descarga completa del ERP. Permite saber qué foto del ERP
estaba vigente cuando se tomó cada decisión, y detectar decisiones obsoletas
tras un refetch.

**Clave:** `_id = snapshot_id` (timestamp ISO compacto).

### 6.2 Validador `$jsonSchema`

```javascript
db.createCollection("erp_snapshots", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "descargado_en", "total_asientos", "vigente", "estado"],
      properties: {
        _id: { bsonType: "string" },
        descargado_en: { bsonType: "date" },
        total_asientos: { bsonType: "int", minimum: 0 },
        paginas: { bsonType: "int", minimum: 0 },
        vigente: { bsonType: "bool" },
        estado: { enum: ["EN_CURSO", "COMPLETO", "FALLIDO"] },
        reintentos: {
          bsonType: "object",
          properties: {
            ora_00600: { bsonType: "int", minimum: 0 },
            ses_401: { bsonType: "int", minimum: 0 },
            erp_429: { bsonType: "int", minimum: 0 }
          }
        },
        duracion_ms: { bsonType: "int", minimum: 0 },
        esquema_version: { bsonType: "int", minimum: 1 }
      }
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 6.3 Documento de ejemplo

```javascript
{
  _id: "snap-2026-09-19T09-00-00Z",
  descargado_en: ISODate("2026-09-19T09:00:00Z"),
  total_asientos: 516,
  paginas: 26,
  vigente: true,
  estado: "COMPLETO",
  reintentos: { ora_00600: 44, ses_401: 3, erp_429: 0 },
  duracion_ms: 8420,
  esquema_version: 1
}
```

### 6.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` | Lectura por snapshot | Clave natural |
| `ix_vigente_parcial` | `{vigente: 1}` **parcial** `{vigente: true}` | "¿cuál es el snapshot activo?" | Índice parcial: solo 1 documento |
| `ix_descargado` | `{descargado_en: -1}` | Historial de descargas | Orden cronológico inverso |

---

## 7. Colección `ejecuciones`

### 7.1 Propósito

Un documento por ejecución del pipeline. Contiene los contadores agregados que
la defensa necesita citar: distribución de resultados, hits/misses del ERP,
reintentos por tipo, latencias, coste.

**Clave:** `_id = run_id`.

**Escritura:** los contadores se actualizan con **`$inc` atómico** sobre este
único documento. No se usan transacciones: cada worker incrementa su contador
sin coordinación, y MongoDB garantiza la atomicidad de la operación sobre un
documento.

### 7.2 Validador `$jsonSchema`

```javascript
db.createCollection("ejecuciones", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "lote_id", "inicio", "estado"],
      properties: {
        _id: { bsonType: "string" },
        lote_id: { enum: ["lote1", "lote2"] },
        inicio: { bsonType: "date" },
        fin: { bsonType: ["date", "null"] },
        estado: { enum: ["EN_CURSO", "COMPLETADO", "FALLIDO"] },
        reglas_version: { bsonType: ["int", "null"] },
        erp_snapshot_id: { bsonType: ["string", "null"] },
        contadores: {
          bsonType: "object",
          properties: {
            total_facturas: { bsonType: "int", minimum: 0 },
            pagar: { bsonType: "int", minimum: 0 },
            no_pagar: { bsonType: "int", minimum: 0 },
            escalar: { bsonType: "int", minimum: 0 },
            erp_hits: { bsonType: "int", minimum: 0 },
            erp_misses: { bsonType: "int", minimum: 0 },
            ocr_fallos: { bsonType: "int", minimum: 0 },
            reintentos_erp: { bsonType: "int", minimum: 0 },
            coste_total_cents: { bsonType: "int", minimum: 0 }
          }
        },
        latencias_ms: {
          bsonType: "object",
          properties: {
            ocr_p50: { bsonType: "int", minimum: 0 },
            ocr_p95: { bsonType: "int", minimum: 0 },
            parse_p50: { bsonType: "int", minimum: 0 },
            decide_p50: { bsonType: "int", minimum: 0 },
            total_s: { bsonType: "double", minimum: 0 }
          }
        },
        esquema_version: { bsonType: "int", minimum: 1 }
      }
    }
  },
  validationLevel: "strict",
  validationAction: "error"
});
```

### 7.3 Documento de ejemplo

```javascript
{
  _id: "run-20260919-091402",
  lote_id: "lote1",
  inicio: ISODate("2026-09-19T09:14:02Z"),
  fin: ISODate("2026-09-19T09:17:09Z"),
  estado: "COMPLETADO",
  reglas_version: 3,
  erp_snapshot_id: "snap-2026-09-19T09-00-00Z",
  contadores: {
    total_facturas: 500,
    pagar: 234, no_pagar: 118, escalar: 148,
    erp_hits: 421, erp_misses: 79,
    ocr_fallos: 2, reintentos_erp: 47,
    coste_total_cents: 0
  },
  latencias_ms: { ocr_p50: 1180, ocr_p95: 2410, parse_p50: 12, decide_p50: 1, total_s: 187.4 },
  esquema_version: 1
}
```

### 7.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `_id_` | `{_id: 1}` | Lectura por run | Clave natural |
| `ix_lote_inicio` | `{lote_id: 1, inicio: -1}` | "última ejecución del lote 1" | Filtro + orden sin `SORT` en memoria |
| `ix_estado_parcial` | `{estado: 1}` **parcial** `{estado: "EN_CURSO"}` | Detectar runs colgados | Índice parcial: normalmente vacío |

---

## 8. Colección `eventos` (time-series)

### 8.1 Propósito

Flujo de señales operativas: reintentos del ERP por tipo, fallos del OCR,
cache hits, cambios de estado. Es la materia prima de las métricas de
observabilidad (20 puntos de la rúbrica).

**Tipo:** **time-series**. Motivo: los eventos son append-only, con marca
temporal, y se consultan siempre por rango temporal y agrupados por tipo. El
almacenamiento columnar de las time-series reduce drásticamente el espacio y
acelera las agregaciones frente a una colección normal.

**TTL:** 90 días. Los eventos son señales operativas, no evidencia contable.

### 8.2 Definición

```javascript
db.createCollection("eventos", {
  timeseries: {
    timeField: "ts",
    metaField: "run_id",
    granularity: "seconds"
  },
  expireAfterSeconds: 7776000   // 90 días
});
```

> ⚠️ **Una colección time-series NO admite `$jsonSchema` en MongoDB 7.0.**
> Verificado empíricamente contra `mongo:7.0`:
>
> - En `createCollection`:
>   `MongoServerError: albertitos.eventos: 'timeseries' is not allowed with 'validator'`
> - En `collMod` posterior:
>   `MongoServerError: option not supported on a time-series collection: validator`
>
> Es decir: no hay forma de adjuntar un validador, ni al crear ni después.
> **La integridad de `eventos` se hace cumplir en la capa de aplicación:**
> `obs.rs::emitir_evento` es el **único** escritor de esta colección y aplica el
> contrato de campos de la tabla siguiente. La colección `system.buckets.eventos`
> sí muestra un `validator`, pero es el esquema interno de buckets que MongoDB
> crea automáticamente — no es nuestro.

**Contrato de campos (aplicado por `obs.rs::emitir_evento`):**

| Campo | Tipo | Obligatorio | Restricción |
|---|---|---|---|
| `ts` | `date` | sí | `timeField` de la time-series |
| `run_id` | `string` | sí | `metaField` de la time-series |
| `tipo` | `string` | sí | `enum` de 11 valores (ver abajo) |
| `file_id` | `string` \| `null` | no | Nombre del PDF; `null` en eventos de run |
| `detalle` | `object` | no | Libre; contexto específico del evento |
| `duracion_ms` | `int` \| `null` | no | `>= 0` |
| `esquema_version` | `int` | no | `>= 1` |

Valores permitidos de `tipo`:

```
ERP_RETRY_ORA_00600, ERP_RETRY_SES_401, ERP_RETRY_ERP_429,
ERP_CACHE_HIT, ERP_CACHE_MISS,
OCR_FAIL, OCR_OK,
EXPEDIENTE_ESTADO, DECISION_EMITIDA,
REVISION_ABIERTA, REVISION_RESUELTA
```

Como el motor no puede rechazar un `tipo` inválido, el `enum` se implementa en
Rust como un `enum` con `serde` — la única vía de escritura pasa por él, así que
un valor fuera de la lista es un error de compilación, no un dato corrupto.

### 8.3 Documento de ejemplo

```javascript
{
  ts: ISODate("2026-09-19T09:14:31.204Z"),
  run_id: "run-20260919-091402",
  tipo: "ERP_RETRY_ORA_00600",
  file_id: null,
  detalle: { pagina: 11, intento: 2, backoff_ms: 400 },
  duracion_ms: 412
}
```

### 8.4 Índices

| Índice | Definición | Patrón de acceso | Justificación |
|---|---|---|---|
| `ix_run_tipo_ts` | `{run_id: 1, tipo: 1, ts: 1}` | **Métricas de un run**: agrupar por tipo | Índice compuesto que sirve el `$match` + `$group` completo |
| `ix_tipo_ts` | `{tipo: 1, ts: -1}` | "últimos fallos de OCR" | Filtro por tipo con orden temporal inverso |
| `ix_file_ts` | `{file_id: 1, ts: 1}` | Traza temporal de una factura concreta | Seguimiento puntual de un expediente |

> **Nota 1 — índices creados automáticamente.** Al crear una time-series,
> MongoDB crea automáticamente `run_id_1_ts_1` sobre `{run_id: 1, ts: 1}`
> (a partir del `metaField` y del `timeField`). Los tres índices anteriores son
> adicionales. `run_id_1_ts_1` queda **redundante** con el prefijo de
> `ix_run_tipo_ts` (`{run_id, tipo, ts}` también sirve `{run_id, ts}`), así que
> es un candidato a eliminar cuando importe el espacio: ocupa el mismo orden de
> magnitud que `ix_run_tipo_ts`. Se mantiene porque el coste es despreciable a
> esta escala y porque eliminar un índice auto-creado de una time-series es una
> operación no reversible sin recrear la colección.
>
> **Nota 2 — los índices se materializan sobre los buckets.** En
> `system.buckets.eventos` los índices aparecen transformados y los nombres de
> campo llevan el prefijo `meta` / `control.min.` / `control.max.`:
>
> ```
> ix_run_tipo_ts  {"meta":1,"control.min.tipo":1,"control.max.tipo":1,
>                  "control.min.ts":1,"control.max.ts":1}
> ix_tipo_ts      {"control.min.tipo":1,"control.max.tipo":1,
>                  "control.max.ts":-1,"control.min.ts":-1}
> ix_file_ts      {"control.min.file_id":1,"control.max.file_id":1,
>                  "control.min.ts":1,"control.max.ts":1}
> ```
>
> Consecuencia práctica: **toda consulta directa a `system.buckets.eventos` debe
> acotar por rango temporal** para ser elegible de índice. Una consulta sin
> límite temporal sobre la colección de buckets no usa índice; sobre la vista
> `eventos` el motor empuja el predicado y sí lo usa.
>
> **Nota 3 — `explain()` en una time-series devuelve otra forma.** `eventos` es
> una vista sobre `system.buckets.eventos`, así que
> `db.eventos.find(...).explain("executionStats")` **no** devuelve
> `queryPlanner` ni `executionStats` en la raíz. La forma es:
> `explainVersion, stages, serverInfo, serverParameters, command, ok`.
> El plan está en `ex.stages[0]["$cursor"].queryPlanner.winningPlan` y las
> estadísticas en `ex.stages[0]["$cursor"].executionStats`. Verificado: los
> tres patrones usan `IXSCAN` con `docsExamined == nReturned` y ningún
> `COLLSCAN` (`ix_run_tipo_ts` → 9/9; `ix_tipo_ts` → 90/90; `ix_file_ts` → 55/55).

### 8.5 Agregación de métricas (una sola consulta)

```javascript
// Reintentos por tipo en un run — resuelve el requisito de observabilidad
db.eventos.aggregate([
  { $match: { run_id: "run-20260919-091402", tipo: { $regex: "^ERP_RETRY_" } } },
  { $group: { _id: "$tipo", n: { $sum: 1 }, ms_total: { $sum: "$duracion_ms" } } },
  { $sort: { n: -1 } }
]);
// → [{ _id: "ERP_RETRY_ORA_00600", n: 44, ms_total: 18120 },
//    { _id: "ERP_RETRY_SES_401",   n: 3,  ms_total: 1240 }]
```

### 8.6 Advertencia: patrón *no* cubierto

**Consultas por rango de `ts` sin filtro por `run_id`, `tipo` ni `file_id` no
están cubiertas por un índice dedicado.** Por ejemplo "últimos 100 eventos del
sistema". Estas consultas hacen un *scan* de buckets acotado por el rango
temporal, lo que es aceptable a esta escala porque el TTL limita la colección a
90 días. Si en el futuro aparece un panel que solo filtre por tiempo, el plan
correcto es añadir un índice sobre `{ts: -1}` — pero **no** antes de necesitarlo
(YAGNI): ningún patrón de la §11 lo requiere hoy.

---

## 9. GridFS — bucket `pdfs`

### 9.1 Propósito

Almacenar los PDFs originales dentro de MongoDB, de modo que **exista un único
almacén operativo** y el backup sea coherente (documentos + binarios en el
mismo `mongodump`).

### 9.2 Configuración

```javascript
// Bucket por defecto: "pdfs" -> colecciones pdfs.files y pdfs.chunks
// Se crea implícitamente al primer upload, o explícitamente:
db.createCollection("pdfs.files");
db.createCollection("pdfs.chunks");
```

### 9.3 Metadatos de `pdfs.files`

```javascript
{
  _id: ObjectId("651f2a1c9e4b0d0012ab34cd"),
  length: NumberLong(184320),
  chunkSize: 261120,          // 255 KB (por defecto)
  uploadDate: ISODate("2026-09-19T09:14:02.113Z"),
  filename: "factura_5518.pdf",
  metadata: {
    file_id: "factura_5518.pdf",   // enlace con expedientes._id
    lote_id: "lote1",
    sha256: "3f786850e387550fdab836ed7e6dc881de23001b1a2b3c4d5e6f708192a3b4c5",
    content_type: "application/pdf"
  }
}
```

### 9.4 Índices

| Índice | Colección | Definición | Patrón de acceso |
|---|---|---|---|
| `ix_filename` | `pdfs.files` | `{filename: 1}` | Recuperar el PDF por nombre |
| `ix_meta_file_id` | `pdfs.files` | `{"metadata.file_id": 1}` | Enlace desde el expediente |
| `ix_meta_sha256` | `pdfs.files` | `{"metadata.sha256": 1}` | Deduplicación por contenido |
| `ix_files_id_n` | `pdfs.chunks` | `{files_id: 1, n: 1}` (creado por GridFS) | Reensamblado de chunks en orden |

### 9.5 Streaming

El driver Rust usa `GridFsBucket::open_upload_stream` / `open_download_stream`,
de modo que un PDF nunca se carga entero en memoria. A 540 PDFs de ~180 KB el
impacto es nulo, pero a 50 000 (≈ 9 GB) es la diferencia entre funcionar y no
funcionar.

---

## 10. Colección `migraciones`

### 10.1 Propósito

Registro de las migraciones de esquema aplicadas (validadores, índices,
colecciones). Garantiza que aplicar migraciones sea **idempotente**: si la
versión ya está registrada, se salta.

### 10.2 Definición

```javascript
db.createCollection("migraciones", {
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["_id", "aplicada_en", "descripcion"],
      properties: {
        _id: { bsonType: "int", minimum: 1 },
        aplicada_en: { bsonType: "date" },
        descripcion: { bsonType: "string" },
        hash_script: { bsonType: ["string", "null"] }
      }
    }
  }
});
```

### 10.3 Estrategia de migración

**Recomendado para el hackathon:** script `mongosh` idempotente
(`docker/mongosh/02-schema-init.js`) que se ejecuta en el arranque del
contenedor. Ventajas: cero código Rust, reproducible, versionado en git.

> **Qué NO va aquí.** La fase de initdb solo crea **objetos**: colecciones,
> validadores, índices y usuarios. **No** inicializa el replica set
> (`rs.initiate()`): el mongod temporal de esa fase se lanza sin `--replSet`, así
> que la llamada fallaría siempre y —peor— **en silencio**. Esa tarea vive en el
> servicio de un solo uso `mongo-init` (`docker/init/`). Detalle en §13.4.
> De igual modo, los fallos de un script de initdb no abortan el arranque: hay
> que verificar los **objetos creados**, nunca la línea de log (§13.6b).

**Evolución futura:** subcomando `cargo run -- --migrate` que aplica los mismos
cambios desde el binario, leyendo los scripts de `migrations/`. Se documenta
como evolución, no se implementa ahora.

**Regla de oro:** toda migración debe ser **idempotente** (`createCollection`
con `$exists` check, `createIndex` es idempotente por naturaleza,
`collMod` para actualizar validadores).

---

## 11. Mapa de índices ↔ patrones de acceso

Esta tabla es la justificación 1:1 de cada índice. **Ningún índice existe sin un
patrón de acceso que lo consuma.**

| # | Patrón de acceso | Colección | Consulta | Índice usado |
|---|---|---|---|---|
| P1 | ¿Ya está procesada esta factura? (idempotencia) | `expedientes` | `findOne({_id: file_id})` | `_id_` |
| P2 | Match por pedido (estrategia fuerte) | `asientos` | `findOne({pedido, vigente: true})` | `ix_pedido_vigente` |
| P3 | Match por (nif, importe ± tolerancia) | `asientos` | `find({nif, vigente: true, importe: {$gte, $lte}})` | `ix_nif_importe_vigente` |
| P4 | Match por NIF único (estrategia débil) | `asientos` | `find({nif, vigente: true})` | `ix_nif_vigente` |
| P5 | Cruce con Excel por NIF | `excel_filas` | `find({"indices.nif": nif})` | `ix_indices_nif` |
| P6 | Cruce con Excel por pedido | `excel_filas` | `find({"indices.pedido": pedido})` | `ix_indices_pedido` |
| P7 | Cola de trabajo del operador (UI) | `expedientes` | `find({lote_id, "decision.resultado": "ESCALAR"})` | `ix_lote_resultado` + `ix_escalar_pendiente` |
| P8 | Métricas de un run | `eventos` | `aggregate([{$match: {run_id}}, {$group: {_id: "$tipo"}}])` | `ix_run_tipo_ts` |
| P9 | Decisiones obsoletas tras refetch del ERP | `expedientes` | `find({"decision.huellas.erp_snapshot_id": {$ne: vigente}})` | `ix_snapshot_obsoleto` |
| P10 | Decisiones obsoletas tras cambio de reglas | `expedientes` | `find({"decision.huellas.reglas_version": {$lt: vigente}})` | `ix_reglas_version` (acotado por lote: `ix_lote_resultado`) |
| P11 | Export JSONL de entrega | `expedientes` | `find({lote_id}, {_id: 1, "decision.resultado": 1}).sort({_id: 1})` | `ix_lote_resultado` |
| P12 | Detección de duplicados | `expedientes` | `find({huella_negocio})` | `ix_huella_negocio` |
| P13 | Trazabilidad inversa ERP → facturas | `expedientes` | `find({"evidencia.asiento_id": asiento_id})` | `ix_asiento` |
| P14 | Recuperar el PDF original | `pdfs.files` | `findOne({"metadata.file_id": file_id})` | `ix_meta_file_id` |
| P15 | Versión de reglas activa | `reglas_versiones` | `findOne({vigente: true})` | `ix_vigente_parcial` |
| P16 | Snapshot del ERP activo | `erp_snapshots` | `findOne({vigente: true})` | `ix_vigente_parcial` |
| P17 | Última ejecución de un lote | `ejecuciones` | `find({lote_id}).sort({inicio: -1}).limit(1)` | `ix_lote_inicio` |
| P18 | Todas las facturas de un proveedor | `expedientes` | `find({"factura.nif_emisor.valor": nif})` | `ix_nif` |
| P19 | Facturas que comparten un pedido (duplicados) | `expedientes` | `find({"factura.pedido.valor": pedido})` | `ix_pedido` |

---

## 12. Estrategia de escritura

### 12.1 Idempotencia por upsert

```javascript
// Reprocesar una factura es seguro: el _id es el file_id
db.expedientes.updateOne(
  { _id: "factura_5518.pdf" },
  {
    $set: {
      lote_id: "lote1",
      estado_proceso: "COMPLETADA",
      huella_negocio: "B12345678|PED-2024-0912",
      documento: { /* ... */ },
      ocr: { /* ... */ },
      factura: { /* ... */ },
      evidencia: { /* ... */ },
      decision: { /* ... */ },
      actualizado_en: new Date()
    },
    $setOnInsert: { creado_en: new Date(), esquema_version: 1 }
  },
  { upsert: true }
);
```

### 12.2 Contadores con `$inc` atómico

```javascript
// Sin transacciones: cada worker incrementa su contador sin coordinación
db.ejecuciones.updateOne(
  { _id: "run-20260919-091402" },
  {
    $inc: {
      "contadores.total_facturas": 1,
      "contadores.pagar": 1
    },
    $set: { actualizado_en: new Date() }
  }
);
```

### 12.3 Ingesta masiva del ERP

```javascript
// ordered: false -> si un asiento falla, el resto continúa
db.asientos.bulkWrite(
  operaciones,   // [{ updateOne: { filter: {_id}, update: {$set: {...}}, upsert: true } }, ...]
  { ordered: false }
);
```

**Protocolo del snapshot:** el snapshot se crea con `estado: "EN_CURSO"` y
`vigente: false`; se insertan los asientos; al terminar se marca
`estado: "COMPLETO"` y `vigente: true`, y se desmarca el anterior. Si el proceso
muere a mitad, el snapshot queda `EN_CURSO` y **nunca se marca vigente** → el
sistema sigue usando el snapshot anterior. **Nunca hay un snapshot a medias
considerado válido.**

### 12.4 `writeConcern` por colección

| Colección | `writeConcern` | Motivo |
|---|---|---|
| `expedientes` | `{ w: "majority" }` | Evidencia contable: no se puede perder |
| `reglas_versiones` | `{ w: "majority" }` | Auditoría de la regla del sábado |
| `erp_snapshots` | `{ w: "majority" }` | Determina la validez de las decisiones |
| `asientos`, `excel_filas` | `{ w: 1 }` | Recargables desde la fuente |
| `ejecuciones` | `{ w: 1 }` | Métricas, no evidencia |
| `eventos` | `{ w: 1 }` | Señales operativas, descartables |

---

## 13. Docker Compose

### 13.1 `docker-compose.yml`

```yaml
services:
  mongo:
    image: mongo:7.0
    container_name: albertitos-mongo
    restart: unless-stopped
    # Envuelve al entrypoint oficial para generar el keyfile antes del arranque.
    entrypoint: ["/usr/local/bin/mongo-keyfile-entrypoint.sh"]
    command:
      - "--replSet"
      - "rs0"
      - "--keyFile"
      - "/data/configdb/keyfile"
      - "--bind_ip_all"
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_ROOT_USER}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_ROOT_PASSWORD}
      MONGO_INITDB_DATABASE: ${MONGO_DB:-albertitos}
      MONGO_APP_USER: ${MONGO_APP_USER}
      MONGO_APP_PASSWORD: ${MONGO_APP_PASSWORD}
      MONGO_DB: ${MONGO_DB:-albertitos}
    ports:
      # Solo accesible desde la propia máquina. No cambiar a 0.0.0.0.
      - "127.0.0.1:${MONGO_PORT:-27017}:27017"
    volumes:
      - mongo_data:/data/db
      # El keyfile vive fuera del dbPath y en su propio volumen para no
      # regenerarse en cada "docker compose up".
      - mongo_config:/data/configdb
      - ./docker/mongo/entrypoint.sh:/usr/local/bin/mongo-keyfile-entrypoint.sh:ro
      # Scripts de inicialización: se ejecutan una sola vez, en el primer
      # arranque con el volumen vacío. Deben ser idempotentes.
      - ./docker/mongosh:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "mongosh --quiet --host 127.0.0.1 --eval 'db.adminCommand({ping:1}).ok' || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
    networks:
      - albertitos

  # Arranque de un solo uso: inicia el replica set y espera al PRIMARY.
  # Termina con éxito y se queda parado (restart: "no").
  mongo-init:
    image: mongo:7.0
    container_name: albertitos-mongo-init
    restart: "no"
    depends_on:
      mongo:
        condition: service_healthy
    entrypoint: ["/bin/sh", "/init/run.sh"]
    environment:
      MONGO_INIT_HOST: mongo:27017
      # Miembro anunciado del replica set. Se usa 127.0.0.1 (y no el nombre del
      # servicio) para que un cliente que corra en la MÁQUINA ANFITRIONA pueda
      # resolverlo con replicaSet=rs0 sin necesitar directConnection=true.
      MONGO_RS_HOST: 127.0.0.1:27017
      MONGO_ROOT_USER: ${MONGO_ROOT_USER}
      MONGO_ROOT_PASSWORD: ${MONGO_ROOT_PASSWORD}
    volumes:
      - ./docker/init:/init:ro
    networks:
      - albertitos

volumes:
  mongo_data:
    name: albertitos_mongo_data
  mongo_config:
    name: albertitos_mongo_config

networks:
  albertitos:
    name: albertitos_net
```

### 13.2 `.env.example`

```dotenv
# Copiar a .env y rellenar. .env NO se versiona.
MONGO_ROOT_USER=albertitos_admin
MONGO_ROOT_PASSWORD=cambiame_en_produccion
MONGO_APP_USER=albertitos_app
MONGO_APP_PASSWORD=cambiame_tambien
MONGO_PORT=27017
MONGO_DB=albertitos
```

### 13.3 `--keyFile`: por qué un replica set lo necesita

Un replica set de 1 nodo sigue siendo un replica set: sus miembros se
autentican **entre sí** además de autenticar a los clientes. MongoDB exige para
ello un `--keyFile` compartido. Sin él:

```
BadValue: security.keyFile is required when authorization is enabled with replica sets
```

La imagen oficial `mongo:7.0` **no genera el keyfile**. Por eso se envuelve el
entrypoint oficial con `docker/mongo/entrypoint.sh`, que lo crea antes de
delegar en él:

```sh
#!/bin/sh
set -eu

KEYFILE="${MONGO_KEYFILE_PATH:-/data/configdb/keyfile}"

if [ ! -s "$KEYFILE" ]; then
	echo "[mongo-entrypoint] Generando keyfile de autenticacion interna en ${KEYFILE}"
	mkdir -p "$(dirname "$KEYFILE")"
	openssl rand -base64 756 >"$KEYFILE"
	chmod 400 "$KEYFILE"
	chown mongodb:mongodb "$KEYFILE"
else
	echo "[mongo-entrypoint] Reutilizando keyfile existente en ${KEYFILE}"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
```

Hasta seis decisiones viven en esas nueve líneas:

1. **`openssl rand -base64 756`** es exactamente lo que exige la documentación
   oficial: 756 bytes en base64. Un keyfile más corto es rechazado.
2. **`chmod 400` + `chown mongodb:mongodb`** son obligatorios: mongod rechaza
   un keyfile legible por otros o con otro propietario.
3. **El keyfile vive en `/data/configdb`, no en `/data/db`.** Así sobrevive a
   `docker compose down` gracias al volumen `mongo_config`, sin mezclarse con
   los ficheros de datos ni aparecer en los backups de `mongodump`.
4. **La comprobación `[ ! -s "$KEYFILE" ]` lo hace idempotente.** Regenerarlo en
   cada arranque cambiaría el secreto mientras los demás miembros aún usan el
   antiguo. Verificado en logs: "Generando…" en el primer arranque,
   "Reutilizando…" en todos los siguientes.
5. **`exec`** sustituye el proceso para que mongod reciba las señales
   (`SIGTERM`) directamente y el `stop` del contenedor siga siendo limpio.
6. **`--auth` no se pasa nunca.** `keyFile` *implica*
   `security.authorization: enabled`; añadir `--auth` además es redundante. Se
   documenta porque es una fuente clásica de confusión.

### 13.4 Por qué `rs.initiate()` NO puede ir en `/docker-entrypoint-initdb.d`

Es el error que provoca un bucle de reinicio (`Restarting (2)`) y este mensaje:

```
MongoServerError: This node was not started with replication enabled
```

El entrypoint oficial, cuando arranca los scripts de
`/docker-entrypoint-initdb.d`, lanza un **mongod temporal** y **elimina
argumentos deliberadamente**:

| Argumento | ¿Lo conserva el mongod temporal? |
|---|---|
| `--bind_ip_all` | ❌ sustituido por `--bind_ip 127.0.0.1` |
| `--auth` | ❌ eliminado |
| `--keyFile` | ❌ eliminado |
| `--replSet` | ❌ eliminado **cuando** hay `MONGO_INITDB_ROOT_USERNAME` + `MONGO_INITDB_ROOT_PASSWORD` |

Ese mongod temporal también arranca `--fork` y sirve únicamente para crear el
usuario root y ejecutar los scripts de initdb. Como no tiene `--replSet`, un
`rs.initiate()` allí **siempre** falla. Por tanto:

> **Regla:** los scripts de `/docker-entrypoint-initdb.d` solo crean colecciones,
> validadores, índices y usuarios. **Nunca** inician el replica set.

El replica set se inicia después, cuando mongod ya corre con su `--replSet`,
desde un servicio de un solo uso: **`mongo-init`**.

### 13.5 Servicio `mongo-init`

Dos scripts, separados para poder ejecutarlos a mano durante el desarrollo:

- **`docker/init/run.sh`** → vive en el contenedor `mongo-init`. Espera a que
  mongod responda (`ping`), construye la URI con las credenciales de root y
  delega en `replicaset.js`, reintentando.
- **`docker/init/replicaset.js`** → `mongosh` idempotente. Si `rs.status()`
  devuelve `ok: 1` con algún miembro en `PRIMARY`, no hace nada; si no, llama a
  `rs.initiate({_id: "rs0", members: [{_id: 0, host: "127.0.0.1:27017"}]})`.

```javascript
// Inicia el replica set de 1 nodo. Idempotente.
try {
  rs.status();                                  // ya iniciado → no tocar nada
  print("[init] Replica set ya iniciado.");
} catch (e) {
  print("[init] Iniciando replica set rs0...");
  rs.initiate({
    _id: "rs0",
    members: [{ _id: 0, host: "127.0.0.1:27017" }]
  });
}
```

Detalles que importan:

- **`restart: "no"`** — es un servicio de arranque, no un daemon. Termina con
  `Exited (0)` y ahí se queda.
- **`depends_on: condition: service_healthy`** — no basta con `depends_on` a
  secas: mongod tarda en aceptar conexiones y el script fallaría.
- **El miembro se anuncia como `127.0.0.1:27017`**, no como `mongo:27017`. Así
  un cliente de la máquina anfitriona resuelve el replica set con
  `replicaSet=rs0` sin necesitar `directConnection=true`. Como el puerto está
  publicado en `127.0.0.1`, la dirección del miembro es alcanzable desde ambos
  lados.
- **Es idempotente y seguro de re-ejecutar.** Tras un `docker compose down`, el
  contenedor se recrea y `mongo-init` vuelve a correr; detecta el `PRIMARY` ya
  existente y sale inmediatamente.

### 13.6 `docker/mongosh/` — scripts de initdb

Se ejecutan **una sola vez**, en el primer arranque con el volumen vacío, en
orden alfabético:

| Script | Responsabilidad |
|---|---|
| `01-app-user.js` | Crea `albertitos_app` con `readWrite` **solo** sobre `albertitos`. Idempotente (`getUser` → `updateUser`/`createUser`). |
| `02-schema-init.js` | Crea las 9 colecciones, sus validadores `$jsonSchema`, los índices y la time-series `eventos`. Siembra `migraciones` con `_id: 1`. Idempotente. |

> ⚠️ **Dos trampas de JavaScript que ya se pisaron y están resueltas en el
> fichero. No reintroducirlas:**
>
> **(a) `const db = db.getSiblingDB(...)` es siempre un `ReferenceError`.**
> La declaración `const db` crea un *binding* en todo el bloque, así que la
> lectura de `db` del lado derecho cae en su **zona muerta temporal** (TDZ):
>
> ```
> ReferenceError: Cannot access 'db' before initialization
> ```
>
> La solución es pasar `db` como **parámetro** del IIFE, no declararlo dentro:
>
> ```javascript
> (function (db) {
>   const dbName = process.env.MONGO_DB || "albertitos";
>   // ...todas las referencias `db.` siguen siendo válidas...
> })(db.getSiblingDB(process.env.MONGO_DB || "albertitos"));
> ```
>
> El script hermano `01-app-user.js` evita el problema de otro modo, con un
> alias: `const target = db.getSiblingDB(dbName);`
>
> **(b) Los fallos de los scripts de initdb son SILENCIOSOS.** Un script que
> lanza una excepción **no** detiene el contenedor: mongod arranca, el
> healthcheck pasa y `docker compose ps` muestra `Up (healthy)`. En el log solo
> aparece la línea inocente `running /docker-entrypoint-initdb.d/02-schema-init.js`.
> La trampa (a) mantuvo el esquema sin crear durante varios ciclos pareciendo
> todo correcto.
>
> > **Regla:** tras el primer arranque, verificar **los objetos**, nunca la
> > línea del log. `db.getCollectionNames()` y `db.getCollectionInfos()` son la
> > prueba; el log no lo es.

### 13.7 `.gitattributes` — LF obligatorio en los scripts montados

Los scripts `.sh` se montan dentro de contenedores Linux y los ejecuta
`/bin/sh`. Si git los convierte a CRLF en Windows, el contenedor falla con
`exec format error` o con `\r: not found`. Por eso:

```gitattributes
# Los scripts montados en contenedores Linux deben conservar finales de línea LF.
# /bin/sh falla con CRLF ("\r: not found" o "exec format error").
docker/**/*.sh text eol=lf
docker/**/*.js text eol=lf
*.sh text eol=lf
```

Verificado: `docker/mongo/entrypoint.sh` (40 líneas) y `docker/init/run.sh`
(49 líneas) tienen **0 retornos de carro**.

### 13.8 Cadena de conexión

```
mongodb://albertitos_app:<password>@127.0.0.1:27017/albertitos?replicaSet=rs0&authSource=albertitos
```

`authSource=albertitos` es obligatorio: el usuario de aplicación está definido
**dentro** de `albertitos`, no en `admin`. (El usuario root sí usa
`authSource=admin`, que es justo lo que hace el contenedor `mongo-init` para
construir su URI.)

### 13.9 Procedimiento de arranque

```bash
cp .env.example .env          # 1. rellenar credenciales
docker compose up -d          # 2. arrancar
docker compose ps -a          # 3. mongo "healthy"; mongo-init "Exited (0)"
docker compose exec mongo mongosh -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" \
  --authenticationDatabase admin --eval "rs.status().ok"    # 4. debe devolver 1
```

> **En el primer arranque en frío, `docker compose up -d` puede abortar** con
> `dependency failed to start: container albertitos-mongo is unhealthy`. No es
> un fallo: la inicialización del volumen vacío (crear usuario root, ejecutar
> los scripts, escribir el oplog) tarda más que el margen del healthcheck. El
> contenedor termina quedando sano por su cuenta; basta **repetir**
> `docker compose up -d`. En arranques posteriores el volumen ya está
> inicializado y no ocurre.

A partir de ahí, el arranque de trabajo es `docker compose up -d` y ya.

### 13.10 Verificación del despliegue

Los seis pasos que se comprobaron sobre este stack, y su resultado:

| # | Comprobación | Resultado |
|---|---|---|
| 1 | `rs.status().ok == 1`, `setName == rs0`, miembro `PRIMARY` | ✅ |
| 2 | El usuario de app autentica y está **restringido**: `listDatabases` no muestra bases, `admin.system.users` → `Unauthorized` | ✅ |
| 3 | 10 colecciones + validadores en las 8 regulares + `migraciones` con `_id: 1` | ✅ |
| 4 | El validador **acepta** documentos correctos y **rechaza** (código `121`) los inválidos; `_id` duplicado → código `11000` | ✅ |
| 5 | Los 17 patrones de la §11 usan el índice previsto (`IXSCAN`, o `IDHACK` en P1), sin `COLLSCAN` | ✅ |
| 6 | Tras `docker compose down && docker compose up -d`: `rs.status().ok == 1`, el marcador insertado sigue ahí, 10 colecciones y 10 índices intactos | ✅ |

---

## 14. Copia de seguridad y restauración

### 14.1 Backup lógico (recomendado)

```bash
# Backup completo: documentos + GridFS en un solo dump coherente
docker compose exec mongo mongodump \
  --uri="mongodb://albertitos_app:<pass>@127.0.0.1:27017/albertitos?replicaSet=rs0&authSource=albertitos" \
  --archive=/tmp/albertitos-$(date +%Y%m%d).archive --gzip

docker compose cp mongo:/tmp/albertitos-20260919.archive ./backups/
```

### 14.2 Restauración

```bash
docker compose cp ./backups/albertitos-20260919.archive mongo:/tmp/
docker compose exec mongo mongorestore \
  --uri="mongodb://albertitos_app:<pass>@127.0.0.1:27017/albertitos?replicaSet=rs0&authSource=albertitos" \
  --archive=/tmp/albertitos-20260919.archive --gzip --drop
```

### 14.3 Backup físico (snapshot del volumen)

```bash
docker compose stop mongo
docker run --rm -v albertitos_mongo_data:/data -v "$PWD/backups:/backup" \
  alpine tar czf /backup/mongo_data-$(date +%Y%m%d).tar.gz -C /data .
docker compose start mongo
```

### 14.4 Qué se puede perder sin consecuencias

`eventos` (TTL 90 días, señales operativas) y `ejecuciones` (métricas). Todo lo
demás es evidencia contable y debe estar en el backup.

---

## 15. Capacidad y rendimiento

### 15.1 Cálculo de tamaño a 540 expedientes

| Colección | Documentos | Tamaño/doc | Total |
|---|---:|---:|---:|
| `expedientes` | 540 | ~100 KB (OCR dominante) | **~54 MB** |
| `asientos` | 516 × 2 snapshots | ~300 B | **~0,3 MB** |
| `excel_filas` | ~500 | ~400 B | **~0,2 MB** |
| `reglas_versiones` | ~5 | ~1 KB | **~5 KB** |
| `erp_snapshots` | ~5 | ~300 B | **~1,5 KB** |
| `ejecuciones` | ~10 | ~1 KB | **~10 KB** |
| `eventos` | ~5 000 | ~200 B | **~1 MB** |
| GridFS `pdfs` | 540 | ~180 KB | **~97 MB** |
| **Total** | | | **~152 MB** |

**Conclusión:** cabe holgadamente en memoria. No hace falta bucketing, ni
sharding, ni compresión. El límite de 16 MB por documento está a dos órdenes de
magnitud de distancia (el expediente más grande es ~100 KB).

### 15.2 Cálculo a 50 000 expedientes

| Colección | Total estimado |
|---|---:|
| `expedientes` | ~5 GB |
| GridFS `pdfs` | ~9 GB |
| `eventos` (con TTL 90 d) | ~10 MB (acotado por TTL) |
| **Total** | **~14 GB** |

**Conclusión:** sigue cabiendo en un solo nodo con 32 GB de RAM. El sharding
(§16) es una preparación, no una necesidad inmediata.

### 15.3 Presupuesto de latencia por operación

| Operación | Objetivo | Índice que lo garantiza |
|---|---:|---|
| Upsert de un expediente | < 5 ms | `_id_` |
| Match por pedido | < 2 ms | `ix_pedido_vigente` |
| Match por (nif, importe) | < 3 ms | `ix_nif_importe_vigente` |
| Cruce Excel | < 3 ms | `ix_indices_nif` / `ix_indices_pedido` |
| Export JSONL (540 docs) | < 200 ms | `ix_lote_resultado` (recorrido ordenado) |
| Agregación de métricas | < 50 ms | `ix_run_tipo_ts` |

---

## 16. Escalado a 50 000 facturas

### 16.1 Cuándo shardear

**No ahora.** A 540 expedientes (~152 MB) el sharding añadiría complejidad sin
beneficio. Los umbrales que lo justificarían:

| Señal | Umbral | Acción |
|---|---|---|
| Tamaño total | > 500 GB | Sharding obligatorio |
| Escrituras | > 5 000/s sostenidas | Sharding por escritura |
| Working set | > RAM disponible | Sharding o más RAM |
| Expedientes | > 5 000 000 | Sharding |

A 50 000 expedientes (~14 GB) **ninguno se cumple**. La preparación consiste en
elegir las shard keys correctas **desde ahora**, para que activarlas sea un
`sh.enableSharding` + `sh.shardCollection` sin rediseño.

### 16.2 Shard keys propuestas

| Colección | Shard key | Tipo | Justificación |
|---|---|---|---|
| `expedientes` | `{lote_id: 1, _id: "hashed"}` | Compuesta | `lote_id` tiene baja cardinalidad (2 valores) → prefijo de localidad; `_id` hashed reparte uniformemente. Las consultas del pipeline filtran siempre por `lote_id` |
| `eventos` | `{run_id: "hashed"}` | Hashed | Todas las consultas agrupan por `run_id`; hashed reparte uniformemente y evita hotspots temporales |
| `asientos` | `{snapshot_id: 1, _id: "hashed"}` | Compuesta | Los asientos se consultan siempre dentro de un snapshot |
| `excel_filas` | `{excel_id: 1, _id: "hashed"}` | Compuesta | Se consultan siempre dentro de un Excel |
| `pdfs.chunks` | `{files_id: "hashed"}` | Hashed | GridFS requiere que los chunks de un fichero estén juntos; hashed sobre `files_id` lo garantiza |
| `ejecuciones` | `{_id: "hashed"}` | Hashed | Colección pequeña; hashed evita hotspots |
| `reglas_versiones` | **No shardear** | — | Colección diminuta; se replica entera |

### 16.3 Activación (cuando llegue el momento)

```javascript
sh.enableSharding("albertitos");
sh.shardCollection("albertitos.expedientes", { lote_id: 1, _id: "hashed" });
sh.shardCollection("albertitos.eventos", { run_id: "hashed" });
sh.shardCollection("albertitos.asientos", { snapshot_id: 1, _id: "hashed" });
sh.shardCollection("albertitos.excel_filas", { excel_id: 1, _id: "hashed" });
sh.shardCollection("albertitos.pdfs.chunks", { files_id: "hashed" });
```

### 16.4 Otras palancas de escalado (antes de shardear)

1. **Réplica de 3 nodos** con `readPreference: "secondaryPreferred"` para las
   consultas de la UI y las métricas → descarga el primario.
2. **Índices parciales** (ya usados) → menos RAM de índice.
3. **Proyección** en el export JSONL → solo `_id` y `decision.resultado`.
4. **TTL de `eventos`** → la colección no crece indefinidamente.
5. **GridFS con streaming** → los PDFs nunca se cargan enteros en memoria.
6. **`bulkWrite` con `ordered: false`** → la ingesta del ERP es paralelizable.

### 16.5 Qué NO cambia al escalar

El modelo de datos, los validadores, los índices y el código de la aplicación.
**Ese es el objetivo del diseño:** que pasar de 540 a 50 000 sea una operación
de infraestructura, no una reescritura.

---

## 17. Compatibilidad con el entregable

### 17.1 Generación de `outcomes.jsonl` desde MongoDB

```javascript
// Una línea por factura, ordenada, sin duplicados, sin líneas vacías
db.expedientes
  .find(
    { lote_id: "lote1", estado_proceso: "COMPLETADA" },
    { _id: 1, "decision.resultado": 1 }
  )
  .sort({ _id: 1 })
  .forEach(doc => {
    print(JSON.stringify({ file_id: doc._id, result: doc.decision.resultado }));
  });
```

**Garantías que ofrece el diseño:**
- **Una línea por factura**: `_id` es único por construcción.
- **Sin duplicados**: el upsert idempotente lo garantiza.
- **Sin líneas vacías**: el validador exige `decision` cuando
  `estado_proceso = COMPLETADA` (INV-2).
- **`result` válido**: el `enum` del validador lo garantiza (INV-1).
- **`file_id` exacto**: es el `_id`, que es el nombre exacto del PDF.

### 17.2 Relación con `traces/`

`traces/<file_id>/` pasa a ser una **exportación de conveniencia** para la UI
del bonus: se materializa desde el documento del expediente. La fuente de
verdad es MongoDB. Si `traces/` se borra, se regenera.

### 17.3 Relación con `data/erp_snapshot.json`

El fichero en disco sigue existiendo como **caché de arranque rápido** (evita
esperar a Mongo para el primer refetch), pero la fuente de verdad de los
asientos es la colección `asientos`. Al arrancar: si el snapshot de Mongo está
vigente y fresco, no se toca ni el ERP ni el fichero.

---

## 18. Resumen de decisiones lógicas

| # | Decisión | Justificación |
|---|---|---|
| L-1 | `_id = file_id` en `expedientes` | Idempotencia natural + trazabilidad directa desde el nombre del PDF |
| L-2 | Subdocumentos embebidos en `expedientes` | 1 escritura atómica; sin transacciones en el camino crítico |
| L-3 | `oneOf` por `estado_proceso` en el validador | Hace cumplir INV-2/INV-3 a nivel de base de datos |
| L-4 | `enum` en `resultado` | Hace cumplir INV-1; el JSONL no puede ser inválido |
| L-5 | `Decimal128` en todos los importes | Hace cumplir INV-8; exactitud en tolerancias de ±0,02 € |
| L-6 | `huella_negocio` con índice no único | Hace cumplir INV-7; los duplicados son caso de negocio |
| L-7 | `_id` compuesto `<snapshot_id>#<asiento_id>` | Permite versionar el ERP sin perder historia |
| L-8 | `vigente` + índice parcial | Consultas del snapshot activo sobre un índice diminuto |
| L-9 | `eventos` time-series con TTL 90 d | Métricas con una agregación; crecimiento acotado |
| L-10 | Contadores con `$inc` atómico | Sin transacciones; compatible con el driver 2.8 |
| L-11 | Snapshot del ERP marcado `vigente` solo al completarse | Nunca se usa un snapshot a medias |
| L-12 | `reglas_versiones` guarda el TOML completo + hash | Decisión 100 % reconstruible ante reglas no parametrizables |
| L-13 | GridFS con streaming | Un único almacén; backup coherente; memoria acotada |
| L-14 | `writeConcern` diferenciado por colección | Evidencia contable con `majority`; señales operativas con `w:1` |
| L-15 | Shard keys elegidas ahora, activadas después | Escalar a 50k es infraestructura, no rediseño |
| L-16 | Migraciones idempotentes vía `mongosh` | Cero código Rust; reproducible; versionado en git |
| L-17 | `--keyFile` generado por un entrypoint envolvente en `/data/configdb` | Un replica set exige autenticación interna y la imagen oficial no genera keyfile. En su propio volumen sobrevive a `down` y no se regenera |
| L-18 | `rs.initiate()` en el servicio `mongo-init`, nunca en `/docker-entrypoint-initdb.d` | El mongod temporal del initdb elimina `--replSet`; iniciar allí el replica set falla siempre. Solo los objetos van en initdb |
| L-19 | `db` como parámetro del IIFE en `02-schema-init.js` | `const db = db.getSiblingDB(...)` es un `ReferenceError` por TDZ (ver §13.6a) |
| L-20 | `eventos` sin validador; contrato de campos aplicado en `obs.rs` | El motor no admite `$jsonSchema` en time-series —ni al crear ni vía `collMod`—; el único escritor es la aplicación (ver §8.2) |
| L-21 | `.gitattributes` fuerza LF en los scripts montados | CRLF rompe `/bin/sh` dentro del contenedor (`\r: not found`) |
| L-22 | `ix_reglas_version` en `expedientes` | Sin él, el barrido de decisiones obsoletas por cambio de reglas hace `COLLSCAN` (ver §2.4) |