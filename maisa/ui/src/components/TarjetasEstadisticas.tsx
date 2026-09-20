/**
 * Los contadores de la cabecera, y el estado de salud del sistema.
 *
 * Dos decisiones que no son de pintado:
 *
 * 1. **Las tarjetas de resultado filtran.** El numero que interesa de un vistazo
 *    es "cuantas hay que mirar", y despues de verlo lo siguiente que se quiere
 *    es la lista. Hacer la tarjeta pulsable ahorra el paso de ir a buscar el
 *    filtro. Pulsar la que ya esta activa lo quita, para poder deshacer sin
 *    buscar el boton de limpiar.
 *
 * 2. **La salud del sistema se enseña aunque vaya todo bien.** `mongo.ok` y
 *    `entrega.coincide_con_traza` no son decorativos: el listado se lee de un
 *    fichero de traza y funciona con Mongo caido, asi que el panel **puede**
 *    estar enseñando datos mientras media base de datos no contesta. Enterarse de
 *    eso por una captura de pantalla y no por un aviso seria lo peor.
 */

import { AlertTriangle, Check, Database, ListChecks, type LucideIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { gravedadResultado } from "@/api/severidad";
import {
    ESTADOS_COLA,
    RESULTADOS,
    type Estadisticas,
    type EstadoCola,
    type Resultado,
} from "@/api/types";
import { entero } from "@/lib/formato";
import {
    CLASE_COLA,
    CLASE_GRAVEDAD,
    ETIQUETA_COLA,
    ETIQUETA_RESULTADO,
    EXPLICACION_COLA,
    ICONO_COLA,
    ICONO_RESULTADO,
} from "@/theme";
import { cn } from "@/lib/utils";

export function TarjetasEstadisticas({
    estadisticas,
    cargando,
    resultadoActivo,
    alElegirResultado,
    colaActiva,
    alElegirCola,
}: {
    estadisticas: Estadisticas | null;
    cargando: boolean;
    resultadoActivo: Resultado | null;
    alElegirResultado: (resultado: Resultado) => void;
    /** El estado de cola filtrado, o `null` si no hay ninguno. */
    colaActiva: EstadoCola | null;
    alElegirCola: (estado: EstadoCola) => void;
}) {
    if (cargando && !estadisticas) {
        return (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {[0, 1, 2, 3].map((indice) => (
                    <Skeleton key={indice} className="h-28 rounded-xl" />
                ))}
            </div>
        );
    }

    if (!estadisticas) return null;

    const total = estadisticas.total || 1;

    return (
        <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <TarjetaTotal total={estadisticas.total} />
                {RESULTADOS.map((resultado) => (
                    <TarjetaResultado
                        key={resultado}
                        resultado={resultado}
                        cuantas={estadisticas.por_resultado[resultado] ?? 0}
                        total={total}
                        activa={resultadoActivo === resultado}
                        alPulsar={() => alElegirResultado(resultado)}
                    />
                ))}
            </div>

            {estadisticas.resultados_desconocidos > 0 ? (
                <p className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-300">
                    <AlertTriangle className="size-3.5" />
                    {entero(estadisticas.resultados_desconocidos)} facturas traen un resultado que este
                    panel no conoce. No aparecen en ninguno de los contadores de arriba.
                </p>
            ) : null}

            <SaludDelSistema estadisticas={estadisticas} />

            <ColaSegundaLectura
                estadisticas={estadisticas}
                colaActiva={colaActiva}
                alElegirCola={alElegirCola}
            />
        </div>
    );
}

function TarjetaTotal({ total }: { total: number }) {
    return (
        <Card size="sm" className="gap-0">
            <CardContent className="flex flex-col gap-1">
                <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                    Facturas
                </span>
                <span className="font-heading text-3xl leading-none font-semibold tabular-nums">
                    {entero(total)}
                </span>
                <span className="text-xs text-muted-foreground">en la traza del motor</span>
            </CardContent>
        </Card>
    );
}

/**
 * Una tarjeta de resultado.
 *
 * Es un `<button>` de verdad y no un `<div onClick>`: asi se puede tabular hasta
 * ella, se activa con el espacio y el lector de pantalla anuncia que es pulsable
 * y si esta activa (`aria-pressed`). En un panel que se maneja con prisa, poder
 * llegar al filtro sin raton no es un detalle de accesibilidad, es la diferencia
 * entre usarlo y no usarlo.
 *
 * Se pinta con las clases de una tarjeta a mano en vez de envolver un `<Card>`:
 * meter un `<button>` dentro de un `<div>` deja el area pulsable sin cubrir el
 * padding y hace que el borde no responda al foco.
 */
function TarjetaResultado({
    resultado,
    cuantas,
    total,
    activa,
    alPulsar,
}: {
    resultado: Resultado;
    cuantas: number;
    total: number;
    activa: boolean;
    alPulsar: () => void;
}) {
    const Icono = ICONO_RESULTADO[resultado];
    const porcentaje = Math.round((cuantas / total) * 100);

    return (
        <button
            type="button"
            onClick={alPulsar}
            aria-pressed={activa}
            title={
                activa
                    ? `Quitar el filtro ${ETIQUETA_RESULTADO[resultado]}`
                    : `Ver solo ${ETIQUETA_RESULTADO[resultado]}`
            }
            className={cn(
                "flex cursor-pointer flex-col gap-2 rounded-xl bg-card p-4 text-left ring-1 ring-foreground/10 transition-colors outline-none",
                "hover:bg-muted/40 focus-visible:ring-3 focus-visible:ring-ring/50",
                activa && "ring-2 ring-foreground/30",
            )}
        >
            <span className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                <Icono className="size-3.5" />
                {ETIQUETA_RESULTADO[resultado]}
            </span>
            <span
                className={cn(
                    "font-heading text-3xl leading-none font-semibold tabular-nums",
                    CLASE_GRAVEDAD[gravedadResultado(resultado)],
                )}
            >
                {entero(cuantas)}
            </span>
            <span className="flex items-center gap-2">
                <Progress value={porcentaje} className="h-1 flex-1" />
                <span className="text-xs text-muted-foreground tabular-nums">{porcentaje} %</span>
            </span>
        </button>
    );
}

/**
 * El trabajo humano que queda, en una fila.
 *
 * Es el dato que la rubrica llama **trabajo pendiente**, y por eso va arriba y
 * pulsable: "hay 63 escaladas y 9 ya se han releido" es la frase que abre una
 * reunion, y la siguiente pregunta siempre es "¿cuales?". Los tres botones son esa
 * respuesta y filtran el listado sin pasar por la barra de filtros.
 *
 * Tres cosas que no se hacen aqui a proposito:
 *
 * 1. **No se recalculan las cuentas.** `confirmables`, `desvios` y `con_evidencia`
 *    las sirve la API. Restarlas en el cliente daria otro numero el dia que el
 *    motor anada un estado, y el panel diria una cosa y la traza otra.
 *
 * 2. **`pendientes_revision` nulo no es cero.** Viene de Mongo y es `null` si no
 *    contesta. Ensenar "0 escaladas" cuando la base de datos esta caida es
 *    exactamente la mentira que este panel no se puede permitir, asi que se calla
 *    el denominador y se dice el motivo en el `title`.
 *
 * 3. **No se esconde cuando `anotadas` es 0.** Un cero aqui es informacion —"nadie
 *    ha releido nada todavia"—, no un hueco. Lo que si se esconde es el bloque
 *    entero si no hay ninguna escalada pendiente, porque entonces no hay cola.
 */
function ColaSegundaLectura({
    estadisticas,
    colaActiva,
    alElegirCola,
}: {
    estadisticas: Estadisticas;
    colaActiva: EstadoCola | null;
    alElegirCola: (estado: EstadoCola) => void;
}) {
    const cola = estadisticas.cola_segunda_lectura;
    const pendientes = estadisticas.pendientes_revision;
    if (pendientes === 0 && cola.anotadas === 0) return null;

    const cuantas: Record<EstadoCola, number> = {
        confirmable: cola.confirmables,
        desvio: cola.desvios,
        sin_conclusion: cola.con_evidencia,
    };

    return (
        <Card size="sm" className="gap-0">
            <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-3">
                <div>
                    <span className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        <ListChecks className="size-3.5" />
                        Cola de segunda lectura
                    </span>
                    <p className="mt-1 text-sm">
                        <span className="font-heading text-2xl leading-none font-semibold tabular-nums">
                            {entero(cola.anotadas)}
                        </span>{" "}
                        <span className="text-muted-foreground">
                            {pendientes === null ? (
                                <Tooltip>
                                    <TooltipTrigger asChild>
                                        <span className="cursor-help underline decoration-dotted underline-offset-2">
                                            escaladas releídas
                                        </span>
                                    </TooltipTrigger>
                                    <TooltipContent className="max-w-xs">
                                        No se sabe sobre cuántas escaladas, porque Mongo no responde y
                                        es quien cuenta las revisiones pendientes. El resto del panel
                                        sale del fichero de traza y funciona igual.
                                    </TooltipContent>
                                </Tooltip>
                            ) : (
                                `de ${entero(pendientes)} escaladas releídas`
                            )}
                        </span>
                    </p>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                    {ESTADOS_COLA.map((estado) => (
                        <BotonCola
                            key={estado}
                            estado={estado}
                            cuantas={cuantas[estado]}
                            activa={colaActiva === estado}
                            alPulsar={() => alElegirCola(estado)}
                        />
                    ))}
                </div>
            </CardContent>
        </Card>
    );
}

/**
 * Un boton de la cola. Es un `<button>` de verdad, por lo mismo que las tarjetas
 * de resultado: se tabula, se activa con el espacio y anuncia si esta pulsado.
 */
function BotonCola({
    estado,
    cuantas,
    activa,
    alPulsar,
}: {
    estado: EstadoCola;
    cuantas: number;
    activa: boolean;
    alPulsar: () => void;
}) {
    const Icono = ICONO_COLA[estado];

    return (
        <button
            type="button"
            onClick={alPulsar}
            aria-pressed={activa}
            title={`${EXPLICACION_COLA[estado]} ${
                activa ? "Pulsa para quitar el filtro." : "Pulsa para ver solo estas."
            }`}
            className={cn(
                "inline-flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-[filter,box-shadow] outline-none",
                "hover:brightness-[0.97] focus-visible:ring-3 focus-visible:ring-ring/50 dark:hover:brightness-110",
                CLASE_COLA[estado],
                // El que no tiene nada que enseñar se apaga pero no se esconde:
                // que haya 0 desvíos es un dato, no un hueco.
                cuantas === 0 && "opacity-50",
                activa && "ring-2 ring-foreground/30",
            )}
        >
            <Icono className="size-3.5 shrink-0" />
            {ETIQUETA_COLA[estado]}
            <span className="font-semibold tabular-nums">{entero(cuantas)}</span>
        </button>
    );
}

/**
 * Mongo, la entrega y los asientos.
 *
 * Cada dato lleva su `Tooltip` con la explicacion larga, porque son terminos del
 * sistema que no se deducen del nombre: "asientos vigentes" no dice que es un
 * asiento del ERP ni por que importa que este vigente.
 */
function SaludDelSistema({ estadisticas }: { estadisticas: Estadisticas }) {
    const metodos = Object.entries(estadisticas.por_metodo_lectura);

    return (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
            <Dato
                icono={estadisticas.mongo.ok ? Check : AlertTriangle}
                tono={estadisticas.mongo.ok ? "neutro" : "aviso"}
                texto={estadisticas.mongo.ok ? "Mongo responde" : "Mongo no responde"}
                explicacion={
                    estadisticas.mongo.ok
                        ? "La API puede leer el catálogo de asientos del ERP."
                        : `El listado sale del fichero de traza y funciona igual, pero el catálogo del ERP no está disponible. ${estadisticas.mongo.error ?? ""}`.trim()
                }
            />

            <Dato
                icono={estadisticas.entrega.coincide_con_traza ? Check : AlertTriangle}
                tono={estadisticas.entrega.coincide_con_traza ? "neutro" : "aviso"}
                texto={
                    estadisticas.entrega.coincide_con_traza
                        ? "La entrega coincide con la traza"
                        : "La entrega NO coincide con la traza"
                }
                explicacion={`Entrega de ${entero(estadisticas.entrega.total)} líneas con ${entero(estadisticas.entrega.lineas_invalidas)} inválidas. Si no coincide, hay facturas en el panel que el motor no respalda.`}
            />

            <Dato
                icono={Database}
                tono="neutro"
                texto={
                    estadisticas.asientos_vigentes === null
                        ? "Asientos vigentes sin dato"
                        : `${entero(estadisticas.asientos_vigentes)} asientos vigentes`
                }
                explicacion="Asientos del ERP contra los que se concilia. Solo una descarga está vigente a la vez. Sin dato significa que el catálogo no se pudo leer: no es que haya cero."
            />

            {metodos.map(([metodo, cuantas]) => (
                <span key={metodo} className="tabular-nums">
                    {metodo.replace(/_/g, " ")}: {entero(cuantas)}
                </span>
            ))}
        </div>
    );
}

function Dato({
    icono: Icono,
    tono,
    texto: contenido,
    explicacion,
}: {
    icono: LucideIcon;
    tono: "neutro" | "aviso";
    texto: string;
    explicacion: string;
}) {
    return (
        <Tooltip>
            <TooltipTrigger asChild>
                <span
                    className={cn(
                        "inline-flex cursor-help items-center gap-1.5 underline decoration-dotted underline-offset-2",
                        tono === "aviso" && "text-amber-700 dark:text-amber-300",
                    )}
                >
                    <Icono className="size-3.5" />
                    {contenido}
                </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">{explicacion}</TooltipContent>
        </Tooltip>
    );
}
