// Validaciones puras de la vía OCR: dígito/letra de control de NIF/CIF/DNI/NIE y
// fechas españolas a ISO.
//
// Ver TRASPASO.md §3.2, spec_y_plan.md §3.2/Bloque 3 y
// _scratch/ERP-RUST-CONTRATO.md §4.2.
//
// Este módulo no conoce ni el ERP ni el parser: son funciones totales de su
// argumento a un `bool` / `Option<String>`, que es lo que las hace probables con
// una tabla y sin montar nada. El parser es el único consumidor: cuando un NIF
// se lee del OCR pero no pasa el control, el campo NO es un acierto, es un
// `Identificador::ilegible(...)` (regla R5), y eso es lo que convierte "texto
// borroso" en "texto mal leído".

use chrono::{Datelike, NaiveDate};

use crate::domain::Nif;

/// Letra de control del DNI/NIE, indexada por `número % 23`.
const LETRAS_DNI: &[u8; 23] = b"TRWAGMYFPDXBNJZSQVHLCKE";

/// Equivalente en letra del dígito de control del CIF, indexado por el dígito.
/// El 0 se escribe `J` (no hay letra "cero"), y de ahí el orden raro.
const LETRAS_CONTROL_CIF: &[u8; 10] = b"JABCDEFGHI";

/// Letras iniciales que identifican un CIF. Fuera quedan `K`, `L` y `M`, que no
/// son CIF sino NIF de persona física sin DNI y se validan como el DNI.
const INICIALES_CIF: &[u8] = b"ABCDEFGHJNPQRSUVW";

/// ¿El dígito (o la letra) de control de este NIF cuadra?
///
/// Acepta las cuatro familias que aparecen en una factura española, siempre
/// sobre el **NIF ya canónico** que produce [`Nif::nuevo`] (mayúsculas, sin
/// guiones ni espacios):
///
/// | Forma | Ejemplo | Control |
/// |---|---|---|
/// | DNI | `12345678Z` | letra de `número % 23` |
/// | NIE | `X1234567L` | `X`/`Y`/`Z` → `0`/`1`/`2`, luego como el DNI |
/// | NIF de persona física | `K1234567L` | como el DNI |
/// | CIF | `B46102331` | suma doble-par del CIF |
///
/// # Aviso que no se puede "optimizar"
///
/// **Los CIF del ERP real NO cumplen este control** (medido: sólo 47 de 516).
/// Por eso esta función **jamás** debe usarse para filtrar, puntuar, descartar
/// ni bloquear datos que vengan de `erp.rs`: si se hiciera, se tirarían 9 de
/// cada 10 asientos legítimos y la lista `prohibido_pagar_proveedor` dejaría de
/// casar. Su único uso es sobre lo que el OCR ha leído de un **PDF** (regla R5
/// del contrato), donde un control que no cuadra es la firma de una errata de
/// lectura (`8` ↔ `B`, `1` ↔ `l`, un dígito comido) y hay que escalar, no pagar.
///
/// Y hoy **ni siquiera ahí se aplica**: las facturas del corpus también son
/// sintéticas y sus CIF tampoco respetan el control (425 de 471 impresos), así
/// que `parser.rs` la tiene desactivada con `EXIGIR_DIGITO_DE_CONTROL_NIF`
/// hasta que haya documentos reales. La función se conserva intacta para poder
/// volver a encenderla con una sola línea, y estas pruebas siguen fijando su
/// contrato.
///
/// La comparación de identidad del ERP usa [`Nif`] a secas, que solo canoniza la
/// forma y no valida nada: eso es deliberado.
pub fn nif_valido(nif: &Nif) -> bool {
    // Se trabaja en bytes a propósito: `Nif` es ASCII por construcción (solo
    // alfanuméricos latinos), y así no hay ninguna forma de trocear una `&str`
    // por un índice que no caiga en frontera de carácter.
    let bytes = nif.as_str().as_bytes();
    if bytes.len() != 9 {
        return false;
    }
    let digitos = &bytes[1..8];
    let control = bytes[8];
    let siete_digitos = digitos.iter().all(u8::is_ascii_digit);

    // DNI: 8 dígitos + letra.
    if bytes[..8].iter().all(u8::is_ascii_digit) && control.is_ascii_uppercase() {
        return letra_dni(numero(&bytes[..8])) == control;
    }

    // NIE: [XYZ] + 7 dígitos + letra. X/Y/Z valen 0/1/2 por delante del número.
    if matches!(bytes[0], b'X' | b'Y' | b'Z') && siete_digitos && control.is_ascii_uppercase() {
        let prefijo = match bytes[0] {
            b'X' => 0,
            b'Y' => 1,
            _ => 2,
        };
        return letra_dni(prefijo * 10_000_000 + numero(digitos)) == control;
    }

    // NIF de persona física sin DNI (K/L/M): 7 dígitos + letra del DNI.
    if matches!(bytes[0], b'K' | b'L' | b'M') && siete_digitos && control.is_ascii_uppercase() {
        return letra_dni(numero(digitos)) == control;
    }

    // CIF: letra inicial válida + 7 dígitos + control (dígito o su letra).
    //
    // Es CIF solo si la letra inicial está en la lista blanca: así `PEPE` o
    // `B12A45678` caen por su propio pie sin comprobar la longitud otra vez.
    if !INICIALES_CIF.contains(&bytes[0]) || !siete_digitos {
        return false;
    }
    if !(control.is_ascii_digit() || control.is_ascii_uppercase()) {
        return false;
    }

    let mut suma = 0u32;
    for (posicion, byte) in digitos.iter().enumerate() {
        let mut valor = u32::from(byte - b'0');
        // Posiciones impares (1ª, 3ª, 5ª, 7ª) se doblan y, si superan 9, se les
        // resta 9: es equivalente a sumar las cifras del doble.
        if posicion % 2 == 0 {
            valor *= 2;
            if valor > 9 {
                valor -= 9;
            }
        }
        suma += valor;
    }
    let digito_control = (10 - suma % 10) % 10;

    control == b'0' + digito_control as u8 || control == LETRAS_CONTROL_CIF[digito_control as usize]
}

/// Letra de control del DNI para un número de 8 dígitos (0..=99_999_999).
fn letra_dni(numero: u32) -> u8 {
    LETRAS_DNI[(numero % 23) as usize]
}

/// Los bytes como número. Solo se llama tras comprobar que son dígitos, así que
/// ni desborda ni falla (8 dígitos caben de sobra en `u32`).
fn numero(bytes: &[u8]) -> u32 {
    bytes.iter().fold(0u32, |acc, b| acc * 10 + u32::from(b - b'0'))
}

/// `"21/03/2026"` → `"2026-03-21"`.
///
/// Deliberadamente **estricta** con el orden de los campos: en España la fecha
/// es día-primero, así que `03/04/2026` es el 3 de abril y no el 4 de marzo. Una
/// función tolerante que probase las dos lecturas y se quedase con la que existe
/// convertiría un `03/04` ambiguo en una fecha plausible pero equivocada, que es
/// justo el error que no se ve. Aquí no hay adivinación: día, mes, año, o nada.
///
/// Acepta `d/m/aaaa` y `dd/mm/aaaa` (un solo dígito de día o mes es válido y se
/// rellena con ceros) y **exige** el año de cuatro cifras. Devuelve `None` para
/// cualquier otra cosa: ISO ya formateado, años de dos cifras, separadores
/// distintos de `/`, texto pegado, etc. El día imposible (`29/02/2023`,
/// `31/04`, mes 13) lo rechaza [`NaiveDate::from_ymd_opt`], no una tabla.
pub fn fecha_es_a_iso(bruto: &str) -> Option<String> {
    let texto = bruto.trim();

    // Se parte por `/` y se exige que haya exactamente tres trozos: así
    // `21/03/2026 12:00` o `2026/11604` no pasan ni de la primera criba.
    let mut campos = texto.split('/');
    let dia = campos.next()?;
    let mes = campos.next()?;
    let anio = campos.next()?;
    if campos.next().is_some() {
        return None;
    }

    if dia.is_empty() || dia.len() > 2 || mes.is_empty() || mes.len() > 2 || anio.len() != 4 {
        return None;
    }
    let solo_digitos = |campo: &str| campo.bytes().all(|b| b.is_ascii_digit());
    if !solo_digitos(dia) || !solo_digitos(mes) || !solo_digitos(anio) {
        return None;
    }

    // Ojo con el orden: `from_ymd_opt` es (año, mes, día), **no** (día, mes,
    // año). Con los campos en el orden del texto, `21/03/2026` entra como
    // año 21 y día 2026, `from_ymd_opt` lo rechaza y toda fecha válida saldría
    // `None`. Por eso el `parse` va con el tipo anotado y explícito.
    let anio: i32 = anio.parse().ok()?;
    let mes: u32 = mes.parse().ok()?;
    let dia: u32 = dia.parse().ok()?;
    let fecha = NaiveDate::from_ymd_opt(anio, mes, dia)?;

    // Formato a mano y no con `strftime`: `chrono` va sin la feature de formato,
    // y esto no necesita ninguna biblioteca para ser correcto.
    Some(format!(
        "{:04}-{:02}-{:02}",
        fecha.year(),
        fecha.month(),
        fecha.day()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Un `Nif` canónico a partir de algo canonizable. Falla el test si el bruto
    /// no tiene ni un alfanumérico, que es el único caso en que no existe `Nif`.
    fn nif(bruto: &str) -> Nif {
        Nif::nuevo(bruto).expect("el bruto debería ser canonizable")
    }

    #[test]
    fn nif_valido_acepta_dni_nie_cif_y_nif_de_persona_fisica() {
        // DNI: 12345678 % 23 = 14 → 'Z'.
        assert!(nif_valido(&nif("12345678Z")));
        // NIE: X → 0, 01234567 % 23 = 19 → 'L'.
        assert!(nif_valido(&nif("X1234567L")));
        // CIF con control numérico.
        assert!(nif_valido(&nif("B46102331")));
        assert!(nif_valido(&nif("A58818501")));
        // NIF de persona física sin DNI.
        assert!(nif_valido(&nif("K1234567L")));
    }

    #[test]
    fn nif_valido_no_depende_de_la_caja_ni_de_los_separadores() {
        // `Nif::nuevo` ya canoniza; el validador recibe el canónico, así que un
        // NIF escrito con guiones o en minúsculas tiene que seguir siendo válido.
        assert!(nif_valido(&nif(" b-46102331 ")));
        assert!(nif_valido(&nif("12345678z")));
        assert_eq!(nif(" b-46102331 ").as_str(), "B46102331");
    }

    #[test]
    fn nif_valido_rechaza_un_control_que_no_cuadra() {
        // Estos dos son los casos de la regla R5: parecen un NIF, pero el
        // control no cuadra → el parser los degrada a `ilegible`.
        assert!(!nif_valido(&nif("B12345678"))); // el control sería 4 o D
        assert!(!nif_valido(&nif("B1234567B"))); // letra donde va el control 4/D
        assert!(!nif_valido(&nif("A58818502")));
        assert!(!nif_valido(&nif("12345678A"))); // letra de DNI equivocada
        assert!(!nif_valido(&nif("X1234567A")));
    }

    #[test]
    fn nif_valido_rechaza_longitudes_incorrectas() {
        assert!(!nif_valido(&nif("B1234567")));
        assert!(!nif_valido(&nif("B123456789")));
        assert!(!nif_valido(&nif("1234567Z")));
        assert!(!nif_valido(&nif("123456789Z")));
    }

    #[test]
    fn nif_valido_rechaza_letras_donde_van_digitos_y_basura() {
        assert!(!nif_valido(&nif("B12A45678")));
        assert!(!nif_valido(&nif("PEPE")));
        assert!(!nif_valido(&nif("12345678"))); // 8 dígitos y ninguna letra
        assert!(!nif_valido(&nif("Z12345678"))); // inicial fuera de NIE y de CIF
    }

    #[test]
    fn lo_que_no_tiene_alfanumericos_no_llega_a_ser_un_nif() {
        // `nif_valido` no necesita tratar el vacío: no existe un `Nif` vacío.
        // Esa puerta la cierra `Nif::nuevo` y conviene dejarlo escrito.
        for bruto in ["", "   ", "---", "???", "· ·", "\u{00a0}"] {
            assert!(Nif::nuevo(bruto).is_none(), "bruto: {bruto:?}");
        }
    }

    #[test]
    fn fecha_es_a_iso_es_dia_primero_y_estricta() {
        let casos: [(&str, Option<&str>); 16] = [
            ("21/03/2026", Some("2026-03-21")),
            ("02/09/2024", Some("2024-09-02")),
            ("3/4/2026", Some("2026-04-03")),
            ("21/3/2026", Some("2026-03-21")),
            // La ambigüedad resuelta: día primero.
            ("03/04/2026", Some("2026-04-03")),
            (" 17/01/2026 ", Some("2026-01-17")),
            // Año bisiesto real y año que no lo es.
            ("29/02/2024", Some("2024-02-29")),
            ("29/02/2023", None),
            ("29/02/2100", None),
            // Días y meses imposibles.
            ("31/04/2024", None),
            ("13/13/2024", None),
            ("00/01/2024", None),
            // Formatos que NO son `dd/mm/aaaa`.
            ("21/03/26", None),
            ("2026-03-21", None),
            ("21-03-2026", None),
            ("21/03/2026 12:00", None),
        ];

        for (bruto, esperado) in casos {
            assert_eq!(
                fecha_es_a_iso(bruto),
                esperado.map(str::to_string),
                "bruto: {bruto:?}"
            );
        }
    }

    #[test]
    fn fecha_es_a_iso_devuelve_none_para_texto_suelto() {
        for bruto in ["", "   ", "no es una fecha", "Fecha: 21/03/2026", "1/1/2026/2"] {
            assert_eq!(fecha_es_a_iso(bruto), None, "bruto: {bruto:?}");
        }
    }

    /// El orden de los campos no es un detalle de estilo: `from_ymd_opt` recibe
    /// año primero, así que intercambiarlo convierte toda fecha buena en `None`
    /// sin que nada avise. Este caso lo fija.
    #[test]
    fn el_orden_de_los_campos_es_ano_mes_dia() {
        assert_eq!(fecha_es_a_iso("21/03/2026").as_deref(), Some("2026-03-21"));
        assert_eq!(fecha_es_a_iso("31/12/2026").as_deref(), Some("2026-12-31"));
        assert_eq!(fecha_es_a_iso("01/01/2026").as_deref(), Some("2026-01-01"));
    }
}
