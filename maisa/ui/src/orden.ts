/**
 * Ordenar la tabla.
 *
 * Esto es del navegador y solo del navegador: la API devuelve siempre el listado
 * ordenado por `file_id` (lo hace `buscar()` en `traza.py`, y `filtros.ts` lo
 * imita para que el congelado cuadre), asi que pedirle otro orden seria anadir una
 * capacidad que no tiene. Con 500 filas ya en memoria, ordenar aqui es inmediato y
 * no gasta una peticion por clic de cabecera.
 *
 * La consecuencia hay que tenerla clara: **el orden por defecto tiene que ser
 * exactamente el de la API**. Si `file_id` se comparase aqui con
 * `localeCompare`, los 65 `file_id` con acentos cambiarian de sitio respecto a lo
 * que devuelve el servidor y el mismo panel ensenaria dos ordenes distintos segun
 * si hay red. Por eso `file_id` se compara con `<`, que es lo que hacen Python y
 * `filtros.ts`.
 */

import type { FacturaResumen } from "./api/types";

/** Las columnas por las que se puede ordenar. `file_id` es la de por defecto. */
export const CAMPOS_ORDEN = ["file_id", "fecha", "proveedor", "total", "desvio_importe"] as const;
export type CampoOrden = (typeof CAMPOS_ORDEN)[number];

export interface Orden {
    campo: CampoOrden;
    ascendente: boolean;
}

/** El de la API. No es "el que mas nos gusta", es el que ya traen los datos. */
export const ORDEN_POR_DEFECTO: Orden = { campo: "file_id", ascendente: true };

/** El orden tal como viaja en la direccion: `fecha:desc`. */
export function leerOrden(valor: string | null): Orden {
    if (!valor) return ORDEN_POR_DEFECTO;
    const [campo, sentido] = valor.split(":");
    if (!CAMPOS_ORDEN.includes(campo as CampoOrden)) return ORDEN_POR_DEFECTO;
    return { campo: campo as CampoOrden, ascendente: sentido !== "desc" };
}

export function escribirOrden(orden: Orden): string | null {
    if (orden.campo === ORDEN_POR_DEFECTO.campo && orden.ascendente) return null;
    return `${orden.campo}:${orden.ascendente ? "asc" : "desc"}`;
}

/** Lo que no hay: `null`, `undefined` o cadena vacia. Los tres se van al final. */
function esVacio(valor: unknown): boolean {
    return valor === null || valor === undefined || valor === "";
}

/** El orden natural del campo, sin signo. El sentido lo aplica quien llama. */
function comparar(a: unknown, b: unknown, campo: CampoOrden): number {
    if (typeof a === "number" && typeof b === "number") return a - b;
    const textoA = String(a);
    const textoB = String(b);
    // Ver la cabecera: `file_id` se compara a pelo para no discrepar de la API.
    if (campo === "file_id") return textoA < textoB ? -1 : textoA > textoB ? 1 : 0;
    return textoA.localeCompare(textoB, "es", { numeric: true });
}

/**
 * Una copia ordenada. No muta la entrada.
 *
 * Dos decisiones que no son obvias:
 *
 * - **Lo vacio va al final mire para donde mire el orden.** Ocho facturas no
 *   tienen fecha y `desvio_importe` falta siempre que falte un lado de la resta.
 *   Si al invertir el orden esos huecos saltaran a la cabecera, la primera
 *   pantalla de "ordenar por fecha, descendente" serian ocho guiones en vez de las
 *   facturas mas recientes, que es justo lo que se ha ido a mirar.
 * - **Los empates se rompen por `file_id`.** Sin esto, dos facturas del mismo
 *   proveedor pueden intercambiarse entre renders y la tabla parece moverse sola.
 */
export function aplicarOrden(items: readonly FacturaResumen[], orden: Orden): FacturaResumen[] {
    const sentido = orden.ascendente ? 1 : -1;
    return [...items].sort((a, b) => {
        const va = a[orden.campo];
        const vb = b[orden.campo];
        const vacioA = esVacio(va);
        const vacioB = esVacio(vb);
        if (vacioA || vacioB) {
            if (vacioA && vacioB) return comparar(a.file_id, b.file_id, "file_id");
            return vacioA ? 1 : -1;
        }
        const diferencia = sentido * comparar(va, vb, orden.campo);
        if (diferencia !== 0) return diferencia;
        return comparar(a.file_id, b.file_id, "file_id");
    });
}

/** El siguiente estado de la cabecera al pulsarla: alterna sentido, o empieza por ahi. */
export function alternarOrden(actual: Orden, campo: CampoOrden): Orden {
    if (actual.campo !== campo) return { campo, ascendente: true };
    return { campo, ascendente: !actual.ascendente };
}
