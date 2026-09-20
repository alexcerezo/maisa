/**
 * Una regla evaluada, contada entera.
 *
 * Se ensenan **las seis**, las que se cumplen y las que no, y no solo los
 * problemas. En un panel que decide pagos, "no hay nada aqui" y "no se ha
 * evaluado" se parecen demasiado: si la ficha listara solo los fallos, una factura
 * con R2 sin evaluar tendria el mismo aspecto que una con R2 limpia.
 *
 * Los tres indicadores del pie (`ok`, `duro`, `informativo`) se pintan tal cual
 * porque son la letra de la norma y **no significan lo mismo**:
 *
 * - `duro: true` es lo unico que para el pago, y solo R5 lo pone (9 veces).
 * - `informativo: true` es un aviso que el motor puso a proposito.
 * - incumplir sin ser ninguna de las dos cosas es una anomalia reportada sin
 *   concluir, que no es lo mismo ni se arregla igual.
 *
 * Ver la tabla de `severidadHecho` en `api/severidad.ts`.
 */

import { Fragment } from "react";

import type { Hecho } from "../api/types";
import { siNo, valorLegible } from "../formato";
import { DESCRIPCION_REGLA, ETIQUETA_REGLA, TARJETA } from "../theme";
import { DistintivoSeveridad } from "./Distintivos";

export function FichaHecho({ hecho }: { hecho: Hecho }) {
    const datos = Object.entries(hecho.datos);

    return (
        <article className={`${TARJETA} p-4`}>
            <header className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold text-slate-900">
                    {ETIQUETA_REGLA[hecho.regla]}
                </h3>
                <DistintivoSeveridad hecho={hecho} />
                {hecho.nombre ? (
                    <span className="font-mono text-xs text-slate-500">{hecho.nombre}</span>
                ) : null}
            </header>

            <p className="mt-2 text-sm text-slate-700">{valorLegible(hecho.motivo)}</p>
            <p className="mt-1 text-xs text-slate-400">{DESCRIPCION_REGLA[hecho.regla]}</p>

            {datos.length > 0 ? (
                <dl className="mt-3 grid gap-x-4 gap-y-1 border-t border-slate-100 pt-3 sm:grid-cols-[11rem_1fr]">
                    {datos.map(([clave, valor]) => (
                        <Fragment key={clave}>
                            <dt className="font-mono text-xs text-slate-500">{clave}</dt>
                            <dd className="text-xs break-words text-slate-700">
                                {valorLegible(valor)}
                            </dd>
                        </Fragment>
                    ))}
                </dl>
            ) : null}

            <footer className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-slate-100 pt-2 text-xs text-slate-500">
                <span>ok: {siNo(hecho.ok)}</span>
                <span>duro: {siNo(hecho.duro)}</span>
                <span>informativo: {siNo(hecho.informativo)}</span>
            </footer>
        </article>
    );
}
