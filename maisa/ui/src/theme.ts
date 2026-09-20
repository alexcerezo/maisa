/**
 * Como se pinta cada estado: las clases de Tailwind, en un solo sitio.
 *
 * `severidad.ts` decide **que significa** un hecho y **como se pinta es cosa de
 * los componentes** — su cabecera ya avisaba de que las clases de Tailwind
 * vivirian aqui. Este es ese fichero, y existe por el mismo motivo que
 * `severidad.ts`: la tabla y el detalle tienen que estar de acuerdo. Si la tabla
 * pinta un `ESCALAR` ambar y el detalle lo pinta azul, el panel se contradice a
 * si mismo en la misma pantalla, y en un panel que decide pagos eso es peor que
 * un bug de pintado.
 *
 * Dos criterios al elegir los colores, porque no son decorativos:
 *
 * 1. **`ok` no llama la atencion.** De 500 facturas, 448 se pagan sin un solo
 *    motivo: pintarlas de verde fuerte dejaria la pantalla llena de color y el
 *    ojo no iria a las 52 que importan. Lo que cumple se pinta en gris.
 *
 * 2. **El rojo no es un error del sistema.** `NO_PAGAR` es el motor
 *    funcionando: detecto un pago duplicado y lo paro. Y `bloquea` solo lo
 *    produce R5. Por eso el rojo significa "aqui hay algo que no se paga", no
 *    "algo se ha roto".
 *
 * Todas las clases traen su variante `dark:` explicita. El tema por defecto de
 * shadcn trae claro y oscuro, y un color de estado pensado solo para claro queda
 * ilegible sobre fondo oscuro (un `text-red-700` sobre `oklch(0.145)` no se lee).
 */

import {
    Ban,
    CircleAlert,
    CircleCheck,
    CircleHelp,
    ScanLine,
    ShieldAlert,
    TriangleAlert,
    type LucideIcon,
} from "lucide-react";

import type { Gravedad, Severidad } from "./api/severidad";
import type { EstadoCola, Regla, Resultado } from "./api/types";

/** El texto que se lee en el badge. En castellano, como el resto del panel. */
export const ETIQUETA_RESULTADO: Record<Resultado, string> = {
    PAGAR: "Pagar",
    ESCALAR: "Escalar",
    NO_PAGAR: "No pagar",
};

/**
 * El color del resultado. El mismo par de clases vale para el badge de la tabla
 * y para el del detalle: por eso esta aqui y no duplicado.
 */
export const CLASE_RESULTADO: Record<Resultado, string> = {
    PAGAR: "border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-400/10 dark:text-emerald-300",
    ESCALAR: "border-amber-600/30 bg-amber-500/10 text-amber-700 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-300",
    NO_PAGAR: "border-red-600/30 bg-red-500/10 text-red-700 dark:border-red-400/30 dark:bg-red-400/10 dark:text-red-300",
};

export const ICONO_RESULTADO: Record<Resultado, LucideIcon> = {
    PAGAR: CircleCheck,
    ESCALAR: CircleHelp,
    NO_PAGAR: Ban,
};

/**
 * El nombre de cada regla, para la lista de hechos.
 *
 * `R1_identidad` es un identificador de programa y no se ensena tal cual: en la
 * lista de hechos, "R5_estado" no le dice nada a quien mira y "Estado del
 * pedido" si. La clave sigue siendo la del contrato (`Regla`), asi que si la
 * norma anade una regla el compilador obliga a nombrarla aqui.
 */
export const ETIQUETA_REGLA: Record<Regla, string> = {
    R1_identidad: "Identidad del proveedor",
    R2_pedido: "Pedido del ERP",
    R3_iva: "Base, IVA y total",
    R4_fecha: "Fecha",
    R5_estado: "Estado del pedido",
    R6_anomalia: "Anomalías",
    R7_divisa: "Divisa",
};

/**
 * Que comprueba cada regla, en una linea. Va en el `title` del hecho.
 *
 * Sin esto, un hecho que dice "Cumple" sobre "R3_iva" no explica que se ha
 * comprobado, y en una pantalla de auditoria "cumple" sin decir que es no vale
 * como evidencia.
 */
export const EXPLICACION_REGLA: Record<Regla, string> = {
    R1_identidad: "Que el NIF y el IBAN del documento coincidan con el maestro de proveedores.",
    R2_pedido: "Que el pedido de la factura exista en el ERP y sea el correcto.",
    R3_iva: "Que base, IVA y total cuadren entre sí y con el importe del asiento.",
    R4_fecha: "Que la fecha de la factura sea coherente con el pedido y el asiento.",
    R5_estado: "Que el pedido del ERP no esté ya pagado. Es la única regla que bloquea el pago.",
    R6_anomalia: "Pagos duplicados, documentos repetidos y otras señales de alarma.",
    R7_divisa:
        "Que el importe esté en la divisa del ERP. Si el documento factura en otra, se escala sin convertir: no hay tipo de cambio en ningún sitio.",
};

/** Como se leyo el documento, en castellano. */
export const ETIQUETA_METODO: Record<string, string> = {
    texto_determinista: "Texto del PDF",
    vision_ocr: "Visión OCR",
};

/** De donde salio el texto. */
export const ETIQUETA_ESCALON: Record<string, string> = {
    capa_texto: "Capa de texto",
    cache_ocr: "Caché de OCR",
};

/** El texto del hecho, leido como severidad y no como regla. */
export const ETIQUETA_SEVERIDAD: Record<Severidad, string> = {
    bloquea: "Bloquea el pago",
    aviso: "Aviso",
    anomalia: "Anomalía",
    ok: "Cumple",
};

export const CLASE_SEVERIDAD: Record<Severidad, string> = {
    bloquea: "border-red-600/30 bg-red-500/10 text-red-700 dark:border-red-400/30 dark:bg-red-400/10 dark:text-red-300",
    aviso: "border-amber-600/30 bg-amber-500/10 text-amber-700 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-300",
    anomalia: "border-sky-600/30 bg-sky-500/10 text-sky-700 dark:border-sky-400/30 dark:bg-sky-400/10 dark:text-sky-300",
    ok: "border-border bg-muted text-muted-foreground",
};

export const ICONO_SEVERIDAD: Record<Severidad, LucideIcon> = {
    bloquea: Ban,
    aviso: TriangleAlert,
    anomalia: CircleAlert,
    ok: CircleCheck,
};

/** El color del filete lateral de un hecho, para que la lista se lea en vertical. */
export const FILETE_SEVERIDAD: Record<Severidad, string> = {
    bloquea: "before:bg-red-500/70",
    aviso: "before:bg-amber-500/70",
    anomalia: "before:bg-sky-500/70",
    ok: "before:bg-border",
};

/** El color del numero grande de una tarjeta de contador. */
export const CLASE_GRAVEDAD: Record<Gravedad, string> = {
    verde: "text-emerald-700 dark:text-emerald-300",
    ambar: "text-amber-700 dark:text-amber-300",
    rojo: "text-red-700 dark:text-red-300",
};

/** El color de un desvio de importe: sin desvio no hay nada que mirar. */
export function claseDesvio(desvio: number | null | undefined): string {
    if (desvio === null || desvio === undefined || desvio === 0) return "text-muted-foreground";
    return "font-medium text-amber-700 dark:text-amber-300";
}

/** El icono de "como se leyo el documento", para la columna de trazabilidad. */
export const ICONO_ESCALON: Record<string, LucideIcon> = {
    capa_texto: ScanLine,
    cache_ocr: ShieldAlert,
};

/**
 * En que ha quedado la **segunda lectura** de una factura escalada.
 *
 * El tipo se declara en `api/types.ts`, junto al resto del contrato, porque
 * tambien lo usa el filtro de la vista. Aqui solo se decide como se pinta.
 *
 * Es un estado propio y no un `Resultado` porque no lo es: una escalada con
 * `confirmable` sigue siendo `ESCALAR` en la entrega —la norma no se toca—, lo
 * que cambia es **cuanto trabajo humano le queda por delante**. Mezclar las dos
 * cosas en un solo tipo haria creer que el motor cambio su decision, y no la
 * cambio.
 */
export const ETIQUETA_COLA: Record<EstadoCola, string> = {
    confirmable: "Se puede cerrar",
    desvio: "Desvío de pago",
    sin_conclusion: "La relectura no concluye",
};

/**
 * El color del estado de cola. Sigue la misma regla que `CLASE_RESULTADO`: el
 * verde dice "aqui ya no hay que hacer nada", el rojo "aqui hay una persona
 * obligada" y el ambar "aqui sigue habiendo duda". Ninguno es "el sistema se ha
 * roto", que es lo que diria el `destructive` de shadcn.
 */
export const CLASE_COLA: Record<EstadoCola, string> = {
    confirmable:
        "border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-400/10 dark:text-emerald-300",
    desvio: "border-red-600/30 bg-red-500/10 text-red-700 dark:border-red-400/30 dark:bg-red-400/10 dark:text-red-300",
    sin_conclusion:
        "border-amber-600/30 bg-amber-500/10 text-amber-700 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-300",
};

export const ICONO_COLA: Record<EstadoCola, LucideIcon> = {
    confirmable: CircleCheck,
    desvio: Ban,
    sin_conclusion: TriangleAlert,
};

/**
 * Que significa cada estado, para el `title` y los `Tooltip`.
 *
 * Vive aqui y no en el componente porque el mismo texto lo usan la tabla y el
 * expediente: si cada pantalla escribiera el suyo, la misma fila se explicaria de
 * dos formas distintas y el panel se contradiria a si mismo.
 *
 * La frase de `confirmable` empieza por el verbo a proposito: el dato util no es
 * "esta confirmada", es "**no hace falta abrirla**", que es el trabajo que ahorra.
 */
export const EXPLICACION_COLA: Record<EstadoCola, string> = {
    confirmable:
        "La segunda lectura ha releido el documento y los datos que faltaban cuadran con el maestro. No hace falta abrir el PDF: la incidencia se puede cerrar tal cual.",
    desvio:
        "La segunda lectura ha encontrado un dato que no es el del maestro. No se puede cerrar sola: aqui hay una persona obligada antes de pagar.",
    sin_conclusion:
        "La segunda lectura ha releido el documento y no ha desatado el nudo. La revision humana sigue haciendo la misma falta que antes de releerlo.",
};

/**
 * El estado de cola de una factura, o `null` si nadie la ha releido.
 *
 * Se reexporta desde el contrato para que los componentes tengan un solo sitio
 * del que tirar (`@/theme`) sin que la regla viva dos veces.
 */
export { estadoCola } from "./api/types";
