/**
 * La descarga del ERP: lo que costo traerla y lo que hubo que reintentar.
 *
 * Por que este panel existe
 * -------------------------
 * La rubrica pide ensenar **reintentos** junto al estado, la evidencia y la
 * latencia. El resto de verbos ya tenian sitio en la pantalla (los contadores, la
 * cola de la segunda lectura, los ms de cada lectura); los reintentos no tenian
 * ninguno, y el dato llevaba desde el principio en Mongo sin que nadie lo
 * pintara. Un reintento que solo se cuenta en la defensa es una anecdota; en
 * pantalla, con su hora y su duracion al lado, es una medida.
 *
 * De que son los reintentos (esto importa y no se puede decir de otra forma)
 * -------------------------------------------------------------------------
 * Son de la **descarga de asientos**, no de la decision de cada factura. Cuando
 * el ERP contesta `ORA-00600` o corta la sesion, quien reintenta es el cliente
 * del ERP (`motor/src/maisa/erp.py`), y el reintento queda contado aqui una vez
 * por descarga, no 516 veces.
 *
 * La resiliencia de la decision es otra cosa y vive en otro sitio: el motor **no
 * reintenta una lectura**, la escala. Por eso el reintento de una factura se
 * enseña en la cola de segunda lectura (la tarjeta de al lado) y no en este
 * panel. Mezclar los dos numeros en una sola cifra de "reintentos" seria la
 * mentira facil: sumaria 2 reintentos de red con 9 escaladas de decision y
 * ninguno de los dos seria el numero que se esta viendo.
 *
 * Por que `estado` se pinta aunque sea "COMPLETO"
 * ----------------------------------------------
 * Por el mismo motivo por el que `TarjetasEstadisticas` ensena la salud con todo
 * verde: una descarga a medias y una descarga entera se parecen demasiado si la
 * unica que habla es la que va mal.
 */

import { AlertTriangle, Database, RefreshCw } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { Snapshot } from "@/api/types";
import { entero, fechaHora, latencia } from "@/lib/formato";
import { cn } from "@/lib/utils";

/**
 * Como se llama cada reintento en castellano.
 *
 * Las claves las pone el cliente del ERP y son opacas (`ora_00600`), asi que
 * tienen que pasar por aqui antes de llegar a la pantalla. El mapa **no** es la
 * lista de las que se pintan: una clave que no este se enseña cruda en vez de
 * desaparecer, porque un reintento que no se sabe nombrar sigue siendo un
 * reintento y esconderlo seria justo lo contrario de lo que este panel hace.
 */
const ETIQUETA_REINTENTO: Record<string, string> = {
    ora_00600: "ORA-00600, error interno de Oracle",
    ses_401: "sesion caducada (401)",
    erp_429: "limite del ERP (429)",
};

/** El orden en el que se leen: primero los que no son cero. */
function reintentosOrdenados(reintentos: Record<string, number>): [string, number][] {
    return Object.entries(reintentos).sort(
        ([claveUno, uno], [claveDos, dos]) =>
            // Con los dos a cero manda el alfabetico, para que la fila no baile
            // entre descargas por el orden en que Mongo devolvio las claves.
            dos - uno || claveUno.localeCompare(claveDos),
    );
}

/** Una cifra con su etiqueta debajo. */
function Cifra({ valor, etiqueta }: { valor: string; etiqueta: string }) {
    return (
        <div className="space-y-0.5">
            <div className="font-heading text-lg font-semibold tabular-nums">{valor}</div>
            <div className="text-xs text-muted-foreground">{etiqueta}</div>
        </div>
    );
}

export function PanelErp({
    snapshots,
    cargando,
    error,
}: {
    snapshots: Snapshot[] | null;
    cargando: boolean;
    error: Error | null;
}) {
    if (cargando && !snapshots) {
        return <Skeleton className="h-40 rounded-xl" />;
    }

    // Sin descarga registrada no se pinta una tarjeta vacia: un panel en blanco
    // se lee como "no hay reintentos", que es distinto de "no se sabe". El fallo
    // si se ensena, porque ahi si hay algo que contar y el resto del listado
    // puede estar funcionando igual (la traza no necesita el ERP).
    if (error) {
        return (
            <Card className="rounded-xl border-amber-500/40 bg-amber-500/5">
                <CardContent className="flex items-start gap-3 py-4">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600" />
                    <div className="text-sm">
                        <div className="font-medium">No se pudo leer la salud del ERP</div>
                        <div className="text-muted-foreground">
                            El listado y el detalle se leen de la traza, asi que siguen funcionando.
                        </div>
                    </div>
                </CardContent>
            </Card>
        );
    }

    // La vigente es la que manda. Si no hay ninguna marcada, se enseña la ultima
    // registrada: es lo unico que se sabe, y decir "la vigente" de una descarga
    // que no lo esta seria inventarse el dato.
    const items = snapshots ?? [];
    if (items.length === 0) return null;
    const vigente = items.find((s) => s.vigente) ?? items[items.length - 1];
    const reintentos = reintentosOrdenados(vigente.reintentos ?? {});
    const total = reintentos.reduce((suma, [, cuantos]) => suma + cuantos, 0);

    return (
        <Card className="rounded-xl">
            <CardHeader className="flex-row items-center justify-between gap-3 space-y-0 pb-3">
                <CardTitle className="flex items-center gap-2 font-heading text-sm font-medium">
                    <Database className="size-4 text-muted-foreground" />
                    La descarga del ERP
                </CardTitle>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    {vigente.vigente ? null : (
                        <span className="rounded-full bg-muted px-2 py-0.5">no es la vigente</span>
                    )}
                    <span className="tabular-nums">{fechaHora(vigente.descargado_en)}</span>
                </div>
            </CardHeader>
            <CardContent className="space-y-4">
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                    <Cifra valor={entero(vigente.total_asientos)} etiqueta="asientos" />
                    <Cifra valor={entero(vigente.paginas)} etiqueta="paginas" />
                    <Cifra
                        valor={latencia(vigente.duracion_ms / 1000)}
                        etiqueta="lo que tardo"
                    />
                    <Cifra valor={vigente.estado} etiqueta="estado" />
                </div>

                <div className="flex flex-wrap items-center gap-2 border-t pt-3">
                    <span className="text-xs text-muted-foreground">
                        <RefreshCw className="mr-1 inline size-3 align-[-2px]" />
                        {total === 0 ? "Sin reintentos" : `${entero(total)} reintentos`}
                    </span>
                    {reintentos.map(([clave, cuantos]) => (
                        <span
                            key={clave}
                            className={cn(
                                "rounded-full px-2 py-0.5 text-xs tabular-nums",
                                cuantos > 0
                                    ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
                                    : "bg-muted text-muted-foreground",
                            )}
                            title={ETIQUETA_REINTENTO[clave] ?? clave}
                        >
                            {ETIQUETA_REINTENTO[clave] ?? clave}: {entero(cuantos)}
                        </span>
                    ))}
                </div>

                <p className="text-xs text-muted-foreground">
                    Los reintentos son de la <strong>descarga de asientos</strong>, no de la
                    decisión de cada factura: quien reintenta es el cliente del ERP, y cada
                    reintento se cuenta una vez por descarga. La resiliencia de la decisión se
                    enseña arriba, en la cola de segunda lectura.
                </p>
            </CardContent>
        </Card>
    );
}
