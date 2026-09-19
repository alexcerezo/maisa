// Motor de reglas determinista: (Factura, Evidencia) -> Decision.
//
// Implementa el comportamiento de decisión de spec_y_plan.md §1.1 y las reglas
// ordenadas de albertitos_plan.md §2.3, con la política conservadora del equipo:
// **en la duda, ESCALAR** (nunca PAGAR).
//
// Orden de evaluación (importa):
//   R1  sin identificadores               -> ESCALAR
//   R2  proveedor en lista de pago prohibido -> NO_PAGAR
//   R3  asiento ERP PAGADA                -> NO_PAGAR   (el ERP manda)
//   R4  conflicto PDF/ERP/Excel           -> ESCALAR    (bloquea el pago auto)
//   R5  baja confianza OCR                -> ESCALAR    (bloquea el pago auto)
//   R6  asiento ERP PENDIENTE conciliado  -> PAGAR
//   R7  asiento ERP PENDIENTE descuadrado -> ESCALAR
//   R8  sin asiento ERP pero con Excel    -> ESCALAR
//   R9  sin match en ninguna fuente       -> ESCALAR
//
// Nota de diseño: R4/R5 se evalúan *después* de R3 pero *antes* de R6. Un
// conflicto no impide bloquear un pago ya liquidado (dirección segura), pero sí
// impide ejecutar un pago en automático (dirección de riesgo). Así se concilia
// "el ERP manda" (ADR 1) con "en la duda, ESCALAR".
//
// La función `decidir` es pura: misma entrada => misma decisión, lo que hace la
// traza reproducible. Los umbrales vienen de `config/reglas.toml`.

use std::path::Path;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::domain::{
    Decision, EstadoAsiento, EstadoCampo, Evidencia, Factura, Huellas, Nif, Resultado,
};

/// Parámetros del motor de decisión que se ajustan sin recompilar.
/// Es el punto de inyección de la regla nueva del sábado 18:00.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ReglasConfig {
    /// Versión lógica de este juego de reglas.
    ///
    /// Se copia a `Decision::huellas.reglas_version`, que es el índice por el que
    /// se buscan las decisiones (INV-9): sin esto no se puede saber con qué
    /// reglas se pagó, ni detectar qué decisiones quedaron obsoletas cuando el
    /// sábado se inyecte la regla nueva. La declara el propio `reglas.toml` para
    /// que la traza apunte al artefacto exacto, sin depender de un hash.
    pub version: u32,
    /// Tolerancia de conciliación de importes, en euros.
    ///
    /// Producción la fija a **un céntimo**, que es lo que dice la hoja
    /// `Norma_Pagos_v3` del Excel de control. Absorbe el ruido de redondeo de un
    /// céntimo (IVA al 21%, tercer decimal, el propio Excel que trae basura de
    /// coma flotante) sin abrir la puerta a pagar de más: **dos** céntimos de
    /// diferencia ya caen en R7 (`ESCALAR`). El parámetro se declara aquí para
    /// poder relajarlo o endurecerlo sin recompilar.
    pub tolerancia_importe: Decimal,
    /// Umbral a partir del cual un pago exige revisión manual (reservado).
    pub umbral_pago_maximo: Decimal,
    /// Confianza mínima que debe tener **cada** campo crítico para decidir en
    /// automático.
    ///
    /// Se compara contra el score del `Rastro` de cada campo, no contra un mapa
    /// aparte: la confianza viaja pegada al dato que mide, así que no puede
    /// desincronizarse ni perderse por un nombre de campo mal escrito.
    pub score_minimo: f64,
    /// NIFs cuyo pago está prohibido (regla inyectable del sábado).
    ///
    /// Se guardan ya normalizados como `Nif`: escribir la lista a mano con un
    /// guion (`"B-12345678"`) desactivaría la regla en silencio si se comparase
    /// texto crudo, porque el OCR nunca devuelve el guion. Al ser `Nif`, la
    /// comparación es la misma que la del reconciliador, y una entrada que no
    /// parece un NIF falla al cargar el TOML en vez de no bloquear nada.
    pub prohibido_pagar_proveedor: Vec<Nif>,
    /// Reservado para la regla inyectable de retención de IVA.
    pub retener_iva: bool,
}

impl Default for ReglasConfig {
    fn default() -> Self {
        Self {
            version: 1,
            // Un centimo: coincide con `config/reglas.toml`. Ver el doc de
            // `ReglasConfig::tolerancia_importe` para el porqué.
            tolerancia_importe: Decimal::new(1, 2),       // 0.01
            umbral_pago_maximo: Decimal::new(500_000, 2), // 5000.00
            score_minimo: 0.85,
            prohibido_pagar_proveedor: Vec::new(),
            retener_iva: false,
        }
    }
}

impl ReglasConfig {
    /// Carga la configuración desde una cadena TOML (útil en tests).
    pub fn from_toml_str(s: &str) -> Result<Self, String> {
        toml::from_str(s).map_err(|e| e.to_string())
    }

    /// Carga la configuración desde `config/reglas.toml`.
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, String> {
        let content = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
        Self::from_toml_str(&content)
    }
}

/// Decide qué hacer con una factura a partir de la evidencia reconciliada.
///
/// `factura` es la extracción del PDF, `evidencia` el cruce con ERP y Excel, y
/// `huellas` la bitemporalidad del run (versión de reglas, snapshot, ejecución).
pub fn decidir(
    factura: &Factura,
    evidencia: &Evidencia,
    reglas: &ReglasConfig,
    huellas: Huellas,
) -> Decision {
    let mut evaluadas: Vec<String> = Vec::new();

    // R1 — ¿hay con qué identificar la factura?
    evaluadas.push("R1_sin_identificadores".into());
    if factura.sin_identificadores() {
        // Se distingue "la factura no trae NIF" de "el NIF se leyó pero no se
        // pudo canonizar": lo primero es normal (se concilia por pedido), lo
        // segundo es un fallo de lectura y quien revise el caso necesita saber
        // cuál de los dos es.
        let motivo = if factura.nif_emisor.es_ilegible() {
            "NIF ilegible y sin pedido con el que conciliar".to_string()
        } else {
            "sin identificadores extraíbles (ni NIF ni pedido)".to_string()
        };
        return Decision::new(Resultado::Escalar, motivo, evaluadas, huellas);
    }

    // R2 — proveedor en lista de pago prohibido (regla inyectable del sábado).
    //
    // El NIF se toma del PDF y, si el PDF no lo trajo, del asiento del ERP con
    // el que se ha conciliado. Sin este respaldo, una factura con el NIF
    // ilegible pasaría la criba y se pagaría a un proveedor vetado: R2 es la
    // única regla que no puede depender de que el OCR se porte bien.
    evaluadas.push("R2_prohibido_pagar_proveedor".into());
    let nif_conocido: Option<&Nif> = factura
        .nif_emisor
        .valor()
        .or_else(|| evidencia.asiento.as_ref().map(|a| &a.nif));
    if let Some(nif) = nif_conocido {
        if reglas.prohibido_pagar_proveedor.contains(nif) {
            return Decision::new(
                Resultado::NoPagar,
                format!("proveedor {nif} en lista de pago prohibido"),
                evaluadas,
                huellas,
            );
        }
    }

    // R3 — el ERP manda: un asiento ya pagado no se vuelve a pagar.
    evaluadas.push("R3_erp_pagada".into());
    if let Some(asiento) = evidencia.asiento.as_ref() {
        if asiento.estado == EstadoAsiento::Pagada {
            return Decision::new(
                Resultado::NoPagar,
                format!("asiento {} PAGADA", asiento.asiento_id),
                evaluadas,
                huellas,
            );
        }

        // R4 — conflicto entre fuentes: nunca se paga en automático con datos
        // que se contradicen.
        evaluadas.push("R4_conflicto_fuentes".into());
        if !evidencia.conflictos.is_empty() {
            return Decision::new(
                Resultado::Escalar,
                format!("conflicto entre fuentes: {}", evidencia.conflictos.join("; ")),
                evaluadas,
                huellas,
            );
        }

        // R5 — extracción de calidad dudosa: tampoco se paga en automático.
        //
        // Antes esto era un umbral sobre un mapa `scores` suelto. Ahora se juzga
        // el rastro de cada campo, y el motivo distingue los dos casos porque
        // piden acciones distintas: **ilegible** (había texto y no se pudo
        // canonizar: no se sabe a quién se paga) e **infrafiable** (el valor
        // existe, pero la fuente dudaba de sí misma). El motivo lleva el texto
        // crudo y la línea exacta, que es lo que hace la traza utilizable.
        evaluadas.push("R5_extraccion_dudosa".into());
        let dudosos = campos_de_calidad_dudosa(factura, reglas.score_minimo);
        if !dudosos.is_empty() {
            return Decision::new(
                Resultado::Escalar,
                format!("extracción dudosa en {}", dudosos.join(", ")),
                evaluadas,
                huellas,
            );
        }

        // R6 — asiento pendiente con importe conciliado.
        evaluadas.push("R6_erp_pendiente_conciliado".into());
        if let Some(total) = factura.total.valor() {
            if concilia(*total, asiento.importe, reglas.tolerancia_importe) {
                return Decision::new(
                    Resultado::Pagar,
                    format!(
                        "asiento {} PENDIENTE, importes conciliados",
                        asiento.asiento_id
                    ),
                    evaluadas,
                    huellas,
                );
            }

            // R7 — asiento pendiente pero descuadre de importes.
            evaluadas.push("R7_erp_pendiente_descuadre".into());
            return Decision::new(
                Resultado::Escalar,
                format!(
                    "descuadre: PDF {} vs ERP {}",
                    fmt_eur(*total),
                    fmt_eur(asiento.importe)
                ),
                evaluadas,
                huellas,
            );
        }

        // R7 — pendiente pero sin importe en el PDF: no se puede conciliar.
        evaluadas.push("R7_erp_pendiente_descuadre".into());
        return Decision::new(
            Resultado::Escalar,
            format!(
                "asiento {} PENDIENTE pero el PDF no tiene importe con el que conciliar",
                asiento.asiento_id
            ),
            evaluadas,
            huellas,
        );
    }

    // R8 — no está en el ERP, pero el Excel tiene filas relacionadas.
    evaluadas.push("R8_sin_erp_con_excel".into());
    if !evidencia.excel_filas.is_empty() {
        return Decision::new(
            Resultado::Escalar,
            format!(
                "no consta en ERP; ver filas Excel {}",
                evidencia.excel_filas.join(", ")
            ),
            evaluadas,
            huellas,
        );
    }

    // R9 — ni ERP ni Excel.
    evaluadas.push("R9_sin_match".into());
    Decision::new(
        Resultado::Escalar,
        "no localizada en ninguna fuente (ERP ni Excel)",
        evaluadas,
        huellas,
    )
}

/// ¿Los dos importes concilian dentro de la tolerancia? (`|a − b| ≤ tol`).
///
/// El límite es **inclusivo**, y con `tol = 0` esto degenera en igualdad exacta:
/// no hace falta una rama especial para el caso cero porque `Decimal` es
/// aritmética decimal, no `f64`, así que `1234.5` y `1234.50` comparan iguales
/// pese a tener distinta escala.
pub(crate) fn concilia(a: Decimal, b: Decimal, tolerancia: Decimal) -> bool {
    (a - b).abs() <= tolerancia
}

/// Campos críticos que impiden decidir en automático, con el porqué.
///
/// Los campos **ausentes no cuentan**: no extraer un campo no es un fallo de
/// calidad, y el caso de que no haya ninguno ya lo cubre R1. Juzgar aquí un
/// campo que no se leyó sería inventar una fiabilidad que nadie midió.
///
/// El motivo incluye el texto crudo y la ubicación (página y línea, o fila de
/// Excel) porque quien revise la factura necesita poder ir a mirarla, no solo
/// saber que algo fue mal.
fn campos_de_calidad_dudosa(factura: &Factura, score_minimo: f64) -> Vec<String> {
    factura
        .campos_criticos()
        .iter()
        .filter_map(|campo| match campo.estado {
            EstadoCampo::NoAparece => None,
            EstadoCampo::Ilegible => {
                let crudo = campo.crudo.unwrap_or("¿?");
                let ubicacion = campo.ubicacion();
                Some(if ubicacion.is_empty() {
                    format!("{} ilegible («{crudo}»)", campo.nombre)
                } else {
                    format!("{} ilegible («{crudo}», {ubicacion})", campo.nombre)
                })
            }
            EstadoCampo::Encontrado => campo
                .score
                .filter(|s| *s < score_minimo)
                .map(|s| format!("{} ({s:.2})", campo.nombre)),
        })
        .collect()
}

/// Formatea un importe al estilo español para los motivos de la traza.
pub(crate) fn fmt_eur(v: Decimal) -> String {
    v.round_dp(2).to_string().replace('.', ",")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{Asiento, Identificador, MatchStrategy, Origen};
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).expect("decimal válido")
    }

    fn huellas() -> Huellas {
        Huellas {
            reglas_version: 1,
            erp_snapshot_id: "snap-test".into(),
            run_id: "run-test".into(),
        }
    }

    /// NIF ya canónico, como lo entregaría el parser.
    fn nif(s: &str) -> Nif {
        Nif::nuevo(s).expect("NIF de prueba válido")
    }

    /// Un campo leído con la confianza con la que un parser en buen estado lo
    /// entregaría: por encima del `score_minimo` de producción.
    fn leido<T>(valor: T, crudo: &str) -> Identificador<T> {
        Identificador::encontrado(valor, crudo, 0.98, None)
    }

    fn factura_base() -> Factura {
        Factura {
            nif_emisor: leido(nif("B12345678"), "NIF: B-12345678"),
            pedido: leido("PED-2024-0912".to_string(), "Pedido: PED-2024-0912"),
            numero_factura: leido("F-2024-5518".to_string(), "Factura nº F-2024-5518"),
            fecha: leido("2024-09-02".to_string(), "Fecha: 02/09/2024"),
            base: leido(d("1020.25"), "Base imponible 1.020,25"),
            iva: leido(d("214.25"), "IVA 21% 214,25"),
            total: leido(d("1234.50"), "TOTAL 1.234,50 EUR"),
        }
    }

    fn asiento(estado: EstadoAsiento, importe: &str) -> Asiento {
        Asiento {
            asiento_id: "AS-00412".into(),
            nif: nif("B12345678"),
            pedido: "PED-2024-0912".into(),
            importe: d(importe),
            estado,
            proveedor: Some("Suministros Ibéricos S.L.".into()),
            fecha: Some("2024-09-03".into()),
        }
    }

    fn evidencia(asiento: Option<Asiento>, excel: &[&str], conflictos: &[&str]) -> Evidencia {
        Evidencia {
            asiento_id: asiento.as_ref().map(|a| a.asiento_id.clone()),
            asiento,
            excel_filas: excel.iter().map(|s| s.to_string()).collect(),
            match_por: MatchStrategy::ExactByPedido,
            conflictos: conflictos.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn sin_identificadores_escala() {
        let mut f = factura_base();
        f.nif_emisor = Identificador::no_aparece();
        f.pedido = Identificador::no_aparece();
        let dec = decidir(
            &f,
            &evidencia(None, &[], &[]),
            &ReglasConfig::default(),
            huellas(),
        );
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("sin identificadores"));
    }

    /// Un NIF que se leyó pero no se pudo canonizar no identifica a nadie, y
    /// decir "la factura no trae NIF" ocultaría la verdad: sí venía, fue el OCR
    /// el que no pudo con él.
    #[test]
    fn un_nif_ilegible_sin_pedido_escala_diciendo_que_era_ilegible() {
        let mut f = factura_base();
        f.nif_emisor = Identificador::ilegible("NIF: B1234567B", 0.88, None);
        f.pedido = Identificador::no_aparece();
        let dec = decidir(
            &f,
            &evidencia(None, &[], &[]),
            &ReglasConfig::default(),
            huellas(),
        );
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("ilegible"), "motivo: {}", dec.motivo);
    }

    #[test]
    fn erp_pagada_no_pagar() {
        let ev = evidencia(Some(asiento(EstadoAsiento::Pagada, "1234.50")), &[], &[]);
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::NoPagar);
        assert_eq!(dec.motivo, "asiento AS-00412 PAGADA");
    }

    #[test]
    fn erp_pendiente_conciliado_pagar() {
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Pagar);
        assert!(dec.motivo.contains("importes conciliados"));
    }

    /// Tolerancia de producción = 1 céntimo, y el límite es **inclusivo**
    /// (`|a − b| <= tol`): 0,01 € de diferencia todavía es "conciliado".
    #[test]
    fn un_centimo_de_diferencia_concilia_y_paga() {
        let mut f = factura_base();
        f.total = leido(d("1234.51"), "TOTAL 1.234,51 EUR");
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Pagar);
        assert!(dec.motivo.contains("importes conciliados"), "motivo: {}", dec.motivo);
    }

    /// El centímetro siguiente ya no: **dos** céntimos es descuadre, no ruido.
    /// Es la frontera exacta del valor de `Norma_Pagos_v3`, así que si alguien
    /// toca la tolerancia este test es el que avisa de que se ha movido.
    #[test]
    fn dos_centimos_de_diferencia_ya_son_descuadre_y_escalan() {
        let mut f = factura_base();
        f.total = leido(d("1234.52"), "TOTAL 1.234,52 EUR");
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("descuadre"), "motivo: {}", dec.motivo);
    }

    /// El importe exacto se sigue pagando: sin este test, apretar la tolerancia
    /// podría degenerar en un motor que no paga nunca y nadie lo notaría.
    ///
    /// El PDF trae `1234.5` y el ERP `1234.50`: distinta escala, mismo valor.
    /// `concilia` resta, así que la escala se normaliza sola.
    #[test]
    fn el_importe_exacto_sigue_pagando() {
        let mut f = factura_base();
        f.total = leido(d("1234.5"), "TOTAL 1.234,5 EUR");
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Pagar);
        assert!(dec.motivo.contains("importes conciliados"));
    }

    #[test]
    fn erp_pendiente_descuadre_escala() {
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1200.00")), &[], &[]);
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("descuadre"));
        assert!(dec.motivo.contains("1234,50"));
    }

    #[test]
    fn conflicto_bloquea_pago_automatico() {
        let ev = evidencia(
            Some(asiento(EstadoAsiento::Pendiente, "1234.50")),
            &["Hoja1#42"],
            &["Excel marca PAGADA pero ERP PENDIENTE"],
        );
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("conflicto"));
    }

    #[test]
    fn conflicto_no_revierte_un_pago_ya_liquidado() {
        let ev = evidencia(
            Some(asiento(EstadoAsiento::Pagada, "1234.50")),
            &[],
            &["Excel carece de la fila del asiento"],
        );
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::NoPagar);
    }

    /// El umbral se mide sobre el rastro del propio campo, así que un score
    /// bajo se puede atribuir a un campo concreto y no a "la extracción" en
    /// bloque. El motivo arrastra crudo y ubicación: la traza dice qué texto
    /// era y dónde estaba, no solo que la confianza era baja.
    #[test]
    fn baja_confianza_bloquea_pago_automatico() {
        let mut f = factura_base();
        f.total = Identificador::encontrado(
            d("1234.50"),
            "TOTAL 1.234,50 EUR",
            0.40,
            Some(Origen::Ocr {
                pagina: 0,
                linea: 6,
            }),
        );
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("total (0.40)"), "motivo: {}", dec.motivo);
    }

    /// El caso que antes era invisible: había texto para el NIF, pero no se pudo
    /// canonizar. No es lo mismo que no tener NIF, y no puede decidirse solo.
    #[test]
    fn un_campo_ilegible_bloquea_el_pago_automatico_y_cita_el_crudo() {
        let mut f = factura_base();
        f.nif_emisor = Identificador::ilegible(
            "NIF: B1234567B",
            0.90,
            Some(Origen::Ocr {
                pagina: 0,
                linea: 7,
            }),
        );
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());

        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("nif_emisor ilegible"), "motivo: {}", dec.motivo);
        assert!(dec.motivo.contains("NIF: B1234567B"), "cita el texto: {}", dec.motivo);
        assert!(dec.motivo.contains("pág. 1 línea 8"), "y dónde estaba: {}", dec.motivo);
    }

    /// Un campo que la fuente no trae no es una extracción mala: es una factura
    /// sin ese dato. R5 no puede convertir eso en un ESCALAR, o toda factura sin
    /// nº de factura dejaría de pagarse.
    #[test]
    fn un_campo_ausente_no_dispara_la_duda() {
        let mut f = factura_base();
        f.numero_factura = Identificador::no_aparece();
        f.base = Identificador::no_aparece();
        f.iva = Identificador::no_aparece();
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Pagar);
    }

    #[test]
    fn sin_erp_con_excel_escala() {
        let ev = evidencia(None, &["Hoja1#42", "Hoja2#7"], &[]);
        let dec = decidir(&factura_base(), &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("Hoja1#42"));
    }

    #[test]
    fn sin_match_escala() {
        let dec = decidir(
            &factura_base(),
            &evidencia(None, &[], &[]),
            &ReglasConfig::default(),
            huellas(),
        );
        assert_eq!(dec.resultado, Resultado::Escalar);
        assert!(dec.motivo.contains("ninguna fuente"));
    }

    #[test]
    fn prohibido_pagar_por_nif() {
        let reglas = ReglasConfig {
            prohibido_pagar_proveedor: vec![nif("b12345678")],
            ..ReglasConfig::default()
        };
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&factura_base(), &ev, &reglas, huellas());
        assert_eq!(dec.resultado, Resultado::NoPagar);
        assert!(dec.motivo.contains("prohibido"));
    }

    /// La lista se escribe a mano en el TOML y nadie escribe el guion igual que
    /// lo imprime una factura: `B-12345678` y `B12345678` son el mismo
    /// proveedor. Comparando texto crudo, el guion desactivaba la regla en
    /// silencio; con `Nif` la comparación es la del reconciliador.
    #[test]
    fn lista_con_guion_sigue_bloqueando() {
        let reglas = ReglasConfig {
            prohibido_pagar_proveedor: vec![nif(" b-12345678 ")],
            ..ReglasConfig::default()
        };
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&factura_base(), &ev, &reglas, huellas());
        assert_eq!(dec.resultado, Resultado::NoPagar);
        assert!(dec.motivo.contains("B12345678"), "motivo: {}", dec.motivo);
    }

    /// Agujero que cerró el NIF del ERP: si el OCR no lee el NIF del PDF, la
    /// factura sigue casando por pedido. R2 es la única regla que no puede
    /// depender de que el OCR acierte, así que el veto se comprueba contra el
    /// NIF del asiento. Sin esto, una factura ilegible pagaba a un vetado.
    #[test]
    fn sin_nif_en_pdf_el_veto_lo_detecta_el_erp() {
        let mut f = factura_base();
        f.nif_emisor = Identificador::no_aparece();
        assert!(f.pedido.aparece(), "el pedido sigue casando la factura");

        let reglas = ReglasConfig {
            prohibido_pagar_proveedor: vec![nif("B12345678")],
            ..ReglasConfig::default()
        };
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &reglas, huellas());

        assert_eq!(dec.resultado, Resultado::NoPagar);
        assert!(dec.motivo.contains("B12345678"), "motivo: {}", dec.motivo);
    }

    /// El respaldo del ERP no inventa vetos: si el proveedor no está en la
    /// lista, la factura sin NIF sigue su curso normal.
    #[test]
    fn sin_nif_en_pdf_y_proveedor_limpio_no_bloquea() {
        let mut f = factura_base();
        f.nif_emisor = Identificador::no_aparece();
        let ev = evidencia(Some(asiento(EstadoAsiento::Pendiente, "1234.50")), &[], &[]);
        let dec = decidir(&f, &ev, &ReglasConfig::default(), huellas());
        assert_eq!(dec.resultado, Resultado::Pagar);
    }

    /// Una entrada de la lista que no es un NIF no puede "no bloquear nada":
    /// falla al cargar el TOML, que es el momento en el que hay alguien mirando.
    #[test]
    fn lista_con_entrada_invalida_falla_al_cargar() {
        let toml_src = r#"
            prohibido_pagar_proveedor = ["---"]
        "#;
        assert!(ReglasConfig::from_toml_str(toml_src).is_err());
    }

    #[test]
    fn parsea_reglas_toml_de_produccion() {
        let toml_src = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/config/reglas.toml"
        ))
        .expect("reglas.toml existe");
        let reglas = ReglasConfig::from_toml_str(&toml_src).expect("TOML válido");
        assert_eq!(reglas.version, 2);
        assert_eq!(reglas.tolerancia_importe, d("0.01"));
        // El defecto del código y el fichero de producción no pueden divergir:
        // si divergen, `cargo test --bins` pasa y el lote real decide distinto.
        assert_eq!(reglas.tolerancia_importe, ReglasConfig::default().tolerancia_importe);
        assert_eq!(reglas.umbral_pago_maximo, d("5000.00"));
        assert!((reglas.score_minimo - 0.85).abs() < 1e-9);
        assert!(reglas.prohibido_pagar_proveedor.is_empty());
        assert!(!reglas.retener_iva);
    }
}
