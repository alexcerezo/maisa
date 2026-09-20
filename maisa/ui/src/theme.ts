/**
 * Como se pinta cada estado, en un solo sitio.
 *
 * `api/severidad.ts` decide **que significa** un hecho; aqui se decide **como se
 * ve**. La separacion no es ceremonia: la tabla y el detalle leen de este
 * fichero, asi que una fila ambar y la pantalla a la que da paso no pueden salir
 * de distinto color. Un panel que decide pagos no puede contradecirse entre la
 * lista y el detalle.
 *
 * Las clases se escriben **enteras y literales** (`"bg-emerald-50"`) en vez de
 * componerlas por trozos (`"bg-" + color`). Tailwind no ejecuta el codigo: lee el
 * fuente buscando cadenas completas. Una clase montada con `+` no la encuentra,
 * no la genera, y el color simplemente no aparece sin que falle nada. Es un fallo
 * mudo, de los peores de depurar.
 *
 * La paleta es la de Tailwind sin inventar tonos nuevos, y cada color esta puesto
 * por lo que significa, no por lo que se siente: `NO_PAGAR` se pinta en rojo pero
 * **no es un error del sistema**, es el sistema funcionando (detecto un pago
 * duplicado y lo paro). El que pide ayuda es `ESCALAR`, que es el ambar.
 */

import type { Gravedad, Severidad } from "./api/severidad";
import type { MetodoLectura, Regla, Resultado } from "./api/types";

export interface EstiloEstado {
    /** El texto del distintivo, ya en castellano. */
    etiqueta: string;
    /** La pildora completa: lo que se pega en la tabla y en el detalle. */
    pildora: string;
    /** Solo el punto de color que la precede, para las listas. */
    punto: string;
    /** Color del texto suelto (el desvio, un importe negativo). */
    texto: string;
    /** Fondo muy suave, para una fila o una banda. */
    fondo: string;
}

/** La base comun de todas las pildoras. Se repite literal en cada estilo a proposito. */
const BASE = "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium";

/**
 * El orden en que se ensenan los resultados: de menos a mas atencion.
 *
 * **No** es el orden de `RESULTADOS` en `types.ts` (que es PAGAR, NO_PAGAR,
 * ESCALAR, el de la norma). Aqui se ordena por lo que le importa a quien mira la
 * pantalla: primero lo que esta bien, y despues lo que le va a pedir tiempo, que
 * es escalar. Se declara aparte en vez de reordenar la constante del contrato,
 * porque aquella es el orden de la norma y no se toca.
 */
export const ORDEN_RESULTADOS: readonly Resultado[] = ["PAGAR", "ESCALAR", "NO_PAGAR"];

export const ESTILO_RESULTADO: Record<Resultado, EstiloEstado> = {
    PAGAR: {
        etiqueta: "Pagar",
        pildora: `${BASE} border-emerald-200 bg-emerald-50 text-emerald-700`,
        punto: "bg-emerald-500",
        texto: "text-emerald-700",
        fondo: "bg-emerald-50",
    },
    ESCALAR: {
        etiqueta: "Escalar",
        pildora: `${BASE} border-amber-200 bg-amber-50 text-amber-800`,
        punto: "bg-amber-500",
        texto: "text-amber-800",
        fondo: "bg-amber-50",
    },
    NO_PAGAR: {
        etiqueta: "No pagar",
        pildora: `${BASE} border-rose-200 bg-rose-50 text-rose-700`,
        punto: "bg-rose-500",
        texto: "text-rose-700",
        fondo: "bg-rose-50",
    },
};

/**
 * La frase que acompana al resultado en la cabecera del detalle. Es la diferencia
 * entre ensenar la decision y explicarla, y en `NO_PAGAR` importa mucho: sin
 * texto, un rojo se lee como averia.
 */
export const DESCRIPCION_RESULTADO: Record<Resultado, string> = {
    PAGAR: "La norma no encuentra nada que impida el pago.",
    ESCALAR: "El motor no lo tiene claro: esta factura la tiene que mirar una persona.",
    NO_PAGAR: "Hay un incumplimiento duro de la norma. El motor para el pago a proposito.",
};

/** `Gravedad` es la escala de la decision; los colores son los mismos tres. */
export const ESTILO_GRAVEDAD: Record<Gravedad, EstiloEstado> = {
    verde: ESTILO_RESULTADO.PAGAR,
    ambar: ESTILO_RESULTADO.ESCALAR,
    rojo: ESTILO_RESULTADO.NO_PAGAR,
};

/**
 * La severidad de un hecho.
 *
 * Aqui es donde se nota que `ok`, `duro` e `informativo` son tres cosas y no una.
 * `anomalia` (incumple, no bloquea y nadie lo declaro informativo) se pinta en
 * azul y no en ambar para que se distinga de `aviso`: el aviso lo puso el motor a
 * proposito, la anomalia es algo que reporto sin concluir. Confundirlos haria que
 * 20 ordenes inyectadas se leyeran igual que un IBAN que no cuadra.
 */
export const ESTILO_SEVERIDAD: Record<Severidad, EstiloEstado> = {
    ok: {
        etiqueta: "Se cumple",
        pildora: `${BASE} border-emerald-200 bg-emerald-50 text-emerald-700`,
        punto: "bg-emerald-500",
        texto: "text-emerald-700",
        fondo: "bg-emerald-50",
    },
    bloquea: {
        etiqueta: "Bloquea el pago",
        pildora: `${BASE} border-rose-200 bg-rose-50 text-rose-700`,
        punto: "bg-rose-500",
        texto: "text-rose-700",
        fondo: "bg-rose-50",
    },
    aviso: {
        etiqueta: "Aviso",
        pildora: `${BASE} border-amber-200 bg-amber-50 text-amber-800`,
        punto: "bg-amber-500",
        texto: "text-amber-800",
        fondo: "bg-amber-50",
    },
    anomalia: {
        etiqueta: "Anomalia reportada",
        pildora: `${BASE} border-sky-200 bg-sky-50 text-sky-700`,
        punto: "bg-sky-500",
        texto: "text-sky-700",
        fondo: "bg-sky-50",
    },
};

/**
 * El nombre de cada regla y **que comprueba**, copiado literal de
 * `motor/config/reglas.toml`.
 *
 * Se copia y no se reescribe porque la norma se versiona como datos
 * (`norma_v3.1`): si manana cambia la descripcion alli, esto queda viejo y hay
 * que actualizarlo en los dos sitios. La alternativa era pedir el `reglas.toml`
 * al servidor, que no se sirve, o parafrasearlo, que es como se acaba afirmando
 * en pantalla algo que la norma no dice.
 */
export const ETIQUETA_REGLA: Record<Regla, string> = {
    R1_identidad: "R1 · Identidad",
    R2_pedido: "R2 · Pedido",
    R3_iva: "R3 · IVA",
    R4_fecha: "R4 · Fecha",
    R5_estado: "R5 · Estado",
    R6_anomalia: "R6 · Anomalia",
};

export const DESCRIPCION_REGLA: Record<Regla, string> = {
    R1_identidad: "NIF en el maestro y IBAN de la factura igual al del maestro",
    R2_pedido: "El pedido existe, es del proveedor y el importe cuadra (tol 0,01)",
    R3_iva: "IVA bien calculado y total = base + IVA (tol 0,01)",
    R4_fecha: "La fecha es valida y no futura",
    R5_estado: "Estado ERP del pedido PENDIENTE; nunca pagar dos veces",
    R6_anomalia: "Cualquier anomalia que un humano deba ver: ESCALAR con motivo",
};

/**
 * Como se leyo el documento. Es un dato que en el detalle importa: 29 de las 500
 * pasaron por vision porque su capa de texto no daba la talla, y saberlo explica
 * media docena de motivos.
 */
export const ETIQUETA_METODO: Record<MetodoLectura, string> = {
    texto_determinista: "Texto del PDF",
    vision_ocr: "Vision (OCR)",
};

/** Clases compartidas de las tarjetas, para que no haya dos bordes distintos. */
export const TARJETA = "rounded-xl border border-slate-200 bg-white shadow-sm";

/**
 * Los controles. Estan juntos por el mismo motivo que los colores: asi el boton
 * de "reintentar" de la banda de fuente y el de "limpiar filtros" no se parecen
 * solo "mas o menos".
 */
export const CAMPO =
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-slate-500 focus:ring-2 focus:ring-slate-900/10";

export const ETIQUETA_CAMPO = "mb-1 block text-xs font-medium text-slate-600";

export const BOTON =
    "inline-flex items-center justify-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40";

export const BOTON_PRIMARIO =
    "inline-flex items-center justify-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40";

/** Un enlace con aspecto de boton, para "volver" y para las acciones de la banda. */
export const ENLACE_BOTON = `${BOTON} no-underline`;

/** El texto pequeno de apoyo. Repetirlo a mano acaba en cinco grises distintos. */
export const APUNTE = "text-xs text-slate-500";
