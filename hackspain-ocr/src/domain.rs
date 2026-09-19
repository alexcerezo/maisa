// Tipos de dominio compartidos: Factura, Asiento, Evidencia, Decision.
//
// Están alineados a propósito con los subdocumentos `factura` / `evidencia` /
// `decision` del documento `expedientes` (ver diseño_logico.md §2), de modo que
// se puedan serializar a BSON sin traducciones intermedias.
// La lógica de decisión vive en `rules.rs` (spec_y_plan.md §1.1 y §3.5).
//
// **Normalización de identidad.** Las funciones `normalizar_texto` /
// `normalizar_clave` y el tipo `Nif` viven aquí, no en el reconciliador, porque
// responder a "¿es esto el mismo proveedor?" es una pregunta de dominio y no de
// conciliación. Si vivieran en `reconciler`, `rules` tendría que depender de él
// (y `reconciler` ya depende de `rules`) para poder comparar un NIF contra la
// lista de pago prohibido. Estando aquí, cualquier módulo compara identidad sin
// conocer a los demás, y no existe una sola forma "ad-hoc" de normalizar.

use std::collections::BTreeMap;
use std::fmt;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Normalización de identidad
// ---------------------------------------------------------------------------

/// Pasa un carácter a su equivalente ASCII en mayúsculas.
///
/// La conversión es Unicode (`to_uppercase`, no `to_ascii_uppercase`) porque el
/// ERP devuelve ISO-8859-1 y las facturas vienen con acentos y eñes: hay que
/// pasar por `ó` → `Ó` → `O`.
fn a_ascii_mayus(c: char) -> char {
    let mayuscula = c.to_uppercase().next().unwrap_or(c);
    match mayuscula {
        'Á' => 'A',
        'É' => 'E',
        'Í' => 'I',
        'Ó' => 'O',
        'Ú' => 'U',
        'Ü' => 'U',
        'Ñ' => 'N',
        'Ç' => 'C',
        otro => otro,
    }
}

/// Texto normalizado **para comparar o para mostrar**: sin acentos, en
/// mayúsculas y con los espacios colapsados.
/// `"  Factura  nº 12 "` → `"FACTURA Nº 12"`.
pub fn normalizar_texto(s: &str) -> String {
    let mut salida = String::with_capacity(s.len());
    let mut espacio_pendiente = false;
    for c in s.chars() {
        if c.is_whitespace() {
            espacio_pendiente = !salida.is_empty();
            continue;
        }
        if espacio_pendiente {
            salida.push(' ');
            espacio_pendiente = false;
        }
        salida.push(a_ascii_mayus(c));
    }
    salida
}

/// Clave de identidad **para emparejar**: sin acentos, sin separadores, en
/// mayúsculas. `"PED-00123"`, `"ped 00123"` y `"PED/00123"` colapsan a
/// `"PED00123"`. Lo mismo vale para NIFs: `"b-12345678"` → `"B12345678"`.
///
/// Aviso: al descartar los separadores, `PED-1-23` y `PED-12-3` colapsan al
/// mismo valor. Una colisión así se manifiesta como ambigüedad (varios
/// candidatos) y el reconciliador la convierte en conflicto → ESCALAR, nunca en
/// un match silencioso contra el asiento equivocado.
pub fn normalizar_clave(s: &str) -> String {
    s.chars()
        .map(a_ascii_mayus)
        .filter(|c| c.is_alphanumeric())
        .collect()
}

// ---------------------------------------------------------------------------
// Identificadores
// ---------------------------------------------------------------------------

/// NIF/CIF **canónico**: sin acentos, en mayúsculas y sin separadores.
///
/// Es la única puerta por la que entra un identificador fiscal al sistema, así
/// que dos NIFs escritos distinto son, por construcción, el mismo valor:
/// `"b-12345678"`, `"B 12345678"` y `"B12345678"` colapsan al mismo `Nif`.
///
/// Esto es lo que hace **imposible escribir mal** la lista
/// `prohibido_pagar_proveedor` de `config/reglas.toml`: la lista no se puede
/// cargar con un NIF sin normalizar. Antes, un solo guion (`"B-12345678"`)
/// desactivaba la regla en silencio, porque la comparación era entre cadenas
/// crudas mientras el reconciliador sí normalizaba: las dos capas no coincidían
/// sobre qué era "el mismo proveedor". Ahora hay una sola definición.
///
/// Normalizar **no** es validar: aquí solo se unifica la forma. Comprobar la
/// letra de control del NIF/CIF —y con ello cazar erratas del OCR como `8` ↔ `B`—
/// es trabajo de `validators.rs`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Nif(String);

impl Nif {
    /// Canoniza un NIF en bruto. Devuelve `None` si no queda ningún carácter
    /// alfanumérico (cadena vacía, solo guiones, solo espacios...).
    pub fn nuevo(bruto: &str) -> Option<Self> {
        let canonico = normalizar_clave(bruto);
        if canonico.is_empty() {
            None
        } else {
            Some(Self(canonico))
        }
    }

    /// El NIF ya canónico, tal y como se guarda y se compara.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Nif {
    type Error = String;

    fn try_from(bruto: String) -> Result<Self, String> {
        Self::nuevo(&bruto).ok_or_else(|| {
            format!("NIF sin caracteres alfanuméricos (no es un proveedor identificable): {bruto:?}")
        })
    }
}

impl From<Nif> for String {
    fn from(nif: Nif) -> String {
        nif.0
    }
}

impl AsRef<str> for Nif {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Nif {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

// ---------------------------------------------------------------------------
// Procedencia: el crudo y el normalizado, en el mismo sitio
// ---------------------------------------------------------------------------
//
// Toda extracción guarda **las dos caras** del dato: el texto tal cual se leyó
// (el crudo) y el valor canónico en que se convirtió (el normalizado). No son
// dos copias que puedan divergir, sino un solo valor con dos vistas, y viajan
// juntas para que no exista forma de tener una sin la otra.
//
// Para qué sirve el crudo: sin él, una decisión mala es indistinguible de un OCR
// malo, de una normalización mala o de una regla mala, porque los tres caminos
// acaban en el mismo valor canónico. Con él, la traza dice *en qué paso* se
// torció la cosa, que es lo que convierte la auditoría en diagnóstico.

/// Puntero a la localización exacta de un valor dentro de su fuente.
///
/// Es lo que permite que una traza no diga solo *qué* se leyó, sino *dónde*: se
/// puede volver a la línea del OCR y resaltarla, o al asiento del ERP, sin
/// volver a ejecutar el parser.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE", tag = "fuente")]
pub enum Origen {
    /// Línea `linea` (0-based) de la página `pagina` (0-based) de
    /// `expedientes.ocr.lineas`.
    Ocr { pagina: u32, linea: u32 },
    /// Fila `fila` (`Hoja1#42`) del Excel, en la columna `columna`.
    Excel { fila: String, columna: String },
    /// Asiento `asiento_id` del ERP.
    Erp { asiento_id: String },
}

/// Lo que se leyó, con qué confianza y de dónde salió.
///
/// El `crudo` se guarda **sin normalizar** a propósito: es la prueba.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Rastro {
    /// Texto tal cual lo entregó la fuente.
    pub crudo: String,
    /// Confianza de la fuente en ese campo (0.0..=1.0). El ERP y el Excel
    /// entregan `1.0`: no adivinan, transcriben.
    pub score: f64,
    /// Localización exacta. `None` cuando el valor se compuso a partir de varios
    /// fragmentos y ninguna línea concreta lo contiene.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origen: Option<Origen>,
}

impl Rastro {
    pub fn nuevo(crudo: impl Into<String>, score: f64, origen: Option<Origen>) -> Self {
        Self {
            crudo: crudo.into(),
            score,
            origen,
        }
    }
}

/// Qué se pudo hacer con un campo.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EstadoCampo {
    /// Se leyó y se canonizó.
    Encontrado,
    /// La fuente no lo trae (o el OCR no vio nada ahí).
    NoAparece,
    /// Hay texto, pero no se pudo canonizar.
    Ilegible,
}

/// Un campo extraído: **el crudo y el normalizado en un único valor**.
///
/// Los campos son privados para que la única forma de construir un
/// `Identificador` sea una de sus tres funciones, y estados como "hay valor pero
/// no hay rastro" no se puedan escribir.
///
/// Es un **tri-estado** a propósito. Un `Option<T>` confunde "la factura no trae
/// NIF" con "el OCR leyó algo que parece un NIF, pero no se pudo canonizar", y
/// esa diferencia cambia la decisión: lo primero es un caso normal (la factura
/// se concilia por pedido), lo segundo obliga a escalar, porque no se puede
/// garantizar *a quién* se está pagando.
///
/// Lo que no existe **no se escribe como `null`**: se omite. Un
/// `{estado: "NO_APARECE", valor: null}` es indistinguible, para un índice y
/// para una agregación, de un `{estado: "ILEGIBLE"}` mal escrito; omitir deja
/// `factura.nif_emisor.valor` inexistente, que es lo que de verdad significa.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "IdentificadorCrudo<T>")]
pub struct Identificador<T> {
    estado: EstadoCampo,
    #[serde(skip_serializing_if = "Option::is_none")]
    valor: Option<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    rastro: Option<Rastro>,
}

impl<T> Identificador<T> {
    /// Se leyó y se canonizó. `score` es la confianza de la fuente y `origen` la
    /// línea, celda o asiento del que salió.
    ///
    /// Aún no se usa fuera de los tests: es la puerta por la que `parser.rs`
    /// entregará cada campo extraído, y por eso se define aquí y no allí — el
    /// contrato de "crudo + canónico + confianza + ubicación" es del dominio, no
    /// del OCR.
    #[allow(dead_code)]
    pub fn encontrado(
        valor: T,
        crudo: impl Into<String>,
        score: f64,
        origen: Option<Origen>,
    ) -> Self {
        Self {
            estado: EstadoCampo::Encontrado,
            valor: Some(valor),
            rastro: Some(Rastro::nuevo(crudo, score, origen)),
        }
    }

    /// Hay texto, pero no se pudo canonizar (p. ej. una letra de control que no
    /// cuadra: decidirlo es trabajo de `validators.rs`).
    ///
    /// Guarda el crudo igual que un acierto: es justo el texto que hay que
    /// enseñarle a quien revise el caso.
    ///
    /// Aún no se usa fuera de los tests: lo usará `validators.rs`, que es quien
    /// sabe si un carácter de control cuadra o no.
    #[allow(dead_code)]
    pub fn ilegible(crudo: impl Into<String>, score: f64, origen: Option<Origen>) -> Self {
        Self {
            estado: EstadoCampo::Ilegible,
            valor: None,
            rastro: Some(Rastro::nuevo(crudo, score, origen)),
        }
    }

    /// La fuente no trae el campo. No es un fallo: es información.
    pub fn no_aparece() -> Self {
        Self {
            estado: EstadoCampo::NoAparece,
            valor: None,
            rastro: None,
        }
    }

    pub fn estado(&self) -> EstadoCampo {
        self.estado
    }

    /// El valor canónico: lo único que comparan el reconciliador y el motor.
    pub fn valor(&self) -> Option<&T> {
        self.valor.as_ref()
    }

    /// El rastro, tanto si el campo se leyó como si resultó ilegible.
    ///
    /// Lo usará `obs.rs` para volcar el detalle del campo en un evento.
    #[allow(dead_code)]
    pub fn rastro(&self) -> Option<&Rastro> {
        self.rastro.as_ref()
    }

    /// El texto del que salió el valor, **o** el texto que no se pudo leer.
    pub fn crudo(&self) -> Option<&str> {
        self.rastro.as_ref().map(|r| r.crudo.as_str())
    }

    pub fn score(&self) -> Option<f64> {
        self.rastro.as_ref().map(|r| r.score)
    }

    pub fn origen(&self) -> Option<&Origen> {
        self.rastro.as_ref().and_then(|r| r.origen.as_ref())
    }

    /// ¿Hay valor canónico con el que trabajar?
    pub fn aparece(&self) -> bool {
        self.estado == EstadoCampo::Encontrado
    }

    /// ¿Se leyó texto, pero no se pudo canonizar?
    pub fn es_ilegible(&self) -> bool {
        self.estado == EstadoCampo::Ilegible
    }
}

impl<T> Default for Identificador<T> {
    /// Un campo ausente es `NoAparece`, no un hueco con `None` dentro.
    fn default() -> Self {
        Self::no_aparece()
    }
}

/// Espejo de [`Identificador`] para lo que llega de fuera (JSON o BSON).
///
/// Existe para que un documento con `estado: ENCONTRADO` y sin `valor` —o al
/// revés— no entre en el sistema: se rechaza al deserializar, en vez de viajar
/// hasta el motor como un campo "encontrado" que no tiene valor.
///
/// Los dos campos ausentes se leen como `None` (serde ya trata los `Option` como
/// opcionales), y esa combinación es la que hace que la comprobación de coherencia
/// tenga algo que rechazar.
#[derive(Deserialize)]
struct IdentificadorCrudo<T> {
    estado: EstadoCampo,
    valor: Option<T>,
    rastro: Option<Rastro>,
}

impl<T> TryFrom<IdentificadorCrudo<T>> for Identificador<T> {
    type Error = String;

    fn try_from(espejo: IdentificadorCrudo<T>) -> Result<Self, String> {
        match (espejo.estado, espejo.valor, espejo.rastro) {
            (EstadoCampo::Encontrado, Some(valor), Some(rastro)) => Ok(Self {
                estado: EstadoCampo::Encontrado,
                valor: Some(valor),
                rastro: Some(rastro),
            }),
            (EstadoCampo::Ilegible, None, Some(rastro)) => Ok(Self {
                estado: EstadoCampo::Ilegible,
                valor: None,
                rastro: Some(rastro),
            }),
            (EstadoCampo::NoAparece, None, None) => Ok(Self::no_aparece()),
            (estado, valor, rastro) => Err(format!(
                "campo incoherente: estado {estado:?} con valor {} y rastro {}",
                if valor.is_some() { "presente" } else { "ausente" },
                if rastro.is_some() { "presente" } else { "ausente" }
            )),
        }
    }
}

/// Estado de un asiento en el ERP.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EstadoAsiento {
    /// Aún no liquidado: procede pagarlo si el importe concilia.
    Pendiente,
    /// Ya liquidado: volver a pagarlo sería duplicar el pago.
    Pagada,
}

/// Asiento contable del ERP. Es la **fuente de verdad** de la conciliación.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Asiento {
    pub asiento_id: String,
    /// NIF del proveedor, ya canónico. `None` cuando el ERP no lo trae.
    ///
    /// **No es un caso hipotético**: el catálogo real trae 20 de 516 asientos
    /// con el NIF en blanco (2 por cada uno de los 10 proveedores, todos
    /// PENDIENTE). Se representan como `None` en vez de descartar el asiento
    /// porque el asiento *sí* existe: tiene pedido, importe y estado, y sirve
    /// para conciliar por pedido. Tirarlo convertía una factura legítima en un
    /// `sin_match` indistinguible de "el ERP no tiene esta factura", que es
    /// una mentira sobre la evidencia.
    ///
    /// Lo que `None` **no** hace es autorizar el pago: sin NIF no se puede
    /// comprobar el proveedor contra la lista de pago prohibido, así que el
    /// motor escala con motivo explícito (regla `R10` en `rules.rs`). El tipo
    /// sigue obligando a tratar la fila sucia de forma explícita; lo que cambia
    /// es la respuesta: de "descartar en silencio" a "escalar diciendo por qué".
    ///
    /// Efecto colateral valioso que se conserva: cuando el asiento *sí* trae
    /// NIF, la regla `R2` puede verificar la prohibición de pago incluso si el
    /// PDF no trajo NIF, y el agujero "PDF sin NIF → paga a proveedor
    /// prohibido" queda cerrado sin ninguna regla extra.
    #[serde(default)]
    pub nif: Option<Nif>,
    pub pedido: String,
    pub importe: Decimal,
    pub estado: EstadoAsiento,
    #[serde(default)]
    pub proveedor: Option<String>,
    #[serde(default)]
    pub fecha: Option<String>,
}

/// Campos extraídos del PDF por el parser: el valor canónico **y** su crudo.
///
/// Cada campo es un [`Identificador`], así que la factura no tiene dos
/// representaciones que puedan divergir: la comparación y la decisión usan
/// `valor()`, y la auditoría usa `crudo()`, `score()` y `origen()`, sobre el
/// mismo dato.
///
/// Antes había aquí un mapa `scores` aparte, indexado por nombre de campo. Se ha
/// eliminado a propósito: era una segunda estructura paralela que había que
/// mantener sincronizada a mano con los campos de verdad, y una errata en la
/// clave se perdía en silencio. Ahora la confianza vive dentro del campo, que es
/// donde no se puede perder.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct Factura {
    /// NIF del emisor. `NoAparece` es legítimo (la factura puede no traerlo, o
    /// el OCR no verlo); `Ilegible` no lo es y fuerza una revisión humana (R5).
    ///
    /// Hoy `Ilegible` solo sale cuando no hay ningún token que canonizar. El
    /// dígito de control **no** se comprueba, porque los CIF del corpus son
    /// sintéticos y no lo respetan: ver `parser::EXIGIR_DIGITO_DE_CONTROL_NIF`.
    #[serde(default)]
    pub nif_emisor: Identificador<Nif>,
    /// CIF/NIF del **cliente** (destinatario), tal y como lo imprime la factura.
    ///
    /// Un documento fiscal lleva los dos identificadores, el del emisor y el del
    /// destinatario, y hasta ahora el del cliente se descartaba. Es el mismo tipo
    /// de dato que `nif_emisor`, pero **tampoco** se le exige el dígito de
    /// control: en estas facturas el cliente es el propio banco y su CIF no lo
    /// pasa, así que aplicar `validators::nif_valido` degradaría a `Ilegible` un
    /// dato leído perfectamente y mandaría a revisión facturas correctas. (El
    /// emisor tampoco lo comprueba hoy, por otro motivo: los CIF del corpus son
    /// sintéticos. Ver `parser::EXIGIR_DIGITO_DE_CONTROL_NIF`.)
    ///
    /// Tampoco entra en la conciliación —quien cobra es `nif_emisor`—: es
    /// trazabilidad fiscal. Lo que sí hace es bloquear el pago automático cuando
    /// no aparece (R1 bis), porque sin él la factura no está completa.
    #[serde(default)]
    pub cif_cliente: Identificador<Nif>,
    #[serde(default)]
    pub pedido: Identificador<String>,
    #[serde(default)]
    pub numero_factura: Identificador<String>,
    /// Fecha de emisión (ISO `AAAA-MM-DD`); el pipeline la convierte a BSON Date.
    #[serde(default)]
    pub fecha: Identificador<String>,
    #[serde(default)]
    pub base: Identificador<Decimal>,
    #[serde(default)]
    pub iva: Identificador<Decimal>,
    /// Importe total de la factura: el valor que se concilia contra el ERP.
    #[serde(default)]
    pub total: Identificador<Decimal>,
}

impl Factura {
    /// ¿No hay ningún identificador con el que conciliar? (es lo que mira R1).
    ///
    /// Un campo ilegible cuenta como no utilizable: si no se pudo canonizar el
    /// NIF, no sirve para identificar al proveedor.
    pub fn sin_identificadores(&self) -> bool {
        !self.nif_emisor.aparece() && !self.pedido.aparece()
    }

    /// ¿Falta el CIF del cliente? (es lo que mira R1 bis).
    ///
    /// Cuentan los dos estados en los que el campo no sirve: `NoAparece` (la
    /// plantilla no lo imprime) e `Ilegible` (está impreso pero no se pudo leer).
    /// En ambos la factura está incompleta como documento fiscal, así que
    /// ninguno puede pagarse solo. El tri-estado no se pierde por eso: quién
    /// revisa ve en el campo **cuál** de los dos es, y R1 bis lo dice en el
    /// motivo, que es justo para lo que existe.
    ///
    /// El NIF del emisor **no** tiene un chequeo equivalente a propósito: R2 lo
    /// respalda con el NIF del asiento del ERP y `sin_identificadores()` cubre el
    /// caso en que no hay nada con lo que conciliar. Exigirlo aquí convertiría en
    /// ESCALAR toda factura sin NIF impreso, que es un caso real y ya decidido.
    pub fn sin_cif_cliente(&self) -> bool {
        !self.cif_cliente.aparece()
    }

    /// Los campos críticos como vistas uniformes.
    ///
    /// Son los tres campos de los que depende poder decidir sin mirar: quién
    /// cobra (NIF), por qué concepto (pedido) y cuánto (total). Se exponen como
    /// [`VistaCampo`] para que quien los recorra (el motor de reglas y `obs.rs`)
    /// no tenga que conocer los tres tipos concretos ni repetir a mano la lista,
    /// que es justo lo que se desincroniza cuando alguien añade un campo.
    pub fn campos_criticos(&self) -> [VistaCampo<'_>; 3] {
        [
            VistaCampo::nuevo("nif_emisor", &self.nif_emisor),
            VistaCampo::nuevo("pedido", &self.pedido),
            VistaCampo::nuevo("total", &self.total),
        ]
    }
}

/// Vista uniforme de un campo crítico, sin su tipo concreto dentro.
///
/// No es un envoltorio de datos, sino una ventana de solo lectura: el valor
/// canónico no se expone (para eso está el `Identificador` original), solo lo
/// necesario para juzgar la calidad de la lectura y para contarla en un evento.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VistaCampo<'a> {
    pub nombre: &'static str,
    pub estado: EstadoCampo,
    pub score: Option<f64>,
    pub crudo: Option<&'a str>,
    pub origen: Option<&'a Origen>,
}

impl<'a, T> From<(&'static str, &'a Identificador<T>)> for VistaCampo<'a> {
    fn from((nombre, campo): (&'static str, &'a Identificador<T>)) -> Self {
        Self {
            nombre,
            estado: campo.estado,
            score: campo.score(),
            crudo: campo.crudo(),
            origen: campo.origen(),
        }
    }
}

impl<'a> VistaCampo<'a> {
    fn nuevo<T>(nombre: &'static str, campo: &'a Identificador<T>) -> Self {
        (nombre, campo).into()
    }

    /// Dónde estaba, en texto para la traza (`pág. 1 línea 7`, `Hoja1#42:D`...).
    pub fn ubicacion(&self) -> String {
        match self.origen {
            Some(Origen::Ocr { pagina, linea }) => {
                format!("pág. {} línea {}", pagina + 1, linea + 1)
            }
            Some(Origen::Excel { fila, columna }) => format!("{fila}:{columna}"),
            Some(Origen::Erp { asiento_id }) => format!("asiento {asiento_id}"),
            None => String::new(),
        }
    }
}

/// Fila del Excel caótico, ya normalizada por `excel.rs`.
///
/// El Excel es **contexto, no verdad**: solo sirve para aportar filas de apoyo
/// y para detectar contradicciones que fuercen un ESCALAR.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FilaExcel {
    /// Etiqueta de origen legible (`Hoja1#42`).
    pub id: String,
    /// NIF ya canónico. `None` es normal: el Excel es caótico y muchas filas no
    /// traen proveedor identificable.
    #[serde(default)]
    pub nif: Option<Nif>,
    #[serde(default)]
    pub pedido: Option<String>,
    #[serde(default)]
    pub importe: Option<Decimal>,
    /// Estado tal cual aparece en la hoja (`PAGADA`, `PENDIENTE`, ...).
    #[serde(default)]
    pub estado: Option<String>,
    /// La fila entera, tal cual, con la cabecera como clave.
    ///
    /// Los campos de arriba son la proyección normalizada; esto es el volcado
    /// sin pérdida de §3.7. El Excel es la fuente más caótica de las tres, y no
    /// se sabe de antemano qué columnas trae: guardar solo lo que hoy sabemos
    /// leer significaría perder para siempre el dato que mañana haga falta.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub crudo: BTreeMap<String, String>,
}

/// Estrategia con la que el reconciliador asoció un asiento a la factura.
/// Se guarda para que cualquier decisión sea reconstruible.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MatchStrategy {
    ExactByPedido,
    ByNifYImporte,
    ByNifUnico,
    None,
}

/// Resultado de la conciliación PDF ↔ ERP ↔ Excel: todo lo que el motor de
/// reglas necesita para decidir, más las discrepancias detectadas.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Evidencia {
    /// Copia embebida del asiento en el momento de decidir (auditabilidad).
    #[serde(default)]
    pub asiento: Option<Asiento>,
    /// Referencia al asiento del catálogo (`AS-00412`).
    #[serde(default)]
    pub asiento_id: Option<String>,
    /// Filas del Excel relacionadas (`Hoja1#42`). Es contexto, no verdad.
    #[serde(default)]
    pub excel_filas: Vec<String>,
    pub match_por: MatchStrategy,
    /// Discrepancias legibles, p. ej. "Excel marca PAGADA pero ERP PENDIENTE".
    #[serde(default)]
    pub conflictos: Vec<String>,
}

/// Las tres decisiones posibles del contrato JSONL.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Resultado {
    Pagar,
    NoPagar,
    Escalar,
}

impl Resultado {
    /// Literal exacto que va al JSONL de entrega.
    pub fn as_str(self) -> &'static str {
        match self {
            Resultado::Pagar => "PAGAR",
            Resultado::NoPagar => "NO_PAGAR",
            Resultado::Escalar => "ESCALAR",
        }
    }
}

/// Huellas de bitemporalidad: permiten saber con qué estado del mundo se tomó
/// la decisión y detectar cuáles quedaron obsoletas (INV-9).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Huellas {
    /// Versión de `reglas.toml` aplicada.
    pub reglas_version: u32,
    /// Snapshot del ERP usado.
    pub erp_snapshot_id: String,
    /// Ejecución que produjo la decisión.
    pub run_id: String,
}

/// Decisión final registrada en el expediente.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Decision {
    pub resultado: Resultado,
    /// Explicación legible para el operador (INV-6: nunca vacío).
    pub motivo: String,
    /// Reglas evaluadas, en orden, por su nombre en el código.
    pub reglas_evaluadas: Vec<String>,
    #[serde(default)]
    pub coste_estimado_cents: u32,
    #[serde(default)]
    pub timings_ms: BTreeMap<String, u32>,
    pub huellas: Huellas,
}

impl Decision {
    /// Construye una decisión sin métricas; el pipeline rellena coste y timings.
    pub fn new(
        resultado: Resultado,
        motivo: impl Into<String>,
        reglas_evaluadas: Vec<String>,
        huellas: Huellas,
    ) -> Self {
        Self {
            resultado,
            motivo: motivo.into(),
            reglas_evaluadas,
            coste_estimado_cents: 0,
            timings_ms: BTreeMap::new(),
            huellas,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).expect("decimal válido")
    }

    fn nif(s: &str) -> Nif {
        Nif::nuevo(s).expect("NIF de prueba válido")
    }

    fn origen() -> Origen {
        Origen::Ocr {
            pagina: 0,
            linea: 1,
        }
    }

    /// El crudo y el canónico son el mismo dato: no hay forma de tener uno sin
    /// el otro, ni de que se desincronicen.
    #[test]
    fn un_campo_encontrado_guarda_crudo_y_valor_juntos() {
        let campo = Identificador::encontrado(nif("B-12345678"), "NIF: B-12345678", 0.98, Some(origen()));

        assert_eq!(campo.estado(), EstadoCampo::Encontrado);
        assert!(campo.aparece());
        assert!(!campo.es_ilegible());
        assert_eq!(campo.valor(), Some(&nif("B12345678")));
        // El crudo conserva el guion que el canónico ha eliminado: ahí está
        // justamente la prueba de que la normalización ocurrió.
        assert_eq!(campo.crudo(), Some("NIF: B-12345678"));
        assert_eq!(campo.score(), Some(0.98));
        assert_eq!(campo.origen(), Some(&origen()));
    }

    /// Un campo ilegible **no** es un campo ausente: guarda su texto y su score,
    /// que es lo que hay que enseñarle a quien revise el caso.
    #[test]
    fn un_campo_ilegible_conserva_su_texto_pero_ningun_valor() {
        let campo = Identificador::<Nif>::ilegible("NIF: B1234567B", 0.62, Some(origen()));

        assert_eq!(campo.estado(), EstadoCampo::Ilegible);
        assert!(campo.es_ilegible());
        assert!(!campo.aparece());
        assert_eq!(campo.valor(), None);
        assert_eq!(campo.crudo(), Some("NIF: B1234567B"));
        assert_eq!(campo.score(), Some(0.62));
    }

    #[test]
    fn un_campo_ausente_no_tiene_ni_crudo_ni_score() {
        let campo = Identificador::<Nif>::no_aparece();

        assert_eq!(campo.estado(), EstadoCampo::NoAparece);
        assert!(!campo.aparece());
        assert!(!campo.es_ilegible());
        assert_eq!(campo.valor(), None);
        assert_eq!(campo.crudo(), None);
        assert_eq!(campo.score(), None, "no se juzga un campo que no se leyó");
        assert_eq!(campo.origen(), None);
    }

    /// Lo que no existe se **omite**, no se escribe como `null`.
    ///
    /// Este es el contrato con Mongo: `ix_nif` / `ix_pedido` son índices
    /// dispersos sobre `factura.*.valor`, y solo no guardan basura si un campo
    /// ausente no llega a escribir la clave. Si `NO_APARECE` emitiera
    /// `"valor": null`, el índice se llenaría de nulos y "¿qué facturas son de
    /// este proveedor?" devolvería facturas sin proveedor.
    #[test]
    fn un_campo_ausente_no_se_escribe_como_null() {
        let ausente = serde_json::to_value(Identificador::<Nif>::no_aparece()).expect("serializa");
        assert_eq!(ausente, serde_json::json!({ "estado": "NO_APARECE" }));

        // Ilegible sí tiene rastro (es la prueba de lo que se leyó), pero sigue
        // sin valor canónico: por eso tampoco entra en el índice.
        let ilegible = serde_json::to_value(Identificador::<Nif>::ilegible("B1234567B", 0.62, None))
            .expect("serializa");
        assert_eq!(
            ilegible,
            serde_json::json!({
                "estado": "ILEGIBLE",
                "rastro": { "crudo": "B1234567B", "score": 0.62 }
            }),
            "un rastro sin ubicación tampoco escribe `origen`"
        );

        let encontrado =
            serde_json::to_value(Identificador::encontrado(nif("B-12345678"), "B-12345678", 1.0, None))
                .expect("serializa");
        assert_eq!(
            encontrado,
            serde_json::json!({
                "estado": "ENCONTRADO",
                "valor": "B12345678",
                "rastro": { "crudo": "B-12345678", "score": 1.0 }
            }),
            "el valor es el canónico; el crudo con guion se conserva al lado"
        );
    }

    /// El crudo viaja con el documento: la traza se puede auditar sin volver a
    /// ejecutar el parser.
    #[test]
    fn el_rastro_sobrevive_a_la_ida_y_vuelta_json() {
        let campo = Identificador::encontrado(d("1234.50"), "TOTAL: 1.234,50 EUR", 0.97, Some(origen()));

        let json = serde_json::to_string(&campo).expect("serializa");
        let vuelta: Identificador<Decimal> = serde_json::from_str(&json).expect("deserializa");

        assert_eq!(campo, vuelta);
        assert_eq!(vuelta.crudo(), Some("TOTAL: 1.234,50 EUR"));
    }

    /// Un payload con `ENCONTRADO` pero sin valor se rechaza al entrar, en vez
    /// de llegar al motor como un campo "encontrado" que no tiene nada dentro.
    #[test]
    fn un_campo_incoherente_se_rechaza_al_deserializar() {
        let json = r#"{"estado": "ENCONTRADO"}"#;
        assert!(
            serde_json::from_str::<Identificador<Nif>>(json).is_err(),
            "ENCONTRADO sin valor ni rastro no puede ser un campo válido"
        );

        let json = r#"{"estado": "NO_APARECE", "valor": "B12345678"}"#;
        assert!(
            serde_json::from_str::<Identificador<Nif>>(json).is_err(),
            "NO_APARECE con valor dentro es contradictorio"
        );
    }

    #[test]
    fn sin_identificadores_cuenta_lo_ilegible_como_no_utilizable() {
        let vacia = Factura {
            ..Default::default()
        };
        assert!(vacia.sin_identificadores());

        let con_nif_ilegible = Factura {
            nif_emisor: Identificador::ilegible("NIF: B1234567B", 0.62, None),
            ..Default::default()
        };
        assert!(
            con_nif_ilegible.sin_identificadores(),
            "un NIF que no se pudo canonizar no identifica a nadie"
        );

        let con_pedido = Factura {
            nif_emisor: Identificador::ilegible("NIF: B1234567B", 0.62, None),
            pedido: Identificador::encontrado("PED-00123".to_string(), "Pedido: PED-00123", 0.96, None),
            ..Default::default()
        };
        assert!(
            !con_pedido.sin_identificadores(),
            "el pedido basta para poder conciliar"
        );
    }

    #[test]
    fn los_campos_criticos_distingen_ausente_de_ilegible_y_de_poco_fiable() {
        let factura = Factura {
            nif_emisor: Identificador::ilegible("NIF: B1234567B", 0.62, Some(origen())),
            pedido: Identificador::encontrado("PED-00123".to_string(), "Pedido: PED-00123", 0.40, None),
            total: Identificador::no_aparece(),
            ..Default::default()
        };

        let criticos = factura.campos_criticos();

        assert_eq!(criticos[0].nombre, "nif_emisor");
        assert_eq!(criticos[0].score, Some(0.62), "el score se midió y se guarda");
        assert_eq!(criticos[0].crudo, Some("NIF: B1234567B"));
        assert_eq!(criticos[0].ubicacion(), "pág. 1 línea 2", "1-based para humanos");
        assert_eq!(criticos[0].estado, EstadoCampo::Ilegible);

        assert_eq!(criticos[1].nombre, "pedido");
        assert_eq!(criticos[1].estado, EstadoCampo::Encontrado);

        assert_eq!(criticos[2].nombre, "total");
        assert_eq!(criticos[2].estado, EstadoCampo::NoAparece);
        assert_eq!(criticos[2].score, None, "ausente no es un score de cero");
        assert_eq!(criticos[2].ubicacion(), "");
    }

    /// `Factura` y `FilaExcel` tienen que seguir aceptando los documentos que ya
    /// existen, escritos antes de que hubiera rastro.
    #[test]
    fn los_documentos_antiguos_siguen_cargando() {
        let factura: Factura = serde_json::from_str("{}").expect("una factura sin campos es válida");
        assert!(factura.sin_identificadores());

        let fila: FilaExcel =
            serde_json::from_str(r#"{"id": "Hoja1#42", "pedido": "PED-00123"}"#)
                .expect("fila sin volcado crudo");
        assert!(fila.crudo.is_empty(), "sin crudo no es un error: es un hueco");
        assert_eq!(fila.pedido.as_deref(), Some("PED-00123"));
    }
}
