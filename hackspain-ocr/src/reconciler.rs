// Factura ↔ ERP ↔ Excel → Evidencia (spec_y_plan.md §3.4).
//
// Es la pieza que convierte "tres fuentes que se contradicen" en un objeto
// `Evidencia` reconstruible, que es lo único que consume el motor de reglas.
// El reconciliador **no decide**: solo reúne hechos y explicita discrepancias.
//
// Estrategias, en orden de fuerza (la primera que acierta gana):
//   1. `ExactByPedido`  — el `pedido` del PDF está en el ERP (fuerte).
//   2. `ByNifYImporte`  — mismo NIF e importe dentro de ±tolerancia (medio).
//   3. `ByNifUnico`     — el NIF aparece en un único asiento del ERP (débil).
//   4. Cruce con Excel: se adjuntan las filas relacionadas como contexto.
//
// Ámbitos de discrepancia (qué entra en `Evidencia::conflictos`):
//   - Ambigüedad: varios asientos candidatos con la misma clave.
//   - Incoherencia PDF↔ERP que no sea de importe (NIF distinto, pedido ausente).
//   - Contradicción Excel↔ERP de **estado** ("Excel PAGADA vs ERP PENDIENTE").
//   - Contradicción Excel↔ERP de **importe**.
//
// Excepción deliberada: el descuadre de importe **PDF↔ERP** NO se registra como
// conflicto. Tiene su propia regla (`R7_erp_pendiente_descuadre` en `rules.rs`),
// con un motivo más preciso (`"descuadre: PDF 1234,50 vs ERP 1200,00"`). Si se
// duplicase aquí, `R4_conflicto_fuentes` lo taparía con un mensaje peor.
//
// La normalización de identidad (`normalizar_texto`, `normalizar_clave`, `Nif`)
// vive en `domain.rs`, no aquí: emparejar y comparar deben usar **la misma**
// definición de "el mismo proveedor".

use std::collections::HashMap;

use rust_decimal::Decimal;

use crate::domain::{
    normalizar_clave, normalizar_texto, Asiento, EstadoAsiento, Evidencia, Factura, FilaExcel,
    MatchStrategy, Nif,
};
use crate::rules::{concilia, fmt_eur};

// ---------------------------------------------------------------------------
// Índices en memoria
// ---------------------------------------------------------------------------

/// Índice del catálogo de asientos del ERP. El manual recomienda descargar los
/// asientos una vez al arranque (spec §1.3); esto es lo que se construye encima.
#[derive(Debug, Default)]
pub struct IndiceErp<'a> {
    por_pedido: HashMap<String, Vec<&'a Asiento>>,
    por_nif: HashMap<Nif, Vec<&'a Asiento>>,
}

impl<'a> IndiceErp<'a> {
    pub fn nuevo(asientos: &'a [Asiento]) -> Self {
        let mut indice = Self::default();
        for asiento in asientos {
            indice
                .por_pedido
                .entry(normalizar_clave(&asiento.pedido))
                .or_default()
                .push(asiento);
            // `Asiento::nif` ya es un `Nif` canónico: indexar no normaliza nada,
            // solo copia una clave que ya es comparable por construcción.
            indice
                .por_nif
                .entry(asiento.nif.clone())
                .or_default()
                .push(asiento);
        }
        indice
    }

    /// Asientos cuyo `pedido` normalizado coincide con la clave dada.
    pub fn por_pedido(&self, clave: &str) -> &[&'a Asiento] {
        self.por_pedido.get(clave).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Asientos cuyo NIF coincide con el dado. Como `Nif` es canónico, dos NIFs
    /// escritos distinto caen en la misma entrada sin trabajo extra.
    pub fn por_nif(&self, nif: &Nif) -> &[&'a Asiento] {
        self.por_nif.get(nif).map(Vec::as_slice).unwrap_or(&[])
    }

    /// ¿No hay ningún asiento indexado?
    ///
    /// Hoy solo lo usan los tests: es la condición que distinguirá "el ERP
    /// contestó que no hay nada" de "el ERP no contestó", que no son lo mismo
    /// cuando `erp.rs` haga la descarga de verdad.
    #[allow(dead_code)]
    pub fn esta_vacio(&self) -> bool {
        self.por_pedido.is_empty() && self.por_nif.is_empty()
    }
}

/// Índice de las filas del Excel caótico. El Excel es contexto, nunca verdad.
#[derive(Debug, Default)]
pub struct IndiceExcel<'a> {
    por_pedido: HashMap<String, Vec<&'a FilaExcel>>,
    por_nif: HashMap<Nif, Vec<&'a FilaExcel>>,
}

impl<'a> IndiceExcel<'a> {
    pub fn nuevo(filas: &'a [FilaExcel]) -> Self {
        let mut indice = Self::default();
        for fila in filas {
            if let Some(pedido) = fila.pedido.as_deref() {
                let clave = normalizar_clave(pedido);
                if !clave.is_empty() {
                    indice.por_pedido.entry(clave).or_default().push(fila);
                }
            }
            if let Some(nif) = fila.nif.as_ref() {
                indice.por_nif.entry(nif.clone()).or_default().push(fila);
            }
        }
        indice
    }

    pub fn por_pedido(&self, clave: &str) -> &[&'a FilaExcel] {
        self.por_pedido.get(clave).map(Vec::as_slice).unwrap_or(&[])
    }

    pub fn por_nif(&self, nif: &Nif) -> &[&'a FilaExcel] {
        self.por_nif.get(nif).map(Vec::as_slice).unwrap_or(&[])
    }
}

// ---------------------------------------------------------------------------
// Conciliación
// ---------------------------------------------------------------------------

/// Reúne toda la evidencia disponible sobre una factura.
///
/// `tolerancia` es `ReglasConfig::tolerancia_importe`: el mismo valor que usa el
/// motor, para que "conciliado" signifique lo mismo en las dos fases.
pub fn conciliar(
    factura: &Factura,
    erp: &IndiceErp<'_>,
    excel: &IndiceExcel<'_>,
    tolerancia: Decimal,
) -> Evidencia {
    // `Factura::nif_emisor` ya es un `Nif` canónico: aquí no hay nada que
    // normalizar, solo se pasa la referencia. Si el campo es ILEGIBLE no hay
    // valor, así que la factura no podrá casar por NIF: no se concilia con un
    // identificador que no se pudo leer, ni siquiera si el crudo "se parece"
    // a un NIF del ERP. Habrá que casarla por pedido o escalarla.
    let nif_pdf: Option<&Nif> = factura.nif_emisor.valor();
    let pedido_pdf: Option<String> = factura
        .pedido
        .valor()
        .map(|pedido| normalizar_clave(pedido))
        .filter(|clave| !clave.is_empty());

    let mut conflictos: Vec<String> = Vec::new();
    let mut match_por = MatchStrategy::None;
    let mut asiento: Option<&Asiento> = None;

    // --- Estrategia 1 (fuerte): pedido exacto ------------------------------
    if let Some(pedido) = &pedido_pdf {
        let candidatos = erp.por_pedido(pedido);
        match candidatos.len() {
            0 => {}
            1 => {
                asiento = Some(candidatos[0]);
                match_por = MatchStrategy::ExactByPedido;
            }
            n => conflictos.push(format!(
                "ambigüedad: {n} asientos del ERP comparten el pedido {} (candidatos: {})",
                factura.pedido.valor().map(String::as_str).unwrap_or(""),
                etiquetas(candidatos)
            )),
        }
    }

    // --- Estrategia 2 (media): mismo NIF e importe ±tolerancia -------------
    if asiento.is_none() {
        if let (Some(nif), Some(total)) = (nif_pdf, factura.total.valor()) {
            let candidatos: Vec<&Asiento> = erp
                .por_nif(nif)
                .iter()
                .copied()
                .filter(|a| concilia(a.importe, *total, tolerancia))
                .collect();
            match candidatos.len() {
                0 => {}
                1 => {
                    asiento = Some(candidatos[0]);
                    match_por = MatchStrategy::ByNifYImporte;
                }
                n => conflictos.push(format!(
                    "ambigüedad: {n} asientos del ERP con NIF {nif} e importe {} (candidatos: {})",
                    fmt_eur(*total),
                    etiquetas(&candidatos)
                )),
            }
        }
    }

    // --- Estrategia 3 (débil): el NIF aparece en un único asiento ----------
    if asiento.is_none() {
        if let Some(nif) = nif_pdf {
            let candidatos = erp.por_nif(nif);
            if candidatos.len() == 1 {
                asiento = Some(candidatos[0]);
                match_por = MatchStrategy::ByNifUnico;
            }
        }
    }

    // --- Incoherencias PDF ↔ ERP ------------------------------------------
    if let Some(encontrado) = asiento {
        if match_por == MatchStrategy::ExactByPedido {
            if let Some(nif) = nif_pdf {
                if &encontrado.nif != nif {
                    conflictos.push(format!(
                        "NIF PDF {nif} vs NIF ERP {} (asiento {})",
                        encontrado.nif, encontrado.asiento_id
                    ));
                }
            }
        } else if let Some(pedido) = &pedido_pdf {
            // Emparejado por NIF, pero el pedido del PDF apunta a otro sitio.
            let pedido_erp = normalizar_clave(&encontrado.pedido);
            if !pedido_erp.is_empty() && pedido_erp != *pedido {
                conflictos.push(format!(
                    "pedido PDF {} no consta en el ERP; el asiento {} tiene pedido {}",
                    factura.pedido.valor().map(String::as_str).unwrap_or(""),
                    encontrado.asiento_id,
                    encontrado.pedido
                ));
            }
        }
    }

    // --- Cruce con el Excel (§3.4.4) --------------------------------------
    let filas = filas_relacionadas(pedido_pdf.as_ref(), nif_pdf, excel);
    let excel_filas: Vec<String> = filas.iter().map(|f| f.id.clone()).collect();

    for fila in &filas {
        if let Some(encontrado) = asiento {
            // Contradicción de estado: el Excel no puede contradecir al ERP en silencio.
            if let Some(estado_excel) = fila.estado.as_deref() {
                let estado = normalizar_texto(estado_excel);
                let erp_pagada = encontrado.estado == EstadoAsiento::Pagada;
                let excel_pagada = estado.contains("PAGAD");
                let excel_pendiente = estado.contains("PENDIENT");
                if excel_pagada && !erp_pagada {
                    conflictos.push(format!(
                        "Excel marca PAGADA pero ERP PENDIENTE (asiento {}, fila {})",
                        encontrado.asiento_id, fila.id
                    ));
                } else if excel_pendiente && erp_pagada {
                    conflictos.push(format!(
                        "Excel marca PENDIENTE pero ERP PAGADA (asiento {}, fila {})",
                        encontrado.asiento_id, fila.id
                    ));
                }
            }
            // Contradicción de importe.
            if let Some(importe) = fila.importe {
                if !concilia(encontrado.importe, importe, tolerancia) {
                    conflictos.push(format!(
                        "importe Excel {} vs ERP {} ({})",
                        fmt_eur(importe),
                        fmt_eur(encontrado.importe),
                        fila.id
                    ));
                }
            }
        }
    }

    Evidencia {
        asiento_id: asiento.map(|a| a.asiento_id.clone()),
        asiento: asiento.cloned(),
        excel_filas,
        match_por,
        conflictos,
    }
}

/// Filas del Excel relacionadas por pedido o por NIF, sin duplicar.
fn filas_relacionadas<'a>(
    pedido_pdf: Option<&String>,
    nif_pdf: Option<&Nif>,
    excel: &IndiceExcel<'a>,
) -> Vec<&'a FilaExcel> {
    let mut filas: Vec<&FilaExcel> = Vec::new();
    if let Some(pedido) = pedido_pdf {
        filas.extend(excel.por_pedido(pedido).iter().copied());
    }
    if let Some(nif) = nif_pdf {
        for fila in excel.por_nif(nif).iter().copied() {
            if !filas.iter().any(|ya| ya.id == fila.id) {
                filas.push(fila);
            }
        }
    }
    filas
}

/// Lista de `asiento_id` legible para los motivos de ambigüedad.
fn etiquetas(asientos: &[&Asiento]) -> String {
    asientos
        .iter()
        .map(|a| a.asiento_id.clone())
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Identificador;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).expect("decimal válido")
    }

    /// NIF ya canónico, como lo entregaría el parser.
    fn nif(s: &str) -> Nif {
        Nif::nuevo(s).expect("NIF de prueba válido")
    }

    fn asiento(id: &str, nif_bruto: &str, pedido: &str, importe: &str, estado: EstadoAsiento) -> Asiento {
        Asiento {
            asiento_id: id.into(),
            nif: nif(nif_bruto),
            pedido: pedido.into(),
            importe: d(importe),
            estado,
            proveedor: None,
            fecha: None,
        }
    }

    fn factura(nif_bruto: Option<&str>, pedido: Option<&str>, total: Option<&str>) -> Factura {
        Factura {
            nif_emisor: nif_bruto.map_or_else(Identificador::no_aparece, |bruto| {
                Identificador::encontrado(nif(bruto), format!("NIF: {bruto}"), 0.98, None)
            }),
            // El CIF del cliente no participa en la conciliación: quien casa la
            // factura con el asiento es el NIF del emisor, el pedido y el
            // importe. Se rellena igual para que la factura de prueba sea la que
            // produce el parser, no una a la que le falta un campo.
            cif_cliente: Identificador::encontrado(nif("A58231074"), "CIF: A58231074", 0.98, None),
            pedido: pedido.map_or_else(Identificador::no_aparece, |p| {
                Identificador::encontrado(p.to_string(), format!("Pedido: {p}"), 0.98, None)
            }),
            numero_factura: Identificador::encontrado(
                "F-2024-001".to_string(),
                "Factura F-2024-001",
                0.98,
                None,
            ),
            fecha: Identificador::no_aparece(),
            base: Identificador::no_aparece(),
            iva: Identificador::no_aparece(),
            total: total.map_or_else(Identificador::no_aparece, |t| {
                Identificador::encontrado(d(t), format!("TOTAL {t}"), 0.98, None)
            }),
        }
    }

    /// Tolerancia de producción: **un céntimo**, el valor de `Norma_Pagos_v3`.
    /// Es el mismo valor que `ReglasConfig::default().tolerancia_importe`, porque
    /// "conciliado" tiene que significar lo mismo al emparejar y al decidir. Si
    /// esto se separa del valor real, los tests dejan de probar el
    /// comportamiento real.
    const TOL: &str = "0.01";

    #[test]
    fn normaliza_claves_de_pedido_y_nif() {
        assert_eq!(normalizar_clave(" ped-00123 "), "PED00123");
        assert_eq!(normalizar_clave("PED/00123"), "PED00123");
        assert_eq!(normalizar_clave("b-12345678"), "B12345678");
        assert_eq!(normalizar_texto("  Factura   nº 12 "), "FACTURA Nº 12");
        assert_eq!(normalizar_texto("gestión"), "GESTION");
    }

    #[test]
    fn match_fuerte_por_pedido() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);
        let excel = IndiceExcel::default();

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &excel,
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::ExactByPedido);
        assert_eq!(ev.asiento_id.as_deref(), Some("AS-00412"));
        assert!(ev.conflictos.is_empty(), "conflictos: {:?}", ev.conflictos);
    }

    /// Un NIF ilegible no es un NIF: aunque el texto que se leyó se parezca al de
    /// un asiento, no se usa para emparejar. Conciliar con un identificador que
    /// no se pudo canonizar sería pagar contra una suposición; sin pedido, la
    /// factura tiene que acabar en revisión (R1/R5), no en `PAGAR`.
    #[test]
    fn un_nif_ilegible_no_empareja_por_nif() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let mut f = factura(None, None, Some("1234.50"));
        f.nif_emisor = Identificador::ilegible("NIF: B1234567B", 0.62, None);

        let ev = conciliar(&f, &erp, &IndiceExcel::default(), d(TOL));

        assert_eq!(ev.match_por, MatchStrategy::None);
        assert!(ev.asiento.is_none(), "el ERP no se puede tocar sin identificar");
    }

    /// Match medio en su frontera: un céntimo de diferencia (la tolerancia
    /// exacta) todavía empareja.
    #[test]
    fn match_medio_por_nif_e_importe_dentro_de_tolerancia() {
        let asientos = vec![
            asiento("AS-00100", "B12345678", "PED-99999", "300.00", EstadoAsiento::Pendiente),
            asiento("AS-00412", "B12345678", "PED-00123", "1234.50", EstadoAsiento::Pendiente),
        ];
        let erp = IndiceErp::nuevo(&asientos);

        // Sin pedido en el PDF: se cae a (NIF, importe).
        let ev = conciliar(
            &factura(Some("B12345678"), None, Some("1234.49")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::ByNifYImporte);
        assert_eq!(ev.asiento_id.as_deref(), Some("AS-00412"));
    }

    /// Dos céntimos de ruido ya invalidan el emparejamiento por importe. Como los
    /// dos asientos comparten NIF, tampoco se puede caer al match débil: la
    /// factura queda sin asiento y acabará en `ESCALAR`. Es deliberado — no se
    /// paga contra un importe "que casi cuadra".
    #[test]
    fn dos_centimos_de_ruido_impiden_el_match_por_importe() {
        let asientos = vec![
            asiento("AS-00100", "B12345678", "PED-99999", "300.00", EstadoAsiento::Pendiente),
            asiento("AS-00412", "B12345678", "PED-00123", "1234.50", EstadoAsiento::Pendiente),
        ];
        let erp = IndiceErp::nuevo(&asientos);

        let ev = conciliar(
            &factura(Some("B12345678"), None, Some("1234.48")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::None);
        assert!(ev.asiento_id.is_none());
    }

    #[test]
    fn match_debil_por_nif_unico_conserva_el_asiento_para_la_regla_de_descuadre() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1200.00",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        // El pedido del PDF no existe en el ERP, pero el NIF es único.
        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-77777"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::ByNifUnico);
        assert_eq!(ev.asiento_id.as_deref(), Some("AS-00412"));
        assert_eq!(ev.conflictos.len(), 1);
        assert!(
            ev.conflictos[0].contains("pedido PDF PED-77777 no consta en el ERP"),
            "conflictos: {:?}",
            ev.conflictos
        );
    }

    #[test]
    fn no_inventa_match_cuando_hay_varios_asientos_con_el_mismo_nif() {
        let asientos = vec![
            asiento("AS-00100", "B12345678", "PED-00001", "300.00", EstadoAsiento::Pendiente),
            asiento("AS-00412", "B12345678", "PED-00002", "900.00", EstadoAsiento::Pendiente),
        ];
        let erp = IndiceErp::nuevo(&asientos);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-77777"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::None);
        assert!(ev.asiento_id.is_none());
    }

    #[test]
    fn pedido_repetido_en_el_erp_es_ambiguedad() {
        let asientos = vec![
            asiento("AS-00100", "B87654321", "PED-00123", "300.00", EstadoAsiento::Pendiente),
            asiento("AS-00412", "B99999999", "PED-00123", "300.00", EstadoAsiento::Pagada),
        ];
        let erp = IndiceErp::nuevo(&asientos);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("300.00")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        // El pedido es ambiguo y el NIF del PDF no desempata: no hay match.
        assert_eq!(ev.match_por, MatchStrategy::None);
        assert!(ev.asiento_id.is_none());
        assert!(
            ev.conflictos[0].contains("2 asientos del ERP comparten el pedido PED-00123"),
            "conflictos: {:?}",
            ev.conflictos
        );
        assert!(ev.conflictos[0].contains("AS-00100"));
        assert!(ev.conflictos[0].contains("AS-00412"));
    }

    #[test]
    fn nif_distinto_entre_pdf_y_erp_conflicta() {
        let asientos = vec![asiento(
            "AS-00412",
            "B87654321",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::ExactByPedido);
        assert_eq!(ev.conflictos.len(), 1);
        assert!(
            ev.conflictos[0].contains("NIF PDF B12345678 vs NIF ERP B87654321"),
            "conflictos: {:?}",
            ev.conflictos
        );
    }

    #[test]
    fn excel_contradice_el_estado_del_erp() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);
        let filas = vec![FilaExcel {
            id: "Hoja1#42".into(),
            nif: Some(nif("B12345678")),
            pedido: Some("PED-00123".into()),
            importe: Some(d("1234.50")),
            estado: Some("pagada".into()),
            crudo: [
                ("Estado".to_string(), "pagada".to_string()),
                ("Observaciones".to_string(), "pagada en junio".to_string()),
            ]
            .into_iter()
            .collect(),
        }];
        let excel = IndiceExcel::nuevo(&filas);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &excel,
            d(TOL),
        );

        assert_eq!(ev.excel_filas, vec!["Hoja1#42".to_string()]);
        assert_eq!(ev.conflictos.len(), 1);
        assert!(
            ev.conflictos[0].contains("Excel marca PAGADA pero ERP PENDIENTE"),
            "conflictos: {:?}",
            ev.conflictos
        );
    }

    #[test]
    fn excel_con_importe_distinto_conflicta_sin_duplicar_filas() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);
        // La misma fila aparece por pedido y por NIF: debe listarse una sola vez.
        let filas = vec![FilaExcel {
            id: "Hoja1#42".into(),
            nif: Some(nif("B12345678")),
            pedido: Some("PED-00123".into()),
            importe: Some(d("1200.00")),
            estado: None,
            crudo: Default::default(),
        }];
        let excel = IndiceExcel::nuevo(&filas);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &excel,
            d(TOL),
        );

        assert_eq!(ev.excel_filas, vec!["Hoja1#42".to_string()]);
        assert_eq!(ev.conflictos.len(), 1);
        assert!(
            ev.conflictos[0].contains("importe Excel 1200,00 vs ERP 1234,50 (Hoja1#42)"),
            "conflictos: {:?}",
            ev.conflictos
        );
    }

    #[test]
    fn el_descuadre_pdf_erp_no_se_duplica_como_conflicto() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1200.00",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::ExactByPedido);
        assert!(ev.conflictos.is_empty(), "conflictos: {:?}", ev.conflictos);
    }

    #[test]
    fn excel_aporta_contexto_aunque_no_haya_asiento() {
        let erp = IndiceErp::nuevo(&[]);
        let filas = vec![FilaExcel {
            id: "Hoja1#42".into(),
            nif: Some(nif("B12345678")),
            pedido: Some("PED-00123".into()),
            importe: Some(d("1234.50")),
            estado: Some("PENDIENTE".into()),
            crudo: Default::default(),
        }];
        let excel = IndiceExcel::nuevo(&filas);

        let ev = conciliar(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &excel,
            d(TOL),
        );

        assert_eq!(ev.match_por, MatchStrategy::None);
        assert!(ev.asiento.is_none());
        assert_eq!(ev.excel_filas, vec!["Hoja1#42".to_string()]);
        // Sin asiento no hay nada que contradecir: el Excel solo es contexto.
        assert!(ev.conflictos.is_empty());
    }

    #[test]
    fn indice_erp_vacio_detecta_ausencia_total() {
        let erp = IndiceErp::nuevo(&[]);
        assert!(erp.esta_vacio());
        assert!(erp.por_pedido("PED00123").is_empty());
        assert!(erp.por_nif(&nif("B12345678")).is_empty());
    }

    // -----------------------------------------------------------------------
    // End-to-end: conciliar + decidir. Es el camino real del pipeline
    // (`reconciler::conciliar` → `rules::decidir`), el que alimenta el JSONL.
    // -----------------------------------------------------------------------

    fn huellas() -> crate::domain::Huellas {
        crate::domain::Huellas {
            reglas_version: 3,
            erp_snapshot_id: "snap-2024-11-16T10:00Z".into(),
            run_id: "run-test".into(),
        }
    }

    /// Equivale a `main`: concilia y decide con la misma tolerancia.
    fn pipeline(
        factura: &Factura,
        erp: &IndiceErp<'_>,
        excel: &IndiceExcel<'_>,
    ) -> (Evidencia, crate::domain::Decision) {
        let reglas = crate::rules::ReglasConfig::default();
        let evidencia = conciliar(factura, erp, excel, reglas.tolerancia_importe);
        let decision = crate::rules::decidir(factura, &evidencia, &reglas, huellas());
        (evidencia, decision)
    }

    #[test]
    fn e2e_paga_cuando_el_erp_esta_pendiente_y_todo_cuadra() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let (_ev, decision) = pipeline(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
        );

        assert_eq!(decision.resultado.as_str(), "PAGAR");
        assert_eq!(decision.motivo, "asiento AS-00412 PENDIENTE, importes conciliados");
    }

    #[test]
    fn e2e_no_paga_cuando_el_erp_ya_esta_pagada() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pagada,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let (_ev, decision) = pipeline(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
        );

        assert_eq!(decision.resultado.as_str(), "NO_PAGAR");
        assert_eq!(decision.motivo, "asiento AS-00412 PAGADA");
    }

    #[test]
    fn e2e_escala_cuando_el_excel_contradice_al_erp() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1234.50",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);
        let filas = vec![FilaExcel {
            id: "Hoja1#42".into(),
            nif: Some(nif("B12345678")),
            pedido: Some("PED-00123".into()),
            importe: Some(d("1234.50")),
            estado: Some("PAGADA".into()),
            crudo: Default::default(),
        }];
        let excel = IndiceExcel::nuevo(&filas);

        let (_ev, decision) = pipeline(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &excel,
        );

        // El ERP dice PENDIENTE (se podría pagar), pero el Excel lo contradice:
        // en la duda, ESCALAR y que lo mire una persona.
        assert_eq!(decision.resultado.as_str(), "ESCALAR");
        assert!(
            decision.motivo.contains("conflicto entre fuentes"),
            "motivo: {}",
            decision.motivo
        );
        assert!(decision.motivo.contains("Excel marca PAGADA pero ERP PENDIENTE"));
        assert!(decision.reglas_evaluadas.contains(&"R4_conflicto_fuentes".to_string()));
    }

    #[test]
    fn e2e_escala_por_descuadre_y_conserva_el_asiento_en_la_traza() {
        let asientos = vec![asiento(
            "AS-00412",
            "B12345678",
            "PED-00123",
            "1200.00",
            EstadoAsiento::Pendiente,
        )];
        let erp = IndiceErp::nuevo(&asientos);

        let (ev, decision) = pipeline(
            &factura(Some("B12345678"), Some("PED-00123"), Some("1234.50")),
            &erp,
            &IndiceExcel::default(),
        );

        assert_eq!(decision.resultado.as_str(), "ESCALAR");
        assert_eq!(decision.motivo, "descuadre: PDF 1234,50 vs ERP 1200,00");
        // La traza guarda contra qué se comparó: decisión reconstruible.
        assert_eq!(ev.asiento_id.as_deref(), Some("AS-00412"));
        assert_eq!(ev.asiento.unwrap().importe, d("1200.00"));
        assert_eq!(decision.huellas.reglas_version, 3);
    }
}

