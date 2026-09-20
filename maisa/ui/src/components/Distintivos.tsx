/**
 * Los distintivos: el resultado y la severidad de un hecho, ya pintados.
 *
 * Los dos leen de `theme.ts` y **nunca** deciden un color por su cuenta. El
 * sentido de tener esto en un componente es que la tabla y el detalle usen el
 * mismo: si cada uno se pintara el suyo, una fila ambar podria abrir un detalle
 * rojo y nadie se enteraria hasta que alguien tomara una decision mirando la
 * pantalla equivocada.
 */

import { severidadHecho } from "../api/severidad";
import type { Hecho, Resultado } from "../api/types";
import { ESTILO_RESULTADO, ESTILO_SEVERIDAD } from "../theme";

/** La pildora con su punto de color. */
function Pildora({ clases, punto, children }: { clases: string; punto: string; children: string }) {
    return (
        <span className={clases}>
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${punto}`} aria-hidden="true" />
            {children}
        </span>
    );
}

export function DistintivoResultado({ resultado }: { resultado: Resultado }) {
    const estilo = ESTILO_RESULTADO[resultado];
    return (
        <Pildora clases={estilo.pildora} punto={estilo.punto}>
            {estilo.etiqueta}
        </Pildora>
    );
}

/**
 * La severidad de un hecho, que no es lo mismo que "si se cumple o no".
 *
 * El texto de la pildora importa: "Se cumple" y "Bloquea el pago" son las dos
 * caras utiles, pero en medio esta "Aviso" y "Anomalia reportada", que son las que
 * evitan que alguien lea un incumplimiento que no bloquea como si bloqueara.
 */
export function DistintivoSeveridad({ hecho }: { hecho: Hecho }) {
    const estilo = ESTILO_SEVERIDAD[severidadHecho(hecho)];
    return (
        <Pildora clases={estilo.pildora} punto={estilo.punto}>
            {estilo.etiqueta}
        </Pildora>
    );
}

/** Una pildora neutra, para datos que son informacion y no un estado. */
export function DistintivoNeutro({ children }: { children: string }) {
    return (
        <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs font-medium text-slate-600">
            {children}
        </span>
    );
}

/** Una pildora de aviso, para lo que hay que mirar aunque no decida el resultado. */
export function DistintivoAviso({ children }: { children: string }) {
    return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800">
            {children}
        </span>
    );
}
