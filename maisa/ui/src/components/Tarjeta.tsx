/**
 * La tarjeta y las filas de datos del detalle.
 *
 * Son dos piezas pequenas que se repiten mucho, y estan juntas porque se usan
 * juntas: sin un `Dato` comun, cada bloque del detalle acabaria con su propia
 * separacion entre la etiqueta y el valor, y la columna se veria desalineada de
 * una tarjeta a la siguiente.
 */

import type { ReactNode } from "react";

import { TARJETA } from "../theme";

export function Tarjeta({
    titulo,
    acciones,
    children,
}: {
    titulo: string;
    /** A la derecha del titulo: un contador, un boton, un distintivo. */
    acciones?: ReactNode;
    children: ReactNode;
}) {
    return (
        <section className={`${TARJETA} overflow-hidden`}>
            <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/70 px-4 py-2.5">
                <h2 className="text-sm font-semibold text-slate-800">{titulo}</h2>
                {acciones}
            </header>
            <div className="px-4 py-3">{children}</div>
        </section>
    );
}

/**
 * Una fila "etiqueta: valor".
 *
 * El valor es `ReactNode` y no `string` porque a veces es un distintivo o un
 * importe con color, y esas cosas no se pueden reducir a texto.
 */
export function Dato({
    etiqueta,
    children,
    mono = false,
}: {
    etiqueta: string;
    children: ReactNode;
    /** Para identificadores y huellas, que se comparan a ojo. */
    mono?: boolean;
}) {
    return (
        <div className="flex items-baseline justify-between gap-4 py-1">
            <dt className="shrink-0 text-xs text-slate-500">{etiqueta}</dt>
            <dd
                className={`text-right text-sm break-words text-slate-900 ${mono ? "font-mono text-xs" : ""
                    }`}
            >
                {children}
            </dd>
        </div>
    );
}

/** La lista de datos. Es solo el `<dl>` con los separadores. */
export function Datos({ children }: { children: ReactNode }) {
    return <dl className="divide-y divide-slate-100">{children}</dl>;
}
