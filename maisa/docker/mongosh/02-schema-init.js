// 02-schema-init.js
// Crea las colecciones, validadores $jsonSchema e índices del sistema.
//
// Este script es la traducción ejecutable de diseño_logico.md (§2 a §9).
// Es IDEMPOTENTE: puede ejecutarse tantas veces como se quiera.
//   - createCollection: solo si no existe.
//   - collMod: actualiza el validador si la colección ya existe.
//   - createIndex: idempotente por naturaleza.
//
// Se ejecuta automáticamente en el primer arranque del contenedor.
// Para reaplicarlo manualmente:
//   docker compose exec mongo mongosh -u <root> -p <pass> --authenticationDatabase admin \
//     /docker-entrypoint-initdb.d/02-schema-init.js

// NOTA: `db` se recibe como PARÁMETRO del IIFE, no se declara dentro.
// Escribir `const db = db.getSiblingDB(dbName)` provoca un ReferenceError
// ("Cannot access 'db' before initialization"): la declaración `const db`
// crea un binding en todo el bloque y la lectura de `db` del lado derecho
// cae en su zona muerta temporal (TDZ). Pasarlo como parámetro evita el
// sombreado y deja intactas todas las referencias `db.` del cuerpo.
(function (db) {
  const dbName = process.env.MONGO_DB || "albertitos";

  print(`[02-schema] Aplicando esquema en '${dbName}'...`);

  // ---------------------------------------------------------------------
  // Utilidad: crear la colección si no existe, o actualizar su validador.
  // ---------------------------------------------------------------------
  function ensureCollection(name, options) {
    const exists = db.getCollectionInfos({ name: name }).length > 0;
    if (exists) {
      db.runCommand({
        collMod: name,
        validator: options.validator,
        validationLevel: options.validationLevel || "strict",
        validationAction: options.validationAction || "error"
      });
      print(`[02-schema]   ~ ${name} (validador actualizado)`);
    } else {
      db.createCollection(name, options);
      print(`[02-schema]   + ${name} (creada)`);
    }
  }

  // ---------------------------------------------------------------------
  // Forma canónica de un campo extraído: { estado, valor, rastro }.
  //
  // Existe UNA sola forma para los siete campos de `factura`; solo cambia el
  // tipo del `valor`. Guardar el valor normalizado y el texto crudo JUNTOS
  // (en vez de `nif_emisor` + `nif_emisor_bruto` en paralelo) evita dos
  // estructuras que hay que mantener sincronizadas.
  //
  // `estado` es lo que hace trazable la diferencia entre:
  //   NO_APARECE — el PDF no trae el dato (lo trata R1: no se puede conciliar)
  //   ILEGIBLE   — se leyó algo que no se pudo canonizar, y se conserva el texto
  //   ENCONTRADO — hay valor canónico, y `rastro.crudo` dice de dónde salió
  //
  // La coherencia entre `estado`, `valor` y `rastro` la garantiza el tipo Rust
  // `Identificador<T>` (su `TryFrom` rechaza las combinaciones imposibles en el
  // borde), NO este validador: replicarla aquí con `oneOf`/`not` daría un
  // esquema frágil que rechazaría escrituras legítimas. Mongo solo comprueba
  // que las formas y los tipos son los pactados.
  // ---------------------------------------------------------------------
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
            crudo: { bsonType: "string", description: "texto tal cual salió de la fuente, sin normalizar" },
            score: { bsonType: "double", minimum: 0, maximum: 1 },
            origen: {
              bsonType: ["object", "null"],
              required: ["fuente"],
              properties: {
                fuente: { enum: ["OCR", "EXCEL", "ERP"] },
                pagina: { bsonType: "int", minimum: 0 },   // OCR
                linea: { bsonType: "int", minimum: 0 },    // OCR (1-based)
                fila: { bsonType: "string" },              // EXCEL ("42")
                columna: { bsonType: "string" },           // EXCEL ("D")
                asiento_id: { bsonType: "string" }         // ERP ("AS-412")
              }
            }
          }
        }
      }
    };
  }

  // =====================================================================
  // 1. expedientes — agregado raíz, un documento por factura
  // =====================================================================
  ensureCollection("expedientes", {
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
          huella_negocio: { bsonType: ["string", "null"] },
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
              motor: { enum: ["rapidocr", "pytesseract", "pdfplumber", "paddleocr_vl", "ninguno"] },
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

          factura: {
            bsonType: "object",
            properties: {
              nif_emisor: campoExtraido(["string"]),       // Nif canónico: "B12345678"
              pedido: campoExtraido(["string"]),
              numero_factura: campoExtraido(["string"]),
              // La fecha se normaliza a texto ISO-8601. El `bsonType` "date"
              // llegará cuando la proyección de escritura convierta el
              // `Identificador<String>`: hoy NO hay ninguna capa que lo haga.
              fecha: campoExtraido(["string"]),
              // INV-8: los importes son `Decimal` en el dominio. OJO: la
              // proyección de escritura TIENE que mapearlos a `Decimal128`
              // (`rust_decimal` serializa a string vía serde, y un
              // `bson::to_bson(&Factura)` directo daría strings aquí).
              base: campoExtraido(["decimal"]),
              iva: campoExtraido(["decimal"]),
              total: campoExtraido(["decimal"])
            }
          },

          evidencia: {
            bsonType: ["object", "null"],
            required: ["match_por", "conflictos"],
            properties: {
              asiento_id: { bsonType: ["string", "null"] },
              asiento: {
                bsonType: ["object", "null"],
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
                items: { bsonType: "string" }
              },
              match_por: {
                enum: ["EXACT_BY_PEDIDO", "BY_NIF_Y_IMPORTE", "BY_NIF_UNICO", "NONE"]
              },
              conflictos: { bsonType: "array", items: { bsonType: "string" } }
            }
          },

          decision: {
            bsonType: ["object", "null"],
            required: ["resultado", "motivo", "reglas_evaluadas", "huellas"],
            properties: {
              resultado: { enum: ["PAGAR", "NO_PAGAR", "ESCALAR"] },
              motivo: { bsonType: "string", minLength: 1 },
              reglas_evaluadas: { bsonType: "array", items: { bsonType: "string" } },
              coste_estimado_cents: { bsonType: "int", minimum: 0 },
              timings_ms: {
                bsonType: "object",
                additionalProperties: { bsonType: "int", minimum: 0 }
              },
              huellas: {
                bsonType: "object",
                required: ["reglas_version", "erp_snapshot_id", "run_id"],
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

        // INV-2 / INV-3: la decisión existe si y solo si está COMPLETADA.
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

  db.expedientes.createIndex({ lote_id: 1, "decision.resultado": 1, _id: 1 }, { name: "ix_lote_resultado" });
  // El campo es {estado, valor, rastro}, así que se indexa `.valor` (el NIF
  // canónico), no el subdocumento entero. Van con `sparse: true` porque un
  // campo NO_APARECE no escribe `valor` (en Rust es un `Option` que no se
  // serializa): sin sparse, los expedientes sin NIF entrarían en el índice
  // como nulos y la consulta "¿qué facturas son de este proveedor?" tendría
  // que descartarlos a mano en cada consulta.
  db.expedientes.createIndex({ "factura.nif_emisor.valor": 1 }, { name: "ix_nif", sparse: true });
  db.expedientes.createIndex({ "factura.pedido.valor": 1 }, { name: "ix_pedido", sparse: true });
  db.expedientes.createIndex({ "evidencia.asiento_id": 1 }, { name: "ix_asiento" });
  db.expedientes.createIndex({ "decision.huellas.run_id": 1 }, { name: "ix_run" });
  db.expedientes.createIndex({ huella_negocio: 1 }, { name: "ix_huella_negocio" });
  db.expedientes.createIndex(
    { "decision.resultado": 1, "revision.estado": 1 },
    { name: "ix_escalar_pendiente", partialFilterExpression: { "decision.resultado": "ESCALAR" } }
  );
  db.expedientes.createIndex(
    { "decision.huellas.erp_snapshot_id": 1 },
    { name: "ix_snapshot_obsoleto" }
  );
  // Indexación de "decisiones obsoletas por cambio de reglas" (patrón P10).
  // El nombre de campo es `decision.huellas.reglas_version`, NO `run_id`:
  // un run_id identifica la ejecución, mientras que reglas_version identifica
  // la versión de reglas.toml con la que se decidió. Sin este índice el barrido
  // "¿qué decisiones se tomaron con reglas antiguas?" hace COLLSCAN (verificado
  // sobre 1000 documentos: docsExamined = 1000, nReturned = 1000).
  // Es simétrico de `ix_snapshot_obsoleto`, que cubre el caso análogo con el ERP.
  db.expedientes.createIndex(
    { "decision.huellas.reglas_version": 1 },
    { name: "ix_reglas_version" }
  );

  // =====================================================================
  // 2. asientos — catálogo del ERP, versionado por snapshot
  // =====================================================================
  ensureCollection("asientos", {
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
          vigente: { bsonType: "bool" },
          esquema_version: { bsonType: "int", minimum: 1 }
        }
      }
    },
    validationLevel: "strict",
    validationAction: "error"
  });

  db.asientos.createIndex({ pedido: 1, vigente: 1 }, { name: "ix_pedido_vigente" });
  db.asientos.createIndex({ nif: 1, vigente: 1 }, { name: "ix_nif_vigente" });
  db.asientos.createIndex({ nif: 1, importe: 1, vigente: 1 }, { name: "ix_nif_importe_vigente" });
  db.asientos.createIndex({ snapshot_id: 1 }, { name: "ix_snapshot" });
  db.asientos.createIndex(
    { vigente: 1 },
    { name: "ix_vigente_parcial", partialFilterExpression: { vigente: true } }
  );

  // =====================================================================
  // 3. excel_filas — volcado sin pérdida del Excel caótico
  // =====================================================================
  ensureCollection("excel_filas", {
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
            additionalProperties: {
              bsonType: ["string", "double", "int", "long", "decimal", "bool", "null"]
            }
          },
          indices: {
            bsonType: "object",
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

  db.excel_filas.createIndex({ "indices.nif": 1 }, { name: "ix_indices_nif" });
  db.excel_filas.createIndex({ "indices.pedido": 1 }, { name: "ix_indices_pedido" });
  db.excel_filas.createIndex({ excel_id: 1, hoja: 1, indice: 1 }, { name: "ix_excel_hoja_fila" });

  // =====================================================================
  // 4. reglas_versiones — versionado inmutable de reglas.toml
  // =====================================================================
  ensureCollection("reglas_versiones", {
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

  db.reglas_versiones.createIndex(
    { vigente: 1 },
    { name: "ix_vigente_parcial", partialFilterExpression: { vigente: true } }
  );
  db.reglas_versiones.createIndex({ hash: 1 }, { name: "ix_hash" });

  // =====================================================================
  // 5. erp_snapshots — metadatos de cada descarga del ERP
  // =====================================================================
  ensureCollection("erp_snapshots", {
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

  db.erp_snapshots.createIndex(
    { vigente: 1 },
    { name: "ix_vigente_parcial", partialFilterExpression: { vigente: true } }
  );
  db.erp_snapshots.createIndex({ descargado_en: -1 }, { name: "ix_descargado" });

  // =====================================================================
  // 6. ejecuciones — un documento por run del pipeline
  // =====================================================================
  ensureCollection("ejecuciones", {
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

  db.ejecuciones.createIndex({ lote_id: 1, inicio: -1 }, { name: "ix_lote_inicio" });
  db.ejecuciones.createIndex(
    { estado: 1 },
    { name: "ix_estado_parcial", partialFilterExpression: { estado: "EN_CURSO" } }
  );

  // =====================================================================
  // 7. eventos — time-series con TTL de 90 días
  // =====================================================================
  // LIMITACIÓN DE MongoDB 7.0 — eventos NO admite validador.
  // Verificado empíricamente contra mongo:7.0:
  //   - createCollection({timeseries:..., validator:...})
  //       -> MongoServerError: 'timeseries' is not allowed with 'validator'
  //   - collMod({validator:...}) sobre una time-series existente
  //       -> MongoServerError: option not supported on a time-series collection: validator
  // Por tanto la colección se crea SIN $jsonSchema. La integridad del
  // esquema se garantiza en la capa de aplicación (obs.rs::emitir_evento),
  // que es el único punto que escribe en 'eventos'.
  //
  // Contrato de campos (aplicado por el código, no por el motor):
  //   ts          date     requerido — timeField
  //   run_id      string   requerido — metaField
  //   tipo        string   requerido — enum:
  //                          ERP_RETRY_ORA_00600, ERP_RETRY_SES_401, ERP_RETRY_ERP_429,
  //                          ERP_CACHE_HIT, ERP_CACHE_MISS, OCR_FAIL, OCR_OK,
  //                          EXPEDIENTE_ESTADO, DECISION_EMITIDA,
  //                          REVISION_ABIERTA, REVISION_RESUELTA
  //   file_id     string|null
  //   detalle     object   (additionalProperties: true)
  //   duracion_ms int|null >= 0
  const eventosExists = db.getCollectionInfos({ name: "eventos" }).length > 0;
  if (!eventosExists) {
    db.createCollection("eventos", {
      timeseries: {
        timeField: "ts",
        metaField: "run_id",
        granularity: "seconds"
      },
      expireAfterSeconds: 7776000 // 90 días (TTL implícito del bucket)
    });
    print("[02-schema]   + eventos (time-series creada, sin validador: no soportado)");
  } else {
    print("[02-schema]   = eventos (ya existe; las time-series no admiten collMod de validador)");
  }

  db.eventos.createIndex({ run_id: 1, tipo: 1, ts: 1 }, { name: "ix_run_tipo_ts" });
  db.eventos.createIndex({ tipo: 1, ts: -1 }, { name: "ix_tipo_ts" });
  db.eventos.createIndex({ file_id: 1, ts: 1 }, { name: "ix_file_ts" });

  // =====================================================================
  // 8. migraciones — control de versiones de esquema
  // =====================================================================
  ensureCollection("migraciones", {
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
    },
    validationLevel: "strict",
    validationAction: "error"
  });

  // Registrar esta migración (idempotente).
  db.migraciones.updateOne(
    { _id: 1 },
    {
      $setOnInsert: {
        _id: 1,
        aplicada_en: new Date(),
        descripcion: "Esquema inicial: expedientes, asientos, excel_filas, reglas_versiones, erp_snapshots, ejecuciones, eventos, migraciones",
        hash_script: null
      }
    },
    { upsert: true }
  );

  // =====================================================================
  // 9. GridFS — bucket "pdfs" para los PDFs originales
  // =====================================================================
  // Las colecciones pdfs.files y pdfs.chunks se crean implícitamente al
  // primer upload. Las creamos aquí para poder indexarlas desde el inicio.
  if (db.getCollectionInfos({ name: "pdfs.files" }).length === 0) {
    db.createCollection("pdfs.files");
    print("[02-schema]   + pdfs.files (creada)");
  }
  if (db.getCollectionInfos({ name: "pdfs.chunks" }).length === 0) {
    db.createCollection("pdfs.chunks");
    print("[02-schema]   + pdfs.chunks (creada)");
  }

  db["pdfs.files"].createIndex({ filename: 1 }, { name: "ix_filename" });
  db["pdfs.files"].createIndex({ "metadata.file_id": 1 }, { name: "ix_meta_file_id" });
  db["pdfs.files"].createIndex({ "metadata.sha256": 1 }, { name: "ix_meta_sha256" });
  db["pdfs.chunks"].createIndex({ files_id: 1, n: 1 }, { name: "ix_files_id_n" });

  // =====================================================================
  // 10. revisiones — estado de revision humana de facturas ESCALAR
  // =====================================================================
  // Unica coleccion que escribe la API (no el motor): registra si un
  // operador ha marcado una factura como revisada desde el panel. No es una
  // decision del pipeline, es una accion humana sobre lo que el motor decidio.
  ensureCollection("revisiones", {
    validator: {
      $jsonSchema: {
        bsonType: "object",
        title: "Revision humana de una factura",
        required: ["_id", "estado", "actualizado_en"],
        properties: {
          _id: { bsonType: "string", description: "file_id: nombre exacto del PDF" },
          estado: { enum: ["PENDIENTE", "RESUELTA"] },
          revisor: { bsonType: ["string", "null"] },
          comentario: { bsonType: ["string", "null"] },
          actualizado_en: { bsonType: "date" }
        }
      }
    },
    validationLevel: "strict",
    validationAction: "error"
  });

  db.revisiones.createIndex({ estado: 1 }, { name: "ix_estado" });

  db.migraciones.updateOne(
    { _id: 2 },
    {
      $setOnInsert: {
        _id: 2,
        aplicada_en: new Date(),
        descripcion: "Anade coleccion revisiones (estado de revision humana de ESCALAR)",
        hash_script: null
      }
    },
    { upsert: true }
  );

  print("[02-schema] Esquema aplicado correctamente.");
})(db.getSiblingDB(process.env.MONGO_DB || "albertitos"));