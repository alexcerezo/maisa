// De líneas de OCR a `Factura` (regex + anclas de layout).
//
// Ver TRASPASO.md §3.1/§3.2, spec_y_plan.md §3.2/Bloque 3 y
// _scratch/ERP-RUST-CONTRATO.md §4.2.
//
// ## Lo que este módulo decide y lo que no
//
// **No decide nada.** No compara contra el ERP, no aplica reglas y no descarta
// facturas: para cada uno de los ocho campos de `Factura` dice una de tres
// cosas (`encontrado` / `ilegible` / `no_aparece`) y en las tres conserva el
// texto del que salió. Quien juzga es `rules.rs` (R1..R9) y quien compara
// importes es `reconciler.rs`.
//
// ## Procedencia (lo que hace la traza auditable)
//
// * `crudo` es **la línea de OCR entera tal cual llegó**, sin normalizar: es la
//   prueba. Los espacios de ancho cero y el mojibake de página de códigos se
//   reparan **solo para buscar**, no para guardar, para que una revisión vea lo
//   que el OCR entregó de verdad.
// * `origen` es `Origen::Ocr { pagina, linea }`, con la página 0-based que trae
//   `LineaOcr` y el índice **0-based de la línea dentro del `&[LineaOcr]`** (el
//   único índice que el parser puede conocer: la lista llega plana). Si un dato
//   se compone de dos líneas, `origen` es `None`, porque ninguna línea sola lo
//   contiene (ver `domain.rs`).
// * `score` es el de la línea de la que salió el dato. Si se combinan dos
//   líneas, el **mínimo**: un dato no puede ser más fiable que su eslabón peor.
//   Nunca se inventa, se redondea ni se pone un valor por defecto.
//
// ## Total
//
// Se paga **lo impreso**. Un recargo financiero puede dejar `TOTAL ≠ base+IVA`
// y aquí no se recalcula nada: se toma el último número de la línea de total.
//
// ## Identificadores fiscales
//
// Se extraen los **dos** identificadores del documento, y cada uno por su
// camino:
//
// * `nif_emisor` es el del emisor, y es el que decide a quién se paga. El del
//   cliente **no** sirve para eso, así que las líneas del cliente se saltan al
//   buscarlo. Un NIF que parece un NIF pero no pasa el dígito de control es
//   `ilegible`, no `encontrado` (regla R5); ver `validators::nif_valido`.
//
// * `cif_cliente` es el del **cliente**, y se busca **solo** en sus líneas. Ahí
//   no se exige dígito de control —el CIF del banco no lo pasa— y por eso un
//   `encontrado` aquí **no** pesa en la decisión: es trazabilidad fiscal. Que no
//   aparezca sí bloquea el pago automático (regla R1 bis).

use std::str::FromStr;
use std::sync::OnceLock;

use regex::Regex;
use rust_decimal::Decimal;

use crate::domain::{Factura, Identificador, Nif, Origen};
use crate::ocr::LineaOcr;
use crate::validators::{fecha_es_a_iso, nif_valido};

// ---------------------------------------------------------------------------
// Normalización para BUSCAR (nunca para guardar)
// ---------------------------------------------------------------------------

/// Caracteres invisibles que algunos extractores cuelan dentro del texto
/// (espacio de ancho cero, joiner, BOM, guion blando, marcas de dirección).
const INVISIBLES: [char; 8] = [
    '\u{200b}', '\u{200c}', '\u{200d}', '\u{2060}', '\u{200e}', '\u{200f}', '\u{feff}', '\u{00ad}',
];

/// Deja una línea en condiciones de ser buscada: sin caracteres invisibles, con
/// el espacio duro (`\u{00a0}`) convertido en espacio normal y con el mojibake
/// de página de códigos reparado.
///
/// **No** se usa para el `crudo` (que se guarda tal cual): es la vista de trabajo
/// del parser. Tampoco pasa a mayúsculas, porque las expresiones regulares van
/// con `(?i)` y así el texto se toca lo mínimo.
pub fn normalizar_linea(texto: &str) -> String {
    texto
        .chars()
        .filter(|c| !INVISIBLES.contains(c))
        .map(reparar_caracter)
        .collect()
}

/// Repara los caracteres que una decodificación con la página de códigos
/// equivocada deja mal. El PDF trae el texto en una codepage DOS y, al leerlo
/// como Latin-1, `MENSAJERÍA` llega como `MENSAJER═A` y `RÁPIDA` como `R┴PIDA`.
///
/// Es un mapa fijo, no una heurística: cada entrada se ha visto en un PDF real
/// de `data/facturas/`. `Ç` se mapea a `€` porque es como aparece en las líneas
/// de importe (`1.047,51 Ç`); para el texto en prosa no cambia nada de lo que el
/// parser busca.
fn reparar_caracter(c: char) -> char {
    match c {
        '\u{00a0}' => ' ',
        '═' => 'Í',
        '┴' => 'Á',
        'Ê' => 'Í',
        'Ý' => 'í',
        'ß' => 'á',
        'Ú' => 'É',
        '§' => 'ç',
        '±' => 'ñ',
        'Ç' => '€',
        otro => otro,
    }
}

// ---------------------------------------------------------------------------
// Expresiones regulares (compiladas una sola vez por proceso)
// ---------------------------------------------------------------------------
//
// Todas son literales constantes, así que el `expect` no puede dispararse; está
// para no dejar un `unwrap` en el camino de producción.

fn re_nif_etiqueta() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)\b(?:N\.?\s?I\.?\s?F\.?|C\.?\s?I\.?\s?F\.?|D\.?\s?N\.?\s?I\.?|N\.?\s?I\.?\s?E\.?)\b")
            .expect("regex de etiqueta de NIF válida")
    })
}

/// Forma de un identificador fiscal: `B46102331`, `X1234567L`, `12345678Z`, y
/// también `B1234567B` (que *parece* un CIF, para poder degradarlo a ilegible).
fn re_forma_fiscal() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)\b(?:[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z])\b")
            .expect("regex de forma fiscal válida")
    })
}

/// Token alfanumérico inmediatamente posterior a una etiqueta; admite el guion
/// para capturar `B-12345678` entero y no solo la letra.
fn re_token() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)[A-Z0-9][A-Z0-9\-]*").expect("regex de token válida"))
}

fn re_pedido() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)\b(?:PED|PO|PE)[-\s]?\d{2,4}[-/]\d{3,4}\b")
            .expect("regex de pedido válida")
    })
}

/// Etiqueta de número de factura, incluyendo el marcador de número (`Nº`, `#`)
/// para que no se confunda con el identificador que viene detrás.
fn re_etiqueta_factura() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(
            r"(?i)\b(?:REF\s+FACTURA|N[º°o]\s+DE\s+FACTURA|N[º°o]\s+FACTURA|FACTURA\s+SIMPLIFICADA|FACTURA|INVOICE)\s*(?:N[º°o]\.?)?\s*[:#.]?\s*",
        )
        .expect("regex de etiqueta de factura válida")
    })
}

fn re_token_id() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)[A-Z0-9][A-Z0-9\-/]*").expect("regex de id válida"))
}

fn re_fecha_etiqueta() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)\bFECHA\b").expect("regex de etiqueta de fecha válida"))
}

fn re_fecha_es() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\b\d{1,2}/\d{1,2}/\d{4}\b").expect("regex de fecha válida"))
}

/// Línea de total: la etiqueta **al principio** de la línea, que es lo que evita
/// que la prosa del pie de factura («…no debe recalcularse como base mas IVA…»)
/// se cuele como un total. `(?m)` para que un `texto` con saltos embebidos
/// también se analice línea a línea.
fn re_etiqueta_total() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?im)^\s*(?:IMPORTE\s+TOTAL|TOTAL\s+A\s+PAGAR|TOTAL\s+FACTURA|TOTAL)\b")
            .expect("regex de etiqueta de total válida")
    })
}

fn re_etiqueta_base() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?im)^\s*(?:BASE\s+IMPONIBLE|IMPORTE\s+BASE|SUBTOTAL|BASE)\b")
            .expect("regex de etiqueta de base válida")
    })
}

fn re_etiqueta_iva() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?im)^\s*(?:CUOTA\s+IVA|I\.?\s*V\.?\s*A\.?)\b")
            .expect("regex de etiqueta de IVA válida")
    })
}

/// Importe: admite las dos convenciones (`2.967,25` y `1705.37`) y exige
/// separador decimal, porque esto solo se usa sobre base/IVA/total y un entero
/// suelto en esa línea (`21` de `IVA (21%)`, un año) no es un importe.
fn re_importe() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // El orden de las alternativas **es** significativo: la `regex` de Rust
        // es izquierda-primera, así que en `1.234` gana la alternativa de miles
        // si va antes que la de decimal. Con `\d+\.\d{1,2}` por delante, `1.234`
        // casaba como `1.23` (1234 → 1,23) y el total salía mal sin fallar nada.
        Regex::new(
            r"\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,3}(?:,\d{3})+\.\d{1,2}|\d{1,3}(?:\.\d{3})+|\d+,\d{1,2}|\d+\.\d{1,2}",
        )
        .expect("regex de importe válida")
    })
}

/// `\.\d{1,2}$`, el mismo criterio que usa el puerto de importes del ERP.
fn re_decimal_ingles() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\.\d{1,2}$").expect("regex decimal válida"))
}

// ---------------------------------------------------------------------------
// API pública
// ---------------------------------------------------------------------------

/// Extrae una `Factura` de las líneas de OCR de **un** documento.
///
/// Es una función total: para cualquier entrada devuelve una `Factura` (como
/// mucho, toda ella `no_aparece`). No hace E/S, no conoce reglas y no puede
/// tumbar el lote por una factura rara.
pub fn extraer(lineas: &[LineaOcr]) -> Factura {
    let nif_emisor = extraer_nif(lineas);
    // El CIF del cliente se busca pasándole ya el NIF del emisor: es lo que
    // permite descartarlo cuando el OCR pega los dos en la misma línea (ver
    // `candidato_fiscal_cliente`). Se calcula **antes** de construir la factura
    // porque `nif_emisor` se mueve dentro de ella.
    let cif_cliente = extraer_cif_cliente(lineas, nif_emisor.valor());
    Factura {
        nif_emisor,
        cif_cliente,
        pedido: extraer_pedido(lineas),
        numero_factura: extraer_numero_factura(lineas),
        fecha: extraer_fecha(lineas),
        base: extraer_importe_etiquetado(lineas, re_etiqueta_base()),
        iva: extraer_importe_etiquetado(lineas, re_etiqueta_iva()),
        total: extraer_total(lineas),
    }
}

// ---------------------------------------------------------------------------
// Procedencia
// ---------------------------------------------------------------------------

fn origen_de(indice: usize, linea: &LineaOcr) -> Origen {
    Origen::Ocr {
        pagina: linea.pagina,
        linea: indice as u32,
    }
}

/// Crudo de un dato compuesto por dos líneas: las dos, en el orden en que
/// aparecieron. Ninguna de las dos contiene el dato completa, así que el origen
/// del campo será `None`.
fn combinar(primera: &str, segunda: &str) -> String {
    format!("{} {}", primera.trim(), segunda.trim())
}

// ---------------------------------------------------------------------------
// NIF
// ---------------------------------------------------------------------------

/// Líneas que hablan del **cliente**, no del emisor. Todas las variantes que
/// usan las facturas del lote llevan aquí su CIF (siempre el mismo banco).
fn es_linea_de_cliente(normalizada: &str) -> bool {
    const CLAVES: [&str; 5] = ["CLIENTE", "DESTINATARIO", "FACTURAR A", "BILL TO", "CUSTOMER"];
    let up = normalizada.to_uppercase();
    CLAVES.iter().any(|clave| up.contains(clave))
}

/// El IBAN puede contener una subcadena con forma de NIF (`…S27023908…`). Se
/// excluye del rastreo sin etiqueta; con etiqueta no hace falta porque la
/// etiqueta manda.
fn es_linea_de_iban(normalizada: &str) -> bool {
    normalizada.to_uppercase().contains("IBAN")
}

fn extraer_nif(lineas: &[LineaOcr]) -> Identificador<Nif> {
    // 1. Línea con etiqueta fiscal y que no sea del cliente. Es el camino
    //    normal: `NIF: B46102331`, `NIF B90233808 MÁLAGA`, `· NIF A46311208 ·`.
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if es_linea_de_cliente(&normalizada) {
            continue;
        }
        if let Some(etiqueta) = re_nif_etiqueta().find(&normalizada) {
            let origen = Some(origen_de(indice, linea));
            let resto = &normalizada[etiqueta.end()..];
            return match primer_token_fiscal(resto) {
                Some(token) => canonizar_nif(&token, &linea.texto, linea.score, origen),
                // Había etiqueta de NIF y ningún valor legible detrás: eso no es
                // "la factura no trae NIF", es que no se pudo leer.
                None => Identificador::ilegible(linea.texto.clone(), linea.score, origen),
            };
        }
    }

    // 2. Sin etiqueta: solo si hay una forma fiscal clara y fuera de un IBAN.
    //    Es el último recurso, para plantillas que imprimen el NIF suelto.
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if es_linea_de_cliente(&normalizada) || es_linea_de_iban(&normalizada) {
            continue;
        }
        if let Some(forma) = re_forma_fiscal().find(&normalizada) {
            let origen = Some(origen_de(indice, linea));
            return canonizar_nif(forma.as_str(), &linea.texto, linea.score, origen);
        }
    }

    // Ni etiqueta ni forma: la factura no trae NIF. Es un caso normal (se
    // concilia por pedido), no un fallo.
    Identificador::no_aparece()
}

/// El valor que sigue a la etiqueta fiscal: el primer token con **forma** de
/// NIF y, si ninguno la tiene, el primer token con algún dígito.
///
/// Se prefiere la forma porque entre la etiqueta y el número caben palabras. El
/// caso real es `NIF / CIF: B46102331`: la etiqueta casa con el `NIF`, y antes
/// se cogía el token siguiente tal cual —`CIF`—, que no falla al canonizar,
/// **degrada**: la factura queda `Ilegible` y se va a la cola de revisión por el
/// orden de las palabras, no por un problema del documento.
fn primer_token_fiscal(resto: &str) -> Option<String> {
    let tokens: Vec<&str> = re_token().find_iter(resto).map(|m| m.as_str()).collect();
    tokens
        .iter()
        .find(|token| re_forma_fiscal().is_match(token))
        .or_else(|| {
            tokens
                .iter()
                .find(|token| token.chars().any(|c| c.is_ascii_digit()))
        })
        .map(|token| (*token).to_string())
}

/// Canoniza el token y decide su estado. Un NIF que existe como forma pero no
/// pasa el dígito de control es `ilegible`: hay texto, pero no se sabe a quién
/// se está pagando.
fn canonizar_nif(
    token: &str,
    crudo: &str,
    score: f64,
    origen: Option<Origen>,
) -> Identificador<Nif> {
    match Nif::nuevo(token) {
        None => Identificador::ilegible(crudo, score, origen),
        Some(nif) => {
            if nif_valido(&nif) {
                Identificador::encontrado(nif, crudo, score, origen)
            } else {
                Identificador::ilegible(crudo, score, origen)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// CIF del cliente
// ---------------------------------------------------------------------------

/// Cuántas líneas después de `Cliente:` / `Destinatario:` se sigue buscando su
/// identificador.
///
/// El CIF del destinatario no viaja pegado a la etiqueta (que suele ser un
/// bloque de dirección de tres o cuatro líneas); una ventana **finita** es lo
/// que evita que, en una factura sin bloque de cliente, se acabe tomando el NIF
/// de cualquier línea posterior.
const VENTANA_CLIENTE: usize = 3;

/// Canoniza el CIF del cliente. **Sin** `nif_valido`, a diferencia de
/// [`canonizar_nif`]: ver el comentario del campo en `domain.rs`.
fn canonizar_cliente(
    token: &str,
    crudo: &str,
    score: f64,
    origen: Option<Origen>,
) -> Identificador<Nif> {
    match Nif::nuevo(token) {
        None => Identificador::ilegible(crudo, score, origen),
        Some(nif) => Identificador::encontrado(nif, crudo, score, origen),
    }
}

/// El primer identificador fiscal de la línea que **no** sea ya el del emisor.
///
/// Esta es la defensa contra el caso más probable: facturas que imprimen
/// `NIF: B46102331 CIF: A58231074` en la misma línea, o un bloque de cabecera
/// donde el emisor aparece debajo de la etiqueta del cliente. Sin este filtro el
/// emisor se guardaría como CIF del cliente y la trazabilidad diría justo lo
/// contrario de la verdad, que es peor que no tener el dato.
fn candidato_fiscal_cliente(texto: &str, emisor: Option<&Nif>) -> Option<String> {
    let es_del_emisor = |token: &str| Some(token) == emisor.map(Nif::as_str);

    // Con etiqueta fiscal: `CIF: A58231074`, `NIF / CIF: B46102331`.
    for etiqueta in re_nif_etiqueta().find_iter(texto) {
        if let Some(token) = primer_token_fiscal(&texto[etiqueta.end()..]) {
            if !es_del_emisor(&token) {
                return Some(token);
            }
        }
    }

    // Sin etiqueta: solo una forma fiscal clara (`A58231074`), para plantillas
    // que imprimen el CIF del destinatario suelto tras su razón social.
    re_forma_fiscal()
        .find_iter(texto)
        .map(|forma| forma.as_str().to_string())
        .find(|forma| !es_del_emisor(forma))
}

/// CIF/NIF del **cliente**, buscado solo en sus líneas.
///
/// Se abre ventana con `Cliente:` / `Destinatario:` / `Facturar a:` / `Bill to:`
/// y se rastrea esa línea y las `VENTANA_CLIENTE` siguientes. Fuera de la
/// ventana no se mira: el identificador del destinatario está *en su bloque*, y
/// buscarlo por todo el documento solo sirve para encontrar el del emisor.
fn extraer_cif_cliente(lineas: &[LineaOcr], emisor: Option<&Nif>) -> Identificador<Nif> {
    let mut ventana = 0usize;
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if es_linea_de_cliente(&normalizada) {
            ventana = VENTANA_CLIENTE;
        } else if ventana == 0 {
            continue;
        } else {
            ventana -= 1;
        }
        // El IBAN lleva una subcadena con forma de NIF; no es el CIF.
        if es_linea_de_iban(&normalizada) {
            continue;
        }
        if let Some(token) = candidato_fiscal_cliente(&normalizada, emisor) {
            return canonizar_cliente(
                &token,
                &linea.texto,
                linea.score,
                Some(origen_de(indice, linea)),
            );
        }
        // Había etiqueta fiscal y ningún identificador legible detrás: el CIF se
        // imprimió y no se pudo leer. Decir `NoAparece` aquí borraría la
        // diferencia entre "esta plantilla no lo imprime" y "el OCR no pudo con
        // él", que es exactamente lo que el tri-estado existe para no confundir.
        if re_nif_etiqueta().is_match(&normalizada) {
            return Identificador::ilegible(
                &linea.texto,
                linea.score,
                Some(origen_de(indice, linea)),
            );
        }
    }
    Identificador::no_aparece()
}

// ---------------------------------------------------------------------------
// Pedido
// ---------------------------------------------------------------------------

fn canalizar_pedido(bruto: &str) -> String {
    let mut salida = String::with_capacity(bruto.len());
    let mut ultimo_separador = false;
    for c in bruto.trim().chars() {
        if c == '-' || c == '/' || c.is_whitespace() {
            if !ultimo_separador && !salida.is_empty() {
                salida.push('-');
                ultimo_separador = true;
            }
        } else {
            salida.extend(c.to_uppercase());
            ultimo_separador = false;
        }
    }
    salida.trim_end_matches('-').to_string()
}

fn extraer_pedido(lineas: &[LineaOcr]) -> Identificador<String> {
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if let Some(coincidencia) = re_pedido().find(&normalizada) {
            return Identificador::encontrado(
                canalizar_pedido(coincidencia.as_str()),
                linea.texto.clone(),
                linea.score,
                Some(origen_de(indice, linea)),
            );
        }
    }
    Identificador::no_aparece()
}

// ---------------------------------------------------------------------------
// Número de factura
// ---------------------------------------------------------------------------

/// Primer token que contiene al menos un dígito. Se salta marcadores sueltos
/// (`n`, `Nº`) que hayan quedado entre la etiqueta y el número.
fn primer_token_con_digito(texto: &str) -> Option<String> {
    re_token_id()
        .find_iter(texto)
        .map(|m| m.as_str().to_string())
        .find(|token| token.bytes().any(|b| b.is_ascii_digit()))
}

fn canalizar_numero_factura(bruto: &str) -> String {
    bruto
        .trim()
        .chars()
        .filter(|c| !c.is_whitespace())
        .collect::<String>()
        .to_uppercase()
}

fn extraer_numero_factura(lineas: &[LineaOcr]) -> Identificador<String> {
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if let Some(etiqueta) = re_etiqueta_factura().find(&normalizada) {
            if let Some(valor) = primer_token_con_digito(&normalizada[etiqueta.end()..]) {
                return Identificador::encontrado(
                    canalizar_numero_factura(&valor),
                    linea.texto.clone(),
                    linea.score,
                    Some(origen_de(indice, linea)),
                );
            }
        }
    }
    Identificador::no_aparece()
}

// ---------------------------------------------------------------------------
// Fecha
// ---------------------------------------------------------------------------

fn extraer_fecha(lineas: &[LineaOcr]) -> Identificador<String> {
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if !re_fecha_etiqueta().is_match(&normalizada) {
            continue;
        }
        if let Some(coincidencia) = re_fecha_es().find(&normalizada) {
            let origen = Some(origen_de(indice, linea));
            return match fecha_es_a_iso(coincidencia.as_str()) {
                Some(iso) => Identificador::encontrado(
                    iso,
                    linea.texto.clone(),
                    linea.score,
                    origen,
                ),
                // Había una fecha con forma de fecha y no es una fecha real
                // (`31/04`, año bisiesto falso): texto sin canonizar.
                None => Identificador::ilegible(linea.texto.clone(), linea.score, origen),
            };
        }
    }
    Identificador::no_aparece()
}

// ---------------------------------------------------------------------------
// Importes: base, IVA y total
// ---------------------------------------------------------------------------

/// Último importe de la línea, ya convertido a `Decimal`.
/// Un importe tal y como estaba impreso: la cifra y si venía en negativo.
///
/// Van juntos a propósito. `Identificador<Decimal>` no sabe de signos, así que
/// el `-` hay que mirarlo **al leer** y decidir ahí qué se hace con él; si se
/// pierde en el camino, ya no hay forma de recuperarlo.
#[derive(Debug, Clone, Copy, PartialEq)]
struct ImporteLeido {
    valor: Decimal,
    negativo: bool,
}

/// El último importe de la línea, con su signo.
fn ultimo_importe(normalizada: &str) -> Option<ImporteLeido> {
    let encontrado = re_importe().find_iter(normalizada).last()?;
    let valor = importe_a_decimal(encontrado.as_str())?;
    Some(ImporteLeido {
        valor,
        negativo: viene_en_negativo(&normalizada[..encontrado.start()]),
    })
}

/// ¿La cifra que viene después de este texto es negativa?
///
/// `-1.234,56` y `(1.234,56)` son las dos formas de imprimir un abono (la
/// segunda es la de la contabilidad clásica) y significan lo mismo: ese importe
/// **no** se paga, se devuelve. `re_importe` solo captura la cifra —el signo se
/// queda fuera del match—, así que sin esto un abono de `-1.234,56` se leía como
/// un total de `1.234,56` y R6 lo daba por bueno: una devolución convertida en
/// un pago, que es el peor error posible en este sistema.
///
/// Se mira solo el carácter inmediatamente anterior para no confundir el guion
/// de un texto (`factura nº-12`) con un signo: el signo tiene que estar pegado a
/// la cifra, que es como lo imprime cualquier generador de PDF.
fn viene_en_negativo(antes: &str) -> bool {
    matches!(antes.chars().last(), Some('-') | Some('(') | Some('\u{2212}'))
}

/// Puerto local de `importe_a_decimal` del ERP (`descargar_erp.py`): quita el
/// espacio duro y decide la convención por **el último separador**, que es lo
/// único que no se puede confundir.
///
/// Con `,` y `.` en la misma cifra, el que va al final es el decimal: `1.234,56`
/// es español (1234.56) y `1,234.56` es anglosajón (1234.56). Decidirlo por
/// «¿hay una coma?» funcionaba de casualidad en la mitad de los casos y fallaba
/// en `12,874.40`, que es como imprime un importe de doce mil ochocientos
/// setenta y cuatro un ERP en inglés: salía 12,87. Si solo hay un separador,
/// manda la regla del ERP —`,` decimal si hay coma, y si no `.\d{1,2}` final es
/// decimal y cualquier otro punto es de miles—, que es la que hace que `1.234`
/// sean mil doscientos treinta y cuatro.
fn importe_a_decimal(bruto: &str) -> Option<Decimal> {
    let limpio: String = bruto
        .chars()
        .filter(|c| !c.is_whitespace() && *c != '\u{00a0}' && *c != '€')
        .collect();
    if limpio.is_empty() {
        return None;
    }

    let ultima_coma = limpio.rfind(',');
    let ultimo_punto = limpio.rfind('.');

    let normalizado = match (ultima_coma, ultimo_punto) {
        // Los dos separadores: manda el que esté más a la derecha.
        (Some(coma), Some(punto)) if coma > punto => limpio.replace('.', "").replace(',', "."),
        (Some(_), Some(_)) => limpio.replace(',', ""),
        // Solo coma: es el decimal.
        (Some(_), None) => limpio.replace('.', "").replace(',', "."),
        // Solo puntos: `.\d{1,2}` final es decimal y el resto son miles.
        (None, Some(_)) if re_decimal_ingles().is_match(&limpio) => limpio.clone(),
        (None, Some(_)) => limpio.replace('.', ""),
        (None, None) => limpio.clone(),
    };

    Decimal::from_str(&normalizado).ok()
}

/// Construye el campo a partir de la línea `indice`; si esa línea tiene la
/// etiqueta pero ningún importe, mira la **siguiente** línea (en un OCR real la
/// etiqueta y la cifra caen en cajas distintas más veces de las que parece).
///
/// Cuando el dato sale de dos líneas: `score` = el mínimo y `origen` = `None`.
fn candidato_importe(
    lineas: &[LineaOcr],
    indice: usize,
    normalizada: &str,
) -> Option<Identificador<Decimal>> {
    let linea = &lineas[indice];
    if let Some(leido) = ultimo_importe(normalizada) {
        return Some(campo_de_importe(
            leido,
            linea.texto.clone(),
            linea.score,
            Some(origen_de(indice, linea)),
        ));
    }

    let siguiente = lineas.get(indice + 1)?;
    let normalizada_siguiente = normalizar_linea(&siguiente.texto);
    let leido = ultimo_importe(&normalizada_siguiente)?;
    Some(campo_de_importe(
        leido,
        combinar(&linea.texto, &siguiente.texto),
        linea.score.min(siguiente.score),
        None,
    ))
}

/// Envuelve un importe leído en el `Identificador` que espera la `Factura`.
///
/// Un importe **negativo se marca `Ilegible`**, y es una decisión, no un
/// descarte: `Identificador` no tiene signo, así que las dos salidas honestas
/// son no encontrado o ilegible. `NoAparece` diría que en la factura no había
/// cifra (falso: había una y bien visible) y `Encontrado` con el valor absoluto
/// daría por bueno un pago que la propia factura niega. `Ilegible` es lo que
/// para el pago automático: R5 escala la factura a manos humanas con el crudo
/// delante, que es exactamente lo que pide un abono o una rectificativa.
fn campo_de_importe(
    leido: ImporteLeido,
    crudo: String,
    score: f64,
    origen: Option<Origen>,
) -> Identificador<Decimal> {
    if leido.negativo {
        return Identificador::ilegible(crudo, score, origen);
    }
    Identificador::encontrado(leido.valor, crudo, score, origen)
}

/// Base e IVA: la primera línea que lleve la etiqueta y un importe.
fn extraer_importe_etiquetado(lineas: &[LineaOcr], etiqueta: &Regex) -> Identificador<Decimal> {
    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if !etiqueta.is_match(&normalizada) {
            continue;
        }
        if let Some(campo) = candidato_importe(lineas, indice, &normalizada) {
            return campo;
        }
    }
    Identificador::no_aparece()
}

/// ¿La línea habla de un subtotal o de una base? Entonces no es el total aunque
/// lleve la palabra `TOTAL` dentro.
fn menciona_subtotal_o_base(normalizada: &str) -> bool {
    let up = normalizada.to_uppercase();
    up.contains("SUBTOTAL") || up.contains("BASE")
}

/// Prioridad de una línea de total: las formas explícitas (`TOTAL A PAGAR`,
/// `IMPORTE TOTAL`, `TOTAL FACTURA`) ganan a un `TOTAL` genérico anterior.
fn prioridad_total(normalizada: &str) -> u8 {
    let up = normalizada.to_uppercase();
    if up.contains("TOTAL A PAGAR") || up.contains("IMPORTE TOTAL") || up.contains("TOTAL FACTURA")
    {
        3
    } else {
        2
    }
}

/// Se queda con el **último** número de la línea de total (los puntos de relleno
/// y el `EUR`/`€` van por delante o por detrás, nunca detrás de la cifra), sin
/// recalcular nada: un recargo financiero puede hacer `TOTAL ≠ base + IVA` y lo
/// que se paga es lo impreso.
fn extraer_total(lineas: &[LineaOcr]) -> Identificador<Decimal> {
    let mut mejor: Option<(u8, usize, Identificador<Decimal>)> = None;

    for (indice, linea) in lineas.iter().enumerate() {
        let normalizada = normalizar_linea(&linea.texto);
        if !re_etiqueta_total().is_match(&normalizada) || menciona_subtotal_o_base(&normalizada) {
            continue;
        }

        let Some(campo) = candidato_importe(lineas, indice, &normalizada) else {
            continue;
        };
        let prioridad = prioridad_total(&normalizada);

        // Con la misma prioridad gana la última: el total del pie es el de la
        // factura entera.
        let reemplaza = match &mejor {
            None => true,
            Some((prioridad_previa, indice_previo, _)) => {
                prioridad > *prioridad_previa
                    || (prioridad == *prioridad_previa && indice > *indice_previo)
            }
        };
        if reemplaza {
            mejor = Some((prioridad, indice, campo));
        }
    }

    mejor
        .map(|(_, _, campo)| campo)
        .unwrap_or_else(Identificador::no_aparece)
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::str::FromStr;

    use crate::domain::EstadoCampo;

    /// Línea de OCR a mano: en estas pruebas no hay PDF, ni servicio, ni red.
    fn linea(pagina: u32, texto: &str, score: f64) -> LineaOcr {
        LineaOcr {
            pagina,
            texto: texto.to_string(),
            bbox: [0.0, 0.0, 1.0, 1.0],
            score,
        }
    }

    fn dec(valor: &str) -> Decimal {
        Decimal::from_str(valor).expect("decimal de prueba")
    }

    /// El total de una factura de una sola línea, que es la forma en la que se
    /// prueban las siete familias de layout.
    fn total_de(texto: &str) -> Option<Decimal> {
        extraer(&[linea(0, texto, 1.0)]).total.valor().copied()
    }

    // -- Las siete familias de layout adversarias ---------------------------

    #[test]
    fn familia_1_puntos_de_relleno() {
        let texto = "TOTAL.............................      2.967,25";
        let factura = extraer(&[linea(0, texto, 0.91)]);

        assert_eq!(factura.total.valor(), Some(&dec("2967.25")));
        // La procedencia es la línea entera, con sus puntos de relleno.
        assert_eq!(factura.total.crudo(), Some(texto));
        assert_eq!(factura.total.score(), Some(0.91));
        assert_eq!(
            factura.total.origen(),
            Some(&Origen::Ocr {
                pagina: 0,
                linea: 0
            })
        );
    }

    #[test]
    fn familia_2_total_a_pagar_con_prefijo_eur_y_decimal_anglosajon() {
        assert_eq!(total_de("TOTAL A PAGAR: EUR 1705.37"), Some(dec("1705.37")));
        assert_eq!(
            total_de("TOTAL A PAGAR: EUR 1705.37").map(|v| v.to_string()),
            Some("1705.37".to_string())
        );
    }

    #[test]
    fn familia_3_tabuladores() {
        assert_eq!(total_de("TOTAL\t\t1.234,56"), Some(dec("1234.56")));
    }

    #[test]
    fn familia_4_variantes_de_etiqueta_y_simbolo() {
        assert_eq!(total_de("TOTAL: 1.234,56 €"), Some(dec("1234.56")));
        assert_eq!(total_de("IMPORTE TOTAL: 8.845,10"), Some(dec("8845.10")));
        assert_eq!(total_de("Total factura: 3.538,00 €"), Some(dec("3538.00")));
    }

    #[test]
    fn familia_5_espacios_de_ancho_cero() {
        // El \u{200b} parte la línea por sitios que romperían cualquier regex
        // ingenua (`TOTAL\u{200b}:`, `56\u{200b} €`).
        assert_eq!(total_de("TOTAL\u{200b}: 1.234,56\u{200b} €"), Some(dec("1234.56")));

        let factura = extraer(&[linea(0, "TOTAL\u{200b}: 1.234,56\u{200b} €", 0.9)]);
        // El crudo conserva el original; solo la búsqueda lo reparó.
        assert_eq!(factura.total.crudo(), Some("TOTAL\u{200b}: 1.234,56\u{200b} €"));
    }

    #[test]
    fn familia_6_mojibake_de_pagina_de_codigos() {
        let lineas = vec![
            linea(0, "MENSAJER═A R┴PIDA DEL SUR S.L.", 0.93),
            linea(0, "NIF B46102331  M┴LAGA", 0.95),
            linea(0, "PapelerÝa e Informßtica", 0.90),
            linea(0, "TOTAL: 1.047,51 Ç", 0.94),
        ];
        let factura = extraer(&lineas);

        // Se encuentra el NIF y el total aunque la línea venga con mojibake.
        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
        assert_eq!(factura.total.valor(), Some(&dec("1047.51")));

        // Y el nombre queda legible tras reparar el texto.
        assert_eq!(
            normalizar_linea(&lineas[0].texto),
            "MENSAJERÍA RÁPIDA DEL SUR S.L."
        );
        assert_eq!(normalizar_linea(&lineas[2].texto), "Papelería e Informática");
        assert_eq!(normalizar_linea(&lineas[3].texto), "TOTAL: 1.047,51 €");
    }

    #[test]
    fn familia_7_con_recargo_no_se_recalcula_el_total_impreso() {
        // El recargo financiero deja base + IVA por debajo del total.
        let lineas = vec![
            linea(0, "Base: 2.310,00", 0.95),
            linea(0, "IVA (21%): 485,10", 0.95),
            linea(0, "TOTAL A PAGAR: 2.920,10", 0.97),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.base.valor(), Some(&dec("2310.00")));
        assert_eq!(factura.iva.valor(), Some(&dec("485.10")));
        assert_eq!(factura.total.valor(), Some(&dec("2920.10")));
        assert_ne!(
            *factura.base.valor().unwrap() + *factura.iva.valor().unwrap(),
            *factura.total.valor().unwrap(),
            "el total impreso no tiene por qué ser base + IVA"
        );
    }

    // -- NIF ----------------------------------------------------------------

    #[test]
    fn un_nif_con_digito_de_control_correcto_es_encontrado_y_se_canoniza() {
        let factura = extraer(&[linea(0, "NIF: b-46102331", 0.99)]);

        assert!(factura.nif_emisor.aparece());
        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
        // El crudo es la línea tal cual, no el valor canónico.
        assert_eq!(factura.nif_emisor.crudo(), Some("NIF: b-46102331"));
        assert_eq!(factura.nif_emisor.score(), Some(0.99));
    }

    #[test]
    fn un_nif_que_parece_real_pero_falla_el_control_es_ilegible_y_nunca_encontrado() {
        let lineas = vec![
            linea(0, "Papelería Ruzafa S.C.", 0.97),
            linea(0, "NIF: B1234567B", 0.62),
            linea(0, "TOTAL: 950,00", 0.99),
        ];
        let factura = extraer(&lineas);

        assert!(factura.nif_emisor.es_ilegible(), "estado: {:?}", factura.nif_emisor.estado());
        assert!(
            !factura.nif_emisor.aparece(),
            "un control que no cuadra no es un acierto, es R5"
        );
        assert_eq!(factura.nif_emisor.valor(), None);
        // El crudo y el score se conservan para que la revisión sepa qué mirar.
        assert_eq!(factura.nif_emisor.crudo(), Some("NIF: B1234567B"));
        assert_eq!(factura.nif_emisor.score(), Some(0.62));
    }

    #[test]
    fn una_etiqueta_de_nif_sin_valor_tambien_es_ilegible() {
        let factura = extraer(&[linea(0, "NIF:", 0.4)]);

        assert_eq!(factura.nif_emisor.estado(), EstadoCampo::Ilegible);
        assert_eq!(factura.nif_emisor.crudo(), Some("NIF:"));
    }

    #[test]
    fn sin_ninguna_pista_de_nif_el_campo_no_aparece() {
        let lineas = vec![
            linea(0, "FACTURA", 0.99),
            linea(0, "Pedido: PO-2026-0096", 0.97),
            linea(0, "TOTAL: 1.234,56", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.nif_emisor.estado(), EstadoCampo::NoAparece);
        assert!(factura.nif_emisor.crudo().is_none());
        assert!(
            !factura.sin_identificadores(),
            "no aparece el NIF pero sí el pedido: no es un error, es información"
        );
    }

    #[test]
    fn el_cif_del_cliente_no_se_confunde_con_el_nif_del_emisor() {
        let lineas = vec![
            linea(0, "Suministros Levante S.L.", 0.98),
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "Cliente: Banco Miralmar S.A. · CIF: A58231074", 0.99),
        ];

        assert_eq!(
            extraer(&lineas).nif_emisor.valor().map(|n| n.as_str()),
            Some("B46102331")
        );
    }

    /// Entre la etiqueta y el número puede haber palabras (`NIF / CIF: …`). Si
    /// se coge el token siguiente a la etiqueta tal cual, el `CIF` no casa como
    /// NIF y la factura escala a revisión sin que el documento tenga nada malo.
    #[test]
    fn una_etiqueta_doble_no_esconde_el_nif() {
        for texto in [
            "NIF / CIF: B46102331",
            "N.I.F. (CIF) B46102331",
            "CIF: B46102331",
        ] {
            assert_eq!(
                extraer(&[linea(0, texto, 0.96)])
                    .nif_emisor
                    .valor()
                    .map(|n| n.as_str()),
                Some("B46102331"),
                "no se extrajo el NIF de: {texto}"
            );
        }
    }

    #[test]
    fn sin_nif_de_emisor_el_cif_del_cliente_no_lo_suplanta() {
        let lineas = vec![
            linea(0, "FACTURA", 0.99),
            linea(0, "Facturar a: Banco Miralmar S.A. — CIF A58231074", 0.99),
            linea(0, "TOTAL: 100,00", 0.99),
        ];

        assert_eq!(extraer(&lineas).nif_emisor.estado(), EstadoCampo::NoAparece);
    }

    // -- CIF del cliente -----------------------------------------------------

    #[test]
    fn el_cif_del_cliente_se_extrae_de_su_propia_linea() {
        let lineas = vec![
            linea(0, "Suministros Levante S.L.", 0.98),
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "Cliente: Banco Miralmar S.A. · CIF: A58231074", 0.99),
        ];
        let factura = extraer(&lineas);

        assert!(factura.cif_cliente.aparece());
        assert_eq!(factura.cif_cliente.valor().map(|n| n.as_str()), Some("A58231074"));
        // El crudo es la línea entera, y el origen apunta a ella.
        assert_eq!(factura.cif_cliente.crudo(), Some("Cliente: Banco Miralmar S.A. · CIF: A58231074"));
        assert_eq!(factura.cif_cliente.origen(), Some(&Origen::Ocr { pagina: 0, linea: 2 }));
        // Y no contamina al emisor: cada uno sale de su línea.
        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
    }

    /// El CIF del cliente es **trazabilidad fiscal**, no la llave del pago, así
    /// que no se le exige dígito de control: el mismo token que como emisor
    /// sería `Ilegible` aquí es `Encontrado`. Cambiar esto degradaría a revisión
    /// facturas perfectamente legibles, porque el CIF del banco no lo pasa.
    #[test]
    fn el_cif_del_cliente_no_exige_digito_de_control() {
        let token = "B1234567B";

        // El mismo token en la línea del emisor sí se degrada (regla R5).
        assert_eq!(
            extraer(&[linea(0, &format!("NIF: {token}"), 0.9)])
                .nif_emisor
                .estado(),
            EstadoCampo::Ilegible
        );

        let factura = extraer(&[linea(0, &format!("Cliente: Banco Miralmar · CIF: {token}"), 0.9)]);
        assert_eq!(factura.cif_cliente.estado(), EstadoCampo::Encontrado);
        assert_eq!(factura.cif_cliente.valor().map(|n| n.as_str()), Some(token));
    }

    /// Una etiqueta de CIF sin valor legible detrás no es "la plantilla no lo
    /// imprime": el CIF está impreso y no se pudo leer. La diferencia la ve el
    /// revisor en el estado y la ve R1 bis en el motivo.
    #[test]
    fn un_cif_cliente_impreso_pero_sin_valor_es_ilegible_y_no_ausente() {
        let lineas = vec![
            linea(0, "Cliente: Banco Miralmar S.A.", 0.97),
            linea(0, "CIF: /", 0.35),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.cif_cliente.estado(), EstadoCampo::Ilegible);
        assert!(factura.cif_cliente.crudo().is_some());
        assert!(!factura.cif_cliente.aparece());
    }

    #[test]
    fn sin_bloque_de_cliente_el_cif_no_aparece() {
        let lineas = vec![
            linea(0, "Suministros Levante S.L.", 0.98),
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "TOTAL: 100,00", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.cif_cliente.estado(), EstadoCampo::NoAparece);
        assert!(factura.cif_cliente.crudo().is_none());
    }

    /// Cuando el OCR pega los dos identificadores en la misma línea, el del
    /// emisor ya está cogido: guardarlo otra vez como CIF del cliente dejaría la
    /// trazabilidad diciendo justo lo contrario de la verdad.
    #[test]
    fn el_cif_del_cliente_no_se_copia_del_emisor_aunque_compartan_linea() {
        let lineas = vec![
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "Cliente: Banco Miralmar S.A. · NIF: B46102331 · CIF: A58231074", 0.96),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
        assert_eq!(factura.cif_cliente.valor().map(|n| n.as_str()), Some("A58231074"));
    }

    /// El IBAN lleva dentro una subcadena con forma de NIF: ni es el CIF del
    /// cliente ni debe hacer que la búsqueda se dé por resuelta.
    #[test]
    fn un_iban_en_la_ventana_del_cliente_no_es_su_cif() {
        let lineas = vec![
            linea(0, "Cliente: Banco Miralmar S.A.", 0.98),
            linea(0, "IBAN: ES21 0049 1500 0512 3456 7890", 0.98),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.cif_cliente.estado(), EstadoCampo::NoAparece);
    }

    // -- resto de campos ----------------------------------------------------

    #[test]
    fn extrae_los_ocho_campos_de_una_factura_real() {
        let lineas = vec![
            linea(0, "FACTURA", 0.99),
            linea(0, "Factura: 2026/11604    Fecha: 08/01/2026", 0.96),
            linea(0, "Pedido: PO-2026-0096", 0.97),
            linea(0, "Suministros Levante S.L.", 0.98),
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "IBAN: ES21 0049 1500 0512 3456 7890", 0.98),
            linea(0, "Cliente: Banco Miralmar S.A. · CIF: A58231074", 0.99),
            linea(0, "Servicio mensual ....... 2.489,99", 0.97),
            linea(0, "Base: 2.489,99", 0.98),
            linea(0, "IVA (21%): 522,90", 0.98),
            linea(0, "TOTAL: 3.012,89", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
        assert_eq!(factura.cif_cliente.valor().map(|n| n.as_str()), Some("A58231074"));
        assert_eq!(factura.pedido.valor().map(String::as_str), Some("PO-2026-0096"));
        assert_eq!(factura.numero_factura.valor().map(String::as_str), Some("2026/11604"));
        assert_eq!(factura.fecha.valor().map(String::as_str), Some("2026-01-08"));
        assert_eq!(factura.base.valor(), Some(&dec("2489.99")));
        assert_eq!(factura.iva.valor(), Some(&dec("522.90")));
        assert_eq!(factura.total.valor(), Some(&dec("3012.89")));

        // La fecha se canoniza, pero el crudo conserva `dd/mm/aaaa`.
        assert_eq!(factura.fecha.crudo(), Some("Factura: 2026/11604    Fecha: 08/01/2026"));

        // El origen es la posición 0-based dentro del `&[LineaOcr]`.
        assert_eq!(
            factura.pedido.origen(),
            Some(&Origen::Ocr { pagina: 0, linea: 2 })
        );
        assert_eq!(factura.fecha.origen(), Some(&Origen::Ocr { pagina: 0, linea: 1 }));
    }

    #[test]
    fn el_origen_lleva_la_pagina_del_ocr() {
        let lineas = vec![
            linea(0, "FACTURA", 0.99),
            linea(2, "NIF: B46102331", 0.99),
        ];

        assert_eq!(
            extraer(&lineas).nif_emisor.origen(),
            Some(&Origen::Ocr { pagina: 2, linea: 1 })
        );
    }

    #[test]
    fn el_numero_de_factura_admite_varias_plantillas() {
        let casos = [
            ("Factura: FA-3359    Fecha: 12/01/2026", "FA-3359"),
            ("FACTURA SIMPLIFICADA Nº 2026/25704", "2026/25704"),
            ("Nº de factura: F26-9524", "F26-9524"),
            ("REF FACTURA: FA-5138", "FA-5138"),
            ("Invoice # FA-1480", "FA-1480"),
            ("Invoice # 2026/0811-B", "2026/0811-B"),
            // La línea de título sola no inventa un número.
            ("FACTURA", ""),
        ];

        for (texto, esperado) in casos {
            let factura = extraer(&[linea(0, texto, 0.9)]);
            assert_eq!(
                factura.numero_factura.valor().map(String::as_str).unwrap_or(""),
                esperado,
                "texto: {texto:?}"
            );
        }
    }

    #[test]
    fn los_importes_admiten_las_dos_convenciones() {
        assert_eq!(total_de("TOTAL: 1.234,50"), Some(dec("1234.50")));
        assert_eq!(total_de("TOTAL: 1234.50"), Some(dec("1234.50")));
        assert_eq!(total_de("TOTAL: 1.234"), Some(dec("1234")));
        assert_eq!(total_de("TOTAL: 2.110,00 EUR"), Some(dec("2110.00")));
    }

    /// Con los dos separadores en la misma cifra, manda el que va **al final**.
    /// Antes se decidía por «¿hay una coma?», y eso convertía los miles
    /// anglosajones en céntimos: `12,874.40` salía 12,87. Doce euros en vez de
    /// doce mil.
    #[test]
    fn el_ultimo_separador_decide_la_convencion() {
        assert_eq!(total_de("TOTAL: 12,874.40"), Some(dec("12874.40")));
        assert_eq!(total_de("TOTAL: 1,234,567.89"), Some(dec("1234567.89")));
        assert_eq!(total_de("TOTAL: 12.874,40"), Some(dec("12874.40")));
        // Y con un solo separador se mantiene la regla del ERP.
        assert_eq!(total_de("TOTAL: 1.234"), Some(dec("1234")));
        // Un entero sin separador no es un importe para `re_importe` (exigir
        // separador decimal es lo que evita que el `21` de `IVA (21%)` o un año
        // se cuelen como cifra), así que no hay total del que tirar.
        assert_eq!(total_de("TOTAL: 1234"), None);
    }

    /// Un abono (`-1.234,56`) o una rectificativa en la notación contable
    /// (`(1.234,56)`) **no** son un cobro. El signo no cabe en `Identificador`,
    /// así que el total queda ilegible y R5 manda la factura a manos humanas:
    /// sin esto, el importe absoluto se leía como un total normal y R6 lo pagaba.
    #[test]
    fn un_total_en_negativo_no_se_lee_como_un_cobro() {
        for texto in [
            "TOTAL A PAGAR: -1.234,56 €",
            "TOTAL A PAGAR: (1.234,56)",
            "TOTAL A PAGAR: \u{2212}1.234,56",
        ] {
            let factura = extraer(&[linea(0, texto, 0.97)]);

            assert_eq!(
                factura.total.estado(),
                EstadoCampo::Ilegible,
                "un total negativo no puede quedar como encontrado: {texto}"
            );
            assert_eq!(factura.total.valor(), None, "{texto}");
            // El crudo se conserva: quien revise la factura tiene que poder ver
            // de dónde salió la duda.
            assert_eq!(factura.total.crudo(), Some(texto), "{texto}");
        }
    }

    /// El signo tiene que estar **pegado** a la cifra. Un guion suelto en el
    /// texto (`Nº-1234 1.234,56`) no convierte el importe en negativo.
    #[test]
    fn un_guion_que_no_es_signo_no_invalida_el_importe() {
        assert_eq!(total_de("TOTAL FACTURA Nº-1234: 1.234,56"), Some(dec("1234.56")));
    }

    #[test]
    fn el_total_es_el_ultimo_numero_de_la_linea() {
        // Dot leaders, porcentaje y base por delante: manda la última cifra.
        assert_eq!(total_de("TOTAL 21% 214,25 1.234,50"), Some(dec("1234.50")));
    }

    #[test]
    fn prefiere_total_a_pagar_sobre_un_total_generico_anterior() {
        let lineas = vec![
            linea(0, "TOTAL: 1.000,00", 0.99),
            linea(0, "TOTAL A PAGAR: 1.210,00", 0.98),
        ];

        assert_eq!(extraer(&lineas).total.valor(), Some(&dec("1210.00")));
        assert_eq!(extraer(&lineas).total.score(), Some(0.98));
    }

    #[test]
    fn con_la_misma_prioridad_gana_el_ultimo_total() {
        let lineas = vec![
            linea(0, "TOTAL: 1.000,00", 0.99),
            linea(0, "TOTAL: 1.200,00", 0.90),
        ];

        assert_eq!(extraer(&lineas).total.valor(), Some(&dec("1200.00")));
    }

    #[test]
    fn nunca_toma_un_subtotal_ni_una_base_como_total() {
        let lineas = vec![
            linea(0, "Subtotal: EUR 1409.40", 0.99),
            linea(0, "IVA (21%): 295,97", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.total.estado(), EstadoCampo::NoAparece);
        // El subtotal sí sirve como base (es la misma magnitud con otro nombre).
        assert_eq!(factura.base.valor(), Some(&dec("1409.40")));
        assert_eq!(factura.iva.valor(), Some(&dec("295.97")));
    }

    #[test]
    fn la_prosa_del_pie_no_se_cuela_como_total() {
        let lineas = vec![
            linea(0, "TOTAL A PAGAR: EUR 2920.10", 0.97),
            linea(
                0,
                "El total incluye un recargo financiero; no debe recalcularse como base mas IVA",
                0.9,
            ),
            linea(0, "1619/2012. Domicilio social a disposicion del cliente.", 0.9),
        ];

        assert_eq!(extraer(&lineas).total.valor(), Some(&dec("2920.10")));
    }

    #[test]
    fn una_linea_que_solo_menciona_el_total_no_es_la_linea_del_total() {
        // Que la etiqueta esté anclada al principio de línea no es decorativo:
        // hay líneas del pie que *hablan* del total y llevan cifra. Colarlas
        // daría un importe plausible y falso; no encontrarlo, en cambio, es el
        // lado seguro del error (R7 escala: no se puede conciliar sin importe,
        // así que la factura no se paga sola).
        let lineas = vec![linea(
            0,
            "Segun contrato, el TOTAL pendiente asciende a: 4.500,00",
            0.9,
        )];

        assert_eq!(extraer(&lineas).total.estado(), EstadoCampo::NoAparece);
    }

    // -- procedencia del score ---------------------------------------------

    #[test]
    fn cada_campo_lleva_el_score_de_su_linea() {
        let lineas = vec![
            linea(0, "Base: 1.000,00", 0.93),
            linea(0, "IVA (21%): 210,00", 0.77),
            linea(0, "TOTAL: 1.210,00", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.base.score(), Some(0.93));
        assert_eq!(factura.iva.score(), Some(0.77));
        assert_eq!(factura.total.score(), Some(0.99));
    }

    #[test]
    fn un_dato_compuesto_de_dos_lineas_usa_el_score_minimo() {
        // La etiqueta y la cifra caen en cajas distintas: el dato se compone de
        // dos líneas, así que no puede ser más fiable que la peor de las dos.
        let lineas = vec![
            linea(0, "TOTAL A PAGAR", 0.96),
            linea(0, "1.705,37 €", 0.71),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.total.valor(), Some(&dec("1705.37")));
        assert_eq!(factura.total.score(), Some(0.71), "el mínimo de las dos líneas");
        assert!(
            factura.total.origen().is_none(),
            "compuesto por dos líneas: ninguna línea sola contiene el dato"
        );
        assert_eq!(factura.total.crudo(), Some("TOTAL A PAGAR 1.705,37 €"));
    }

    // -- robustez -----------------------------------------------------------

    #[test]
    fn sin_lineas_no_hay_nada_y_no_revienta() {
        let factura = extraer(&[]);

        assert_eq!(factura, Factura::default());
        assert!(factura.sin_identificadores());
    }

    #[test]
    fn una_linea_de_basura_no_inventa_ningun_campo() {
        let factura = extraer(&[linea(0, "~~~ ??? ---", 0.1)]);

        assert_eq!(factura, Factura::default());
    }

    #[test]
    fn los_datos_se_buscan_aunque_las_lineas_lleguen_desordenadas() {
        // Un OCR no garantiza el orden de lectura: los puntos de datos pueden
        // venir de cualquier línea, y el parser no depende del orden.
        let lineas = vec![
            linea(3, "TOTAL: 1.210,00", 0.99),
            linea(1, "Pedido: PO-2026-0222", 0.97),
            linea(2, "NIF: B46102331", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.nif_emisor.valor().map(|n| n.as_str()), Some("B46102331"));
        assert_eq!(factura.pedido.valor().map(String::as_str), Some("PO-2026-0222"));
        assert_eq!(factura.total.valor(), Some(&dec("1210.00")));
    }

    #[test]
    fn el_pedido_se_canoniza_a_la_forma_del_erp() {
        for (texto, esperado) in [
            ("Pedido: PO-2026-0096", "PO-2026-0096"),
            ("Su pedido: PO 2026/0070", "PO-2026-0070"),
            ("PEDIDO CLIENTE: PO-2026-0184", "PO-2026-0184"),
            ("Pedido asociado: PO-2026-0220", "PO-2026-0220"),
            ("PO: PED-2024-0912", "PED-2024-0912"),
        ] {
            let factura = extraer(&[linea(0, texto, 0.9)]);
            assert_eq!(
                factura.pedido.valor().map(String::as_str),
                Some(esperado),
                "texto: {texto:?}"
            );
        }
    }

    #[test]
    fn el_pedido_que_no_aparece_no_es_un_error() {
        let lineas = vec![
            linea(0, "NIF: B46102331", 0.99),
            linea(0, "TOTAL: 2.110,00", 0.99),
        ];
        let factura = extraer(&lineas);

        assert_eq!(factura.pedido.estado(), EstadoCampo::NoAparece);
        assert!(!factura.sin_identificadores());
    }
}
