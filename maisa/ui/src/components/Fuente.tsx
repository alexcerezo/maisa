/**
 * Enseñar de donde salen los datos.
 *
 * Esto no es un adorno de cabecera: es el requisito que hace util el plan B.
 * `fuente.ts` puede caer al congelado sin que nadie se entere, y un panel que
 * enseña los datos de ayer como si fueran de ahora es peor que uno que no
 * arranca. Por eso el estado de la fuente se enseña siempre, en la cabecera
 * (compacto) y, cuando esta congelado, tambien en grande con el motivo y la
 * fecha de la foto.
 *
 * Las tres piezas de este fichero son la misma informacion a tres escalas:
 * `BadgeFuente` para la cabecera, `AvisoFuente` para el cuerpo de la pantalla y
 * `PieFuente` para el detalle tecnico (que URL se esta usando y de donde salio).
 */

import { AlertTriangle, Database, RefreshCw, Snowflake, Wifi } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useFuente } from "@/api/hooks";
import { fechaHora, entero } from "@/lib/formato";
import { cn } from "@/lib/utils";

/**
 * El punto de color. Late cuando esta vivo para que se vea que es un estado y no
 * una etiqueta decorativa, pero sin `animate-ping` a pantalla completa: en un
 * panel que se mira durante horas, una animacion constante cansa.
 */
function Punto({ className }: { className: string }) {
    return <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", className)} />;
}

/**
 * El distintivo compacto de la cabecera.
 *
 * Mientras se comprueba **no se enseña nada fijo**: se enseña un esqueleto. Si
 * aqui se pusiera "congelado" por defecto, el panel diria algo falso durante el
 * primer segundo de cada carga, y ese es justo el segundo en que alguien mira la
 * cabecera para saber de donde vienen los datos.
 */
export function BadgeFuente() {
    const { estado, cargando } = useFuente();

    if (cargando || !estado) {
        return <Skeleton className="h-5 w-24 rounded-4xl" />;
    }

    if (estado.fuente === "vivo") {
        return (
            <Badge
                variant="outline"
                className="gap-1.5 border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-400/10 dark:text-emerald-300"
                title="Los datos vienen de la API, en vivo."
            >
                <Punto className="bg-emerald-500" />
                API viva
            </Badge>
        );
    }

    return (
        <Badge
            variant="outline"
            className="gap-1.5 border-amber-600/30 bg-amber-500/10 text-amber-700 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-300"
            title={estado.motivo ?? "Los datos vienen del congelado."}
        >
            <Snowflake />
            Congelado
        </Badge>
    );
}

/**
 * El aviso grande, cuando no se esta leyendo de la API.
 *
 * Dos estados distintos, y por eso son dos bloques y no uno con un texto
 * variable: **congelado** es el plan B funcionando (informativo, ambar) y
 * **error** es que no se pudo ni comprobar la fuente (destructivo). Mezclarlos
 * daria un ambar alarmante o un rojo tranquilizador, segun el dia.
 */
export function AvisoFuente() {
    const { estado, cargando, error, reintentar } = useFuente();

    if (error) {
        return (
            <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>No he podido comprobar la fuente de datos</AlertTitle>
                <AlertDescription>
                    <p>{error}</p>
                    <p className="mt-1 text-muted-foreground">
                        Ni la API ni el congelado han contestado. El panel no puede enseñar nada
                        hasta saber cual de los dos usar.
                    </p>
                </AlertDescription>
                <BotonReintentar alPulsar={reintentar} />
            </Alert>
        );
    }

    if (cargando || !estado || estado.fuente === "vivo") return null;

    const manifiesto = estado.manifiesto;

    return (
        <Alert className="border-amber-600/30 bg-amber-500/5 dark:border-amber-400/30 dark:bg-amber-400/5">
            <Snowflake className="text-amber-700 dark:text-amber-300" />
            <AlertTitle className="text-amber-800 dark:text-amber-200">
                Estás viendo el congelado, no la API
            </AlertTitle>
            <AlertDescription className="text-amber-800/80 dark:text-amber-200/70">
                <p>{estado.motivo}</p>
                {manifiesto ? (
                    <p className="mt-1">
                        Datos del {fechaHora(manifiesto.congelado_en)} desde{" "}
                        <span className="font-mono text-xs">{manifiesto.origen}</span>:{" "}
                        {entero(manifiesto.facturas_en_el_listado)} facturas en el listado y{" "}
                        {entero(manifiesto.detalles_guardados)} expedientes completos.
                        {manifiesto.entrega?.coincide_con_traza
                            ? " La entrega cubre los lotes que le tocan."
                            : ""}
                    </p>
                ) : (
                    <p className="mt-1">
                        Y no he podido leer el manifiesto, así que no sé de cuándo son estos datos.
                    </p>
                )}
            </AlertDescription>
            <BotonReintentar alPulsar={reintentar} />
        </Alert>
    );
}

function BotonReintentar({ alPulsar }: { alPulsar: () => void }) {
    return (
        <div className="col-start-2 row-span-2 mt-2 justify-self-start has-[>svg]:col-start-2">
            <Button variant="outline" size="sm" onClick={alPulsar}>
                <RefreshCw data-icon="inline-start" />
                Volver a comprobar
            </Button>
        </div>
    );
}

/**
 * El pie con el detalle tecnico.
 *
 * Existe porque la URL de la API es lo que mas se mueve del sistema: la VM de
 * Oracle puede cambiar de IP y `sslip.io` deriva el nombre de ella. Cuando algo
 * no carga, lo primero que se quiere saber es **contra que** se esta llamando, y
 * tenerlo en pantalla ahorra una consola de navegador.
 */
export function PieFuente() {
    const { estado } = useFuente();
    if (!estado) return null;

    const { config } = estado;

    return (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
                <Database className="size-3.5" />
                <span className="font-mono">{config.api ?? "sin API configurada"}</span>
            </span>
            <span>origen: {config.origen}</span>
            <span className="inline-flex items-center gap-1.5">
                <Wifi className="size-3.5" />
                clave: {config.apiKey ? "configurada" : "ninguna"}
            </span>
        </div>
    );
}
