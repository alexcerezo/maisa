/**
 * Los tres estados que no son "hay datos": cargando, fallo y vacio.
 *
 * Existen porque una tabla vacia es ambigua. Con 500 facturas en el lote, "no hay
 * filas" puede significar tres cosas muy distintas —que aun se esta pidiendo, que
 * la peticion ha fallado, o que el filtro no casa con nada— y cada una se arregla
 * de una manera. Un hueco en blanco obliga a adivinar cual es; estos tres bloques
 * lo dicen y, cuando se puede, ofrecen el boton que lo resuelve.
 */

import type { ReactNode } from "react";

import { BOTON, BOTON_PRIMARIO, TARJETA } from "../theme";

/**
 * El mensaje de un error, ya en castellano.
 *
 * Casi todos los mensajes de este panel vienen redactados desde abajo
 * (`cliente.ts`, `datos.ts`) precisamente para poder ensenarlos tal cual, asi que
 * aqui **no se traduce nada**: se pinta el mensaje y, si lo trae, el codigo, que
 * es lo unico util para buscar en la API.
 *
 * Se acepta tambien una cadena suelta porque no todos los fallos son objetos de
 * error: `useFuente` devuelve el suyo como texto (un fallo al comprobar la fuente
 * no es una peticion fallida, es "no he podido saber de donde leer"). Envolverlo
 * en un `Error` solo para pasarlo por aqui seria ruido.
 */
function ContenidoFallo({ error }: { error: Error | string }) {
    const mensaje = typeof error === "string" ? error : error.message;
    const codigo = typeof error === "string" ? null : (error as { codigo?: string | null }).codigo;
    return (
        <>
            <p className="text-sm text-rose-900">{mensaje}</p>
            {codigo ? <p className="mt-1 font-mono text-xs text-rose-700/80">{codigo}</p> : null}
        </>
    );
}

export function Fallo({
    error,
    alReintentar,
    children,
}: {
    error: Error | string;
    alReintentar?: () => void;
    /** Un apunte extra: que hacer, o por que pasa. */
    children?: ReactNode;
}) {
    return (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4">
            <div className="flex items-start gap-3">
                <span aria-hidden="true" className="mt-0.5 text-rose-500">
                    ▲
                </span>
                <div className="min-w-0 flex-1">
                    <ContenidoFallo error={error} />
                    {children ? <div className="mt-2 text-xs text-rose-800/90">{children}</div> : null}
                    {alReintentar ? (
                        <button type="button" onClick={alReintentar} className={`${BOTON} mt-3`}>
                            Volver a intentarlo
                        </button>
                    ) : null}
                </div>
            </div>
        </div>
    );
}

export function Cargando({ queEs }: { queEs: string }) {
    return (
        <div className="flex items-center justify-center gap-3 py-16 text-slate-500">
            <span
                aria-hidden="true"
                className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700"
            />
            <span className="text-sm">Cargando {queEs}…</span>
        </div>
    );
}

export function Vacio({
    titulo,
    children,
    accion,
}: {
    titulo: string;
    children?: ReactNode;
    accion?: { etiqueta: string; alPulsar: () => void };
}) {
    return (
        <div className={`${TARJETA} px-6 py-12 text-center`}>
            <p className="text-sm font-medium text-slate-700">{titulo}</p>
            {children ? (
                <div className="mx-auto mt-2 max-w-xl text-sm text-slate-500">{children}</div>
            ) : null}
            {accion ? (
                <button type="button" onClick={accion.alPulsar} className={`${BOTON_PRIMARIO} mt-4`}>
                    {accion.etiqueta}
                </button>
            ) : null}
        </div>
    );
}
