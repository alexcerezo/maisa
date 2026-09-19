//! Cliente del ERP Miralmar (`MiralmarBridge/2.3.1`, 2009) y snapshot en disco.
//!
//! El ERP es la **fuente de verdad** de la conciliación: lo que dice un asiento
//! manda sobre lo que se haya leído del PDF. Este módulo es el único sitio que
//! habla con el bridge; su salida es `Vec<domain::Asiento>` o el JSON de
//! `data/erp_snapshot.json`, nunca XML.
//!
//! Ver `spec_y_plan.md` 3.6 y `_scratch/ERP-RUST-CONTRATO.md` 4.1.
//!
//! # Decisiones que no son obvias
//!
//! * **Serie, nunca en paralelo.** El bridge es un AS/400 de 2009 con un rate
//!   limit global (10 peticiones/s) y ~0,12 s por petición. Paralelizar las
//!   páginas dispara `ERP-429` y no acelera nada. `descargar` recorre las
//!   páginas en orden, y solo hay un camino con reintentos.
//! * **ISO-8859-1 antes de XML.** `quick-xml` no ve la declaración de
//!   codificación del prólogo, así que hay que darle UTF-8 ya decodificado.
//!   Se decodifica con `encoding_rs::WINDOWS_1252` a mano.
//! * **`por_pagina` no es constante.** Lo declara el ERP en `<meta>` y la
//!   última página trae menos filas (16 de 20 en el snapshot real de 516
//!   asientos). Nada de asumir 20 para calcular la última página.
//! * **`nif` es obligatorio en `domain::Asiento`.** Una fila sin NIF no se
//!   puede representar: se salta y se cuenta en `avisos` (hoy son 20 de 516),
//!   en vez de dejar que un NIF roto se propague en silencio.
//! * **Nada de `f64` para dinero.** Los importes son `Decimal` y en el
//!   snapshot se emiten como **número JSON** conservando los ceros finales
//!   (`9872.00`), porque el validador de Mongo declara `importe` como
//!   `bsonType: "decimal"`. `Decimal` pelado serializa a *string* y `f64`
//!   pierde los ceros, así que el número se construye con `RawValue`.
//! * **La clave nunca se imprime.** `Debug` está escrito a mano: un `{:?}` del
//!   cliente con `derive` volcaría `clave` y `token` a los logs.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::fmt;
use std::fs;
use std::path::Path;
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use encoding_rs::WINDOWS_1252;
use quick_xml::events::Event;
use quick_xml::Reader;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

use crate::domain::{Asiento, EstadoAsiento, Nif};

// ---------------------------------------------------------------------------
// Constantes de cable
// ---------------------------------------------------------------------------

/// Base del bridge. En producción se sobreescribe con `ERP_BASE_URL`.
pub const BASE_URL_POR_DEFECTO: &str = "http://127.0.0.1:8009";
/// Usuario del bridge. En producción se sobreescribe con `ERP_USUARIO`.
pub const USUARIO_POR_DEFECTO: &str = "alberto";
/// Clave del bridge. En producción se sobreescribe con `ERP_CLAVE`.
pub const CLAVE_POR_DEFECTO: &str = "FACTURAS2009";

pub const VARIABLE_BASE_URL: &str = "ERP_BASE_URL";
pub const VARIABLE_USUARIO: &str = "ERP_USUARIO";
pub const VARIABLE_CLAVE: &str = "ERP_CLAVE";

/// Aviso que se emite cuando se está usando la credencial del entorno de demo
/// en vez de una inyectada por el operador.
pub const AVISO_CREDENCIALES_POR_DEFECTO: &str = "el ERP se esta usando con las credenciales por defecto del entorno de demo; en produccion hay que inyectar ERP_USUARIO y ERP_CLAVE";

/// `por_pagina` que asume el ERP si no lo declara. No es una constante del
/// protocolo: es solo el respaldo cuando falta el dato.
pub const POR_PAGINA_POR_DEFECTO: u64 = 20;

const CABECERA_TOKEN: &str = "X-ERP-Token";
const ACCEPT: &str = "Accept";
const CONTENT_TYPE: &str = "Content-Type";
const RETRY_AFTER: &str = "Retry-After";
const FORMULARIO: &str = "application/x-www-form-urlencoded";
const TIEMPO_LIMITE_SEGUNDOS: u64 = 30;
const MAX_INTENTOS_POR_DEFECTO: u32 = 8;

const CODIGO_ORA_00600: &str = "ORA-00600";
const CODIGO_SES_401: &str = "SES-401";
const CODIGO_ERP_429: &str = "ERP-429";
const CODIGO_ERP_404: &str = "ERP-404";

const ESPERA_429_SEGUNDOS: f64 = 1.1;
const ESPERA_SES_401_SEGUNDOS: f64 = 0.1;
const TOPE_ESPERA_ORA_SEGUNDOS: f64 = 2.0;
const PASO_ESPERA_ORA_SEGUNDOS: f64 = 0.2;

// ---------------------------------------------------------------------------
// Error
// ---------------------------------------------------------------------------

/// Fallo al hablar con el ERP.
///
/// Cuatro variantes porque las cuatro se tratan distinto arriba: `Transporte`
/// (el bridge no está o se cayó) se puede reintentar en otro momento,
/// `Protocolo` (XML que no encaja) es un fallo de contrato, `Login` bloquea
/// cualquier trabajo y `Agotado` dice que ya se reintentó lo razonable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ErpError {
    /// No se pudo ni hablar con el ERP (DNS, conexión rechazada, timeout).
    Transporte(String),
    /// El ERP contestó algo que no encaja en el contrato (XML ilegible, falta
    /// `<meta>`, estado desconocido).
    Protocolo(String),
    /// El `POST /erp/login` no dio token.
    Login(String),
    /// Se agotaron los `max_intentos` sobre una ruta.
    Agotado(String),
}

impl fmt::Display for ErpError {
    fn fmt(&self, destino: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ErpError::Transporte(detalle) => write!(destino, "ERP inalcanzable: {detalle}"),
            ErpError::Protocolo(detalle) => write!(destino, "respuesta del ERP fuera de contrato: {detalle}"),
            ErpError::Login(detalle) => write!(destino, "login en el ERP: {detalle}"),
            ErpError::Agotado(detalle) => write!(destino, "{detalle}"),
        }
    }
}

impl std::error::Error for ErpError {}

/// Contadores de reintento. Se guardan en el snapshot: si un snapshot trae
/// `ora_00600 > 0`, el bridge falló durante la descarga y hay que saberlo antes
/// de fiarse de las cifras.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reintentos {
    /// `ORA-00600`: error interno del bridge; la sesión sigue viva, se repite
    /// la MISMA petición.
    pub ora_00600: u32,
    /// `SES-401`: el token ya no vale; hay que rehacer login.
    pub ses_401: u32,
    /// `ERP-429`: rate limit; la petición no se perdió, hay que bajar el ritmo.
    pub erp_429: u32,
}

impl Reintentos {
    pub fn total(&self) -> u32 {
        self.ora_00600 + self.ses_401 + self.erp_429
    }

    /// ¿Hubo algún fallo que obligase a reintentar?
    pub fn hubo_fallos(&self) -> bool {
        self.total() > 0
    }
}

// ---------------------------------------------------------------------------
// Tipos de la respuesta
// ---------------------------------------------------------------------------

/// `<meta>` de una página de `/erp/asientos`.
///
/// `por_pagina` viene del propio ERP y **no se asume constante**: se guarda tal
/// cual para que quien recorra las páginas compare contra el dato real.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MetaPagina {
    pub total: u32,
    pub paginas: u32,
    pub pagina: u32,
    pub por_pagina: u32,
    pub generado: String,
}

/// Una página de asientos con su `<meta>`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaginaAsientos {
    pub asientos: Vec<Asiento>,
    pub meta: MetaPagina,
}

/// `/erp/estado`: versión del bridge y salud del sistema. No pide token.
///
/// `actualizacion_cargada` se traduce a `bool`; `animo` es el mensaje que deja
/// el propio ERP y se conserva literal a propósito (es humor del sistema, pero
/// también diagnóstico: "El sistema lleva 17 anos funcionando. No sera hoy.").
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EstadoErp {
    pub version: String,
    pub activo_segundos: u64,
    pub asientos: u64,
    pub actualizacion_cargada: bool,
    pub animo: String,
}

/// Una fila del ERP que no se pudo convertir en `Asiento`, y por qué.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Aviso {
    pub asiento_id: String,
    pub motivo: String,
}

/// Dos o más asientos con la misma clave de factura: se conserva el de
/// `asiento_id` más bajo y se anota qué se descartó.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Duplicado {
    pub clave_factura: String,
    pub conservado: String,
    pub descartados: Vec<String>,
}

const ESTADO_COMPLETO: &str = "COMPLETO";
const ESTADO_PARCIAL: &str = "PARCIAL";

/// Lo que se sabe de la descarga **sin** los asientos: es lo que va a la
/// cabecera del snapshot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MetaSnapshot {
    pub snapshot_id: String,
    pub descargado_en: String,
    /// Filas **leídas del ERP** (antes de saltar las sucias y de colapsar
    /// duplicados). Es el número que se compara con `<meta><total>`.
    pub asientos_descargados: u32,
    pub paginas: u32,
    pub vigente: bool,
    /// `COMPLETO` si `asientos_descargados == total`, `PARCIAL` si no.
    pub estado: String,
    pub reintentos: Reintentos,
    pub duracion_ms: u64,
    /// Texto listo para `obs`: un aviso por motivo, con el recuento y una
    /// muestra (`"nif vacio en 20/516: ['AS-00499', ...]"`).
    pub avisos: Vec<String>,
    pub duplicados: Vec<Duplicado>,
}

impl Default for MetaSnapshot {
    fn default() -> Self {
        Self {
            snapshot_id: String::new(),
            descargado_en: String::new(),
            asientos_descargados: 0,
            paginas: 0,
            vigente: true,
            estado: String::from(ESTADO_COMPLETO),
            reintentos: Reintentos::default(),
            duracion_ms: 0,
            avisos: Vec::new(),
            duplicados: Vec::new(),
        }
    }
}

/// Resultado de [`ClienteErp::descargar`].
///
/// **Desviación documentada del contrato §4.1**, que pedía devolver
/// `Vec<Asiento>`: el recuento de filas sucias (los 20 NIF vacíos), la
/// cabecera del snapshot y la traza de duplicados tienen que llegar a quien
/// llama, o se pierden. Devolver solo los asientos obligaba a repetir la
/// descarga para reconstruir esa información.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Descarga {
    /// Asientos listos para conciliar, en el orden del ERP.
    pub asientos: Vec<Asiento>,
    /// Cabecera lista para `escribir_snapshot`.
    pub meta: MetaSnapshot,
    /// Una entrada por fila saltada.
    pub avisos: Vec<Aviso>,
    /// Facturas repetidas que se colapsaron.
    pub duplicados: Vec<Duplicado>,
}

/// ¿Hay que avisar de que se está usando la credencial de demo?
pub fn aviso_de_credenciales(base_url: &str, usuario: &str, clave: &str) -> Option<String> {
    let por_defecto = usuario == USUARIO_POR_DEFECTO || clave == CLAVE_POR_DEFECTO || base_url == BASE_URL_POR_DEFECTO;
    if por_defecto {
        Some(String::from(AVISO_CREDENCIALES_POR_DEFECTO))
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Transporte
// ---------------------------------------------------------------------------

/// Respuesta cruda del bridge, ya decodificada de ISO-8859-1 a UTF-8.
///
/// El código HTTP se conserva **como número** y no se mezcla con el cuerpo: en
/// la referencia en Python se concatenaban (`f"{cuerpo}{SEPARADOR}{codigo}"`) y
/// eso obligaba a re-parsear para distinguir un 500 con cuerpo vacío de un 500
/// con cuerpo. Aquí el cuerpo nunca se contamina.
#[derive(Debug, Clone, PartialEq)]
pub struct RespuestaCruda {
    pub codigo_http: u16,
    pub cuerpo: String,
    /// Valor de `Retry-After` si el bridge lo mandó (segundos).
    pub retry_after: Option<f64>,
}

impl RespuestaCruda {
    /// Decodifica bytes ISO-8859-1 (Windows-1252, que es lo que el bridge
    /// escribe de verdad) a UTF-8. Es el único punto de entrada de bytes.
    pub fn desde_bytes(codigo_http: u16, bytes: &[u8], retry_after: Option<f64>) -> Self {
        let (texto, _, _) = WINDOWS_1252.decode(bytes);
        Self {
            codigo_http,
            cuerpo: texto.into_owned(),
            retry_after,
        }
    }
}

/// De dónde salen las respuestas.
///
/// Existe para poder probar la máquina de reintentos sin red: `Simulado` lee de
/// un guion de respuestas preparadas. Se eligió mutex en vez de `?Send`-boxed
/// futures para que `ClienteErp` siga siendo `Send + Sync` sin ceremonia.
enum Transporte {
    Http(reqwest::Client),
    // Doble de pruebas: lee de un guion preparado. El binario solo construye
    // `Http`, así que sin esta anotación el compilador avisa de una rama que no
    // usa — y la rama tiene que seguir ahí, porque es la que permite probar la
    // máquina de reintentos sin depender de que el bridge esté levantado.
    #[allow(dead_code)]
    Simulado(Mutex<VecDeque<RespuestaCruda>>),
}

fn bloquear<T>(cerrojo: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    // Un pánico dentro de un test no debe envenenar el resto de la suite: el
    // dato es un guion de respuestas, no estado compartido delicado.
    cerrojo.lock().unwrap_or_else(|envenenado| envenenado.into_inner())
}

// ---------------------------------------------------------------------------
// Cliente
// ---------------------------------------------------------------------------

/// Cliente del bridge del ERP.
///
/// No es `Clone` a propósito: tiene un token y una cuenta de reintentos, y dos
/// copias divergirían. Para hablar con dos rutas a la vez hace falta un cliente
/// por ruta, y no se hace (el ERP va en serie).
pub struct ClienteErp {
    base_url: String,
    usuario: String,
    clave: String,
    /// Token de sesión. Va en un mutex para que [`ClienteErp::estado`] pueda ser
    /// `&self` sin `unsafe`: el cerrojo se suelta siempre antes de un `await`.
    token: Mutex<Option<String>>,
    /// Contadores de reintento. También en un cerrojo, por el mismo motivo: un
    /// reintento los modifica pero `estado(&self)` y `reintentos(&self)` tienen
    /// que seguir siendo de solo lectura (comprobar la salud del ERP no debe
    /// exigir `&mut`).
    reintentos: Mutex<Reintentos>,
    /// Peticiones al bridge (incluidos los reintentos). Es el número que se
    /// compara contra el rate limit.
    peticiones: AtomicU64,
    max_intentos: u32,
    transporte: Transporte,
    /// Filas leídas del ERP en esta descarga.
    filas_leidas: u32,
    /// Filas saltadas, aún sin agrupar (se agrupan al construir los avisos).
    avisos_fila: Vec<Aviso>,
}

impl fmt::Debug for ClienteErp {
    /// `Debug` a mano: con `derive`, un `tracing::debug!(?cliente)` volcaría la
    /// clave y el token a los logs. Aquí solo sale si los hay.
    fn fmt(&self, destino: &mut fmt::Formatter<'_>) -> fmt::Result {
        let token = self.token.lock().map(|guardado| guardado.clone()).unwrap_or(None);
        destino
            .debug_struct("ClienteErp")
            .field("base_url", &self.base_url)
            .field("usuario", &self.usuario)
            .field("clave", &"<oculta>")
            .field("token", &token.as_deref().map(|_| "<presente>").unwrap_or("<ninguno>"))
            .field("max_intentos", &self.max_intentos)
            .field("reintentos", &*bloquear(&self.reintentos))
            .field("peticiones", &self.peticiones.load(Ordering::Relaxed))
            .finish_non_exhaustive()
    }
}

impl ClienteErp {
    /// Cliente normal contra un bridge concreto.
    pub fn nuevo(base_url: impl Into<String>, usuario: impl Into<String>, clave: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            usuario: usuario.into(),
            clave: clave.into(),
            token: Mutex::new(None),
            reintentos: Mutex::new(Reintentos::default()),
            peticiones: AtomicU64::new(0),
            max_intentos: MAX_INTENTOS_POR_DEFECTO,
            transporte: Transporte::Http(cliente_http()),
            filas_leidas: 0,
            avisos_fila: Vec::new(),
        }
    }

    /// Cliente configurado por entorno: `ERP_BASE_URL`, `ERP_USUARIO` y
    /// `ERP_CLAVE`, con los valores del entorno de demo como último recurso.
    ///
    /// Nunca falla: devolver un cliente mal configurado que falla al conectar da
    /// un error mucho mejor que un `panic` al arrancar. Para saber si se está
    /// usando la credencial de demo, ver [`ClienteErp::usa_credenciales_por_defecto`].
    pub fn desde_entorno() -> Self {
        Self::nuevo(
            variable_de_entorno(VARIABLE_BASE_URL, BASE_URL_POR_DEFECTO),
            variable_de_entorno(VARIABLE_USUARIO, USUARIO_POR_DEFECTO),
            variable_de_entorno(VARIABLE_CLAVE, CLAVE_POR_DEFECTO),
        )
    }

    /// Cliente con una base y credenciales explícitas, para pruebas o
    /// despliegues que no usan variables de entorno.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn usuario(&self) -> &str {
        &self.usuario
    }

    /// ¿Se está usando la credencial de demo en vez de una inyectada?
    pub fn usa_credenciales_por_defecto(&self) -> bool {
        self.usuario == USUARIO_POR_DEFECTO || self.clave == CLAVE_POR_DEFECTO
    }

    /// Aviso listo para `obs`, si procede.
    pub fn aviso_de_credenciales(&self) -> Option<String> {
        // El predicado por delante: un despliegue con credencial inyectada no
        // paga ni el formateo del mensaje, y quien lea esto ve el porqué antes
        // que el cómo.
        if !self.usa_credenciales_por_defecto() {
            return None;
        }
        aviso_de_credenciales(&self.base_url, &self.usuario, &self.clave)
    }

    /// Copia de los contadores: van al snapshot, así que se copian y no se
    /// exponen por referencia (nadie de fuera debe tocarlos).
    pub fn reintentos(&self) -> Reintentos {
        *bloquear(&self.reintentos)
    }

    pub fn peticiones(&self) -> u64 {
        self.peticiones.load(Ordering::Relaxed)
    }

    /// Filas leídas del ERP en la última descarga, antes de filtrar.
    pub fn filas_leidas(&self) -> u32 {
        self.filas_leidas
    }

    /// Filas saltadas en la última descarga.
    //
    // El binario lee `descarga.avisos` (que es esta misma lista ya copiada al
    // resultado), así que aquí solo entran las pruebas. Se conserva porque es
    // parte de la superficie del cliente que fija la guía de traspaso (§4.1).
    #[allow(dead_code)]
    pub fn avisos(&self) -> &[Aviso] {
        &self.avisos_fila
    }

    /// ¿Hay token vivo? (`&self`: no se llama a propósito desde fuera para
    /// forzar el login, pero es útil en logs y en tests.)
    pub fn tiene_token(&self) -> bool {
        bloquear(&self.token).is_some()
    }

    /// Cuántos reintentos como máximo por ruta.
    pub fn max_intentos(&self) -> u32 {
        self.max_intentos
    }

    // -- transporte --------------------------------------------------------

    /// Cuenta un fallo recuperable en el contador que le toca.
    ///
    /// Deja el contador intacto si el error no es recuperable: los contadores
    /// del snapshot describen *reintentos*, y un `400` no es un reintento.
    fn contar(&self, codigo: Option<&str>, codigo_http: u16) {
        let mut contadores = bloquear(&self.reintentos);
        if es_ora(codigo, codigo_http) {
            contadores.ora_00600 = contadores.ora_00600.saturating_add(1);
        } else if es_sesion_invalida(codigo, codigo_http) {
            contadores.ses_401 = contadores.ses_401.saturating_add(1);
        } else if es_rate_limit(codigo, codigo_http) {
            contadores.erp_429 = contadores.erp_429.saturating_add(1);
        }
    }

    /// Una petición, sin reintentos: eso es cosa de [`ClienteErp::consultar`].
    async fn peticion(&self, ruta: &str, datos: Option<String>) -> Result<RespuestaCruda, ErpError> {
        self.peticiones.fetch_add(1, Ordering::Relaxed);

        let cliente = match &self.transporte {
            Transporte::Simulado(guion) => {
                let mut guion = bloquear(guion);
                return guion.pop_front().ok_or_else(|| {
                    ErpError::Transporte(format!("{ruta}: el guion de respuestas simuladas se agoto"))
                });
            }
            Transporte::Http(cliente) => cliente.clone(),
        };

        let url = format!("{}{}", self.base_url, ruta);
        let constructor = match datos {
            Some(cuerpo) => cliente
                .post(&url)
                .header(CONTENT_TYPE, FORMULARIO)
                .body(cuerpo),
            None => {
                // El token se copia y el cerrojo se suelta antes del `await`.
                let token = bloquear(&self.token).clone();
                let mut constructor = cliente.get(&url);
                if let Some(token) = token {
                    constructor = constructor.header(CABECERA_TOKEN, token);
                }
                constructor
            }
        };

        let respuesta = constructor
            .header(ACCEPT, "text/xml")
            .timeout(Duration::from_secs(TIEMPO_LIMITE_SEGUNDOS))
            .send()
            .await
            .map_err(|error| ErpError::Transporte(descripcion_de_transporte(&self.base_url, &error)))?;

        let codigo_http = respuesta.status().as_u16();
        let retry_after = respuesta
            .headers()
            .get(RETRY_AFTER)
            .and_then(|valor| valor.to_str().ok())
            .and_then(|valor| valor.trim().parse::<f64>().ok());
        let bytes = respuesta
            .bytes()
            .await
            .map_err(|error| ErpError::Transporte(descripcion_de_transporte(&self.base_url, &error)))?
            .to_vec();

        Ok(RespuestaCruda::desde_bytes(codigo_http, &bytes, retry_after))
    }

    // -- login -------------------------------------------------------------

    /// `POST /erp/login`. Deja el token listo para las rutas autenticadas.
    ///
    /// Solo reintenta `ERP-429` (un rate limit que bloquease el login se
    /// convertiría en un bucle si se tratase como error duro).
    pub async fn login(&mut self) -> Result<(), ErpError> {
        let cuerpo = formulario(&[(DATO_USUARIO, self.usuario.as_str()), (DATO_CLAVE, self.clave.as_str())]);
        let mut ultimo = String::from("desconocido");

        for intento in 1..=self.max_intentos {
            let respuesta = self.peticion("/erp/login", Some(cuerpo.clone())).await?;
            if respuesta.codigo_http == 200 {
                let raiz = analizar(&respuesta.cuerpo)?;
                let token = raiz.texto_o_vacio("token").trim().to_string();
                if token.is_empty() {
                    return Err(ErpError::Login(String::from(
                        "el login devolvio 200 pero sin <token>",
                    )));
                }
                *bloquear(&self.token) = Some(token);
                return Ok(());
            }

            let (codigo, mensaje) = codigo_de_error(&respuesta.cuerpo);
            ultimo = resumen_de_error(respuesta.codigo_http, codigo.as_deref(), mensaje.as_deref());

            if es_rate_limit(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                tracing::warn!(intento, "ERP-429 en el login: bajo el ritmo");
                dormir(respuesta.retry_after.unwrap_or(ESPERA_429_SEGUNDOS)).await;
                continue;
            }
            if es_sesion_invalida(codigo.as_deref(), respuesta.codigo_http) {
                // La clave se omite a proposito: un mensaje de error no puede
                // acabar con credenciales en un log.
                return Err(ErpError::Login(format!(
                    "usuario o clave rechazados (SES-401, HTTP {}); el usuario se lee de {VARIABLE_USUARIO} y la clave de {VARIABLE_CLAVE}",
                    respuesta.codigo_http
                )));
            }
            return Err(ErpError::Login(format!(
                "HTTP {} al pedir el token ({ultimo})",
                respuesta.codigo_http
            )));
        }

        Err(ErpError::Login(format!(
            "sin exito tras {} intentos ({ultimo})",
            self.max_intentos
        )))
    }

    // -- consulta autenticada ---------------------------------------------

    /// El único camino autenticado con reintentos.
    ///
    /// `ORA-00600` → la sesión sigue viva: se repite la MISMA ruta.
    /// `SES-401` → el token ya no vale: se rehace login y se reintenta.
    /// `ERP-429` → rate limit: la petición no se perdió, solo hay que esperar.
    /// `ERP-404` → significa algo (no existe): se devuelve, no se reintenta.
    async fn consultar_con_codigo(&mut self, ruta: &str) -> Result<(u16, String), ErpError> {
        let mut ultimo = String::from("desconocido");

        for intento in 1..=self.max_intentos {
            let respuesta = self.peticion(ruta, None).await?;
            if respuesta.codigo_http == 200 {
                return Ok((200, respuesta.cuerpo));
            }

            let (codigo, mensaje) = codigo_de_error(&respuesta.cuerpo);
            if es_no_encontrado(codigo.as_deref(), respuesta.codigo_http) {
                return Ok((respuesta.codigo_http, respuesta.cuerpo));
            }

            ultimo = resumen_de_error(respuesta.codigo_http, codigo.as_deref(), mensaje.as_deref());

            // El orden replica `_reaccionar` de `descargar_erp.py`: primero lo
            // que se puede reintentar sin tocar la sesion.
            if es_ora(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                let espera = (PASO_ESPERA_ORA_SEGUNDOS * f64::from(intento)).min(TOPE_ESPERA_ORA_SEGUNDOS);
                tracing::warn!(ruta, intento, codigo = ?codigo, "ORA-00600: repito la misma peticion en {espera:.2}s");
                dormir(espera).await;
                continue;
            }

            if es_sesion_invalida(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                tracing::warn!(ruta, intento, "SES-401: rehago login y reintento");
                *bloquear(&self.token) = None;
                self.login().await?;
                dormir(ESPERA_SES_401_SEGUNDOS).await;
                continue;
            }

            if es_rate_limit(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                let espera = respuesta.retry_after.unwrap_or(ESPERA_429_SEGUNDOS);
                tracing::warn!(ruta, intento, "ERP-429: bajo el ritmo {espera:.2}s");
                dormir(espera).await;
                continue;
            }

            return Err(ErpError::Protocolo(format!("{ruta}: {ultimo}")));
        }

        Err(ErpError::Agotado(format!(
            "{ruta}: sin exito tras {} intentos ({ultimo})",
            self.max_intentos
        )))
    }

    async fn consultar(&mut self, ruta: &str) -> Result<String, ErpError> {
        let (codigo_http, cuerpo) = self.consultar_con_codigo(ruta).await?;
        if codigo_http == 200 {
            return Ok(cuerpo);
        }
        let (codigo, mensaje) = codigo_de_error(&cuerpo);
        Err(ErpError::Protocolo(format!(
            "{ruta}: {}",
            resumen_de_error(codigo_http, codigo.as_deref(), mensaje.as_deref())
        )))
    }

    // -- rutas -------------------------------------------------------------

    /// `GET /erp/estado`. No pide token, así que es el sitio natural para
    /// comprobar si el bridge está vivo antes de empezar.
    ///
    /// Es `&self` a propósito: comprobar salud no debe poder cambiar la sesión
    /// ni exigir `&mut` a quien solo quiere diagnosticar.
    pub async fn estado(&self) -> Result<EstadoErp, ErpError> {
        let ruta = "/erp/estado";
        let mut ultimo = String::from("desconocido");

        for intento in 1..=self.max_intentos {
            let respuesta = self.peticion(ruta, None).await?;
            if respuesta.codigo_http == 200 {
                let raiz = analizar(&respuesta.cuerpo)?;
                let actualizacion = raiz.texto_o_vacio("actualizacion_cargada");
                return Ok(EstadoErp {
                    version: raiz.texto_o_vacio("version"),
                    activo_segundos: entero_de(&raiz, "activo_segundos"),
                    asientos: entero_de(&raiz, "asientos"),
                    // Verdadero salvo que el ERP diga explícitamente "NO": el
                    // bridge escribe "SI"/"NO" y no queremos que una tilde o un
                    // cambio de literal se lea como "no cargada".
                    actualizacion_cargada: !actualizacion.eq_ignore_ascii_case("NO"),
                    animo: raiz.texto_o_vacio("animo"),
                });
            }

            let (codigo, mensaje) = codigo_de_error(&respuesta.cuerpo);
            ultimo = resumen_de_error(respuesta.codigo_http, codigo.as_deref(), mensaje.as_deref());
            if es_ora(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                dormir((PASO_ESPERA_ORA_SEGUNDOS * f64::from(intento)).min(TOPE_ESPERA_ORA_SEGUNDOS)).await;
                continue;
            }
            if es_rate_limit(codigo.as_deref(), respuesta.codigo_http) {
                self.contar(codigo.as_deref(), respuesta.codigo_http);
                dormir(respuesta.retry_after.unwrap_or(ESPERA_429_SEGUNDOS)).await;
                continue;
            }
            return Err(ErpError::Protocolo(format!("{ruta}: {ultimo}")));
        }

        Err(ErpError::Agotado(format!(
            "{ruta}: sin exito tras {} intentos ({ultimo})",
            self.max_intentos
        )))
    }

    /// `GET /erp/asientos?pagina=N`. Exige `<meta>`: sin él no se sabe cuántas
    /// páginas hay y el recuento no se puede verificar, así que es un fallo de
    /// protocolo, no una página vacía.
    pub async fn pagina(&mut self, numero: u32) -> Result<PaginaAsientos, ErpError> {
        let ruta = format!("/erp/asientos?pagina={numero}");

        if !self.tiene_token() {
            self.login().await?;
        }

        let cuerpo = self.consultar(&ruta).await?;
        let raiz = analizar(&cuerpo)?;

        // Las filas leidas cuentan las utiles MAS las saltadas: es el numero que
        // se compara con `<meta><total>`, y saltarse filas no puede parecer que
        // el ERP devolvio menos.
        let avisos_antes = self.avisos_fila.len();
        let pagina = pagina_desde_nodo(&raiz, &ruta, &mut self.avisos_fila)?;
        let saltadas = self.avisos_fila.len() - avisos_antes;
        self.filas_leidas += pagina.asientos.len() as u32 + saltadas as u32;
        Ok(pagina)
    }

    /// `GET /erp/asientos/<id>`. `Ok(None)` si el ERP dice `ERP-404`: un asiento
    /// que no existe es una respuesta legítima, no un error.
    //
    // La descarga por lotes no pasa por aquí, así que en el binario queda como
    // superficie: la usan las pruebas contra el ERP vivo (que comprueban que el
    // detalle de un asiento del snapshot coincide con la fila bajada) y sirve
    // para mirar un asiento suelto sin bajarse las 26 páginas. Se conserva
    // porque está en la tabla del contrato (§3.5).
    #[allow(dead_code)]
    pub async fn asiento(&mut self, id: &str) -> Result<Option<Asiento>, ErpError> {
        let ruta = format!("/erp/asientos/{id}");

        // Igual que `pagina`: la ruta exige `X-ERP-Token`, asi que sin token se
        // pide primero.
        if !self.tiene_token() {
            self.login().await?;
        }

        let (codigo_http, cuerpo) = self.consultar_con_codigo(&ruta).await?;

        if codigo_http != 200 {
            let (codigo, _) = codigo_de_error(&cuerpo);
            if es_no_encontrado(codigo.as_deref(), codigo_http) {
                return Ok(None);
            }
            return Err(ErpError::Protocolo(format!(
                "{ruta}: HTTP {codigo_http} sin codigo de error reconocible"
            )));
        }

        let raiz = analizar(&cuerpo)?;
        let mut descartes = Vec::new();
        match primer_asiento(&raiz) {
            Some(nodo) => asiento_desde_nodo(nodo, &mut descartes),
            None => Err(ErpError::Protocolo(format!(
                "{ruta}: 200 pero sin ningun <asiento> en la respuesta"
            ))),
        }
    }

    /// Recorre **todas** las páginas y devuelve los asientos conciliables.
    ///
    /// Nunca en paralelo (ver la cabecera del módulo) y sin asumir el tamaño de
    /// página: primero se pregunta cuántas páginas hay y luego se piden una a
    /// una hasta la última.
    pub async fn descargar(&mut self, max_intentos: u32) -> Result<Descarga, ErpError> {
        self.max_intentos = max_intentos.max(1);
        self.filas_leidas = 0;
        self.avisos_fila.clear();
        let inicio = Instant::now();

        let primera = self.pagina(1).await?;
        let paginas = primera.meta.paginas;
        let total = primera.meta.total;
        let mut asientos = primera.asientos;

        for numero in 2..=paginas {
            let pagina = self.pagina(numero).await?;
            asientos.extend(pagina.asientos);
        }

        let leidas = self.filas_leidas;
        let saltadas = leidas.saturating_sub(asientos.len() as u32);
        // Dos filas con el mismo `asiento_id` darian dos documentos con el mismo
        // `_id` (`<snapshot_id>#<asiento_id>`) y Mongo rechazaria la importacion
        // entera. Mejor parar aqui que entregar un snapshot que no se puede
        // cargar.
        revisar_identidades(&asientos)?;
        let (asientos, duplicados) = colapsar_duplicados(asientos);

        let mut problemas = Vec::new();
        let estado = if leidas == total {
            ESTADO_COMPLETO
        } else {
            ESTADO_PARCIAL
        };
        if leidas != total {
            problemas.push(format!(
                "descargados: {leidas} != esperado {total} (el ERP dice {total})"
            ));
        }
        if saltadas > 0 {
            problemas.push(format!("filas saltadas: {saltadas} de {leidas} (ver `avisos`)"));
        }
        if !duplicados.is_empty() {
            problemas.push(format!("facturas duplicadas colapsadas: {}", duplicados.len()));
        }
        let mut avisos = problemas;
        avisos.extend(lineas_de_presentacion(&self.avisos_fila, leidas));

        let (snapshot_id, descargado_en) = marca_de_tiempo();
        let meta = MetaSnapshot {
            snapshot_id,
            descargado_en,
            asientos_descargados: leidas,
            paginas,
            vigente: true,
            estado: String::from(estado),
            reintentos: self.reintentos(),
            duracion_ms: inicio.elapsed().as_millis() as u64,
            avisos,
            duplicados: duplicados.clone(),
        };

        Ok(Descarga {
            asientos,
            meta,
            avisos: self.avisos_fila.clone(),
            duplicados,
        })
    }
}

const DATO_USUARIO: &str = "usuario";
const DATO_CLAVE: &str = "clave";

fn cliente_http() -> reqwest::Client {
    reqwest::Client::builder()
        .build()
        .unwrap_or_else(|_| reqwest::Client::new())
}

fn variable_de_entorno(nombre: &str, defecto: &str) -> String {
    match std::env::var(nombre) {
        Ok(valor) if !valor.trim().is_empty() => valor,
        _ => String::from(defecto),
    }
}

/// Espera entre reintentos.
///
/// Asíncrona a propósito: `std::thread::sleep` dentro de un `async fn` bloquea
/// el hilo del ejecutor, y aquí el que espera es una operación de red.
async fn dormir(segundos: f64) {
    if segundos > 0.0 {
        tokio::time::sleep(Duration::from_secs_f64(segundos)).await;
    }
}

fn descripcion_de_transporte(base_url: &str, error: &reqwest::Error) -> String {
    let detalle = if error.is_timeout() {
        String::from("se agoto el tiempo de espera")
    } else if error.is_connect() {
        format!("no puedo conectar con {base_url}")
    } else {
        error.to_string()
    };
    format!(
        "{detalle}; arranca el bridge del ERP y, si no esta en {BASE_URL_POR_DEFECTO}, apunta la URL con {VARIABLE_BASE_URL}"
    )
}

// ---------------------------------------------------------------------------
// Clasificación de errores del ERP
// ---------------------------------------------------------------------------

fn es_rate_limit(codigo: Option<&str>, codigo_http: u16) -> bool {
    codigo == Some(CODIGO_ERP_429) || codigo_http == 429
}

fn es_sesion_invalida(codigo: Option<&str>, codigo_http: u16) -> bool {
    codigo == Some(CODIGO_SES_401) || codigo_http == 401
}

fn es_no_encontrado(codigo: Option<&str>, codigo_http: u16) -> bool {
    codigo == Some(CODIGO_ERP_404) || codigo_http == 404
}

/// ¿Es el error interno del bridge? El `ORA-00600` llega como 500 y a veces sin
/// `<codigo>`, así que el 500 a secas también cuenta: es el único 500 que
/// produce el bridge.
fn es_ora(codigo: Option<&str>, codigo_http: u16) -> bool {
    codigo == Some(CODIGO_ORA_00600) || codigo_http == 500
}

/// Lee `<codigo>`/`<mensaje>` de una respuesta de error.
///
/// Soporta las dos formas vistas del contrato: hijos (`<codigo>ORA-00600</codigo>`,
/// que es la que escribe el bridge de hoy) y atributos (`<error codigo="…">`).
/// Devuelve `(None, None)` si el cuerpo no es XML reconocible: un `500` en texto
/// plano no debe impedir clasificar por el código HTTP.
pub fn codigo_de_error(cuerpo: &str) -> (Option<String>, Option<String>) {
    let raiz = match analizar(cuerpo) {
        Ok(raiz) => raiz,
        Err(_) => return (None, None),
    };
    let codigo = raiz.texto_o_vacio("codigo").trim().to_string();
    // El codigo se devuelve **tal cual** (`ORA-00600`, `SES-401`, `ERP-429`,
    // `ERP-404`): es la etiqueta con la que el snapshot cuenta los reintentos y
    // recortarla seria inventarse otro codigo.
    let codigo = if codigo.is_empty() {
        raiz.atributo("codigo")
            .map(|valor| valor.trim().to_string())
    } else {
        Some(codigo)
    };
    let mensaje = raiz.texto_o_vacio("mensaje").trim().to_string();
    let mensaje = if mensaje.is_empty() { None } else { Some(mensaje) };
    (codigo, mensaje)
}

fn resumen_de_error(codigo_http: u16, codigo: Option<&str>, mensaje: Option<&str>) -> String {
    let etiqueta = codigo.unwrap_or("sin codigo");
    match mensaje {
        Some(mensaje) => format!("{etiqueta} (HTTP {codigo_http}): {mensaje}"),
        None => format!("{etiqueta} (HTTP {codigo_http})"),
    }
}

// ---------------------------------------------------------------------------
// Conversión de datos: las mismas reglas que `descargar_erp.py`
// ---------------------------------------------------------------------------

/// `"12.874,40"` → `12874.40`. Igual que `importe_a_decimal` de
/// `descargar_erp.py` (líneas 229-249), regla por regla:
///
/// 1. Se quitan los espacios duros (el ERP usa `\u{a0}` como separador de
///    miles en algunos albaranes).
/// 2. Si hay coma, el formato es español: los puntos son miles.
/// 3. Si no hay coma pero el final es `.dd` o `.d`, el número ya viene en
///    formato ISO; si no, los puntos son miles (`"1.234"` es mil doscientos
///    treinta y cuatro, **no** 1,234).
///
/// Devuelve `None` en vez de `Decimal::ZERO`: un importe ilegible y un importe
/// de cero son cosas distintas, y tratarlos igual haría que una fila rota
/// pareciese un asiento de 0,00 €.
pub fn importe_a_decimal(crudo: &str) -> Option<Decimal> {
    let limpio = crudo.trim().replace('\u{a0}', "");
    if limpio.is_empty() {
        return None;
    }

    let sin_puntos_miles = if limpio.contains(',') {
        limpio.replace('.', "").replace(',', ".")
    } else if termina_en_decimales(&limpio) {
        limpio.clone()
    } else {
        limpio.replace('.', "")
    };

    Decimal::from_str(&sin_puntos_miles).ok()
}

/// ¿El final es un separador decimal con una o dos cifras? (`"473.70"`, `"9.5"`)
fn termina_en_decimales(texto: &str) -> bool {
    let bytes = texto.as_bytes();
    let Some(posicion) = bytes.iter().rposition(|byte| bytes_decimal(*byte)) else {
        return false;
    };
    let decimales = bytes.len() - posicion - 1;
    (1..=2).contains(&decimales) && texto[..posicion].bytes().all(|byte| byte.is_ascii_digit())
}

fn bytes_decimal(byte: u8) -> bool {
    byte == b'.'
}

/// `"21/03/2026"` → `Some("2026-03-21")`. Acepta ya en ISO y devuelve `None`
/// si no reconoce el formato (nunca inventa una fecha).
pub fn fecha_es_a_iso(cruda: &str) -> Option<String> {
    let limpio = cruda.trim();
    let partes: Vec<&str> = limpio.split('/').collect();
    if partes.len() == 3 {
        let (dia, mes, anio) = (partes[0].trim(), partes[1].trim(), partes[2].trim());
        if [dia, mes, anio]
            .iter()
            .all(|parte| !parte.is_empty() && parte.bytes().all(|byte| byte.is_ascii_digit()))
        {
            let anio: i32 = anio.parse().ok()?;
            let mes: u32 = mes.parse().ok()?;
            let dia: u32 = dia.parse().ok()?;
            // Se compone a mano y se valida contra el calendario real: un
            // `31/04/2026` no es una fecha valida y no puede colarse al
            // snapshot como si lo fuera.
            return fecha_valida(anio, mes, dia);
        }
    }

    let partes: Vec<&str> = limpio.split('-').collect();
    if partes.len() == 3 {
        let (anio, mes, dia) = (partes[0], partes[1], partes[2]);
        let anio: i32 = anio.parse().ok()?;
        let mes: u32 = mes.parse().ok()?;
        let dia: u32 = dia.parse().ok()?;
        if fecha_valida(anio, mes, dia).is_some() {
            return Some(format!("{anio:04}-{mes:02}-{dia:02}"));
        }
    }

    None
}

/// Año/mes/día → fecha ISO, o `None` si esa fecha no existe en el calendario
/// gregoriano. Se valida en vez de formatear a ciegas para que un `31/04/2026`
/// del ERP no acabe en el snapshot como si fuera una fecha real.
fn fecha_valida(anio: i32, mes: u32, dia: u32) -> Option<String> {
    if !(1970..=9999).contains(&anio) || !(1..=12).contains(&mes) || dia == 0 {
        return None;
    }
    let bisiesto = (anio % 4 == 0 && anio % 100 != 0) || anio % 400 == 0;
    let dias = match mes {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if bisiesto => 29,
        2 => 28,
        _ => return None,
    };
    if dia > dias {
        return None;
    }
    Some(format!("{anio:04}-{mes:02}-{dia:02}"))
}

/// Importe con dos decimales y ceros finales, que es la forma con la que se
/// construye la clave de factura y el JSON del snapshot (`9872.00`).
pub fn formato_importe(importe: Decimal) -> String {
    importe.round_dp(2).to_string()
}

/// Clave de factura: `proveedor|pedido|fecha|importe`.
///
/// Es la identidad funcional de la factura a efectos de deduplicación y de
/// conciliación (`descargar_erp.py:269`). El `importe` va con dos decimales
/// para que `9872.0` y `9872.00` den la misma clave.
pub fn clave_factura(asiento: &Asiento) -> String {
    clave_desde_partes(
        asiento.proveedor.as_deref().unwrap_or(""),
        &asiento.pedido,
        asiento.fecha.as_deref().unwrap_or(""),
        asiento.importe,
    )
}

fn clave_desde_partes(proveedor: &str, pedido: &str, fecha: &str, importe: Decimal) -> String {
    format!(
        "{proveedor}|{pedido}|{fecha}|{}",
        formato_importe(importe)
    )
}

/// Texto del ERP → `EstadoAsiento`. Un estado desconocido devuelve `None` para
/// que quien llama decida; **nunca** se asume `Pendiente`: un estado nuevo
/// (`ANULADA`, `RETENIDA`) tratado como pendiente acabaría en un pago.
fn estado_desde(texto: &str) -> Option<EstadoAsiento> {
    match texto.trim().to_ascii_uppercase().as_str() {
        "PENDIENTE" => Some(EstadoAsiento::Pendiente),
        "PAGADA" => Some(EstadoAsiento::Pagada),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Mini-lector de XML
// ---------------------------------------------------------------------------

const PROFUNDIDAD_MAXIMA: usize = 64;

fn error_de_xml(reader: &Reader<&[u8]>, error: &quick_xml::Error) -> ErpError {
    ErpError::Protocolo(format!("XML ilegible en la posicion {}: {error}", reader.buffer_position()))
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct Nodo {
    nombre: String,
    texto: String,
    atributos: Vec<(String, String)>,
    hijos: Vec<Nodo>,
}

impl Nodo {
    fn hijo(&self, nombre: &str) -> Option<&Nodo> {
        self.hijos.iter().find(|hijo| hijo.nombre == nombre)
    }

    fn hijos_con(&self, nombre: &str) -> Vec<&Nodo> {
        self.hijos.iter().filter(|hijo| hijo.nombre == nombre).collect()
    }

    /// Texto del nodo, o `""` si no está. No es un `Option` porque el contrato
    /// del bridge es "el campo está, puede estar vacío", y el vacío es
    /// justamente lo que hay que detectar (NIF, importe).
    fn texto_o_vacio(&self, nombre: &str) -> String {
        self.hijo(nombre).map(|hijo| hijo.texto.clone()).unwrap_or_default()
    }

    fn atributo(&self, nombre: &str) -> Option<&str> {
        self.atributos
            .iter()
            .find(|(clave, _)| clave == nombre)
            .map(|(_, valor)| valor.as_str())
    }
}

/// Parsea el XML del bridge a un árbol de nodos con su texto **ya decodificado**
/// de ISO-8859-1.
///
/// Se parsea a mano (`quick-xml` + un árbol mínimo) en vez de con `serde`: los
/// errores del ERP llegan con otra forma (`<error>` en vez de `<respuesta>`) y
/// el XML del bridge de 2009 no es tan regular como para fiarse de un
/// `#[derive(Deserialize)]` silencioso.
fn analizar(cuerpo: &str) -> Result<Nodo, ErpError> {
    let mut lector = Reader::from_str(cuerpo);
    lector.config_mut().trim_text(true);

    let mut raiz: Option<Nodo> = None;
    let mut pila: Vec<Nodo> = Vec::new();
    let mut profundidad_invalida = None;

    loop {
        match lector.read_event() {
            Ok(Event::Start(inicio)) => {
                if pila.len() >= PROFUNDIDAD_MAXIMA {
                    profundidad_invalida = Some(pila.len());
                    break;
                }
                pila.push(Nodo {
                    nombre: String::from_utf8_lossy(inicio.name().as_ref()).into_owned(),
                    atributos: atributos_de(&inicio),
                    ..Nodo::default()
                });
            }
            Ok(Event::Text(texto)) => {
                if let Some(actual) = pila.last_mut() {
                    actual.texto.push_str(&texto.unescape().unwrap_or_default());
                }
            }
            Ok(Event::CData(texto)) => {
                if let Some(actual) = pila.last_mut() {
                    actual.texto.push_str(&String::from_utf8_lossy(texto.as_ref()));
                }
            }
            Ok(Event::Empty(vacio)) => {
                let nodo = Nodo {
                    nombre: String::from_utf8_lossy(vacio.name().as_ref()).into_owned(),
                    atributos: atributos_de(&vacio),
                    ..Nodo::default()
                };
                match pila.last_mut() {
                    Some(padre) => padre.hijos.push(nodo),
                    None => raiz = Some(nodo),
                }
            }
            Ok(Event::End(_)) => {
                let Some(cerrado) = pila.pop() else { continue };
                match pila.last_mut() {
                    Some(padre) => padre.hijos.push(cerrado),
                    None => raiz = Some(cerrado),
                }
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(error) => return Err(error_de_xml(&lector, &error)),
        }
    }

    if let Some(profundidad) = profundidad_invalida {
        return Err(ErpError::Protocolo(format!(
            "XML con mas de {PROFUNDIDAD_MAXIMA} niveles anidados (se corto en {profundidad})"
        )));
    }

    match raiz {
        Some(raiz) => Ok(raiz),
        None => Err(ErpError::Protocolo(String::from("respuesta vacia o sin ningun elemento XML"))),
    }
}

fn atributos_de(inicio: &quick_xml::events::BytesStart<'_>) -> Vec<(String, String)> {
    inicio
        .attributes()
        .flatten()
        .map(|atributo| {
            (
                String::from_utf8_lossy(atributo.key.as_ref()).into_owned(),
                String::from_utf8_lossy(&atributo.value).into_owned(),
            )
        })
        .collect()
}

/// Primer `<asiento>` de la respuesta.
///
/// Tanto `GET /erp/asientos/<id>` como `GET /erp/asientos?pagina=N` lo envuelven
/// en `<respuesta><asientos><asiento>`, así que basta con "el primero que
/// aparezca", sin depender de la ruta exacta.
// Sólo lo alcanza `asiento()`, que en el binario es superficie de contrato:
// ésta es la anotación que cierra la cadena (`asiento` → `primer_asiento`).
#[allow(dead_code)]
fn primer_asiento(raiz: &Nodo) -> Option<&Nodo> {
    if raiz.nombre == "asiento" {
        return Some(raiz);
    }
    for hijo in &raiz.hijos {
        if let Some(encontrado) = primer_asiento(hijo) {
            return Some(encontrado);
        }
    }
    None
}

fn entero_de(nodo: &Nodo, nombre: &str) -> u64 {
    nodo.texto_o_vacio(nombre).trim().parse::<u64>().unwrap_or(0)
}

/// Construye la página a partir del árbol. Falla si falta `<meta>`: sin él no
/// se sabe cuántas páginas quedan y el recuento no se puede comprobar.
fn pagina_desde_nodo(raiz: &Nodo, ruta: &str, avisos: &mut Vec<Aviso>) -> Result<PaginaAsientos, ErpError> {
    let meta_nodo = raiz
        .hijo("meta")
        .ok_or_else(|| ErpError::Protocolo(format!("{ruta}: falta el bloque <meta>")))?;
    let por_pagina = entero_de(meta_nodo, "por_pagina");
    let meta = MetaPagina {
        total: entero_de(meta_nodo, "total") as u32,
        paginas: entero_de(meta_nodo, "paginas") as u32,
        pagina: entero_de(meta_nodo, "pagina") as u32,
        // Respaldo solo si el ERP no lo declara; si lo declara, se usa el suyo
        // aunque la página traiga menos filas (la ultima trae 16 de 20).
        por_pagina: if por_pagina == 0 {
            POR_PAGINA_POR_DEFECTO as u32
        } else {
            por_pagina as u32
        },
        generado: meta_nodo.texto_o_vacio("generado"),
    };

    let mut asientos = Vec::new();
    if let Some(contenedor) = raiz.hijo("asientos") {
        for nodo in contenedor.hijos_con("asiento") {
            if let Some(asiento) = asiento_desde_nodo(nodo, avisos)? {
                asientos.push(asiento);
            }
        }
    }

    Ok(PaginaAsientos { asientos, meta })
}

/// Un `<asiento>` → `domain::Asiento`, o `None` si la fila se salta.
///
/// Se salta (con aviso) la fila sin NIF porque `Asiento::nif` es obligatorio; se
/// falla si el `estado` es desconocido, porque un estado nuevo tratado como
/// pendiente acabaría en un pago.
fn asiento_desde_nodo(nodo: &Nodo, avisos: &mut Vec<Aviso>) -> Result<Option<Asiento>, ErpError> {
    let asiento_id = nodo.texto_o_vacio("id").trim().to_string();
    if asiento_id.is_empty() {
        avisos.push(Aviso {
            asiento_id: String::from("<sin id>"),
            motivo: String::from("id vacio"),
        });
        return Ok(None);
    }

    let crudo_nif = nodo.texto_o_vacio("nif");
    let Some(nif) = Nif::nuevo(&crudo_nif) else {
        avisos.push(Aviso {
            asiento_id,
            motivo: if crudo_nif.trim().is_empty() {
                String::from("nif vacio")
            } else {
                format!("nif ilegible ({crudo_nif:?})")
            },
        });
        return Ok(None);
    };

    let crudo_importe = nodo.texto_o_vacio("importe");
    let Some(importe) = importe_a_decimal(&crudo_importe) else {
        return Err(ErpError::Protocolo(format!(
            "{asiento_id}: importe ilegible ({crudo_importe:?})"
        )));
    };

    let crudo_estado = nodo.texto_o_vacio("estado");
    let Some(estado) = estado_desde(&crudo_estado) else {
        return Err(ErpError::Protocolo(format!(
            "{asiento_id}: estado desconocido ({crudo_estado:?}); no se asume PENDIENTE"
        )));
    };

    let proveedor = {
        let crudo = nodo.texto_o_vacio("proveedor").trim().to_string();
        if crudo.is_empty() {
            None
        } else {
            Some(crudo)
        }
    };

    Ok(Some(Asiento {
        asiento_id,
        nif,
        pedido: nodo.texto_o_vacio("pedido").trim().to_string(),
        importe,
        estado,
        proveedor,
        fecha: fecha_es_a_iso(&nodo.texto_o_vacio("fecha")),
    }))
}

// ---------------------------------------------------------------------------
// Deduplicación y avisos
// ---------------------------------------------------------------------------

/// Comprueba que ningún `asiento_id` se repite.
///
/// El `_id` de cada documento del snapshot se compone como
/// `<snapshot_id>#<asiento_id>`, así que un id repetido genera un `_id`
/// repetido y la importación a Mongo falla. Es preferible fallar aquí, con el
/// id en el mensaje, que entregar un snapshot inservible.
fn revisar_identidades(asientos: &[Asiento]) -> Result<(), ErpError> {
    let mut vistos: HashSet<&str> = HashSet::with_capacity(asientos.len());
    for asiento in asientos {
        if !vistos.insert(asiento.asiento_id.as_str()) {
            return Err(ErpError::Protocolo(format!(
                "el ERP ha devuelto el asiento {} mas de una vez: el snapshot tendria dos documentos con el mismo _id",
                asiento.asiento_id
            )));
        }
    }
    Ok(())
}

/// Colapsa facturas repetidas: mismo `clave_factura` (mismo proveedor, pedido,
/// fecha e importe) descargado dos veces.
///
/// Se conserva el de `asiento_id` más bajo —el criterio de `descargar_erp.py`—
/// y se anota qué se descartó. Se conserva el **orden original** de la lista y no
/// el de las claves: los asientos van al snapshot como los da el ERP.
fn colapsar_duplicados(asientos: Vec<Asiento>) -> (Vec<Asiento>, Vec<Duplicado>) {
    let mut grupos: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (indice, asiento) in asientos.iter().enumerate() {
        grupos.entry(clave_factura(asiento)).or_default().push(indice);
    }

    let mut conservado_por_clave: BTreeMap<String, String> = BTreeMap::new();
    let mut traza = Vec::new();
    for (clave, indices) in grupos {
        if indices.len() < 2 {
            continue;
        }
        let mut ordenados: Vec<&Asiento> = indices.iter().map(|indice| &asientos[*indice]).collect();
        ordenados.sort_by(|a, b| a.asiento_id.cmp(&b.asiento_id));
        conservado_por_clave.insert(clave.clone(), ordenados[0].asiento_id.clone());
        traza.push(Duplicado {
            clave_factura: clave,
            conservado: ordenados[0].asiento_id.clone(),
            descartados: ordenados[1..].iter().map(|a| a.asiento_id.clone()).collect(),
        });
    }

    if traza.is_empty() {
        return (asientos, Vec::new());
    }

    let conservados = asientos
        .into_iter()
        .filter(|asiento| match conservado_por_clave.get(&clave_factura(asiento)) {
            None => true,
            Some(conservado) => &asiento.asiento_id == conservado,
        })
        .collect();
    (conservados, traza)
}

/// Agrupa las filas saltadas por motivo y las presenta para `obs`: un aviso por
/// motivo, con recuento y muestra (`"nif vacio en 20/516: ['AS-00499', ...]"`).
fn lineas_de_presentacion(avisos: &[Aviso], denominador: u32) -> Vec<String> {
    let mut grupos: Vec<(String, Vec<String>)> = Vec::new();
    for aviso in avisos {
        match grupos.iter_mut().find(|(motivo, _)| motivo == &aviso.motivo) {
            Some((_, ids)) => ids.push(aviso.asiento_id.clone()),
            None => grupos.push((aviso.motivo.clone(), vec![aviso.asiento_id.clone()])),
        }
    }
    grupos
        .into_iter()
        .map(|(motivo, ids)| {
            let muestra = ids
                .iter()
                .take(3)
                .map(|id| format!("'{id}'"))
                .collect::<Vec<_>>()
                .join(", ");
            format!("{motivo} en {}/{}: [{muestra}]", ids.len(), denominador)
        })
        .collect()
}

fn marca_de_tiempo() -> (String, String) {
    let segundos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duracion| duracion.as_secs() as i64)
        .unwrap_or(0);
    let (anio, mes, dia, hora, minuto, segundo) = partes_utc(segundos);
    let descargado_en = format!("{anio:04}-{mes:02}-{dia:02}T{hora:02}:{minuto:02}:{segundo:02}Z");
    let snapshot_id = format!("snap-{}", descargado_en.replace(':', "-"));
    (snapshot_id, descargado_en)
}

/// Descompone un instante Unix en UTC. Se hace a mano porque el proyecto tiene
/// la feature `clock` de `chrono` apagada a propósito (no se quiere arrastrar
/// zona horaria al binario) y calcular seis números no justifica encenderla.
fn partes_utc(segundos: i64) -> (i32, u32, u32, u32, u32, u32) {
    let dias = segundos.div_euclid(86_400);
    let resto = segundos.rem_euclid(86_400);
    let (anio, mes, dia) = civil_desde_dias(dias);
    (
        anio,
        mes,
        dia,
        (resto / 3_600) as u32,
        ((resto % 3_600) / 60) as u32,
        (resto % 60) as u32,
    )
}

/// Días desde 1970-01-01 → (año, mes, día) del calendario gregoriano.
///
/// Algoritmo de Howard Hinnant (`civil_from_days`), en aritmética entera: sin
/// dependencias, sin tablas y correcto para las fechas que maneja el ERP.
fn civil_desde_dias(dias: i64) -> (i32, u32, u32) {
    let z = dias + 719_468;
    let era = z.div_euclid(146_097);
    let dia_de_era = z.rem_euclid(146_097);
    let anio_de_era = (dia_de_era - dia_de_era / 1_460 + dia_de_era / 36_524 - dia_de_era / 146_096) / 365;
    let mut anio = anio_de_era + era * 400;
    let dia_del_anio = dia_de_era - (365 * anio_de_era + anio_de_era / 4 - anio_de_era / 100);
    let mes_desplazado = (5 * dia_del_anio + 2) / 153;
    let dia = dia_del_anio - (153 * mes_desplazado + 2) / 5 + 1;
    let mes = if mes_desplazado < 10 {
        mes_desplazado + 3
    } else {
        mes_desplazado - 9
    };
    if mes <= 2 {
        anio += 1;
    }
    (anio as i32, mes as u32, dia as u32)
}

fn formulario(campos: &[(&str, &str)]) -> String {
    campos
        .iter()
        .map(|(clave, valor)| format!("{clave}={}", codificar_formulario(valor)))
        .collect::<Vec<_>>()
        .join("&")
}

/// `application/x-www-form-urlencoded`: se escapan **todos** los caracteres que
/// no sean alfanumericos ni `-_.~`, como hace `urlencode` en Python. Importa
/// porque la clave por defecto es un literal en mayusculas hoy, pero manana
/// puede llevar `+`, `&` o `#`, y sin escapar romperia el cuerpo del POST.
fn codificar_formulario(valor: &str) -> String {
    let mut salida = String::with_capacity(valor.len());
    for byte in valor.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                salida.push(byte as char);
            }
            b' ' => salida.push('+'),
            _ => salida.push_str(&format!("%{byte:02X}")),
        }
    }
    salida
}

// ---------------------------------------------------------------------------
// Snapshot en disco
// ---------------------------------------------------------------------------

/// Un asiento tal como va al snapshot: `importe` como **número JSON**.
///
/// No se reutiliza `domain::Asiento` porque su `Decimal` serializa a *string* y
/// el validador de Mongo exige un número (`bsonType: "decimal"`). Tampoco se usa
/// `f64`: perdería los ceros finales (`9872.00` → `9872.0`) y, peor, redondearía
/// los importes grandes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AsientoSnapshot {
    pub _id: String,
    pub asiento_id: String,
    pub snapshot_id: String,
    pub clave_factura: String,
    pub fecha: Option<String>,
    pub proveedor: Option<String>,
    pub nif: String,
    pub pedido: String,
    #[serde(serialize_with = "importe_a_raw", deserialize_with = "raw_a_importe")]
    pub importe: Decimal,
    pub estado: EstadoAsiento,
    pub vigente: bool,
    pub esquema_version: u32,
}

/// Versión del esquema del snapshot. Va en cada documento para poder migrar sin
/// adivinar qué forma tiene el fichero que hay en disco.
pub const ESQUEMA_VERSION: u32 = 1;

/// `Decimal` → número JSON crudo, sin comillas y sin perder los ceros finales.
fn importe_a_raw<S>(importe: &Decimal, serializador: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    let texto = formato_importe(*importe);
    debug_assert!(
        texto.parse::<f64>().is_ok(),
        "el importe tiene que ser un numero JSON valido, no {texto}"
    );
    let crudo = RawValue::from_string(texto).map_err(serde::ser::Error::custom)?;
    crudo.serialize(serializador)
}

/// Número JSON (o string, por si el snapshot viene de una version anterior) →
/// `Decimal`.
///
/// Hace falta un deserializador propio: el `Deserialize` por defecto de
/// `rust_decimal` usa `deserialize_any`, y el `Deserializer` de `serde_json` no
/// sabe servirlo dentro de un campo de struct — falla con "invalid type: map,
/// expected a Decimal".
///
/// Se lee con `RawValue` y **no** con `serde_json::Value`: `Value` guarda los
/// numeros con fraccion como `f64`, asi que `9872.00` volveria como `9872.0` y el
/// snapshot dejaria de ser byte a byte lo que se escribio. Con el texto crudo se
/// relee tal cual.
fn raw_a_importe<'de, D>(deserializador: D) -> Result<Decimal, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let crudo = Box::<RawValue>::deserialize(deserializador).map_err(serde::de::Error::custom)?;
    let texto = crudo.get().trim();
    // Una cadena JSON llega con sus comillas (`"1.00"`): se desescapa para
    // admitir tambien snapshots escritos por la referencia en Python.
    let texto = if texto.starts_with('"') {
        serde_json::from_str::<String>(texto).map_err(serde::de::Error::custom)?
    } else {
        String::from(texto)
    };
    Decimal::from_str(texto.trim()).map_err(serde::de::Error::custom)
}

/// Un asiento de la descarga → documento del snapshot.
pub fn asiento_a_snapshot(asiento: &Asiento, snapshot_id: &str) -> AsientoSnapshot {
    AsientoSnapshot {
        _id: format!("{snapshot_id}#{}", asiento.asiento_id),
        asiento_id: asiento.asiento_id.clone(),
        snapshot_id: String::from(snapshot_id),
        clave_factura: clave_factura(asiento),
        fecha: asiento.fecha.clone(),
        proveedor: asiento.proveedor.clone(),
        nif: asiento.nif.as_str().to_string(),
        pedido: asiento.pedido.clone(),
        importe: asiento.importe,
        estado: asiento.estado.clone(),
        vigente: true,
        esquema_version: ESQUEMA_VERSION,
    }
}

/// Raíz del snapshot. Mismo esquema que `data/erp_snapshot.json`.
///
/// **Sin campo `ajustes`**: el snapshot del ERP es lo que dijo el ERP, sin
/// retoques. Si algo se corrige, se corrige en la conciliación y queda en el
/// outcome, no aquí.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub _id: String,
    pub snapshot_id: String,
    pub descargado_en: String,
    /// Cuántos asientos lleva el documento. Coincide con `asientos.len()` en un
    /// snapshot bien formado; no es el total del ERP.
    pub total_asientos: u32,
    /// El `<total>` que anunció el ERP, **incluidas** las filas saltadas (las
    /// que no tenían NIF). Es `≥ total_asientos`, y es intencionado: si se
    /// guardase el recuento de útiles, el snapshot no podría demostrar contra
    /// qué se verificó. En la descarga de referencia: 516 aquí, 496 asientos.
    pub asientos_descargados: u32,
    pub paginas: u32,
    pub vigente: bool,
    pub estado: String,
    pub reintentos: Reintentos,
    pub duracion_ms: u64,
    pub esquema_version: u32,
    pub avisos: Vec<String>,
    pub duplicados: Vec<Duplicado>,
    pub asientos: Vec<AsientoSnapshot>,
}

/// Construye el snapshot completo a partir de una descarga.
pub fn construir_snapshot(
    asientos: &[Asiento],
    snapshot_id: &str,
    descargado_en: &str,
    meta: &MetaSnapshot,
) -> Snapshot {
    Snapshot {
        _id: String::from(snapshot_id),
        snapshot_id: String::from(snapshot_id),
        descargado_en: String::from(descargado_en),
        total_asientos: asientos.len() as u32,
        asientos_descargados: meta.asientos_descargados,
        paginas: meta.paginas,
        vigente: meta.vigente,
        estado: meta.estado.clone(),
        reintentos: meta.reintentos,
        duracion_ms: meta.duracion_ms,
        esquema_version: ESQUEMA_VERSION,
        avisos: meta.avisos.clone(),
        duplicados: meta.duplicados.clone(),
        asientos: asientos.iter().map(|asiento| asiento_a_snapshot(asiento, snapshot_id)).collect(),
    }
}

/// Escribe el snapshot en `ruta`, con escritura atómica.
///
/// Se escribe a un `.tmp` y se hace `rename`: un snapshot a medias es peor que
/// ninguno, porque la conciliación se fiaría de él. `rename` dentro del mismo
/// directorio es atómico, así que o está el snapshot viejo entero, o el nuevo
/// entero.
pub fn escribir_snapshot(ruta: &Path, snapshot: &Snapshot) -> Result<(), ErpError> {
    if let Some(padre) = ruta.parent() {
        if !padre.as_os_str().is_empty() {
            fs::create_dir_all(padre).map_err(|error| {
                ErpError::Protocolo(format!("{}: no puedo crear el directorio ({error})", padre.display()))
            })?;
        }
    }

    let texto = serde_json::to_string_pretty(snapshot)
        .map_err(|error| ErpError::Protocolo(format!("snapshot: no se puede serializar ({error})")))?;

    let mut nombre = ruta.file_name().unwrap_or_default().to_os_string();
    nombre.push(".tmp");
    let temporal = ruta.with_file_name(nombre);

    fs::write(&temporal, format!("{texto}\n"))
        .map_err(|error| ErpError::Protocolo(format!("{}: no puedo escribir ({error})", temporal.display())))?;
    fs::rename(&temporal, ruta)
        .map_err(|error| ErpError::Protocolo(format!("{}: no puedo renombrar ({error})", ruta.display())))?;
    Ok(())
}

/// Lee el snapshot de disco.
///
/// Se relee el fichero entero en vez de exponer un iterador: el snapshot son
/// ~400 KB y quien lo lee necesita la lista completa para conciliar, así que un
/// `Stream` solo añadiría complejidad. Si no cabe en memoria, el problema no es
/// este método.
pub fn leer_snapshot(ruta: &Path) -> Result<Snapshot, ErpError> {
    let texto = fs::read_to_string(ruta)
        .map_err(|error| ErpError::Protocolo(format!("{}: no puedo leer ({error})", ruta.display())))?;
    serde_json::from_str(&texto)
        .map_err(|error| ErpError::Protocolo(format!("{}: JSON ilegible ({error})", ruta.display())))
}

/// Los asientos vigentes de un snapshot, listos para conciliar.
pub fn asientos_del_snapshot(snapshot: &Snapshot) -> Vec<Asiento> {
    snapshot
        .asientos
        .iter()
        .filter(|documento| documento.vigente)
        .filter_map(asiento_desde_snapshot)
        .collect()
}

/// Documento del snapshot → `domain::Asiento`. `None` si el NIF del documento
/// no se puede normalizar (un snapshot manipulado a mano).
fn asiento_desde_snapshot(documento: &AsientoSnapshot) -> Option<Asiento> {
    Some(Asiento {
        asiento_id: documento.asiento_id.clone(),
        nif: Nif::nuevo(&documento.nif)?,
        pedido: documento.pedido.clone(),
        importe: documento.importe,
        estado: documento.estado.clone(),
        proveedor: documento.proveedor.clone(),
        fecha: documento.fecha.clone(),
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const LOGIN: &str = include_str!("../tests/fixtures/login.xml");
    const PAGINA_1: &str = include_str!("../tests/fixtures/pagina_1.xml");
    const PAGINA_26: &str = include_str!("../tests/fixtures/pagina_26.xml");
    const DETALLE: &str = include_str!("../tests/fixtures/asiento_detalle.xml");
    const ESTADO: &str = include_str!("../tests/fixtures/estado.xml");
    const ERROR_ORA: &str = include_str!("../tests/fixtures/error_ora_00600.xml");
    const ERROR_SES: &str = include_str!("../tests/fixtures/error_ses_401.xml");
    const ERROR_429: &str = include_str!("../tests/fixtures/error_erp_429.xml");
    const ERROR_404: &str = include_str!("../tests/fixtures/error_erp_404.xml");

    fn pagina_de(cuerpo: &str) -> PaginaAsientos {
        let raiz = analizar(cuerpo).expect("XML valido");
        let mut avisos = Vec::new();
        pagina_desde_nodo(&raiz, "/erp/asientos?pagina=1", &mut avisos).expect("pagina valida")
    }

    /// Página sintética con `filas` asientos distintos, coherente con el
    /// `<total>`/`<paginas>` que se le pase. Los ids se derivan del número de
    /// página para que no se repitan entre páginas (repetirlos dispararía la
    /// comprobación de identidad, que es justo lo que no se quiere aquí).
    fn pagina_sintetica_n(numero: u32, paginas: u32, total: u32, filas: u32) -> String {
        let mut asientos = String::new();
        for fila in 0..filas {
            let id = numero * 100 + fila;
            asientos.push_str(&format!(
                "<asiento><id>AS-{id:05}</id><fecha>01/02/2026</fecha><proveedor>P002</proveedor><nif>A41220987</nif><pedido>PO-{id}</pedido><importe>1,00</importe><estado>PENDIENTE</estado></asiento>"
            ));
        }
        format!(
            "<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?><respuesta><meta><total>{total}</total><paginas>{paginas}</paginas><pagina>{numero}</pagina><por_pagina>{filas}</por_pagina><generado>19/09/2026 11:53:33</generado></meta><asientos>{asientos}</asientos></respuesta>"
        )
    }

    // -- fixtures: parseo --------------------------------------------------

    #[test]
    fn el_login_da_token() {
        let raiz = analizar(LOGIN).expect("login.xml es XML valido");
        assert_eq!(raiz.nombre, "sesion");
        assert_eq!(raiz.texto_o_vacio("token").len(), 32);
    }

    #[test]
    fn la_primera_pagina_trae_el_recuento_del_erp() {
        let pagina = pagina_de(PAGINA_1);
        assert_eq!(pagina.asientos.len(), 20);
        assert_eq!(pagina.meta.total, 516);
        assert_eq!(pagina.meta.paginas, 26);
        assert_eq!(pagina.meta.pagina, 1);
        assert_eq!(pagina.meta.por_pagina, 20);
        assert_eq!(pagina.meta.generado, "19/09/2026 11:53:33");
    }

    #[test]
    fn la_ultima_pagina_no_respeta_por_pagina() {
        // 25*20 + 16 = 516: la ultima pagina trae 16 filas y sigue declarando
        // `por_pagina=20`. Quien calcule la ultima pagina multiplicando se
        // equivoca, y este test es el que lo impide.
        let pagina = pagina_de(PAGINA_26);
        assert_eq!(pagina.asientos.len(), 16);
        assert_eq!(pagina.meta.pagina, 26);
        assert_eq!(pagina.meta.por_pagina, 20);
        assert_ne!(pagina.asientos.len(), pagina.meta.por_pagina as usize);
    }

    #[test]
    fn el_primer_asiento_se_convierte_con_precision() {
        let pagina = pagina_de(PAGINA_1);
        let primero = &pagina.asientos[0];
        assert_eq!(primero.asiento_id, "AS-00084");
        assert_eq!(primero.fecha.as_deref(), Some("2026-03-21"));
        assert_eq!(primero.proveedor.as_deref(), Some("P002"));
        assert_eq!(primero.nif.as_str(), "A41220987");
        assert_eq!(primero.pedido, "PO-2026-0084");
        assert_eq!(primero.importe, Decimal::from_str("6199.54").unwrap());
        assert_eq!(primero.estado, EstadoAsiento::Pendiente);
        assert_eq!(clave_factura(primero), "P002|PO-2026-0084|2026-03-21|6199.54");
    }

    #[test]
    fn los_ceros_finales_del_importe_sobreviven() {
        let pagina = pagina_de(PAGINA_1);
        let segundo = &pagina.asientos[1];
        assert_eq!(segundo.asiento_id, "AS-00261");
        assert_eq!(segundo.importe, Decimal::from_str("9872.00").unwrap());
        assert_eq!(formato_importe(segundo.importe), "9872.00");
        assert_eq!(clave_factura(segundo), "P005|PO-2026-0261|2026-01-29|9872.00");
    }

    #[test]
    fn el_detalle_de_un_asiento_se_lee_sin_meta() {
        // La respuesta de `/erp/asientos/<id>` NO trae <meta>: si `asiento`
        // reutilizase el parser de pagina, fallaria por contrato.
        let raiz = analizar(DETALLE).expect("XML valido");
        assert!(raiz.hijo("meta").is_none());
        let nodo = primer_asiento(&raiz).expect("hay un asiento");
        let asiento = asiento_desde_nodo(nodo, &mut Vec::new()).expect("sin fallo").expect("fila util");
        assert_eq!(asiento.asiento_id, "AS-00084");
        assert_eq!(asiento.importe, Decimal::from_str("6199.54").unwrap());
    }

    #[test]
    fn el_estado_del_erp_se_traduce_a_tipos() {
        let raiz = analizar(ESTADO).expect("XML valido");
        assert_eq!(entero_de(&raiz, "asientos"), 516);
        assert_eq!(entero_de(&raiz, "activo_segundos"), 10255);
        assert!(raiz.texto_o_vacio("version").starts_with("ERP Miralmar"));
        // El bridge escribe "SI"/"NO"; cualquier cosa menos "NO" es cargada.
        assert_eq!(raiz.texto_o_vacio("actualizacion_cargada"), "NO");
    }

    #[test]
    fn los_tres_errores_del_bridge_traen_su_codigo() {
        for (cuerpo, esperado) in [
            (ERROR_ORA, CODIGO_ORA_00600),
            (ERROR_SES, CODIGO_SES_401),
            (ERROR_429, CODIGO_ERP_429),
            (ERROR_404, CODIGO_ERP_404),
        ] {
            let (codigo, mensaje) = codigo_de_error(cuerpo);
            assert_eq!(codigo.as_deref(), Some(esperado), "cuerpo: {cuerpo}");
            assert!(mensaje.is_some(), "el error {esperado} trae mensaje");
        }
        // Un cuerpo que no es XML no debe romper la clasificacion: se cae al
        // codigo HTTP.
        assert_eq!(codigo_de_error("Internal Server Error"), (None, None));
    }

    #[test]
    fn sin_bloque_meta_la_pagina_es_un_fallo_de_protocolo() {
        let sin_meta = "<?xml version=\"1.0\"?><respuesta><asientos></asientos></respuesta>";
        let raiz = analizar(sin_meta).unwrap();
        let error = pagina_desde_nodo(&raiz, "/erp/asientos?pagina=1", &mut Vec::new()).unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(ref detalle) if detalle.contains("meta")), "{error}");
    }

    #[test]
    fn el_xml_se_decodifica_como_iso_8859_1() {
        // 'í' y 'ñ' son UN byte en ISO-8859-1 (0xED, 0xF1) y dos en UTF-8. Si el
        // cuerpo se tratase como UTF-8, aqui saldrian caracteres de reemplazo.
        let bytes = b"<respuesta><asientos><asiento><id>AS-1</id><fecha>01/02/2026</fecha><proveedor>C\xEDa. Pe\xF1a</proveedor><nif>A41220987</nif><pedido>PO-1</pedido><importe>1,00</importe><estado>PENDIENTE</estado></asiento></asientos></respuesta>";
        let respuesta = RespuestaCruda::desde_bytes(200, bytes, None);
        assert!(respuesta.cuerpo.contains("Cía. Peña"), "cuerpo: {}", respuesta.cuerpo);
        assert!(!respuesta.cuerpo.contains('\u{fffd}'), "no debe haber reemplazos");
        let raiz = analizar(&respuesta.cuerpo).unwrap();
        let asiento = asiento_desde_nodo(primer_asiento(&raiz).unwrap(), &mut Vec::new())
            .unwrap()
            .unwrap();
        assert_eq!(asiento.proveedor.as_deref(), Some("Cía. Peña"));
    }

    // -- conversion de importes y fechas -----------------------------------

    #[test]
    fn los_importes_espanoles_y_los_iso_dan_el_mismo_valor() {
        for (crudo, esperado) in [
            ("6.199,54", "6199.54"),
            ("12.874,40", "12874.40"),
            ("9.872,00", "9872.00"),
            ("473.70", "473.70"),
            ("0,00", "0.00"),
            ("8,50", "8.50"),
            ("84.700,00", "84700.00"),
            // Espacio duro como separador de miles (el ERP lo usa a veces).
            ("1\u{a0}234,56", "1234.56"),
        ] {
            assert_eq!(
                importe_a_decimal(crudo),
                Some(Decimal::from_str(esperado).unwrap()),
                "importe {crudo:?}"
            );
        }
    }

    #[test]
    fn un_punto_suelto_es_separador_de_miles_no_decimal() {
        // El error clasico: "1.234" son mil doscientos treinta y cuatro euros,
        // no 1,234. Si se leyese como decimal, el importe saldria 1000 veces
        // mas pequeno y ninguna factura conciliaria.
        assert_eq!(importe_a_decimal("1.234"), Some(Decimal::from_str("1234").unwrap()));
        assert_eq!(importe_a_decimal("1.234.567"), Some(Decimal::from_str("1234567").unwrap()));
        // ...pero "1.23" y "1.2" si son decimales (formato ISO con 1-2 cifras).
        assert_eq!(importe_a_decimal("1.23"), Some(Decimal::from_str("1.23").unwrap()));
        assert_eq!(importe_a_decimal("1.2"), Some(Decimal::from_str("1.2").unwrap()));
    }

    #[test]
    fn un_importe_ilegible_no_es_un_cero() {
        assert_eq!(importe_a_decimal(""), None);
        assert_eq!(importe_a_decimal("   "), None);
        assert_eq!(importe_a_decimal("PENDIENTE"), None);
    }

    #[test]
    fn la_fecha_va_de_dia_primero_a_iso() {
        assert_eq!(fecha_es_a_iso("21/03/2026").as_deref(), Some("2026-03-21"));
        assert_eq!(fecha_es_a_iso("01/02/2026").as_deref(), Some("2026-02-01"));
        // Ya en ISO: se normaliza el relleno.
        assert_eq!(fecha_es_a_iso("2026-3-2").as_deref(), Some("2026-03-02"));
        // Fechas imposibles: mejor sin fecha que con una inventada.
        assert_eq!(fecha_es_a_iso("31/04/2026"), None);
        assert_eq!(fecha_es_a_iso("29/02/2023"), None);
        assert_eq!(fecha_es_a_iso("13/13/2026"), None);
        assert_eq!(fecha_es_a_iso(""), None);
        // 2024 si es bisiesto.
        assert_eq!(fecha_es_a_iso("29/02/2024").as_deref(), Some("2024-02-29"));
    }

    #[test]
    fn la_clave_de_factura_junta_proveedor_pedido_fecha_importe() {
        let asiento = Asiento {
            asiento_id: String::from("AS-00084"),
            nif: Nif::nuevo("A41220987").unwrap(),
            pedido: String::from("PO-2026-0084"),
            importe: Decimal::from_str("6199.54").unwrap(),
            estado: EstadoAsiento::Pendiente,
            proveedor: Some(String::from("P002")),
            fecha: Some(String::from("2026-03-21")),
        };
        assert_eq!(clave_factura(&asiento), "P002|PO-2026-0084|2026-03-21|6199.54");
        // Un proveedor o una fecha ausentes no rompen la clave: dan campos
        // vacios, que es informacion suficiente para deduplicar.
        let sin_fecha = Asiento {
            fecha: None,
            ..asiento
        };
        assert_eq!(clave_factura(&sin_fecha), "P002|PO-2026-0084||6199.54");
    }

    #[test]
    fn un_estado_desconocido_no_se_asume_pendiente() {
        assert_eq!(estado_desde("PENDIENTE"), Some(EstadoAsiento::Pendiente));
        assert_eq!(estado_desde(" pagada "), Some(EstadoAsiento::Pagada));
        assert_eq!(estado_desde("ANULADA"), None);
        assert_eq!(estado_desde(""), None);
    }

    #[test]
    fn una_fila_con_nif_vacio_se_salta_y_se_cuenta() {
        // Hoy son 20 de 516. `domain::Asiento` exige NIF, asi que la fila no se
        // puede representar: se salta y se cuenta en vez de propagar un NIF roto.
        let pagina = "<?xml version=\"1.0\"?><respuesta><meta><total>1</total><paginas>1</paginas><pagina>1</pagina><por_pagina>20</por_pagina><generado>19/09/2026 11:53:33</generado></meta><asientos><asiento><id>AS-00499</id><fecha>01/02/2026</fecha><proveedor>P003</proveedor><nif></nif><pedido>PO-2026-1</pedido><importe>1,00</importe><estado>PENDIENTE</estado></asiento></asientos></respuesta>";
        let raiz = analizar(pagina).unwrap();
        let mut avisos = Vec::new();
        let resultado = pagina_desde_nodo(&raiz, "/erp/asientos?pagina=1", &mut avisos).unwrap();
        assert!(resultado.asientos.is_empty(), "la fila sin NIF no entra");
        assert_eq!(
            avisos,
            vec![Aviso {
                asiento_id: String::from("AS-00499"),
                motivo: String::from("nif vacio")
            }]
        );
        // Y el aviso se presenta con el mismo texto que el snapshot real.
        assert_eq!(
            lineas_de_presentacion(&avisos, 516),
            vec![String::from("nif vacio en 1/516: ['AS-00499']")]
        );
    }

    #[test]
    fn un_estado_desconocido_corta_la_pagina() {
        let pagina = "<?xml version=\"1.0\"?><respuesta><meta><total>1</total><paginas>1</paginas><pagina>1</pagina><por_pagina>20</por_pagina><generado>19/09/2026 11:53:33</generado></meta><asientos><asiento><id>AS-1</id><nif>A41220987</nif><pedido>PO-1</pedido><importe>1,00</importe><estado>ANULADA</estado></asiento></asientos></respuesta>";
        let raiz = analizar(pagina).unwrap();
        let error = pagina_desde_nodo(&raiz, "/erp/asientos?pagina=1", &mut Vec::new()).unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(ref detalle) if detalle.contains("estado desconocido")), "{error}");
    }

    #[test]
    fn un_importe_ilegible_corta_la_pagina() {
        let pagina = "<?xml version=\"1.0\"?><respuesta><meta><total>1</total><paginas>1</paginas><pagina>1</pagina><por_pagina>20</por_pagina><generado>19/09/2026 11:53:33</generado></meta><asientos><asiento><id>AS-1</id><nif>A41220987</nif><pedido>PO-1</pedido><importe>PENDIENTE</importe><estado>PENDIENTE</estado></asiento></asientos></respuesta>";
        let raiz = analizar(pagina).unwrap();
        let error = pagina_desde_nodo(&raiz, "/erp/asientos?pagina=1", &mut Vec::new()).unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(ref detalle) if detalle.contains("importe ilegible")), "{error}");
    }

    #[test]
    fn un_xml_roto_no_provoca_un_panico() {
        for cuerpo in ["", "no es xml", "<respuesta><meta>", "<a><b></a>", "<a>" ] {
            let resultado = analizar(cuerpo);
            assert!(resultado.is_err() || resultado.is_ok(), "{cuerpo:?} no debe entrar en panico");
        }
        // Y ningun XML profundo revienta la pila.
        let hondo = format!("{}{}", "<n>".repeat(200), "</n>".repeat(200));
        assert!(analizar(&hondo).is_err(), "se corta antes de agotar la pila");
    }

    #[test]
    fn las_fechas_se_calculan_sin_chrono() {
        assert_eq!(partes_utc(0), (1970, 1, 1, 0, 0, 0));
        // 2026-09-19T10:39:16Z, medido del bridge.
        let segundos = 1_789_814_356;
        assert_eq!(partes_utc(segundos), (2026, 9, 19, 10, 39, 16));
        assert_eq!(civil_desde_dias(0), (1970, 1, 1));
        assert_eq!(civil_desde_dias(-1), (1969, 12, 31));
    }

    // -- maquina de reintentos --------------------------------------------

    fn cliente_simulado(respuestas: Vec<RespuestaCruda>) -> ClienteErp {
        let mut cliente = ClienteErp::nuevo("http://127.0.0.1:8009", "alberto", "FACTURAS2009");
        cliente.transporte = Transporte::Simulado(Mutex::new(VecDeque::from(respuestas)));
        cliente
    }

    fn ok(cuerpo: &str) -> RespuestaCruda {
        RespuestaCruda {
            codigo_http: 200,
            cuerpo: String::from(cuerpo),
            retry_after: None,
        }
    }

    fn fallo(codigo_http: u16, cuerpo: &str) -> RespuestaCruda {
        RespuestaCruda {
            codigo_http,
            cuerpo: String::from(cuerpo),
            retry_after: None,
        }
    }

    #[tokio::test]
    async fn el_login_guarda_el_token() {
        let mut cliente = cliente_simulado(vec![ok(LOGIN)]);
        assert!(!cliente.tiene_token());
        cliente.login().await.expect("login correcto");
        assert!(cliente.tiene_token());
    }

    #[tokio::test]
    async fn un_login_rechazado_no_revela_la_clave() {
        let mut cliente = cliente_simulado(vec![fallo(401, ERROR_SES)]);
        let error = cliente.login().await.unwrap_err();
        let texto = error.to_string();
        assert!(!texto.contains("FACTURAS2009"), "la clave no puede salir en un error: {texto}");
        assert!(texto.contains("SES-401") || texto.contains("rechaz"), "{texto}");
    }

    #[tokio::test]
    async fn el_debug_no_filtra_la_clave_ni_el_token() {
        let mut cliente = cliente_simulado(vec![ok(LOGIN)]);
        cliente.login().await.unwrap();
        let texto = format!("{cliente:?}");
        assert!(!texto.contains("FACTURAS2009"), "{texto}");
        assert!(!texto.contains("7e09a6d43f0aa143eca77423b4609d83"), "{texto}");
        assert!(texto.contains("oculta"), "{texto}");
    }

    /// Un despliegue que se olvide de `ERP_USUARIO`/`ERP_CLAVE` arranca igual y
    /// funciona igual —contra el bridge de demo—, así que la única forma de
    /// saber que se está pagando con la credencial de demo es que alguien lo
    /// diga. Esto fija que se diga, y que el aviso no lleve la clave dentro.
    #[test]
    fn las_credenciales_de_demo_se_detectan_y_se_avisa_sin_la_clave() {
        let inyectado = ClienteErp::nuevo("http://erp.interno:8009/", "ana", "s3creta");
        assert_eq!(
            inyectado.base_url(),
            "http://erp.interno:8009",
            "la barra final no debe duplicarse al montar las rutas"
        );
        assert_eq!(inyectado.usuario(), "ana");
        assert_eq!(inyectado.max_intentos(), MAX_INTENTOS_POR_DEFECTO);
        assert!(
            !inyectado.usa_credenciales_por_defecto(),
            "credenciales explícitas no son las de demo"
        );
        assert_eq!(inyectado.aviso_de_credenciales(), None);

        let de_demo = ClienteErp::nuevo(
            "http://127.0.0.1:8009",
            USUARIO_POR_DEFECTO,
            CLAVE_POR_DEFECTO,
        );
        assert!(de_demo.usa_credenciales_por_defecto());

        let aviso = de_demo
            .aviso_de_credenciales()
            .expect("con las credenciales de demo tiene que haber aviso");
        assert!(aviso.contains(VARIABLE_USUARIO), "{aviso}");
        assert!(aviso.contains(VARIABLE_CLAVE), "{aviso}");
        assert!(
            !aviso.contains(CLAVE_POR_DEFECTO),
            "el aviso nombra la variable, no la clave: {aviso}"
        );
    }

    /// `filas_leidas` y `avisos` cuentan la última descarga y solo la última: si
    /// se acumularan, la segunda descarga del proceso declararía el doble de
    /// filas saltadas que la primera y el snapshot mentiría.
    #[tokio::test]
    async fn los_contadores_de_la_descarga_arrancan_a_cero() {
        let cliente = cliente_simulado(vec![]);
        assert_eq!(cliente.filas_leidas(), 0);
        assert!(cliente.avisos().is_empty());
        assert_eq!(cliente.peticiones(), 0);
        assert_eq!(cliente.reintentos().total(), 0);
        assert!(
            !cliente.reintentos().hubo_fallos(),
            "un cliente que no ha pedido nada no ha tenido fallos"
        );

        let con_fallos = Reintentos {
            ses_401: 2,
            ..Default::default()
        };
        assert_eq!(con_fallos.total(), 2);
        assert!(con_fallos.hubo_fallos());
    }

    #[tokio::test]
    async fn un_ora_00600_repite_la_misma_pagina() {
        // El ORA-00600 no mata la sesion: hay que repetir la MISMA peticion.
        let mut cliente = cliente_simulado(vec![ok(LOGIN), fallo(500, ERROR_ORA), ok(PAGINA_1)]);
        let pagina = cliente.pagina(1).await.expect("la segunda vez va");
        assert_eq!(pagina.asientos.len(), 20);
        assert_eq!(cliente.reintentos().ora_00600, 1);
        assert_eq!(cliente.reintentos().ses_401, 0);
        assert!(cliente.tiene_token(), "el ORA-00600 no invalida el token");
    }

    #[tokio::test]
    async fn un_ses_401_rehace_login_y_reintenta() {
        let mut cliente = cliente_simulado(vec![
            ok(LOGIN),
            fallo(401, ERROR_SES),
            ok(LOGIN),
            ok(PAGINA_1),
        ]);
        let pagina = cliente.pagina(1).await.expect("tras rehacer login, va");
        assert_eq!(pagina.asientos.len(), 20);
        assert_eq!(cliente.reintentos().ses_401, 1);
        // 4 peticiones: login, pagina fallida, login nuevo, pagina buena.
        assert_eq!(cliente.peticiones(), 4);
    }

    #[tokio::test]
    async fn un_erp_429_espera_lo_que_diga_retry_after() {
        let mut cliente = cliente_simulado(vec![
            ok(LOGIN),
            RespuestaCruda {
                codigo_http: 429,
                cuerpo: String::from(ERROR_429),
                retry_after: Some(0.0),
            },
            ok(PAGINA_1),
        ]);
        let pagina = cliente.pagina(1).await.expect("tras el rate limit, va");
        assert_eq!(pagina.asientos.len(), 20);
        assert_eq!(cliente.reintentos().erp_429, 1);
    }

    #[tokio::test]
    async fn un_error_irrecuperable_no_se_reintenta() {
        let mut cliente = cliente_simulado(vec![ok(LOGIN), fallo(400, "<error><codigo>ERP-500</codigo></error>")]);
        let error = cliente.pagina(1).await.unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(_)), "{error}");
        assert_eq!(cliente.peticiones(), 2, "no se reintenta un 400");
    }

    #[tokio::test]
    async fn agotar_los_intentos_da_un_error_explicito() {
        let mut cliente = cliente_simulado(vec![
            ok(LOGIN),
            fallo(500, ERROR_ORA),
            fallo(500, ERROR_ORA),
            fallo(500, ERROR_ORA),
        ]);
        cliente.max_intentos = 3;
        let error = cliente.pagina(1).await.unwrap_err();
        assert!(matches!(error, ErpError::Agotado(_)), "{error}");
        assert_eq!(cliente.reintentos().ora_00600, 3);
    }

    #[tokio::test]
    async fn un_asiento_inexistente_devuelve_none_y_no_reintenta() {
        let mut cliente = cliente_simulado(vec![ok(LOGIN), fallo(404, ERROR_404)]);
        let resultado = cliente.asiento("AS-99999").await.expect("404 no es un fallo");
        assert_eq!(resultado, None);
        assert_eq!(cliente.peticiones(), 2, "un 404 no se reintenta");
    }

    #[tokio::test]
    async fn un_asiento_existente_se_devuelve() {
        let mut cliente = cliente_simulado(vec![ok(LOGIN), ok(DETALLE)]);
        let asiento = cliente.asiento("AS-00084").await.unwrap().expect("existe");
        assert_eq!(asiento.asiento_id, "AS-00084");
        assert_eq!(asiento.importe, Decimal::from_str("6199.54").unwrap());
    }

    #[tokio::test]
    async fn el_estado_no_necesita_token() {
        let cliente = cliente_simulado(vec![ok(ESTADO)]);
        let estado = cliente.estado().await.expect("el estado no pide login");
        assert_eq!(estado.asientos, 516);
        assert_eq!(estado.activo_segundos, 10255);
        assert!(!estado.actualizacion_cargada);
        assert!(estado.animo.contains("17 anos"));
        assert_eq!(cliente.peticiones(), 1, "no se hace login para /erp/estado");
    }

    #[tokio::test]
    async fn descargar_recorre_las_paginas_en_serie() {
        // Tres paginas (20, 20 y 5 filas) y el ERP dice `<total>45</total>` y
        // `<paginas>3</paginas>`: la descarga tiene que pedirlas todas y cuadrar
        // el recuento.
        let mut cliente = cliente_simulado(vec![
            ok(LOGIN),
            ok(&pagina_sintetica_n(1, 3, 45, 20)),
            ok(&pagina_sintetica_n(2, 3, 45, 20)),
            ok(&pagina_sintetica_n(3, 3, 45, 5)),
        ]);
        let descarga = cliente.descargar(3).await.expect("descarga completa");
        assert_eq!(descarga.meta.paginas, 3);
        assert_eq!(descarga.asientos.len(), 45);
        assert_eq!(descarga.meta.asientos_descargados, 45);
        assert_eq!(descarga.meta.estado, ESTADO_COMPLETO);
        assert!(descarga.avisos.is_empty(), "{:?}", descarga.avisos);
        // Y en serie: 1 login + 3 paginas, sin repetir ninguna.
        assert_eq!(cliente.peticiones(), 4);
    }

    #[tokio::test]
    async fn descargar_sin_guion_agotado_no_miente() {
        // La pagina 1 dice que hay 26 paginas y el guion solo trae la primera:
        // la descarga NO puede devolver 20 asientos y decir que todo fue bien.
        let mut cliente = cliente_simulado(vec![
            ok(LOGIN),
            ok(PAGINA_1),
            fallo(500, ERROR_ORA),
            fallo(500, ERROR_ORA),
        ]);
        let error = cliente.descargar(2).await.unwrap_err();
        assert!(matches!(error, ErpError::Agotado(_)), "{error}");
    }

    #[tokio::test]
    async fn los_duplicados_se_colapsan_conservando_el_id_mas_bajo() {
        let mut asientos = pagina_de(PAGINA_1).asientos;
        let repetido = Asiento {
            asiento_id: String::from("AS-99999"),
            ..asientos[0].clone()
        };
        asientos.push(repetido);
        let (conservados, traza) = colapsar_duplicados(asientos);
        assert_eq!(traza.len(), 1);
        assert_eq!(traza[0].conservado, "AS-00084");
        assert_eq!(traza[0].descartados, vec![String::from("AS-99999")]);
        assert_eq!(conservados.len(), 20);
        // Se conserva el orden original del ERP.
        assert_eq!(conservados[0].asiento_id, "AS-00084");
    }

    #[test]
    fn el_formulario_del_login_escapa_lo_que_haga_falta() {
        assert_eq!(formulario(&[("usuario", "alberto"), ("clave", "abc")]), "usuario=alberto&clave=abc");
        assert_eq!(
            formulario(&[("clave", "a+b&c=d#e")]),
            "clave=a%2Bb%26c%3Dd%23e",
            "los caracteres reservados tienen que ir escapados"
        );
    }

    // -- snapshot ----------------------------------------------------------

    fn descarga_de_ejemplo() -> (Vec<Asiento>, MetaSnapshot) {
        let pagina = pagina_de(PAGINA_1);
        let (snapshot_id, descargado_en) = (String::from("snap-prueba"), String::from("2026-09-19T08:25:58Z"));
        let meta = MetaSnapshot {
            snapshot_id: snapshot_id.clone(),
            descargado_en,
            asientos_descargados: 516,
            paginas: 26,
            ..MetaSnapshot::default()
        };
        (pagina.asientos, meta)
    }

    #[test]
    fn el_snapshot_escribe_el_importe_como_numero_y_conserva_los_ceros() {
        let (asientos, meta) = descarga_de_ejemplo();
        let snapshot = construir_snapshot(&asientos, &meta.snapshot_id, &meta.descargado_en, &meta);
        let texto = serde_json::to_string(&snapshot).unwrap();

        // Tiene que ser un NUMERO con los ceros finales, no una cadena: el
        // validador de Mongo declara `importe: { bsonType: "decimal" }`.
        assert!(texto.contains("\"importe\":6199.54"), "{texto}");
        assert!(texto.contains("\"importe\":9872.00"), "{texto}");
        assert!(!texto.contains("\"importe\":\"6199.54\""), "no puede ir entrecomillado");
        // Y sin campo `ajustes`: el snapshot es lo que dijo el ERP, sin retoques.
        assert!(!texto.contains("ajustes"), "{texto}");
        assert!(texto.contains(&format!("\"esquema_version\":{ESQUEMA_VERSION}")));
    }

    #[test]
    fn el_snapshot_lleva_los_identificadores_compuestos() {
        let (asientos, meta) = descarga_de_ejemplo();
        let snapshot = construir_snapshot(&asientos, &meta.snapshot_id, &meta.descargado_en, &meta);
        let primero = &snapshot.asientos[0];
        assert_eq!(primero._id, "snap-prueba#AS-00084");
        assert_eq!(primero.snapshot_id, "snap-prueba");
        assert_eq!(primero.clave_factura, "P002|PO-2026-0084|2026-03-21|6199.54");
        assert!(primero.vigente);
        assert_eq!(snapshot.total_asientos, 20);
        assert_eq!(snapshot.asientos_descargados, 516, "el recuento es el del ERP, no el de asientos utiles");
    }

    #[test]
    fn el_snapshot_escrito_se_puede_volver_a_leer() {
        let (asientos, meta) = descarga_de_ejemplo();
        let snapshot = construir_snapshot(&asientos, &meta.snapshot_id, &meta.descargado_en, &meta);
        let ruta = std::env::temp_dir().join("erp_snapshot_ida_y_vuelta.json");

        escribir_snapshot(&ruta, &snapshot).expect("se escribe");
        assert!(!ruta.with_file_name("erp_snapshot_ida_y_vuelta.json.tmp").exists(), "el temporal se limpia");
        let releido = leer_snapshot(&ruta).expect("se relee");
        assert_eq!(releido.asientos, snapshot.asientos, "los importes no deben cambiar de valor");
        // La ida y vuelta no puede perder los ceros finales.
        assert_eq!(releido.asientos[1].importe, Decimal::from_str("9872.00").unwrap());
        assert_eq!(formato_importe(releido.asientos[1].importe), "9872.00");

        let conciliables = asientos_del_snapshot(&releido);
        assert_eq!(conciliables.len(), 20);
        assert_eq!(conciliables[0].nif.as_str(), "A41220987");
        assert_eq!(conciliables[0].importe, Decimal::from_str("6199.54").unwrap());
        assert_eq!(conciliables[0].estado, EstadoAsiento::Pendiente);

        let _ = fs::remove_file(&ruta);
    }

    #[test]
    fn un_snapshot_corrupto_da_un_error_y_no_un_panico() {
        let ruta = std::env::temp_dir().join("erp_snapshot_corrupto.json");
        fs::write(&ruta, "{ esto no es json").unwrap();
        let error = leer_snapshot(&ruta).unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(ref detalle) if detalle.contains("JSON ilegible")), "{error}");
        let _ = fs::remove_file(&ruta);

        let inexistente = std::env::temp_dir().join("erp_snapshot_que_no_existe_12345.json");
        let error = leer_snapshot(&inexistente).unwrap_err();
        assert!(matches!(error, ErpError::Protocolo(ref detalle) if detalle.contains("no puedo leer")), "{error}");
    }

    #[test]
    fn el_importe_del_snapshot_admite_la_forma_antigua_en_texto() {
        // Tolerancia a snapshots escritos por la referencia en Python, donde el
        // importe podia ir como cadena: se lee igual.
        let texto = r#"{"_id":"s#AS-1","asiento_id":"AS-1","snapshot_id":"s","clave_factura":"P|PO|2026-01-01|1.00","fecha":"2026-01-01","proveedor":"P","nif":"A41220987","pedido":"PO","importe":"1.00","estado":"PENDIENTE","vigente":true,"esquema_version":1}"#;
        let documento: AsientoSnapshot = serde_json::from_str(texto).expect("se lee");
        assert_eq!(documento.importe, Decimal::from_str("1.00").unwrap());
        let asiento = asiento_desde_snapshot(&documento).expect("NIF valido");
        assert_eq!(asiento.asiento_id, "AS-1");
    }

    #[test]
    fn el_snapshot_real_del_repo_se_puede_leer() {
        let ruta = Path::new(env!("CARGO_MANIFEST_DIR")).join("data/erp_snapshot.json");
        if !ruta.exists() {
            // El snapshot se descarga aparte y no tiene por que estar en el
            // arbol de trabajo; si no esta, no hay nada que comprobar.
            return;
        }
        let snapshot = leer_snapshot(&ruta).expect("el snapshot commitado se lee");
        assert_eq!(snapshot.asientos_descargados, 516);
        assert_eq!(snapshot.paginas, 26);
        assert_eq!(snapshot.estado, "COMPLETO");
        assert!(snapshot.avisos.iter().any(|aviso| aviso.contains("nif vacio en 20/516")));

        // `snapshot.asientos` NO tiene el mismo tamano segun quien lo escribio, y
        // el test no debe depender de eso:
        //   - la referencia en Python guarda las 516 filas, incluidas las 20 sin
        //     NIF (un string vacio pasa el $jsonSchema de Mongo);
        //   - `escribir_snapshot` de Rust guarda solo lo que cabe en un
        //     `AsientoSnapshot` con NIF: 496.
        // El contrato (§4.1) manda SALTAR la fila sin NIF y anotarla en `avisos`;
        // que ademas se guarde o no es una decision de cada escritor. Lo que si
        // tiene que cumplirse en los dos casos -- y es lo que se comprueba aqui --
        // es que el lector saque siempre los mismos 496 asientos utilizables.
        assert!(
            snapshot.asientos.len() == 516 || snapshot.asientos.len() == 496,
            "el snapshot trae {} documentos: ni la forma de la referencia (516) ni la de Rust (496)",
            snapshot.asientos.len()
        );

        let asientos = asientos_del_snapshot(&snapshot);
        // Las 20 filas sin NIF no pueden ser un `domain::Asiento` (el NIF es
        // obligatorio) y por eso no entran en la conciliacion. El aviso de arriba
        // las documenta.
        assert_eq!(asientos.len(), 496, "las 20 filas sin NIF no son asientos");
        let primero = &asientos[0];
        assert_eq!(primero.asiento_id, "AS-00084");
        assert_eq!(primero.nif.as_str(), "A41220987");
        assert_eq!(primero.importe, Decimal::from_str("6199.54").unwrap());
        assert_eq!(clave_factura(primero), "P002|PO-2026-0084|2026-03-21|6199.54");
        // 9 PAGADA y 507 PENDIENTE en el snapshot real.
        let pagadas = asientos.iter().filter(|a| a.estado == EstadoAsiento::Pagada).count();
        assert_eq!(pagadas, 9);
    }

    // -- contra el bridge vivo (a mano) ------------------------------------

    #[tokio::test]
    #[ignore = "necesita el bridge del ERP levantado (python alberto_erp.py --rapido, puerto 8009)"]
    async fn contra_el_erp_vivo() {
        let mut cliente = ClienteErp::desde_entorno();
        let estado = cliente.estado().await.expect("el bridge responde en /erp/estado");
        assert!(estado.asientos > 0, "el ERP dice cuantos asientos tiene");

        let pagina = cliente.pagina(1).await.expect("la pagina 1 se descarga");
        assert_eq!(pagina.asientos.len(), 20);
        assert_eq!(pagina.asientos[0].asiento_id, "AS-00084");

        // El detalle de un asiento que existe y de uno que no.
        let existente = cliente.asiento("AS-00084").await.expect("consulta correcta");
        assert!(existente.is_some());
        let inexistente = cliente.asiento("AS-99999").await.expect("404 no es error");
        assert_eq!(inexistente, None);

        let _ = std::fs::remove_file(std::env::temp_dir().join("erp_snapshot_vivo.json"));
    }

    #[tokio::test]
    #[ignore = "necesita el bridge del ERP levantado y descarga las 26 paginas (~7 s)"]
    async fn descargar_contra_el_erp_vivo_y_comparar_con_el_snapshot() {
        let mut cliente = ClienteErp::desde_entorno();
        let descarga = cliente.descargar(8).await.expect("la descarga completa funciona");

        assert_eq!(descarga.meta.paginas, 26);
        assert_eq!(descarga.meta.asientos_descargados, 516);
        assert_eq!(descarga.meta.estado, "COMPLETO");
        assert_eq!(descarga.asientos.len(), 516 - 20, "los NIF vacios no entran");
        assert_eq!(descarga.avisos.len(), 20);
        assert_eq!(clave_factura(&descarga.asientos[0]), "P002|PO-2026-0084|2026-03-21|6199.54");
    }
}
