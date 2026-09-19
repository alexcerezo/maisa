use axum::{
    extract::{Multipart, State},
    http::StatusCode,
    routing::post,
    Json, Router,
};
use mongodb::{
    bson::{doc, Bson, DateTime, Decimal128, Document},
    options::ClientOptions,
    Client, Collection, Database,
};
use rust_decimal::Decimal;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tracing_subscriber;

use clap::Parser;

// Módulos del motor de conciliación (spec_y_plan.md §1.1 / diseño_logico.md §2).
mod domain;
mod reconciler;
mod rules;

// Módulos de la vía real de datos: el ERP (XML/ISO-8859-1 con reintentos), el
// OCR (HTTP multipart) y el parser/validador que convierte líneas en `Factura`.
// Ver TRASPASO.md y _scratch/ERP-RUST-CONTRATO.md.
mod erp;
mod ocr;
mod parser;
mod validators;

use domain::{
    Asiento, Decision, EstadoAsiento, Evidencia, Factura, FilaExcel, Huellas, Nif, Resultado,
};
use reconciler::{conciliar, IndiceErp, IndiceExcel};
use rules::{decidir, ReglasConfig};

// --- ADAPTADOR DE PERSISTENCIA: documento Mongo → tipo de dominio ---

/// Espejo de los campos que el motor **consume** del documento `asientos` tal y
/// como lo guarda Mongo.
///
/// Existe porque el tipo de dominio no puede —ni debe— deserializarse directo
/// desde BSON. La colección guarda `fecha` como BSON **`date`** e `importe` como
/// BSON **`decimal`**, y el deserializador de `bson` presenta los dos como un
/// *mapa*; `domain::Asiento` los declara como `Option<String>` y
/// `rust_decimal::Decimal`, que solo aceptan escalares. El resultado era un
/// descarte **silencioso** de los 516 asientos: el servidor arrancaba en verde
/// con el catálogo del ERP vacío y decidía `ESCALAR` para toda factura.
///
/// El coste de mantener este espejo es que un cambio de esquema hay que
/// reflejarlo aquí; a cambio, `domain.rs` sigue siendo agnóstico al
/// almacenamiento (no se le cuela ni un tipo de `bson`, así que no hay tipos
/// Mongo-específicos en la lógica de decisión) y los tests de dominio siguen
/// construyendo `Asiento` con tipos Rust corrientes.
///
/// Nota: la conversión es trabajo explícito de la capa de persistencia
/// (TRASPASO.md §3.8), no un detalle que deba resolverse con atributos serde
/// dentro del dominio.
#[derive(Debug, Deserialize)]
struct AsientoDoc {
    asiento_id: String,
    nif: String,
    pedido: String,
    importe: Decimal128,
    estado: EstadoAsiento,
    #[serde(default)]
    proveedor: Option<String>,
    #[serde(default)]
    fecha: Option<DateTime>,
}

impl AsientoDoc {
    /// Pasa el documento de Mongo al tipo de dominio.
    ///
    /// Un NIF en blanco **no** invalida el asiento: se traduce a `None` y el
    /// asiento se conserva con su pedido, su importe y su estado. Descartarlo
    /// —que es lo que hacía antes esta función— borraba la única prueba de que
    /// el ERP *sí* tiene la factura, y la dejaba escalando como `sin_match`,
    /// igual que una factura que no existe. Quien decide qué hacer con un
    /// asiento sin NIF es el motor de reglas (`R10`), no el cargador: el
    /// cargador solo transporta la evidencia tal y como está.
    ///
    /// Sigue siendo falible por el importe, que es lo único que no tiene
    /// lectura segura: un `decimal` ilegible descarta la fila y se cuenta.
    fn a_dominio(self) -> Result<Asiento, String> {
        // Vía el texto (`Decimal128` implementa `Display`), no vía `f64`: el
        // importe es dinero y un float perdería céntimos.
        let importe = Decimal::from_str(&self.importe.to_string()).map_err(|e| {
            format!(
                "asiento {} con importe ilegible ({:?}): {e}",
                self.asiento_id, self.importe
            )
        })?;

        Ok(Asiento {
            asiento_id: self.asiento_id,
            nif: Nif::nuevo(&self.nif),
            pedido: self.pedido,
            importe,
            estado: self.estado,
            proveedor: self.proveedor,
            fecha: self.fecha.map(formatear_fecha),
        })
    }
}

/// BSON `date` → `"YYYY-MM-DD"` en UTC.
///
/// Es la misma forma que usa la clave de factura del ERP
/// (`proveedor|pedido|fecha|importe`), para que los dos caminos hablen del mismo
/// día y la fecha sea comparable y auditable como texto.
fn formatear_fecha(fecha: DateTime) -> String {
    match fecha.try_to_rfc3339_string() {
        Ok(rfc3339) => rfc3339.split('T').next().unwrap_or(&rfc3339).to_string(),
        // Solo ocurre con años fuera del rango representable en RFC 3339.
        Err(_) => fecha.timestamp_millis().to_string(),
    }
}

// --- CATÁLOGO DEL ERP (en memoria) ---

/// Vista del ERP que el motor necesita tener a mano: los asientos **vigentes**.
///
/// Se carga una vez al arranque (spec_y_plan.md §1.3: descargar los asientos una
/// sola vez) y se consulta en cada decisión. Aquí **no** se guardan las filas del
/// Excel: `excel_filas` en Mongo es el volcado *crudo* del Excel caótico y
/// convertirlo en `FilaExcel` es trabajo de `excel.rs`, que aún no existe. Por eso
/// el Excel entra ya normalizado en cada petición.
#[derive(Debug, Default)]
struct Catalogo {
    asientos: Vec<Asiento>,
    snapshot_id: String,
}

impl Catalogo {
    /// Carga los asientos vigentes y el `_id` del snapshot al que pertenecen.
    ///
    /// Es *best effort* a propósito: en un hackathon la colección puede no existir
    /// todavía y el servidor tiene que arrancar igual (el motor responderá ESCALAR
    /// por falta de evidencia, que es la respuesta segura). Un asiento que no
    /// encaje con el esquema se descarta y se registra; no puede tumbar el catálogo.
    async fn desde_mongo(db: &Database) -> Self {
        // Se leen como `AsientoDoc` (la forma BSON real) y se convierten al
        // dominio. Leer directo `Asiento` aquí no funciona: `fecha` llega como
        // BSON `date` e `importe` como BSON `decimal`, y ninguno de los dos
        // encaja con los tipos del dominio.
        let documentos = leer_coleccion::<AsientoDoc>(
            db,
            "asientos",
            doc! { "vigente": true },
            "asiento",
        )
        .await;

        let mut asientos = Vec::with_capacity(documentos.len());
        let mut invalidos = 0usize;
        for documento in documentos {
            match documento.a_dominio() {
                Ok(asiento) => asientos.push(asiento),
                Err(e) => {
                    invalidos += 1;
                    tracing::warn!("asiento descartado al normalizar: {e}");
                }
            }
        }
        if invalidos > 0 {
            tracing::warn!("'asientos': {invalidos} asiento(s) descartado(s) al pasar al dominio");
        }

        let snapshot_id = snapshot_vigente(db)
            .await
            .unwrap_or_else(|| format!("local-{}", epoch_millis()));
        Self {
            asientos,
            snapshot_id,
        }
    }
}

/// Corre el pipeline completo para una factura: `conciliar` → `decidir`.
///
/// Los índices se construyen por petición. Así no hay que mantener índices y
/// vectores sincronizados, ni meter un struct autorreferencial en el estado del
/// servidor. Con el catálogo del hackathon sobra; si creciera, cachear esto es lo
/// primero que habría que hacer.
fn decidir_factura(
    factura: &Factura,
    asientos: &[Asiento],
    filas_excel: &[FilaExcel],
    reglas: &ReglasConfig,
    erp_snapshot_id: &str,
) -> (Evidencia, Decision) {
    let erp = IndiceErp::nuevo(asientos);
    let excel = IndiceExcel::nuevo(filas_excel);
    // La misma tolerancia que usará el motor: "conciliado" tiene que significar
    // lo mismo en las dos fases o una factura puede cuadrar y no cuadrar a la vez.
    let evidencia = conciliar(factura, &erp, &excel, reglas.tolerancia_importe);
    let huellas = Huellas {
        reglas_version: reglas.version,
        erp_snapshot_id: erp_snapshot_id.to_string(),
        run_id: nuevo_run_id(),
    };
    let decision = decidir(factura, &evidencia, reglas, huellas);
    (evidencia, decision)
}

/// Lee una colección entera y deserializa documento a documento.
///
/// Se lee como `Document` en vez de `Collection<T>` para poder saltarse los
/// documentos que no encajen con el esquema en lugar de abortar la lectura
/// completa con el primer documento malo.
async fn leer_coleccion<T>(
    db: &Database,
    nombre: &str,
    filtro: Document,
    etiqueta: &str,
) -> Vec<T>
where
    T: DeserializeOwned,
{
    let coleccion = db.collection::<Document>(nombre);
    let mut cursor = match coleccion.find(filtro, None).await {
        Ok(cursor) => cursor,
        Err(e) => {
            tracing::warn!("no se pudo leer '{nombre}': {e} (el motor arrancará sin esos datos)");
            return Vec::new();
        }
    };

    let mut salida = Vec::new();
    let mut descartados = 0usize;
    loop {
        match cursor.advance().await {
            Ok(true) => {}
            Ok(false) => break,
            Err(e) => {
                tracing::warn!("error recorriendo '{nombre}': {e}");
                break;
            }
        }
        match cursor.deserialize_current() {
            Ok(documento) => match mongodb::bson::from_document::<T>(documento) {
                Ok(valor) => salida.push(valor),
                Err(e) => {
                    descartados += 1;
                    tracing::warn!("{etiqueta} descartado en '{nombre}': {e}");
                }
            },
            Err(e) => {
                descartados += 1;
                tracing::warn!("{etiqueta} ilegible en '{nombre}': {e}");
            }
        }
    }

    if descartados > 0 {
        tracing::warn!(
            "'{nombre}': {descartados} {etiqueta}(s) descartado(s) por no encajar con el esquema"
        );
    }
    salida
}

/// `_id` del snapshot de ERP vigente: es la huella que dice **qué foto del ERP**
/// se usó para decidir.
async fn snapshot_vigente(db: &Database) -> Option<String> {
    let coleccion = db.collection::<Document>("erp_snapshots");
    match coleccion.find_one(doc! { "vigente": true }, None).await {
        Ok(Some(snapshot)) => snapshot.get("_id").and_then(id_legible),
        Ok(None) => {
            tracing::warn!("no hay snapshot de ERP vigente; se usa un id local en las huellas");
            None
        }
        Err(e) => {
            tracing::warn!("no se pudo leer 'erp_snapshots': {e}");
            None
        }
    }
}

/// El `_id` de un snapshot puede ser texto, ObjectId o entero según cómo se cree.
fn id_legible(valor: &Bson) -> Option<String> {
    match valor {
        Bson::String(s) => Some(s.clone()),
        Bson::ObjectId(oid) => Some(oid.to_hex()),
        Bson::Int32(i) => Some(i.to_string()),
        Bson::Int64(i) => Some(i.to_string()),
        _ => None,
    }
}

fn epoch_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Identificador de ejecución, único dentro del proceso.
///
/// Epoch en milisegundos + contador monótono: no hace falta traer una dependencia
/// de UUID solo para poder decir "esta decisión salió de esta ejecución".
fn nuevo_run_id() -> String {
    static CONTADOR: AtomicU64 = AtomicU64::new(0);
    format!(
        "run-{}-{}",
        epoch_millis(),
        CONTADOR.fetch_add(1, Ordering::Relaxed)
    )
}

// Estado compartido para inyectar MongoDB en las rutas de Axum
#[derive(Clone)]
struct AppState {
    /// Facturas subidas por HTTP, tal y como se extrajeron del PDF: con su
    /// estado y su rastro, que es lo único que permite auditar la decisión.
    db_collection: Collection<Factura>,
    /// Reglas cargadas de `config/reglas.toml` al arranque.
    reglas: Arc<ReglasConfig>,
    /// Asientos del ERP vigentes, en memoria.
    catalogo: Arc<Catalogo>,
}


// --- INTERFAZ DE LÍNEA DE COMANDOS (spec_y_plan.md Bloque 5) ---

/// El proceso tiene **dos modos excluyentes**, y el que se elige decide cómo se
/// comporta todo lo demás:
///
/// - **Servidor** (sin `--pdf-dir` ni `--fixture`): escucha en el puerto 3000 y
///   decide facturas que le llegan por HTTP. Es el modo de desarrollo.
/// - **Lote**: recorre una entrada, decide cada factura y escribe el JSONL de
///   entrega. No abre puerto.
///
/// Son excluyentes a propósito: un ejecutor de lote que además se queda
/// escuchando en un puerto es un proceso que hay que matar a mano, y un servidor
/// que escribe ficheros de entrega es un servidor que sorprende.
#[derive(Debug, Parser)]
#[command(
    name = "hackspain-ocr",
    about = "Conciliación PDF ↔ ERP ↔ Excel y decisión PAGAR/NO_PAGAR/ESCALAR"
)]
struct Args {
    /// Carpeta con los PDFs del lote (modo lote de producción).
    ///
    /// El camino real: PDF → OCR (`ocr.rs`) → `Factura` (`parser.rs`) → decisión
    /// contra los asientos del snapshot del ERP.
    #[arg(long, value_name = "DIR", conflicts_with = "fixture")]
    pdf_dir: Option<PathBuf>,

    /// Lote de entrada **ya extraído**: una factura JSON por línea.
    ///
    /// Es el mismo trabajo que hace `parser.rs`, pero escrito a mano, así que
    /// sirve para comprobar el motor contra datos realistas sin depender del
    /// OCR ni del ERP. Un fixture es autocontenido a propósito: sus líneas traen
    /// sus propios `asientos`, para que el caso dorado no cambie según lo que
    /// haya hoy en el ERP.
    #[arg(long, value_name = "JSONL")]
    fixture: Option<PathBuf>,

    /// Fichero JSONL de entrega a escribir (una línea por factura).
    #[arg(long, value_name = "JSONL")]
    out: Option<PathBuf>,

    /// Reglas del motor. `REGLAS_PATH` (entorno) tiene prioridad sobre este flag.
    #[arg(long, value_name = "TOML", default_value = "config/reglas.toml")]
    reglas: PathBuf,

    /// Excel caótico con el contexto financiero.
    #[arg(long, value_name = "XLSX")]
    excel: Option<PathBuf>,

    /// Ignora el snapshot de ERP guardado y vuelve a descargarlo del bridge.
    #[arg(long)]
    refetch_erp: bool,

    /// Snapshot del ERP con el que se decide: el lote lo lee y `--refetch-erp`
    /// lo reescribe. `ERP_SNAPSHOT` (entorno) tiene prioridad sobre el flag.
    #[arg(long, value_name = "JSON", default_value = "data/erp_snapshot.json")]
    erp_snapshot: PathBuf,
}

impl Args {
    /// De dónde sale el lote, si es que este proceso va a ejecutar uno.
    fn fuente(&self) -> Option<FuenteLote> {
        if let Some(ruta) = &self.fixture {
            Some(FuenteLote::Fixture(ruta.clone()))
        } else {
            self.pdf_dir.clone().map(FuenteLote::PdfDir)
        }
    }
}

/// De dónde salen las facturas del lote.
#[derive(Debug, Clone)]
enum FuenteLote {
    /// Carpeta con PDFs: OCR → parser → decisión (producción).
    PdfDir(PathBuf),
    /// Facturas ya extraídas en JSONL (comprobación del motor).
    Fixture(PathBuf),
}

/// Ruta de `reglas.toml`, dando prioridad al entorno.
///
/// `REGLAS_PATH` sigue mandando porque es así como se inyecta la regla del
/// sábado sin tocar la línea de comandos (spec_y_plan.md §1.3).
fn ruta_reglas(args: &Args) -> PathBuf {
    std::env::var("REGLAS_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|_| args.reglas.clone())
}

/// Ruta del snapshot del ERP, dando prioridad al entorno (`ERP_SNAPSHOT`).
///
/// Es configurable a propósito: el snapshot es un dato del despliegue (hay uno
/// por entorno) y no una constante del binario, así que la ruta por defecto
/// tiene que funcionar en cualquier máquina sin que nadie edite código.
fn ruta_snapshot(args: &Args) -> PathBuf {
    std::env::var("ERP_SNAPSHOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| args.erp_snapshot.clone())
}

/// Carga las reglas del motor, negándose a arrancar si no son válidas.
///
/// Arrancar con las reglas por defecto sería peor que fallar: una regla
/// inyectada que no se carga a tiempo es un pago indebido.
fn cargar_reglas(ruta: &Path) -> Result<ReglasConfig, Box<dyn std::error::Error>> {
    match ReglasConfig::from_path(ruta) {
        Ok(reglas) => {
            tracing::info!("reglas v{} cargadas desde {}", reglas.version, ruta.display());
            Ok(reglas)
        }
        Err(e) => {
            tracing::error!("no se pudo cargar {}: {e}", ruta.display());
            Err(format!("reglas inválidas en {}: {e}", ruta.display()).into())
        }
    }
}

// --- EL ERP: SNAPSHOT EN DISCO O DESCARGA REAL ---

/// Los avisos que trae un snapshot, en voz alta.
///
/// Las filas sucias del ERP (hoy 20 de 516 vienen sin NIF) siguen siendo un dato
/// sucio y se publican: **entran** en el catálogo —se pueden conciliar por pedido—
/// pero no se pueden pagar en automático, así que un ESCALAR por R10 tiene que
/// poder explicarse leyendo el arranque. Se resumen en el log porque el snapshot
/// es una foto del despliegue y sus defectos son los mismos para todo el lote.
fn avisar_de_snapshot(snapshot: &erp::Snapshot) {
    if !snapshot.estado.eq_ignore_ascii_case("COMPLETO") {
        tracing::warn!(
            "el snapshot {} viene {} ({} fila(s) del ERP, {} asiento(s) leído(s)): conviene \
             repetir la descarga con --refetch-erp antes de fiarse del lote",
            snapshot.snapshot_id,
            snapshot.estado,
            snapshot.asientos_descargados,
            snapshot.total_asientos
        );
    }
    for aviso in &snapshot.avisos {
        tracing::warn!("ERP: {aviso}");
    }
    // Un snapshot descargado a base de reintentos no es sospechoso por sí solo
    // (el bridge inyecta fallos a propósito), pero sí es la explicación de un
    // recuento raro, así que se deja anotado junto a la foto.
    if snapshot.reintentos.hubo_fallos() {
        tracing::warn!(
            "el snapshot {} se descargó con {} reintento(s) (ORA {}, SES {}, 429 {}): si el \
             recuento no cuadra, empieza por aquí",
            snapshot.snapshot_id,
            snapshot.reintentos.total(),
            snapshot.reintentos.ora_00600,
            snapshot.reintentos.ses_401,
            snapshot.reintentos.erp_429
        );
    }
}

/// Los asientos del ERP con los que decide este proceso, y de qué foto salieron.
///
/// Dos caminos y solo dos (TRASPASO.md §3.5):
///
/// * **snapshot en disco** (lo normal): se lee y no se toca la red. Es lo que
///   hace reproducible la decisión —la misma foto da el mismo JSONL— y lo que
///   permite decidir aunque el bridge no esté levantado.
/// * **`--refetch-erp`**: se descarga del bridge (en serie y con reintentos
///   porque el ERP lo pide así), se escribe el snapshot y se decide con lo
///   recién bajado.
///
/// El id que se devuelve es el `snapshot_id` real, no un literal: es la huella
/// con la que después se reconstruye a qué foto del ERP se apuntó cada pago
/// (INV-9).
async fn cargar_erp(args: &Args) -> Result<(Vec<Asiento>, String), Box<dyn std::error::Error>> {
    let ruta = ruta_snapshot(args);

    if !args.refetch_erp {
        let snapshot = erp::leer_snapshot(&ruta).map_err(|e| {
            format!(
                "sin asientos del ERP no se puede decidir: {e}. Baja el snapshot con \
                 `--refetch-erp`, o apunta `--erp-snapshot` / `ERP_SNAPSHOT` al bueno"
            )
        })?;
        let asientos = erp::asientos_del_snapshot(&snapshot);
        // Un asiento sin NIF **entra** en el catálogo: se concilia por pedido y
        // es `R10` quien lo escala con motivo. Se cuenta aparte porque es el
        // número que explica los ESCALAR del lote.
        let sin_nif = asientos
            .iter()
            .filter(|asiento| asiento.nif.is_none())
            .count();
        tracing::info!(
            "ERP: snapshot {} ({}) → {} asiento(s) de {} fila(s) ({sin_nif} sin NIF, escalan por \
             R10), {} página(s)",
            snapshot.snapshot_id,
            ruta.display(),
            asientos.len(),
            snapshot.asientos_descargados,
            snapshot.paginas
        );
        avisar_de_snapshot(&snapshot);
        return Ok((asientos, snapshot.snapshot_id));
    }

    let mut cliente = erp::ClienteErp::desde_entorno();
    if let Some(aviso) = cliente.aviso_de_credenciales() {
        tracing::warn!("{aviso}");
    }

    // Antes de bajarse 516 filas se pregunta quién es: `GET /erp/estado` no pide
    // token y devuelve la versión del bridge y cuántos asientos declara tener.
    // Es la única forma de que el log de un despliegue diga que se está hablando
    // con el ERP de verdad y no con cualquier cosa que escuche en la URL.
    //
    // Si no contesta no se aborta: la descarga tiene sus propios reintentos y es
    // ella la que decide si el ERP está utilizable; el estado es información,
    // no una puerta.
    match cliente.estado().await {
        Ok(estado) => tracing::info!(
            "ERP: {} · activo {} s · {} asiento(s) declarados{} · «{}»",
            estado.version,
            estado.activo_segundos,
            estado.asientos,
            if estado.actualizacion_cargada {
                " · actualización cargada"
            } else {
                ""
            },
            estado.animo
        ),
        Err(e) => {
            tracing::warn!("ERP: no se pudo leer el estado ({e}); se intenta la descarga igual")
        }
    }

    tracing::info!(
        "ERP: descargando de {} como `{}` …",
        cliente.base_url(),
        cliente.usuario()
    );

    let intentos = cliente.max_intentos();
    let descarga = cliente.descargar(intentos).await?;

    // El snapshot se escribe **antes** de decidir: si el lote muere a mitad, la
    // foto con la que se decidieron las primeras facturas sigue en disco y el
    // fallo es reproducible.
    let snapshot = erp::construir_snapshot(
        &descarga.asientos,
        &descarga.meta.snapshot_id,
        &descarga.meta.descargado_en,
        &descarga.meta,
    );
    erp::escribir_snapshot(&ruta, &snapshot)?;

    tracing::info!(
        "ERP: {} asiento(s) de {} fila(s) leída(s) en {} página(s) — {} peticione(s), \
         {} reintento(s) (ORA {}, SES {}, 429 {}), {} ms → {}",
        descarga.asientos.len(),
        cliente.filas_leidas(),
        descarga.meta.paginas,
        cliente.peticiones(),
        descarga.meta.reintentos.total(),
        descarga.meta.reintentos.ora_00600,
        descarga.meta.reintentos.ses_401,
        descarga.meta.reintentos.erp_429,
        descarga.meta.duracion_ms,
        ruta.display()
    );
    avisar_de_snapshot(&snapshot);
    // Los avisos no son filas perdidas: una fila sin NIF se conserva con
    // `nif: None` y escala por `R10`. Solo desaparecen las que no traen id, que
    // no se pueden ni identificar. Se registran igualmente porque el recuento de
    // suciedad del ERP es información de operación.
    for aviso in &descarga.avisos {
        tracing::warn!("ERP: fila {} anotada ({})", aviso.asiento_id, aviso.motivo);
    }
    for duplicado in &descarga.duplicados {
        tracing::warn!(
            "ERP: factura {} repetida: se conserva {} y se descartan {:?}",
            duplicado.clave_factura,
            duplicado.conservado,
            duplicado.descartados
        );
    }

    Ok((descarga.asientos, descarga.meta.snapshot_id))
}

/// Catálogo del servidor: el snapshot del ERP si está en disco, y si no los
/// asientos vigentes de Mongo.
///
/// El snapshot manda porque es *lo que dijo el ERP*, sin pasar por una
/// importación intermedia: si Mongo está vacío o desincronizado, el servidor
/// sigue decidiendo con la foto correcta. Mongo queda como red de seguridad
/// para un despliegue al que todavía no se le ha copiado el snapshot.
async fn catalogo_inicial(
    args: &Args,
    db: &Database,
) -> Result<Catalogo, Box<dyn std::error::Error>> {
    if args.refetch_erp {
        let (asientos, snapshot_id) = cargar_erp(args).await?;
        return Ok(Catalogo { asientos, snapshot_id });
    }

    let ruta = ruta_snapshot(args);
    match erp::leer_snapshot(&ruta) {
        Ok(snapshot) => {
            let asientos = erp::asientos_del_snapshot(&snapshot);
            tracing::info!(
                "catálogo del ERP desde {} ({}): {} asiento(s) vigente(s)",
                ruta.display(),
                snapshot.snapshot_id,
                asientos.len()
            );
            avisar_de_snapshot(&snapshot);
            Ok(Catalogo {
                asientos,
                snapshot_id: snapshot.snapshot_id,
            })
        }
        Err(e) => {
            tracing::warn!(
                "no se pudo leer {} ({e}); se usa Mongo como respaldo del catálogo",
                ruta.display()
            );
            Ok(Catalogo::desde_mongo(db).await)
        }
    }
}

// --- PUNTO DE ENTRADA ---

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Inicializar logs (imprescindible para debugear en el hackathon)
    tracing_subscriber::fmt::init();

    // El modo se decide antes de tocar Mongo, la red o el puerto: en modo lote no
    // se abre ninguna de las tres cosas.
    let args = Args::parse();
    if let Some(fuente) = args.fuente() {
        return ejecutar_lote(fuente, &args).await;
    }
    servidor(args).await
}

// --- MODO SERVIDOR (HTTP) ---

async fn servidor(args: Args) -> Result<(), Box<dyn std::error::Error>> {
    // 1. Conexión a MongoDB
    let mongo_uri = std::env::var("MONGO_URI").unwrap_or_else(|_| "mongodb://localhost:27017".into());
    let client_options = ClientOptions::parse(mongo_uri).await?;
    let client = Client::with_options(client_options)?;
    // BD del diseño lógico (diseño_logico.md §2). El scaffold usaba
    // "hackspain_db", que no es la BD del pipeline: `albertitos` es donde viven
    // `asientos`, `expedientes`, `reglas_versiones`... Configurable por env.
    let nombre_db = std::env::var("MONGO_DB").unwrap_or_else(|_| "albertitos".into());
    let db = client.database(&nombre_db);
    let collection = db.collection::<Factura>("invoices");

    // 2. Reglas del motor: se leen una vez al arranque; inyectar la regla del
    //    sábado es editar el TOML y reiniciar, sin recompilar.
    let reglas = cargar_reglas(&ruta_reglas(&args))?;

    // 3. Catálogo del ERP en memoria (ver `catalogo_inicial`).
    let catalogo = catalogo_inicial(&args, &db).await?;
    tracing::info!(
        "catálogo del ERP listo: {} asiento(s) vigente(s), snapshot {}",
        catalogo.asientos.len(),
        catalogo.snapshot_id
    );

    let state = AppState {
        db_collection: collection,
        reglas: Arc::new(reglas),
        catalogo: Arc::new(catalogo),
    };

    // 4. Definir Rutas (Axum)
    let app = Router::new()
        .route("/api/upload-invoice", post(handle_upload))
        .route("/api/decidir", post(handle_decidir))
        .with_state(state);

    // 5. Levantar Servidor
    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    tracing::info!("🚀 Servidor Rust escuchando en {}", addr);
    
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

// --- MODO LOTE: el ejecutor del JSONL de entrega ---

/// Un elemento del lote: la factura ya extraída más el contexto que la acompaña.
///
/// Es **exactamente** lo que tendrá que producir `parser.rs` (y `erp.rs` /
/// `excel.rs` para el contexto). El formato se define aquí, en el orquestador, y
/// no en el parser, porque es el contrato de entrada del lote: el día que el
/// parser exista, el lote no tiene que cambiar, solo la fuente.
#[derive(Debug, Deserialize)]
struct ElementoLote {
    /// Nombre exacto del PDF, con extensión y mayúsculas incluidas, porque es lo
    /// que va en la línea de entrega (`albertitos_plan.md` §0).
    file_id: String,
    factura: Factura,
    /// Asientos con los que conciliar. Si vienen, mandan sobre el catálogo.
    #[serde(default)]
    asientos: Vec<Asiento>,
    #[serde(default)]
    filas_excel: Vec<FilaExcel>,
}

/// La línea de entrega: **solo** estos dos campos (`albertitos_plan.md` §0).
///
/// Los campos de traza son opcionales según el enunciado y se omiten a
/// propósito: cuanto menos haya que parsear, menos puede romperse en la entrega.
#[derive(Debug, Serialize)]
struct LineaEntrega<'a> {
    file_id: &'a str,
    result: &'static str,
}

/// Recuento del lote, para poder decir de un vistazo si la distribución de
/// decisiones es razonable. El `obs.rs` de verdad lo hará con contadores
/// atómicos y escribiendo en `eventos`; esto es el resumen de consola.
#[derive(Debug, Default)]
struct Recuento {
    pagar: usize,
    no_pagar: usize,
    escalar: usize,
}

impl Recuento {
    fn suma(&mut self, resultado: Resultado) {
        match resultado {
            Resultado::Pagar => self.pagar += 1,
            Resultado::NoPagar => self.no_pagar += 1,
            Resultado::Escalar => self.escalar += 1,
        }
    }

    fn total(&self) -> usize {
        self.pagar + self.no_pagar + self.escalar
    }
}

/// Ejecuta el lote completo y escribe el JSONL de entrega.
async fn ejecutar_lote(fuente: FuenteLote, args: &Args) -> Result<(), Box<dyn std::error::Error>> {
    let out = args
        .out
        .as_ref()
        .ok_or("en modo lote `--out <JSONL>` es obligatorio")?;
    let reglas = cargar_reglas(&ruta_reglas(args))?;

    // Se avisa de lo que se ignora en lugar de callarlo: un flag aceptado y no
    // aplicado es la forma más rápida de creer que se ha hecho algo que no.
    if args.excel.is_some() {
        tracing::warn!("--excel ignorado: `excel.rs` aún no existe (ver TRASPASO.md)");
    }

    let etiqueta = match &fuente {
        FuenteLote::Fixture(_) => "fixture",
        FuenteLote::PdfDir(_) => "pdf-dir",
    };

    let es_lote_de_pdfs = matches!(fuente, FuenteLote::PdfDir(_));

    // El ERP se resuelve **antes** de mirar la fuente: es el contexto con el que
    // se decide cada factura, y si no se puede tener, mejor saberlo ahora que a
    // mitad del lote con el JSONL ya empezado.
    //
    // Con `--fixture` el snapshot es opcional a propósito: sus líneas ya traen
    // sus propios asientos, porque el caso dorado del motor tiene que poder
    // correrse en un clon recién bajado, donde `data/` —que está en
    // `.gitignore`— todavía no tiene snapshot del ERP.
    let (asientos_erp, erp_snapshot_id) = match cargar_erp(args).await {
        Ok(par) => par,
        Err(e) if !es_lote_de_pdfs => {
            tracing::warn!(
                "lote `--fixture` sin snapshot del ERP ({e}): se decide con los asientos que \
                 traiga cada línea"
            );
            (Vec::new(), String::from("sin-erp"))
        }
        Err(e) => return Err(e),
    };

    let entradas = match &fuente {
        FuenteLote::Fixture(ruta) => leer_fixture(ruta)?,
        // El lote real: PDF → OCR → `Factura`, con los asientos del snapshot
        // del ERP como contexto común de todas.
        FuenteLote::PdfDir(dir) => lote_de_pdfs(dir, &asientos_erp).await?,
    };

    tracing::info!(
        "lote {etiqueta}: {} factura(s) de entrada → {}",
        entradas.len(),
        out.display()
    );

    let mut salida = BufWriter::new(File::create(out)?);
    let mut recuento = Recuento::default();

    for (file_id, elemento) in entradas {
        // Una factura que revienta **no** aborta el lote: sale como ESCALAR con el
        // motivo de la excepción (spec_y_plan.md Bloque 5). Abortar sería peor:
        // la factura existe y alguien tiene que mirarla.
        let (resultado, motivo) = match elemento {
            Ok(el) => {
                // La huella dice de dónde salieron los asientos con los que se
                // decidió (INV-9). Un lote de PDFs decide con el snapshot del
                // ERP, así que su huella *es* el `snapshot_id`; un `--fixture`
                // trae los suyos (y si no trae ninguno, se dice, en vez de
                // inventarse un id de snapshot que nadie podrá resolver).
                let snapshot = if es_lote_de_pdfs {
                    erp_snapshot_id.clone()
                } else if el.asientos.is_empty() {
                    format!("sin-erp:{etiqueta}")
                } else {
                    format!("{etiqueta}:entrada")
                };
                let (_evidencia, decision) = decidir_factura(
                    &el.factura,
                    &el.asientos,
                    &el.filas_excel,
                    &reglas,
                    &snapshot,
                );
                (decision.resultado, decision.motivo)
            }
            Err(e) => (Resultado::Escalar, format!("excepción: {e}")),
        };

        // `serde_json::to_writer` + `\n` explícito, en vez de `println!`: la
        // entrega es UTF-8 **sin BOM** y con salto `\n`, y un `println!`
        // redirigido a fichero mete `\r\n` en Windows.
        serde_json::to_writer(
            &mut salida,
            &LineaEntrega {
                file_id: &file_id,
                result: resultado.as_str(),
            },
        )?;
        salida.write_all(b"\n")?;

        recuento.suma(resultado);
        tracing::info!("{file_id} → {} ({motivo})", resultado.as_str());
    }

    salida.flush()?;

    tracing::info!(
        "lote terminado: {} línea(s) — PAGAR {}, NO_PAGAR {}, ESCALAR {}",
        recuento.total(),
        recuento.pagar,
        recuento.no_pagar,
        recuento.escalar
    );

    Ok(())
}

/// Lee el lote de entrada, línea a línea.
///
/// Devuelve `(file_id, Ok(elemento) | Err(motivo))`: una línea ilegible **no**
/// aborta la lectura, porque el lote tiene que producir una línea de entrega por
/// factura. Si el objeto no encaja con el esquema se reintenta leer solo el
/// `file_id`, que es el único campo sin el que la línea de entrega no vale nada.
fn leer_fixture(
    ruta: &Path,
) -> Result<Vec<(String, Result<ElementoLote, String>)>, Box<dyn std::error::Error>> {
    let fichero =
        File::open(ruta).map_err(|e| format!("no se pudo abrir el lote {}: {e}", ruta.display()))?;
    let lector = BufReader::new(fichero);

    let mut entradas = Vec::new();
    let mut descartadas = 0usize;

    for (numero, linea) in lector.lines().enumerate() {
        let linea = linea.map_err(|e| format!("error leyendo {}: {e}", ruta.display()))?;
        let linea = linea.trim();
        // Se admiten líneas vacías y comentarios `#`: el lote se edita a mano y
        // poder comentar una factura sin romper el fichero vale más que la pureza.
        if linea.is_empty() || linea.starts_with('#') {
            continue;
        }

        match serde_json::from_str::<ElementoLote>(linea) {
            Ok(elemento) => {
                let file_id = elemento.file_id.clone();
                entradas.push((file_id, Ok(elemento)));
            }
            Err(e) => match file_id_de(linea) {
                Some(file_id) => entradas.push((file_id, Err(format!("línea {}: {e}", numero + 1)))),
                None => {
                    descartadas += 1;
                    tracing::error!(
                        "lote línea {}: ni siquiera se pudo leer el `file_id` ({e}); se descarta",
                        numero + 1
                    );
                }
            },
        }
    }

    if descartadas > 0 {
        tracing::warn!("{descartadas} línea(s) del lote descartadas antes de decidir");
    }
    Ok(entradas)
}

/// Rescata el `file_id` de una línea que no encaja con el esquema completo.
fn file_id_de(linea: &str) -> Option<String> {
    serde_json::from_str::<serde_json::Value>(linea)
        .ok()?
        .get("file_id")?
        .as_str()
        .map(str::to_string)
}

/// Nombres de los PDFs de una carpeta, ordenados.
///
/// Vive en el orquestador y no en el parser porque el lote necesita saber
/// cuántas facturas espera **antes** de poder extraer ninguna: es con eso con lo
/// que se comprueba que el JSONL de entrega tiene una línea por PDF.
fn listar_pdfs(dir: &Path) -> Vec<String> {
    let mut pdfs: Vec<String> = match std::fs::read_dir(dir) {
        Ok(entradas) => entradas
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_file())
            .filter(|e| {
                e.path()
                    .extension()
                    .and_then(|x| x.to_str())
                    .map(|x| x.eq_ignore_ascii_case("pdf"))
                    .unwrap_or(false)
            })
            .filter_map(|e| e.file_name().into_string().ok())
            .collect(),
        Err(e) => {
            tracing::error!("no se pudo leer {}: {e}", dir.display());
            Vec::new()
        }
    };
    pdfs.sort();
    pdfs
}

/// Convierte una carpeta de PDFs en el lote de entrada: OCR → parser → factura.
///
/// El OCR es la parte lenta del pipeline (renderiza a 250 DPI y pasa un modelo
/// ONNX por CPU), así que hay varios documentos en vuelo a la vez, pero **como
/// máximo** `ocr::CONCURRENCIA_MAXIMA`: por encima de ese techo las peticiones
/// no van más rápido, solo se estorban entre ellas.
///
/// El orden de la lista de salida es el de `listar_pdfs` (ordenado), no el de
/// llegada del OCR: si dependiera de quién termina antes, dos ejecuciones del
/// mismo lote darían ficheros distintos y el JSONL de entrega dejaría de ser
/// comparable.
async fn lote_de_pdfs(
    dir: &Path,
    asientos: &[Asiento],
) -> Result<Vec<(String, Result<ElementoLote, String>)>, Box<dyn std::error::Error>> {
    let pdfs = listar_pdfs(dir);
    if pdfs.is_empty() {
        // Fallar es deliberado: un lote vacío produciría un JSONL sin una sola
        // línea y parecería que no hay nada que decidir.
        return Err(format!(
            "`--pdf-dir {}` no tiene ningún PDF: no hay lote que entregar",
            dir.display()
        )
        .into());
    }

    let cliente = reqwest::Client::new();
    let semaforo = Arc::new(tokio::sync::Semaphore::new(ocr::CONCURRENCIA_MAXIMA as usize));
    let mut en_vuelo = tokio::task::JoinSet::new();

    for (indice, nombre) in pdfs.iter().enumerate() {
        let cliente = cliente.clone();
        let semaforo = Arc::clone(&semaforo);
        let ruta = dir.join(nombre);
        let nombre = nombre.clone();
        let asientos = asientos.to_vec();
        en_vuelo.spawn(async move {
            // El permiso se suelta solo al salir del bloque: es lo que mantiene
            // el número de documentos en vuelo por debajo del techo.
            let Ok(_permiso) = semaforo.acquire().await else {
                return (indice, nombre.clone(), Err(String::from("el semáforo del OCR se cerró")));
            };
            let lineas = match ocr::extraer_pdf(&cliente, &ruta).await {
                Ok(lineas) => lineas,
                // Una factura que no se puede leer no aborta el lote: sale como
                // ESCALAR con el motivo (spec_y_plan.md Bloque 5).
                Err(e) => return (indice, nombre.clone(), Err(format!("OCR: {e}"))),
            };
            let factura = parser::extraer(&lineas);
            (
                indice,
                nombre.clone(),
                Ok(ElementoLote {
                    file_id: nombre,
                    factura,
                    asientos,
                    filas_excel: Vec::new(),
                }),
            )
        });
    }

    let mut terminadas: Vec<Option<(String, Result<ElementoLote, String>)>> =
        (0..pdfs.len()).map(|_| None).collect();
    while let Some(terminado) = en_vuelo.join_next().await {
        match terminado {
            Ok((indice, nombre, resultado)) => terminadas[indice] = Some((nombre, resultado)),
            // Una tarea que revienta solo puede ser un fallo de programación,
            // y se dice como tal en vez de dejar huecos en la entrega.
            Err(e) => tracing::error!("una tarea de OCR murió antes de devolver nada: {e}"),
        }
    }

    Ok(pdfs
        .iter()
        .enumerate()
        .map(|(indice, nombre)| match terminadas[indice].take() {
            Some(par) => par,
            None => (
                nombre.clone(),
                Err(String::from("la tarea de OCR no llegó a terminar (ver los logs)")),
            ),
        })
        .collect())
}

// --- HANDLER (Controlador HTTP) ---

/// `POST /api/upload-invoice`: un PDF → una `Factura` extraída.
///
/// Es el mismo camino que `--pdf-dir` (OCR → parser) y devuelve el mismo tipo:
/// antes devolvía un `InvoiceData` con el texto crudo del OCR, que no es lo que
/// decide nada. La subida no decide —eso es `/api/decidir`— porque una factura
/// subida a mano todavía tiene que pasar por el motor con el snapshot vigente.
async fn handle_upload(
    State(state): State<AppState>,
    mut multipart: Multipart,
) -> Result<Json<Factura>, (StatusCode, String)> {
    // 1. El PDF del form-data. El nombre del fichero es lo que después aparece
    //    en la entrega, así que se respeta tal cual llega; si el cliente no lo
    //    manda se usa uno sintético, porque perder el nombre no puede impedir
    //    leer la factura.
    let mut nombre = String::from("subida.pdf");
    let mut bytes: Option<Vec<u8>> = None;
    loop {
        let campo = match multipart.next_field().await {
            Ok(Some(campo)) => campo,
            Ok(None) => break,
            Err(e) => return Err((StatusCode::BAD_REQUEST, format!("form-data ilegible: {e}"))),
        };
        if campo.name() != Some("file") {
            continue;
        }
        if let Some(fichero) = campo.file_name() {
            nombre = fichero.to_string();
        }
        bytes = Some(
            campo
                .bytes()
                .await
                .map_err(|e| (StatusCode::BAD_REQUEST, format!("no se pudo leer `{nombre}`: {e}")))?
                .to_vec(),
        );
        break;
    }
    let Some(bytes) = bytes else {
        return Err((
            StatusCode::BAD_REQUEST,
            String::from("no se envió ningún archivo en el campo `file`"),
        ));
    };

    // 2. OCR → parser: la misma pareja que usa el lote.
    let cliente_ocr = reqwest::Client::new();
    let lineas = ocr::extraer_bytes(&cliente_ocr, &nombre, bytes)
        .await
        .map_err(|e| {
            tracing::error!("OCR de {nombre}: {e}");
            (
                StatusCode::BAD_GATEWAY,
                format!("OCR no disponible para `{nombre}`: {e}"),
            )
        })?;
    let factura = parser::extraer(&lineas);

    tracing::info!(
        "factura `{nombre}`: {} línea(s) de OCR — nif_emisor {:?}, cif_cliente {:?}, pedido {:?}, total {:?}",
        lineas.len(),
        factura.nif_emisor.estado(),
        factura.cif_cliente.estado(),
        factura.pedido.estado(),
        factura.total.estado()
    );

    // 3. Guardar en Mongo (la decisión la toma `/api/decidir`).
    match state.db_collection.insert_one(&factura, None).await {
        Ok(insertado) => tracing::info!(
            "factura `{nombre}` guardada con id {:?}",
            insertado.inserted_id
        ),
        Err(e) => tracing::error!("no se pudo guardar `{nombre}` en Mongo: {e}"),
    }

    // 4. Devolver la factura extraída al frontend.
    Ok(Json(factura))
}

// --- HANDLER: decisión (motor de conciliación) ---

/// Entrada de `POST /api/decidir`.
///
/// `asientos` y `filas_excel` son **opcionales**: si la petición no trae ninguno,
/// se usa el catálogo cargado al arranque. Enviarlos permite probar el motor con
/// datos sueltos sin tocar Mongo y, sobre todo, permite decir "estos son los
/// asientos de ESTE snapshot" y que la huella lo refleje.
#[derive(Debug, Deserialize)]
struct PeticionDecision {
    factura: Factura,
    #[serde(default)]
    asientos: Vec<Asiento>,
    #[serde(default)]
    filas_excel: Vec<FilaExcel>,
}

/// Respuesta con la decisión **y la evidencia**.
///
/// Se devuelven juntas a propósito: una decisión sin la evidencia que la sostiene
/// no es auditable, y todo este diseño va de poder reconstruir por qué se pagó.
#[derive(Debug, Serialize)]
struct RespuestaDecision {
    decision: Decision,
    evidencia: Evidencia,
}

async fn handle_decidir(
    State(state): State<AppState>,
    Json(peticion): Json<PeticionDecision>,
) -> Result<Json<RespuestaDecision>, (StatusCode, String)> {
    let factura = &peticion.factura;

    // Sin identificadores utilizables no hay nada que conciliar, pero eso es una
    // **decisión** (R1 → ESCALAR), no un error de petición. Antes se devolvía un
    // 400 y eso hacía daño de tres formas: tapaba un ESCALAR legítimo (una
    // factura ilegible acababa en el cliente como "petición mal formada"), dejaba
    // el caso fuera de la traza y de los contadores, y contradecía
    // `albertitos_plan.md`, que lista "falta cualquier identificador crítico"
    // como un supuesto de decisión. Aquí solo se avisa, para que quede en el log.
    if factura.sin_identificadores() {
        tracing::warn!(
            "factura sin identificadores utilizables (nif_emisor: {:?}, pedido: {:?}): la decide R1, no un 400",
            factura.nif_emisor.estado(),
            factura.pedido.estado()
        );
    }

    // Lo mismo con el CIF del cliente: que no aparezca es una decisión (R1 bis →
    // ESCALAR), no un documento inválido. Se avisa aparte porque el motivo es
    // otro —aquí sí hay con qué conciliar, lo que falta es que la factura esté
    // completa— y confundir los dos casos en el log escondería cuál se revisa.
    if factura.sin_cif_cliente() {
        tracing::warn!(
            "factura sin CIF del cliente (cif_cliente: {:?}): la escala R1 por incompleta, no un 400",
            factura.cif_cliente.estado()
        );
    }

    // Si la petición aporta datos propios, mandan los suyos: no se mezclan con el
    // catálogo, porque mezclar dos fotos del ERP es justo lo que rompe la huella.
    let trae_datos_propios = !peticion.asientos.is_empty() || !peticion.filas_excel.is_empty();
    let (asientos, filas_excel): (&[Asiento], &[FilaExcel]) = if trae_datos_propios {
        (&peticion.asientos, &peticion.filas_excel)
    } else {
        (state.catalogo.asientos.as_slice(), &[])
    };
    let erp_snapshot_id = if trae_datos_propios {
        // Los asientos llegaron con la petición, no de un snapshot: decirlo así
        // evita que la huella apunte a una foto del ERP que no existe.
        "peticion".to_string()
    } else {
        state.catalogo.snapshot_id.clone()
    };

    let (evidencia, decision) = decidir_factura(
        factura,
        asientos,
        filas_excel,
        &state.reglas,
        &erp_snapshot_id,
    );

    tracing::info!(
        "decisión {} → {} (match: {:?}, {} asiento(s) candidato(s), {} conflicto(s))",
        decision.resultado.as_str(),
        decision.motivo,
        evidencia.match_por,
        asientos.len(),
        evidencia.conflictos.len()
    );

    Ok(Json(RespuestaDecision {
        decision,
        evidencia,
    }))
}

// --- SERVICIO OCR ---
//
// Aquí vivía el «patrón fallback» (`process_ocr` + `send_to_paddle`): dos URLs
// inventadas (`http://api-nube.tuservidor.com/ocr` y `http://localhost:5000/ocr`)
// y una petición que mandaba el PDF como cuerpo crudo, sin `multipart` y sin
// comprobar el contrato. Se ha borrado entero (TRASPASO.md §3.3): el cliente de
// verdad es `ocr::extraer_pdf` / `ocr::extraer_bytes`, que habla con el servicio
// del contrato, interpreta `{"lines": []}` como el fallo que es y no deja dos
// caminos distintos para lo mismo. Dejar los dos al lado era la peor opción: el
// de mentira era el que se ejecutaba.

// ---------------------------------------------------------------------------
// Tests del cableado: `decidir_factura` es lo que ejecuta el handler, así que
// estos tests cubren el camino real petición → conciliar → decidir.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{EstadoAsiento, Identificador, MatchStrategy, Nif, Resultado};
    use rust_decimal::Decimal;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).expect("decimal válido")
    }

    /// Un campo leído con confianza alta, con el crudo tal cual.
    fn leido<T>(valor: T, crudo: &str) -> Identificador<T> {
        Identificador::encontrado(valor, crudo, 0.98, None)
    }

    fn factura() -> Factura {
        Factura {
            // Con guion y en minúsculas a propósito: `Nif` lo canoniza al entrar.
            // El crudo guarda el guion que el canónico ya no tiene, que es la
            // prueba de que la normalización ocurrió y de cómo venía impreso.
            nif_emisor: leido(
                Nif::nuevo("b-12345678").expect("NIF válido"),
                "NIF: b-12345678",
            ),
            // El CIF del cliente, presente: una factura sin él la escala R1 bis
            // antes de llegar a ninguna de las reglas que se prueban aquí.
            cif_cliente: leido(Nif::nuevo("a58231074").expect("CIF válido"), "CIF: A58231074"),
            pedido: leido("PED-00123".to_string(), "Pedido: PED-00123"),
            numero_factura: leido("F-2024-001".to_string(), "Factura F-2024-001"),
            fecha: leido("2024-09-02".to_string(), "Fecha: 02/09/2024"),
            base: leido(d("1020.25"), "Base 1.020,25"),
            iva: leido(d("214.25"), "IVA 214,25"),
            total: leido(d("1234.50"), "TOTAL 1.234,50 EUR"),
        }
    }

    fn asiento(nif: &str, pedido: &str, importe: &str, estado: EstadoAsiento) -> Asiento {
        Asiento {
            asiento_id: "AS-00412".into(),
            nif: Some(Nif::nuevo(nif).expect("NIF de prueba válido")),
            pedido: pedido.into(),
            importe: d(importe),
            estado,
            proveedor: Some("Suministros Ibéricos S.L.".into()),
            fecha: Some("2024-09-03".into()),
        }
    }

    fn asiento_limpio(estado: EstadoAsiento, importe: &str) -> Asiento {
        asiento("B12345678", "PED-00123", importe, estado)
    }

    #[test]
    fn paga_cuando_el_erp_esta_pendiente_y_cuadra() {
        let (ev, dec) = decidir_factura(
            &factura(),
            &[asiento_limpio(EstadoAsiento::Pendiente, "1234.50")],
            &[],
            &ReglasConfig::default(),
            "snap-1",
        );

        assert_eq!(dec.resultado, Resultado::Pagar);
        assert_eq!(ev.match_por, MatchStrategy::ExactByPedido);
        // La decisión tiene que poder reconstruirse: con qué reglas y con qué foto.
        assert_eq!(dec.huellas.reglas_version, ReglasConfig::default().version);
        assert_eq!(dec.huellas.erp_snapshot_id, "snap-1");
        assert!(!dec.huellas.run_id.is_empty());
        assert!(!dec.motivo.is_empty(), "INV-6: el motivo nunca va vacío");
    }

    #[test]
    fn no_paga_si_el_erp_ya_esta_pagada() {
        let (_ev, dec) = decidir_factura(
            &factura(),
            &[asiento_limpio(EstadoAsiento::Pagada, "1234.50")],
            &[],
            &ReglasConfig::default(),
            "snap-1",
        );
        assert_eq!(dec.resultado, Resultado::NoPagar);
    }

    #[test]
    fn escala_si_el_catalogo_esta_vacio() {
        // Sin asientos (colección aún no cargada) la respuesta segura es ESCALAR,
        // no PAGAR: el catálogo vacío nunca debe convertirse en un vago permiso.
        let (_ev, dec) =
            decidir_factura(&factura(), &[], &[], &ReglasConfig::default(), "local-0");
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("ninguna fuente"), "motivo: {}", dec.motivo);
    }

    #[test]
    fn el_veto_survive_a_un_pdf_sin_nif() {
        // El agujero original: sin NIF en el PDF no se podía comprobar el veto.
        // El asiento del ERP sí lo trae, así que la prohibición se detecta igual.
        let reglas = ReglasConfig {
            prohibido_pagar_proveedor: vec![Nif::nuevo("b-12345678").expect("NIF válido")],
            ..ReglasConfig::default()
        };
        let mut sin_nif = factura();
        sin_nif.nif_emisor = Identificador::no_aparece();

        let (_ev, dec) = decidir_factura(
            &sin_nif,
            &[asiento_limpio(EstadoAsiento::Pendiente, "1234.50")],
            &[],
            &reglas,
            "snap-1",
        );

        assert_eq!(dec.resultado, Resultado::NoPagar);
        assert!(dec.motivo.contains("B12345678"), "motivo: {}", dec.motivo);
    }

    #[test]
    fn el_excel_entra_por_peticion_y_fuerza_escalar() {
        // El Excel no es verdad, pero si contradice al ERP nadie paga en automático.
        let filas = vec![FilaExcel {
            id: "Hoja1#42".into(),
            nif: Nif::nuevo("B12345678"),
            pedido: Some("PED-00123".into()),
            importe: Some(d("1234.50")),
            estado: Some("PAGADA".into()),
            crudo: Default::default(),
        }];

        let (ev, dec) = decidir_factura(
            &factura(),
            &[asiento_limpio(EstadoAsiento::Pendiente, "1234.50")],
            &filas,
            &ReglasConfig::default(),
            "snap-1",
        );

        assert_eq!(ev.excel_filas, vec!["Hoja1#42".to_string()]);
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(
            ev.conflictos
                .iter()
                .any(|c| c.contains("Excel marca PAGADA")),
            "conflictos: {:?}",
            ev.conflictos
        );
    }

    #[test]
    fn las_huellas_de_dos_decisiones_distintas_no_se_pisan() {
        // El `run_id` es lo que permite decir de qué ejecución salió cada decisión;
        // si se repitiera, la traza no serviría para reconstruir nada.
        let reglas = ReglasConfig::default();
        let (_, primera) = decidir_factura(&factura(), &[], &[], &reglas, "snap-1");
        let (_, segunda) = decidir_factura(&factura(), &[], &[], &reglas, "snap-1");
        assert_ne!(primera.huellas.run_id, segunda.huellas.run_id);
    }

    /// Una factura de la que no se pudo extraer ningún identificador sale como
    /// **decisión** (ESCALAR), no como error: es una factura que existe y que
    /// alguien tendrá que mirar, y tiene que quedar en la traza y en los
    /// contadores como los demás casos. Antes esto cortaba en un 400 y el caso
    /// desaparecía sin dejar rastro.
    #[test]
    fn sin_identificadores_sale_como_escalar_y_no_como_error() {
        let mut vacia = factura();
        vacia.nif_emisor = Identificador::no_aparece();
        vacia.pedido = Identificador::no_aparece();

        let (_ev, dec) = decidir_factura(
            &vacia,
            &[asiento_limpio(EstadoAsiento::Pendiente, "1234.50")],
            &[],
            &ReglasConfig::default(),
            "snap-1",
        );

        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("sin identificadores"), "motivo: {}", dec.motivo);
        assert!(
            dec.reglas_evaluadas.contains(&"R1_sin_identificadores".to_string()),
            "y la traza dice que la decidió R1: {:?}",
            dec.reglas_evaluadas
        );
    }

    /// Con el NIF ilegible no se paga en automático, aunque el resto cuadre: no
    /// se sabe contra qué proveedor se está pagando.
    #[test]
    fn con_el_nif_ilegible_no_se_paga_aunque_cuadre_todo() {
        let mut ilegible = factura();
        ilegible.nif_emisor = Identificador::ilegible("NIF: B1234567B", 0.62, None);

        let (_ev, dec) = decidir_factura(
            &ilegible,
            &[asiento_limpio(EstadoAsiento::Pendiente, "1234.50")],
            &[],
            &ReglasConfig::default(),
            "snap-1",
        );

        assert_eq!(dec.resultado, Resultado::Escalar);
        assert_eq!(dec.huellas.erp_snapshot_id, "snap-1");
    }

    // -----------------------------------------------------------------------
    // Modo lote: lo que se puede probar sin OCR, sin ERP y sin Excel.
    // -----------------------------------------------------------------------

    /// Escribe un fichero temporal único y devuelve su ruta. El pid va en el
    /// nombre para que dos ejecuciones de los tests no se pisen entre ellas.
    fn fichero_temporal(nombre: &str, contenido: &str) -> PathBuf {
        let ruta = std::env::temp_dir().join(format!(
            "hackspain_{nombre}_{}.jsonl",
            std::process::id()
        ));
        std::fs::write(&ruta, contenido).expect("se escribe el fichero temporal");
        ruta
    }

    /// El recuento final tiene que cuadrar con el número de líneas escritas: es
    /// el único sitio donde se ve de un vistazo si algún caso se ha quedado sin
    /// clasificar. Un contador que no suma es un contador que miente.
    #[test]
    fn el_recuento_cuadra_con_las_lineas_emitidas() {
        let mut r = Recuento::default();
        assert_eq!(r.total(), 0, "un lote vacío no tiene desenlaces");

        r.suma(Resultado::Pagar);
        r.suma(Resultado::Pagar);
        r.suma(Resultado::NoPagar);
        r.suma(Resultado::Escalar);

        assert_eq!((r.pagar, r.no_pagar, r.escalar), (2, 1, 1));
        assert_eq!(r.total(), 4);
    }

    /// El `file_id` se rescata de una línea rota porque es lo único que hace
    /// falta para poder entregar algo por esa factura. Un `file_id` numérico no
    /// vale: el contrato de entrega dice string, y entregar `42` no serviría
    /// para casar el nombre del PDF.
    #[test]
    fn el_file_id_se_rescata_solo_si_esta() {
        assert_eq!(
            file_id_de("{\"file_id\":\"a.pdf\",\"basura\":[1,2}"),
            None,
            "una línea que ni siquiera es JSON no tiene file_id"
        );
        assert_eq!(
            file_id_de("{\"file_id\":\"a.pdf\",\"asientos\":\"roto\"}"),
            Some("a.pdf".to_string()),
            "el file_id sobrevive a que el resto del objeto esté mal"
        );
        assert_eq!(file_id_de("{\"file_id\":42}"), None, "no es un nombre de PDF");
        assert_eq!(file_id_de("{\"otro\":\"a.pdf\"}"), None);
    }

    /// Una línea ilegible **no** aborta el lote: produce su propia línea de
    /// entrega (vía `file_id` rescatado) y quien la mire verá el motivo. Solo se
    /// descarta una línea si no hay ni `file_id`, y eso se cuenta.
    #[test]
    fn el_lote_sobrevive_a_lineas_rotas() {
        let ruta = fichero_temporal(
            "lote_roto",
            concat!(
                "# un comentario suelto\n",
                "\n",
                "{\"file_id\":\"buena.pdf\",\"factura\":{\"total\":{\"estado\":\"ENCONTRADO\",\"valor\":\"10.00\",\"rastro\":{\"crudo\":\"10,00\",\"score\":0.99}}}}\n",
                "{\"file_id\":\"rota.pdf\",\"factura\":{\"nif_emisor\":{\"estado\":\"ENCONTRADO\"}}}\n",
                "{\"no_es_jsonl\":\n",
            ),
        );

        let entradas = leer_fixture(&ruta).expect("el lote se lee");
        let _ = std::fs::remove_file(&ruta);

        assert_eq!(
            entradas.len(),
            2,
            "la línea sin `file_id` se descarta, el comentario y el hueco no cuentan"
        );

        let (id, ok) = &entradas[0];
        assert_eq!(id, "buena.pdf");
        assert!(ok.is_ok(), "la línea buena deserializa: {ok:?}");

        let (id, rota) = &entradas[1];
        assert_eq!(id, "rota.pdf", "se rescata el `file_id` para poder entregar");
        assert!(
            rota.as_ref().unwrap_err().contains("línea 4"),
            "el motivo dice qué línea del fichero falla, no la columna del JSON: {rota:?}"
        );
    }

    /// La lista de PDFs define cuántas facturas espera el lote, así que no puede
    /// colarse nada que no sea un PDF (incluida una carpeta llamada `.pdf`) ni
    /// perderse uno por la caja de la extensión.
    #[test]
    fn los_pdfs_se_listan_ordenados_y_con_la_extension_sea_cual_sea_la_caja() {
        let dir = std::env::temp_dir().join(format!("hackspain_pdfs_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("se crea el directorio temporal");
        for nombre in ["b.pdf", "A.PDF", "notas.txt", "sin_extension"] {
            std::fs::write(dir.join(nombre), b"x").expect("se escribe el fichero");
        }
        std::fs::create_dir_all(dir.join("carpeta.pdf")).expect("carpeta con pinta de PDF");

        let pdfs = listar_pdfs(&dir);
        let inexistente = listar_pdfs(&dir.join("no_existe"));
        let _ = std::fs::remove_dir_all(&dir);

        assert_eq!(
            pdfs,
            vec!["A.PDF".to_string(), "b.pdf".to_string()],
            "ordenados y solo PDFs"
        );
        assert!(
            inexistente.is_empty(),
            "una carpeta que no existe no inventa facturas"
        );
    }

    /// Un `--pdf-dir` sin PDFs no es un lote vacío: es un lote mal montado. Si
    /// se dejara pasar, la entrega saldría con cero líneas y el recuento
    /// (PAGAR 0, NO_PAGAR 0, ESCALAR 0) parecería un resultado limpio en vez del
    /// fallo que es. Se comprueba antes de tocar el OCR, así que no hace falta
    /// ningún servicio levantado.
    #[tokio::test]
    async fn un_directorio_sin_pdfs_es_un_error_y_no_un_lote_vacio() {
        let dir = std::env::temp_dir().join(format!(
            "hackspain_sin_pdfs_{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("se crea el directorio temporal");
        std::fs::write(dir.join("notas.txt"), b"no es un PDF").expect("se escribe el fichero");

        let error = lote_de_pdfs(&dir, &[])
            .await
            .expect_err("un directorio sin PDFs no puede entregar un lote");

        // También el directorio que no existe: es el error de escritura más
        // común (una ruta relativa lanzada desde otro sitio).
        let inexistente = lote_de_pdfs(&dir.join("no_existe"), &[]).await;
        let _ = std::fs::remove_dir_all(&dir);

        assert!(
            error.to_string().contains("no tiene ningún PDF"),
            "el motivo tiene que decir que faltan los PDFs: {error}"
        );
        assert!(
            inexistente.is_err(),
            "una carpeta que no existe tampoco es un lote"
        );
    }

    /// La ruta del snapshot tiene que poder cambiarse por bandera: el snapshot es
    /// un dato del despliegue —hay uno por entorno— y un binario que solo sabe
    /// leer `data/erp_snapshot.json` obliga a editar código para desplegarlo.
    #[test]
    fn el_snapshot_del_erp_tiene_ruta_por_defecto_y_se_puede_cambiar() {
        let por_defecto = Args::parse_from(["hackspain-ocr", "--fixture", "lote.jsonl"]);
        assert_eq!(
            por_defecto.erp_snapshot,
            PathBuf::from("data/erp_snapshot.json"),
            "el caso dorado del motor sigue apuntando al snapshot de siempre"
        );

        let con_bandera = Args::parse_from([
            "hackspain-ocr",
            "--fixture",
            "lote.jsonl",
            "--erp-snapshot",
            "/etc/maisa/erp_snapshot.json",
        ]);
        assert_eq!(
            con_bandera.erp_snapshot,
            PathBuf::from("/etc/maisa/erp_snapshot.json")
        );
    }

    // ---------------------------------------------------------------------
    // Adaptador BSON: la regresión del catálogo vacío
    //
    // Cuando el motor se apuntó por primera vez a Mongo **real** (no a un
    // fixture), los 516 asientos se descartaron en silencio con
    // `invalid type: map, expected a string`: `fecha` llega como BSON `date` e
    // `importe` como BSON `decimal`, y los dos se exponen a serde como mapas.
    // El servidor arrancaba en verde con el catálogo vacío y escalaba todas las
    // facturas. Estos tests fijan el contrato para que no vuelva a pasar sin
    // que nadie se entere.
    // ---------------------------------------------------------------------

    /// Documento con la misma forma que escribe `importar_asientos_mongo.py` y
    /// que exige el validador de `02-schema-init.js`.
    fn documento_asiento_bson() -> Document {
        doc! {
            "_id": "snap-1#AS-00412",
            "asiento_id": "AS-00412",
            "snapshot_id": "snap-1",
            "nif": "B12345678",
            "pedido": "PED-00123",
            "importe": Decimal128::from_str("1234.50").expect("decimal BSON válido"),
            "estado": "PENDIENTE",
            "proveedor": "Suministros Ibéricos S.L.",
            "fecha": DateTime::parse_rfc3339_str("2026-03-21T00:00:00Z").expect("fecha válida"),
            "vigente": true,
            "esquema_version": 1,
        }
    }

    /// El fallo original: `date` y `decimal` tienen que llegar al dominio.
    #[test]
    fn un_asiento_de_mongo_con_date_y_decimal_llega_al_dominio() {
        let documento = documento_asiento_bson();
        let dto: AsientoDoc =
            mongodb::bson::from_document(documento).expect("el documento de Mongo encaja");
        let asiento = dto.a_dominio().expect("conversión al dominio");

        assert_eq!(asiento.asiento_id, "AS-00412");
        assert_eq!(asiento.nif.as_ref().map(Nif::as_str), Some("B12345678"));
        assert_eq!(asiento.pedido, "PED-00123");
        assert_eq!(asiento.importe, d("1234.50"));
        assert_eq!(asiento.estado, EstadoAsiento::Pendiente);
        assert_eq!(
            asiento.fecha.as_deref(),
            Some("2026-03-21"),
            "la fecha BSON se normaliza a YYYY-MM-DD, igual que la clave del ERP"
        );
        assert_eq!(asiento.proveedor.as_deref(), Some("Suministros Ibéricos S.L."));
    }

    /// El importe tiene que llegar **exacto**: es dinero y los céntimos deciden
    /// entre PAGAR y ESCALAR con la tolerancia de 0,01 EUR.
    #[test]
    fn el_importe_decimal128_llega_sin_perder_centimos() {
        let mut documento = documento_asiento_bson();
        documento.insert("importe", Decimal128::from_str("6199.54").expect("decimal"));
        let dto: AsientoDoc = mongodb::bson::from_document(documento).expect("encaja");
        let asiento = dto.a_dominio().expect("conversión");

        // No 6199.539999999999: por eso la conversión va por texto y no por f64.
        assert_eq!(asiento.importe, d("6199.54"));
        assert_eq!(asiento.importe.to_string(), "6199.54");
    }

    /// Un asiento sin NIF utilizable se conserva con `nif: None`: el asiento
    /// existe (tiene pedido, importe y estado) y tirarlo borraría la prueba de
    /// que el ERP sí tiene la factura.
    #[test]
    fn un_asiento_sin_nif_se_conserva_sin_nif() {
        let mut documento = documento_asiento_bson();
        documento.insert("nif", "  --- ");
        let dto: AsientoDoc = mongodb::bson::from_document(documento).expect("encaja");
        let asiento = dto.a_dominio().expect("el asiento se conserva");

        assert_eq!(asiento.nif, None, "un NIF que canoniza a vacío es 'sin NIF'");
        assert_eq!(asiento.pedido, "PED-00123", "el pedido sobrevive");
        assert_eq!(asiento.importe, d("1234.50"), "y el importe también");
    }

    /// Y ese asiento sin NIF **no** se paga en automático: escala diciendo por
    /// qué, en vez de desaparecer y escalar como si el ERP no lo tuviera.
    #[test]
    fn el_asiento_sin_nif_escala_diciendo_que_falta_el_nif() {
        let mut documento = documento_asiento_bson();
        documento.insert("nif", "");
        let dto: AsientoDoc = mongodb::bson::from_document(documento).expect("encaja");
        let asiento = dto.a_dominio().expect("conversión");

        let (evidencia, decision) =
            decidir_factura(&factura(), &[asiento], &[], &ReglasConfig::default(), "snap-1");

        // Lo importante: el match **existe**. La factura no es un "sin match".
        assert_eq!(evidencia.match_por, MatchStrategy::ExactByPedido);
        assert_eq!(decision.resultado, Resultado::Escalar);
        assert!(
            decision.motivo.contains("AS-00412") && decision.motivo.contains("sin NIF"),
            "el motivo cita el asiento y la falta de NIF: {}",
            decision.motivo
        );
        assert!(
            decision
                .reglas_evaluadas
                .contains(&"R10_erp_sin_nif".to_string()),
            "la traza nombra la regla aplicada: {:?}",
            decision.reglas_evaluadas
        );
    }

    /// Prueba de fuego: un asiento que viene de Mongo (no de un fixture) tiene
    /// que producir la misma decisión que el motor da con un fixture.
    #[test]
    fn el_asiento_de_mongo_paga_por_el_camino_completo() {
        let dto: AsientoDoc =
            mongodb::bson::from_document(documento_asiento_bson()).expect("encaja");
        let asiento = dto.a_dominio().expect("conversión");

        let (evidencia, decision) = decidir_factura(
            &factura(),
            &[asiento],
            &[],
            &ReglasConfig::default(),
            "snap-1",
        );

        assert_eq!(decision.resultado, Resultado::Pagar);
        assert_eq!(evidencia.match_por, MatchStrategy::ExactByPedido);
    }

    /// Prueba de fuego del contrato del ERP, de punta a punta y contra el
    /// snapshot real: una factura que casa con uno de los 20 asientos **sin NIF**
    /// tiene que salir `ESCALAR` por `R10`, no `ESCALAR` por «sin match».
    ///
    /// Antes de que `Asiento::nif` fuera opcional, esos 20 asientos se tiraban en
    /// el cargador y estas facturas caían en `R9_sin_match`: el motivo decía que
    /// el ERP no tenía la factura cuando el ERP sí la tenía. Es la diferencia
    /// entre «falta un dato» y «el dato se perdió» — y solo la primera se puede
    /// auditar.
    #[test]
    fn una_factura_que_casa_con_un_asiento_sin_nif_del_erp_escala_por_r10() {
        let ruta = Path::new(env!("CARGO_MANIFEST_DIR")).join("data/erp_snapshot.json");
        if !ruta.exists() {
            // El snapshot se descarga aparte y no tiene por qué estar en el
            // árbol de trabajo; si no está, no hay nada que comprobar.
            return;
        }
        let snapshot = erp::leer_snapshot(&ruta).expect("el snapshot commitado se lee");
        let catalogo = erp::asientos_del_snapshot(&snapshot);
        let sin_nif: Vec<&Asiento> = catalogo
            .iter()
            .filter(|asiento| asiento.nif.is_none())
            .collect();
        assert_eq!(
            sin_nif.len(),
            20,
            "el snapshot real trae 20 asientos sin NIF; si no, este test no prueba nada"
        );

        let asiento = sin_nif[0];
        assert_eq!(
            asiento.estado,
            EstadoAsiento::Pendiente,
            "los 20 sin NIF del snapshot están todos PENDIENTE: el motivo del \
             ESCALAR tiene que ser el NIF, no el estado"
        );

        // La factura del papel: mismo pedido y mismo total que el asiento, pero
        // **con** NIF de emisor, porque el NIF del proveedor lo trae el PDF y no
        // el ERP. Es exactamente el caso que el cargador tiraba.
        let con_pedido = Factura {
            pedido: leido(asiento.pedido.clone(), "Pedido del PDF"),
            total: leido(asiento.importe, "TOTAL del PDF"),
            ..factura()
        };

        let (evidencia, decision) = decidir_factura(
            &con_pedido,
            &catalogo,
            &[],
            &ReglasConfig::default(),
            &snapshot.snapshot_id,
        );

        // Lo que cambia con el merge: el match **existe**. Sin él, R10 no podría
        // dar un motivo útil y la factura sería indistinguible de una que el ERP
        // no tiene.
        assert_eq!(
            evidencia.match_por,
            MatchStrategy::ExactByPedido,
            "el asiento sin NIF se concilia igual: el pedido es la llave"
        );
        assert_eq!(decision.resultado, Resultado::Escalar);
        assert!(
            !decision
                .reglas_evaluadas
                .contains(&"R9_sin_match".to_string()),
            "no es un «sin match»: el ERP sí tiene la factura ({:?})",
            decision.reglas_evaluadas
        );
        assert!(
            decision
                .reglas_evaluadas
                .contains(&"R10_erp_sin_nif".to_string()),
            "la traza nombra la regla aplicada: {:?}",
            decision.reglas_evaluadas
        );
        assert!(
            decision.motivo.contains(&asiento.asiento_id) && decision.motivo.contains("sin NIF"),
            "el motivo cita el asiento y la falta de NIF: {}",
            decision.motivo
        );
    }
}
