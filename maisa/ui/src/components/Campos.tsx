/**
 * La lectura cruda del documento, para quien quiera comprobar el motor.
 *
 * Va plegada y en un `<details>` sin estado de React porque es la unica parte de
 * la ficha que no hace falta para decidir: quien mira el resultado ya lo tiene
 * arriba. Pero cuando hay que discutir una decision, es aqui donde se ve de donde
 * salio cada dato, y sin esto habria que abrir el PDF y hacer el trabajo a mano.
 *
 * Se ensenan **todas** las claves, sin filtrar, y ordenadas alfabeticamente. Dos
 * razones para no seleccionar las "importantes":
 *
 * 1. La lista cambia de una factura a otra: entre 5 y 26 claves, y solo 4 son
 *    universales (`file_id`, `metodo_lectura`, `pedido_candidatos`, `notas`). Un
 *    filtro por lista blanca se comeria justo la clave rara que explica el caso.
 * 2. Los importes aqui son **texto** (`"3012.89"`), no numeros: se ensenan tal
 *    cual vinieron, porque la coherencia del documento original es parte de lo
 *    que se esta auditando.
 */

import { Fragment } from "react";

import type { CamposFactura } from "../api/types";
import { valorLegible } from "../formato";

/** Los textos largos (la nota del documento, los avisos) rompen linea; los cortos, no. */
const CLAVES_LARGAS = new Set(["nota_documento", "notas", "notas_importe", "sospechosos", "ordenes_resultado"]);

export function Campos({ campos }: { campos: CamposFactura }) {
    const entradas = Object.entries(campos).sort(([a], [b]) => a.localeCompare(b));

    return (
        <details className="rounded-xl border border-slate-200 bg-white shadow-sm">
            <summary className="cursor-pointer rounded-xl px-4 py-3 text-sm font-medium text-slate-700 select-none hover:bg-slate-50">
                Datos crudos de la lectura
                <span className="ml-2 text-xs font-normal text-slate-400">
                    {entradas.length} {entradas.length === 1 ? "clave" : "claves"}
                </span>
            </summary>

            <dl className="grid gap-x-4 gap-y-2 border-t border-slate-100 px-4 py-3 sm:grid-cols-[13rem_1fr]">
                {entradas.map(([clave, valor]) => (
                    <Fragment key={clave}>
                        <dt className="font-mono text-xs text-slate-500">{clave}</dt>
                        <dd
                            className={`text-xs break-words text-slate-700 ${CLAVES_LARGAS.has(clave) ? "whitespace-pre-wrap" : ""
                                }`}
                        >
                            {valorLegible(valor)}
                        </dd>
                    </Fragment>
                ))}
            </dl>

            <p className="border-t border-slate-100 px-4 py-2 text-xs text-slate-400">
                Tal como lo leyo el motor, sin normalizar. Aqui los importes vienen como texto.
            </p>
        </details>
    );
}
