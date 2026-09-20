/**
 * Los contadores de arriba, y a la vez el selector de resultado.
 *
 * Los dos papeles en una sola pieza a proposito. Con 500 facturas, 43 escaladas y
 * 9 bloqueadas, saber cuantas hay de cada clase y poder ir a verlas son la misma
 * pregunta: si los numeros estuvieran en un sitio y las pestanas en otro, habria
 * que mirar dos veces lo mismo y adivinar si coinciden.
 *
 * Un aviso sobre estos numeros, porque se presta a confusion: son **del lote
 * entero**, no de la busqueda que haya puesta. Los da `/api/estadisticas`, que
 * cuenta el corpus completo. Los que salen de los filtros son los de la tabla
 * ("N de M coincidencias"), y por eso los dos sitios lo dicen con palabras
 * distintas en vez de ensenar dos cifras iguales que no lo son.
 */

import type { ReactNode } from "react";

import type { Estadisticas, Resultado } from "../api/types";
import { ESTILO_RESULTADO, ORDEN_RESULTADOS } from "../theme";

function Teja({
    activa,
    etiqueta,
    valor,
    descripcion,
    color,
    alPulsar,
}: {
    activa: boolean;
    etiqueta: string;
    valor: string;
    descripcion: string;
    /** Color del numero. El borde de "elegida" es siempre el mismo. */
    color: string;
    alPulsar: () => void;
}) {
    return (
        <button
            type="button"
            onClick={alPulsar}
            aria-pressed={activa}
            title={descripcion}
            className={`rounded-xl border bg-white px-4 py-3 text-left transition ${activa
                    ? "border-slate-900 shadow-sm"
                    : "border-slate-200 hover:border-slate-300 hover:shadow-sm"
                }`}
        >
            <span className="block text-xs font-medium text-slate-500">{etiqueta}</span>
            <span className={`mt-1 block text-2xl font-semibold tabular-nums ${color}`}>{valor}</span>
        </button>
    );
}

/** Un apunte de la linea de abajo, con su etiqueta en gris. */
function Apunte({ etiqueta, children }: { etiqueta: string; children: ReactNode }) {
    return (
        <span className="inline-flex items-baseline gap-1">
            <span className="text-slate-500">{etiqueta}</span>
            <span className="font-medium text-slate-700">{children}</span>
        </span>
    );
}

export function PanelEstadisticas({
    estadisticas,
    comprobando,
    activo,
    alElegir,
}: {
    estadisticas: Estadisticas | null;
    /**
     * Se esta averiguando de donde leer. Sin este dato, un `null` de contadores
     * se leeria como "el congelado no los trae", que es una de las dos causas
     * posibles pero no la que pasa al arrancar: durante la comprobacion no se
     * sabe todavia, y afirmarlo seria inventarse el diagnostico.
     */
    comprobando: boolean;
    activo: Resultado | null;
    alElegir: (resultado: Resultado | null) => void;
}) {
    if (!estadisticas) {
        return (
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500">
                {comprobando
                    ? "Contadores: mirando de donde se leen los datos…"
                    : "Sin contadores: el origen no trae el fichero de estadisticas. La tabla y el detalle funcionan igual."}
            </div>
        );
    }

    const entrega = estadisticas.entrega;

    return (
        <div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Teja
                    activa={activo === null}
                    etiqueta="Todas"
                    valor={String(estadisticas.total)}
                    descripcion="El lote completo, sin filtrar."
                    color="text-slate-900"
                    alPulsar={() => alElegir(null)}
                />
                {ORDEN_RESULTADOS.map((resultado) => (
                    <Teja
                        key={resultado}
                        activa={activo === resultado}
                        etiqueta={ESTILO_RESULTADO[resultado].etiqueta}
                        valor={String(estadisticas.por_resultado[resultado])}
                        descripcion={`Filtrar por las facturas que el motor decidio ${ESTILO_RESULTADO[
                            resultado
                        ].etiqueta.toLowerCase()}.`}
                        color={ESTILO_RESULTADO[resultado].texto}
                        alPulsar={() => alElegir(activo === resultado ? null : resultado)}
                    />
                ))}
            </div>

            {/*
             * La linea de control. No es decoracion: si `coincide_con_traza` fuera
             * falso, todo lo que hay en pantalla estaria en duda, y si Mongo no
             * responde, el listado sigue saliendo del fichero de traza pero el
             * panel tiene que decirlo en vez de parecer completo.
             */}
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                <Apunte etiqueta="Entrega:">
                    {entrega.total} lineas, {entrega.lineas_invalidas} invalidas,{" "}
                    {entrega.coincide_con_traza ? (
                        <span className="text-emerald-700">coincide con la traza</span>
                    ) : (
                        <span className="text-rose-700">NO coincide con la traza</span>
                    )}
                </Apunte>
                <Apunte etiqueta="Asientos vigentes:">{estadisticas.asientos_vigentes}</Apunte>
                <Apunte etiqueta="Mongo:">
                    {estadisticas.mongo.ok ? (
                        <span className="text-emerald-700">responde</span>
                    ) : (
                        <span className="text-rose-700" title={estadisticas.mongo.error ?? undefined}>
                            no responde
                        </span>
                    )}
                </Apunte>
                <Apunte etiqueta="Lectura:">
                    {Object.entries(estadisticas.por_metodo_lectura)
                        .map(([metodo, cuantas]) => `${cuantas} por ${metodo.replace("_", " ")}`)
                        .join(" · ")}
                </Apunte>
                <Apunte etiqueta="Lote:">
                    {Object.entries(estadisticas.por_lote)
                        .map(([lote, cuantas]) => `${lote}: ${cuantas}`)
                        .join(" · ")}
                </Apunte>
                {estadisticas.resultados_desconocidos > 0 ? (
                    <span className="text-amber-700">
                        {estadisticas.resultados_desconocidos} facturas con un resultado que este
                        panel no sabe leer
                    </span>
                ) : null}
            </div>
        </div>
    );
}
