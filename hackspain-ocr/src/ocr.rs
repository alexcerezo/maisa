// Cliente HTTP del servicio OCR (`ocr_service`, `POST /ocr`): de un PDF a
// `Vec<LineaOcr>`. Ver TRASPASO.md §3.3, spec_y_plan.md §3.3 y
// _scratch/ERP-RUST-CONTRATO.md §4.3.
//
// Este módulo sustituye al camino viejo de `main.rs::process_ocr`, que hablaba
// con dos URLs muertas (`api-nube.tuservidor.com` y `localhost:5000`) y devolvía
// un `InvoiceData { emisor, total, raw_text }` que no tiene nada que ver con
// `Factura`. Aquí no se repite ninguno de sus dos errores: ni URLs cableadas a
// fuego (hay una sola y se configura por `OCR_URL`) ni un tipo intermedio
// propio: se devuelven las líneas crudas y es el parser quien decide qué
// significan. El integrador es quien borra `process_ocr` / `send_to_paddle`.
//
// **Una petición por documento.** El cuerpo de la petición *es* el PDF, así que
// no hay forma de mandar varios sin inventarse un campo que el contrato no
// tiene. Tampoco interesaría: la unidad de error y de reintento es el
// documento, y agrupar solo conseguiría que un PDF ilegible arrastrase a los
// que sí se podían leer. La concurrencia (4-8 documentos en vuelo, ver
// `CONCURRENCIA_MAXIMA`) la decide quien llama a `extraer_pdf`; este módulo no
// abre conexiones propias ni lanza tareas: cada llamada es exactamente una
// petición.

use std::env;
use std::fmt;
use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// A qué URL se pregunta si el entorno no dice otra cosa.
const URL_POR_DEFECTO: &str = "http://127.0.0.1:8000/ocr";

/// Presupuesto de espera por documento, en segundos. Ver `extraer_pdf`.
pub const TIMEOUT_SEGUNDOS: u64 = 60;

/// Techo de documentos en vuelo que admite el contrato (spec_y_plan.md §3.3).
///
/// RapidOCR es un modelo ONNX atado a CPU: por encima de esto las peticiones no
/// van más rápido, solo hacen cola dentro del servicio y engordan el tiempo de
/// espera de todas. Es un techo, no una orden: quien llama decide su número
/// entre 4 y este valor.
pub const CONCURRENCIA_MAXIMA: usize = 8;

/// Una línea de texto reconocida por el OCR, con su página, su caja y su
/// confianza.
///
/// **Contrato congelado** (`ERP-RUST-CONTRATO.md` §4.2): `parser.rs` consume
/// exactamente estos campos, en este orden y con estos tipos. No se añade ni se
/// renombra ninguno.
///
/// `pagina` es **0-based** y `bbox` es `[x1, y1, x2, y2]` en el sistema de
/// coordenadas de la página renderizada (píxeles a ~250 DPI). Aquí no se
/// normaliza nada: la geometría se conserva tal cual porque es lo único que
/// permite que una traza vuelva a resaltar la línea en la que se apoyó una
/// decisión. `score` viene del OCR tal cual, sin redondear ni inventar: el
/// score de un dato tiene que ser el de la línea de la que salió, o no sirve
/// para decidir si escalar.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LineaOcr {
    // `alias` (que no `rename`): se **acepta** el nombre inglés que usan
    // spec_y_plan.md §3.3 y TRASPASO.md §3.3 (`page` / `text`) además del
    // castellano que fija el contrato §4.3 (`pagina` / `texto`). Los dos
    // documentos describen el mismo servicio y hoy no coinciden, y el servicio
    // todavía devuelve `{"lines": [], "pages": 0}`, así que no hay una respuesta
    // real que zanje cuál de las dos grafías es la buena. Con el alias funciona
    // con las dos, y **no** cambia lo que este tipo serializa (`pagina` /
    // `texto`, que es lo que lee el parser). Cuando una grafía quede
    // descartada, bórrense estos dos atributos.
    #[serde(alias = "page")]
    pub pagina: u32,
    #[serde(alias = "text")]
    pub texto: String,
    pub bbox: [f64; 4],
    pub score: f64,
}

/// Fallo al obtener las líneas de OCR.
///
/// Los tres casos están separados porque exigen reacciones distintas:
///
/// * `Transporte` — no se pudo ni leer el PDF del disco ni hablar con el
///   servicio (conexión rechazada, timeout, fichero ilegible). Es
///   infraestructura: reintentar tiene sentido.
/// * `Servicio` — el servicio contestó, pero no como dice el contrato (HTTP de
///   error, JSON ilegible, campos que faltan). Es un despliegue roto: repetir
///   la misma petición no arregla nada, hay que mirar el servicio.
/// * `Vacio` — contestó bien y **sin ninguna línea**. Ver `leer_cuerpo`: esto
///   no es «esta factura no trae NIF», es que el OCR no procesó el documento.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OcrError {
    Transporte(String),
    Servicio(String),
    Vacio(String),
}

impl fmt::Display for OcrError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OcrError::Transporte(mensaje) => write!(f, "OCR inalcanzable: {mensaje}"),
            OcrError::Servicio(mensaje) => write!(f, "respuesta inválida del servicio OCR: {mensaje}"),
            OcrError::Vacio(mensaje) => write!(f, "el OCR no extrajo ninguna línea: {mensaje}"),
        }
    }
}

impl std::error::Error for OcrError {}

/// URL del endpoint OCR: `OCR_URL` del entorno o `http://127.0.0.1:8000/ocr`.
///
/// Se lee el entorno **en cada llamada** (y no en un valor cacheado) porque el
/// coste es despreciable frente a un OCR que tarda segundos, y así vale siempre
/// el último valor puesto: se puede cambiar desde un test o desde un arranque
/// que ajuste la configuración después de cargar el módulo.
///
/// Una variable **vacía o en blanco** cuenta como no puesta. En Windows es
/// facilísimo quedarse con un `set OCR_URL=` en la sesión y, sin esta guarda,
/// el cliente pediría contra la URL vacía: el error que verías sería un
/// «builder error» de reqwest en lugar del evidente «no me han dicho dónde está
/// el OCR».
pub fn url_por_defecto() -> String {
    match env::var("OCR_URL") {
        Ok(valor) if !valor.trim().is_empty() => valor.trim().to_owned(),
        _ => URL_POR_DEFECTO.to_owned(),
    }
}

/// Envía **un** PDF al servicio OCR y devuelve sus líneas.
///
/// El PDF se lee entero en memoria y viaja como un único campo multipart
/// llamado exactamente `"file"`: es el nombre que necesita FastAPI para casar
/// el parámetro `file: UploadFile = File(...)` del servicio; con cualquier otro
/// nombre la respuesta es un 422, no un OCR.
///
/// **Por qué 60 s.** La primera petición de una sesión paga la carga del modelo
/// ONNX y el renderizado a 250 DPI de todas las páginas en CPU, y un documento
/// de varias páginas se nota. Un timeout corto convertiría un arranque lento
/// (normal) en un «OCR caído» en mitad de la demo. Se fija **en la petición** y
/// no en el cliente de quien llama, precisamente para que el valor no dependa
/// de cómo se haya construido ese cliente ni de si alguien le puso un timeout
/// general más agresivo.
pub async fn extraer_pdf(cliente: &reqwest::Client, ruta: &Path) -> Result<Vec<LineaOcr>, OcrError> {
    extraer_pdf_en(cliente, ruta, &url_por_defecto()).await
}

/// El mismo OCR, pero con el documento ya en memoria.
///
/// Existe por el servidor HTTP (`POST /api/upload-invoice`), que recibe los
/// bytes del PDF dentro del `multipart` y no tiene ninguna ruta del sistema que
/// leer. Escribirlos a un temporal para volver a leerlos sería dar una vuelta
/// por el disco para nada, y dejaría basura en el directorio temporal si el
/// proceso muere a mitad.
///
/// `documento` es el nombre con el que el servicio verá el fichero (y el que
/// sale en los mensajes de error), así que conviene pasar el nombre original.
pub async fn extraer_bytes(
    cliente: &reqwest::Client,
    documento: &str,
    bytes: Vec<u8>,
) -> Result<Vec<LineaOcr>, OcrError> {
    extraer_documento_en(cliente, documento, bytes, &url_por_defecto()).await
}

/// El cuerpo de `extraer_pdf` con la URL explícita: existe para poder apuntar a
/// un servidor de mentira en los tests sin depender de `OCR_URL` (que es estado
/// global del proceso y haría los tests dependientes del orden).
async fn extraer_pdf_en(
    cliente: &reqwest::Client,
    ruta: &Path,
    url: &str,
) -> Result<Vec<LineaOcr>, OcrError> {
    let bytes = tokio::fs::read(ruta)
        .await
        .map_err(|e| OcrError::Transporte(format!("no se pudo leer {}: {e}", ruta.display())))?;

    let documento = ruta
        .file_name()
        .and_then(|nombre| nombre.to_str())
        .unwrap_or("documento.pdf");

    extraer_documento_en(cliente, documento, bytes, url).await
}

/// El envío en sí: un campo `multipart` con el documento y las tres reacciones
/// posibles a la respuesta (contrato, status raro, red).
async fn extraer_documento_en(
    cliente: &reqwest::Client,
    documento: &str,
    bytes: Vec<u8>,
    url: &str,
) -> Result<Vec<LineaOcr>, OcrError> {
    // `mime_str` solo falla con un MIME inválido, y "application/pdf" es una
    // constante válida: el `map_err` está para no dejar un `unwrap`, no porque
    // pueda dispararse.
    let parte = reqwest::multipart::Part::bytes(bytes)
        .file_name(documento.to_owned())
        .mime_str("application/pdf")
        .map_err(|e| OcrError::Transporte(format!("MIME inválido al montar el multipart: {e}")))?;
    let formulario = reqwest::multipart::Form::new().part("file", parte);

    let respuesta = cliente
        .post(url)
        .timeout(Duration::from_secs(TIMEOUT_SEGUNDOS))
        .multipart(formulario)
        .send()
        .await
        .map_err(|e| {
            if e.is_timeout() {
                OcrError::Transporte(format!(
                    "{url}: sin respuesta en {TIMEOUT_SEGUNDOS} s para {documento} (¿está el \
                     servicio saturado o atascado en la carga del modelo?)"
                ))
            } else if e.is_connect() {
                OcrError::Transporte(format!(
                    "{url}: no se puede conectar con el servicio OCR para {documento}"
                ))
            } else {
                OcrError::Transporte(format!("{url}: {e}"))
            }
        })?;

    // El contrato dice que el servicio **nunca** devuelve 500: ante un fallo
    // contesta 200 con `{"lines": [], "pages": 0}`. Por eso un status de error
    // aquí no es «OCR vacío» sino que hay algo delante que no es el servicio
    // (un proxy, un puerto ocupado por otra cosa...): `Servicio`, no `Vacio`.
    let estado = respuesta.status();
    if !estado.is_success() {
        return Err(OcrError::Servicio(format!(
            "{url} devolvió HTTP {estado} para {documento}"
        )));
    }

    let cuerpo = respuesta
        .bytes()
        .await
        .map_err(|e| OcrError::Transporte(format!("{url}: respuesta cortada para {documento}: {e}")))?;

    leer_cuerpo(&cuerpo, documento)
}

/// Sobre de la respuesta del servicio. `pages` es obligatorio a propósito: el
/// contrato siempre lo manda, así que su ausencia delata que no estamos
/// hablando con el servicio del contrato (un JSON parecido de otra cosa) y eso
/// es mejor saberlo que interpretarlo como cero páginas.
#[derive(Debug, Deserialize)]
struct CuerpoOcr {
    lines: Vec<LineaOcr>,
    pages: u32,
}

/// Convierte el cuerpo de la respuesta en líneas, o en un error explicativo.
///
/// **Por qué `{"lines": []}` es un error y no un `Ok(vec![])`.** El contrato
/// reserva `{"lines": [], "pages": 0}` para «el OCR no ha procesado el
/// documento» (TRASPASO.md §3.3, spec_y_plan.md §3.3): el servicio nunca
/// devuelve 500, así que el vacío *es* su forma de decir «he fallado».
/// Si el cliente lo tratase como éxito, una caída del OCR produciría un montón
/// de facturas que llegan al reconciliador sin un solo identificador y acaban
/// escaladas con motivo «sin identificadores» — es decir, un problema de
/// infraestructura disfrazado de problema de datos. El operario se pondría a
/// buscar a mano el NIF de facturas que nunca se leyeron. Con `Err`, en cambio,
/// la traza dice lo que pasó y el motivo de escalada es «OCR no disponible».
fn leer_cuerpo(cuerpo: &[u8], documento: &str) -> Result<Vec<LineaOcr>, OcrError> {
    let sobre: CuerpoOcr = serde_json::from_slice(cuerpo).map_err(|e| {
        OcrError::Servicio(format!(
            "respuesta de {documento} que no es el JSON del contrato: {e}"
        ))
    })?;

    if sobre.lines.is_empty() {
        return Err(OcrError::Vacio(format!(
            "{documento}: el servicio contestó sin ninguna línea (pages={}); es un fallo del OCR, \
             no una factura sin identificadores",
            sobre.pages
        )));
    }

    // Con líneas pero sin páginas la respuesta se contradice: las líneas salen
    // de páginas renderizadas, así que `pages == 0` es imposible si se ha
    // reconocido algo. Es «respuesta rara» → `Servicio`.
    if sobre.pages == 0 {
        return Err(OcrError::Servicio(format!(
            "{documento}: {} líneas con pages=0, respuesta contradictoria",
            sobre.lines.len()
        )));
    }

    Ok(sobre.lines)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// El cuerpo tal y como lo fija `ERP-RUST-CONTRATO.md` §4.3.
    const CUERPO_EJEMPLO: &str = r#"{"lines":[{"pagina":0,"texto":"TOTAL 2.110,00 EUR","bbox":[0,0,1,1],"score":0.98}],"pages":1}"#;

    #[test]
    fn parsea_el_cuerpo_de_ejemplo() {
        let lineas = leer_cuerpo(CUERPO_EJEMPLO.as_bytes(), "ejemplo.pdf").expect("cuerpo válido");

        assert_eq!(lineas.len(), 1);
        assert_eq!(lineas[0].pagina, 0);
        assert_eq!(lineas[0].texto, "TOTAL 2.110,00 EUR");
        assert_eq!(lineas[0].bbox, [0.0, 0.0, 1.0, 1.0]);
        assert_eq!(lineas[0].score, 0.98);
    }

    #[test]
    fn acepta_los_nombres_en_ingles_y_un_bbox_entero() {
        // spec_y_plan.md §3.3 documenta `page` / `text` y un `bbox` con enteros.
        // El alias tiene que cubrir esa variante sin cambiar el tipo.
        let cuerpo = r#"{"file_id":"factura_5518.pdf","pages":1,"lines":[{"page":0,"text":"NIF: B12345678","bbox":[122,78,320,100],"score":0.981}]}"#;

        let lineas = leer_cuerpo(cuerpo.as_bytes(), "factura_5518.pdf").expect("cuerpo válido");

        assert_eq!(
            lineas,
            vec![LineaOcr {
                pagina: 0,
                texto: "NIF: B12345678".to_owned(),
                bbox: [122.0, 78.0, 320.0, 100.0],
                score: 0.981,
            }]
        );
    }

    #[test]
    fn lines_vacio_es_un_fallo_del_ocr_y_nunca_ok() {
        match leer_cuerpo(br#"{"lines":[],"pages":0}"#, "fallo.pdf") {
            Err(OcrError::Vacio(mensaje)) => assert!(mensaje.contains("fallo.pdf")),
            otro => panic!("se esperaba Err(Vacio) y llegó {otro:?}: el vacío del OCR no es un Ok"),
        }
    }

    #[test]
    fn lines_vacio_es_error_aunque_el_servicio_diga_que_hay_paginas() {
        // Da igual lo que diga `pages`: sin líneas no hay nada que parsear.
        assert!(matches!(
            leer_cuerpo(br#"{"lines":[],"pages":3}"#, "fallo.pdf"),
            Err(OcrError::Vacio(_))
        ));
    }

    #[test]
    fn lineas_con_pages_cero_es_una_respuesta_contradictoria() {
        assert!(matches!(
            leer_cuerpo(
                br#"{"lines":[{"pagina":0,"texto":"FACTURA","bbox":[0,0,1,1],"score":0.9}],"pages":0}"#,
                "raro.pdf"
            ),
            Err(OcrError::Servicio(_))
        ));
    }

    #[test]
    fn falta_el_campo_lines_es_error_de_servicio() {
        assert!(matches!(
            leer_cuerpo(br#"{"file_id":"x.pdf","pages":1}"#, "cojo.pdf"),
            Err(OcrError::Servicio(_))
        ));
    }

    #[test]
    fn un_cuerpo_que_no_es_json_es_error_de_servicio() {
        assert!(matches!(
            leer_cuerpo(b"<html>502 Bad Gateway</html>", "proxy.pdf"),
            Err(OcrError::Servicio(_))
        ));
    }

    #[test]
    fn url_por_defecto_usa_ocr_url_y_si_no_la_constante() {
        // Es el *único* test que toca `OCR_URL` a propósito: `set_var` es
        // estado global del proceso y los tests corren en hilos en paralelo,
        // así que dos tests que la manipulasen serían una carrera.
        let previo = env::var("OCR_URL").ok();

        env::remove_var("OCR_URL");
        assert_eq!(url_por_defecto(), URL_POR_DEFECTO);

        env::set_var("OCR_URL", "http://127.0.0.1:9123/ocr");
        assert_eq!(url_por_defecto(), "http://127.0.0.1:9123/ocr");

        env::set_var("OCR_URL", "   ");
        assert_eq!(
            url_por_defecto(),
            URL_POR_DEFECTO,
            "una variable en blanco no debe pisar el valor por defecto"
        );

        match previo {
            Some(valor) => env::set_var("OCR_URL", valor),
            None => env::remove_var("OCR_URL"),
        }
    }

    /// Posición del primer `\r\n\r\n`: la frontera entre cabeceras y cuerpo HTTP.
    fn encontrar_crlf_crlf(datos: &[u8]) -> Option<usize> {
        datos.windows(4).position(|ventana| ventana == b"\r\n\r\n")
    }

    /// Servidor HTTP de mentira que atiende **una** petición: guarda lo que
    /// recibe y contesta el JSON del contrato.
    ///
    /// Es la forma de probar el camino HTTP completo (URL, multipart, mapeo del
    /// éxito) sin el servicio real y sin red: el bucle de escucha es local.
    async fn servidor_de_una_peticion() -> (String, tokio::task::JoinHandle<Vec<u8>>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let escucha = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("puerto libre");
        let puerto = escucha.local_addr().expect("dirección local").port();
        let url = format!("http://127.0.0.1:{puerto}/ocr");

        let tarea = tokio::spawn(async move {
            let (mut conexion, _) = escucha.accept().await.expect("conexión entrante");
            let mut recibido = Vec::new();
            let mut trozo = [0u8; 4096];

            // Primero las cabeceras, que es donde viene el `Content-Length` del
            // cuerpo multipart.
            let (fin_cabeceras, longitud_cuerpo) = loop {
                let leidos = conexion.read(&mut trozo).await.expect("leer petición");
                assert!(leidos > 0, "el cliente cerró antes de mandar las cabeceras");
                recibido.extend_from_slice(&trozo[..leidos]);

                if let Some(fin) = encontrar_crlf_crlf(&recibido) {
                    let cabeceras = String::from_utf8_lossy(&recibido[..fin]).to_lowercase();
                    let longitud = cabeceras
                        .lines()
                        .find_map(|linea| linea.strip_prefix("content-length:"))
                        .and_then(|valor| valor.trim().parse::<usize>().ok())
                        .expect("el multipart tiene que llevar Content-Length");
                    break (fin, longitud);
                }
            };

            // Y después el cuerpo entero (el PDF troceado en el multipart), que
            // puede llegar en varios segmentos TCP.
            while recibido.len() < fin_cabeceras + 4 + longitud_cuerpo {
                let leidos = conexion.read(&mut trozo).await.expect("leer cuerpo");
                assert!(leidos > 0, "el cliente cortó el cuerpo a medias");
                recibido.extend_from_slice(&trozo[..leidos]);
            }

            let cuerpo = r#"{"file_id":"fake.pdf","pages":1,"lines":[{"pagina":0,"texto":"TOTAL 1.234,56","bbox":[1,2,3,4],"score":0.9}]}"#;
            let respuesta = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{cuerpo}",
                cuerpo.len()
            );
            conexion.write_all(respuesta.as_bytes()).await.expect("responder");
            conexion.flush().await.expect("vaciar");
            let _ = conexion.shutdown().await;

            recibido
        });

        (url, tarea)
    }

    #[tokio::test]
    async fn extraer_pdf_habla_multipart_con_el_campo_file() {
        let ruta = std::env::temp_dir().join(format!("ocr_prueba_{}.pdf", std::process::id()));
        std::fs::write(&ruta, b"%PDF-1.4 contenido de prueba del cliente OCR").expect("escribir PDF");

        let (url, tarea) = servidor_de_una_peticion().await;
        let cliente = reqwest::Client::new();
        let lineas = extraer_pdf_en(&cliente, &ruta, &url)
            .await
            .expect("el camino HTTP debe funcionar de punta a punta");
        let peticion = tarea.await.expect("la tarea del servidor no debe entrar en pánico");

        let _ = std::fs::remove_file(&ruta);

        // Lo que de verdad hay que blindar es el nombre del campo: es lo que
        // casa con `file: UploadFile = File(...)` en el servicio FastAPI.
        let texto_peticion = String::from_utf8_lossy(&peticion);
        assert!(
            texto_peticion.contains(r#"name="file""#),
            "el campo multipart tiene que llamarse `file`: {texto_peticion}"
        );
        assert!(
            texto_peticion.contains("filename="),
            "el PDF debe viajar con nombre de fichero: {texto_peticion}"
        );
        assert!(
            texto_peticion.contains("%PDF-1.4 contenido de prueba"),
            "el cuerpo tiene que ser el PDF leído del disco: {texto_peticion}"
        );

        assert_eq!(
            lineas,
            vec![LineaOcr {
                pagina: 0,
                texto: "TOTAL 1.234,56".to_owned(),
                bbox: [1.0, 2.0, 3.0, 4.0],
                score: 0.9,
            }]
        );
    }

    fn data_facturas() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("data").join("facturas")
    }

    fn primer_pdf_de(directorio: std::path::PathBuf) -> Option<std::path::PathBuf> {
        let mut pdfs: Vec<_> = std::fs::read_dir(directorio)
            .ok()?
            .filter_map(|entrada| entrada.ok())
            .map(|entrada| entrada.path())
            .filter(|ruta| {
                ruta.extension()
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"))
            })
            .collect();
        pdfs.sort();
        pdfs.into_iter().next()
    }

    /// Test contra el servicio **vivo**, ignorado a propósito: en CI no hay
    /// servicio OCR y la petición solo podría fallar por conexión rechazada
    /// (o, peor, esperar 60 s a expirar). Para ejecutarlo:
    ///
    /// ```text
    /// cd ocr_service && uvicorn main:app --port 8000
    /// cargo test --bins -- --ignored contra_el_servicio_vivo
    /// ```
    #[tokio::test]
    #[ignore = "requiere el servicio OCR escuchando en OCR_URL (uvicorn ocr_service.main:app --port 8000)"]
    async fn contra_el_servicio_vivo() {
        let ruta = primer_pdf_de(data_facturas()).expect("no hay ningún PDF en data/facturas");
        let cliente = reqwest::Client::new();

        let lineas = extraer_pdf(&cliente, &ruta)
            .await
            .unwrap_or_else(|e| panic!("{}: {e}", ruta.display()));

        assert!(
            !lineas.is_empty(),
            "un PDF real tiene que devolver al menos una línea"
        );
        // Si el servicio está a medias (el `{"lines": [], "pages": 0}` de hoy),
        // el fallo de arriba ya lo dice; esto solo comprueba que la geometría
        // llega con valores utilizables.
        assert!(
            lineas.iter().all(|linea| linea.bbox[2] > linea.bbox[0] && linea.bbox[3] > linea.bbox[1]),
            "todas las cajas deberían tener ancho y alto positivos"
        );
    }
}
