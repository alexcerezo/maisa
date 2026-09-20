/**
 * La cabecera del expediente: la decision, sus motivos y los datos de la factura.
 *
 * Es lo primero que se lee al abrir una factura y tiene que responder tres
 * preguntas en ese orden: **que se ha decidido**, **por que** y **con que datos**.
 * Por eso el orden no es negociable: la etiqueta de decision arriba y grande, los
 * motivos debajo, y los datos al final. Al reves — primero los datos — hay que
 * bajar hasta el final para saber de que se esta hablando.
 *
 * Dos detalles que vienen del contrato y que no se pueden tratar como "un campo
 * mas":
 *
 * 1. **`motivos` vacio es el caso bueno.** Las 448 facturas que se pagan sin
 *    dudar no traen ninguno. Un panel que ensene "sin motivos" con un triangulo
 *    ambar esta diciendo que hay un problema donde no lo hay.
 *
 * 2. **`identificacion_fiable: false` invalida lo demas.** En 8 de las 500 el
 *    motor no pudo decir de quien es la factura, y entonces el proveedor, el NIF
 *    y el importe son `null` o heredados. Ensenarlos sin mas seria dar por buenos
 *    unos datos que el propio motor marca como poco fiables.
 */

import { BadgeCheck, CircleAlert } from "lucide-react";

import { EtiquetaResultado } from "@/components/Etiquetas";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { FacturaDetalle } from "@/api/types";
import {
    euros,
    eurosConSigno,
    fecha,
    entero,
    porcentaje,
    SIN_DATO,
    texto,
} from "@/lib/formato";
import { claseDesvio, ETIQUETA_ESCALON, ETIQUETA_METODO } from "@/theme";
import { cn } from "@/lib/utils";

export function ResumenFactura({ detalle }: { detalle: FacturaDetalle }) {
    const resumen = detalle.resumen;

    return (
        <div className="space-y-3">
            <Card>
                <CardHeader>
                    <div className="flex flex-wrap items-center gap-3">
                        <EtiquetaResultado resultado={detalle.resultado} className="text-sm" />
                        {detalle.identificacion_fiable ? (
                            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                                <BadgeCheck className="size-3.5" />
                                Proveedor identificado con seguridad
                            </span>
                        ) : null}
                    </div>

                    <CardTitle className="font-mono text-base break-all">
                        {detalle.file_id}
                    </CardTitle>

                    <p className="text-xs text-muted-foreground">
                        Lote {entero(detalle.lote)} · norma {detalle.version_norma} · leída con{" "}
                        {ETIQUETA_METODO[detalle.lectura.metodo_lectura] ??
                            detalle.lectura.metodo_lectura}{" "}
                        ({ETIQUETA_ESCALON[detalle.lectura.escalon_lectura] ??
                            detalle.lectura.escalon_lectura}
                        ) con una confianza de {porcentaje(detalle.lectura.calidad_lectura)} en{" "}
                        {detalle.lectura.segundos_lectura.toFixed(1)} s
                    </p>
                </CardHeader>

                <CardContent className="space-y-4">
                    <Motivos motivos={detalle.motivos} />

                    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                        <Dato etiqueta="Proveedor" valor={texto(resumen.proveedor)} />
                        <Dato etiqueta="NIF" valor={texto(resumen.nif)} mono />
                        <Dato etiqueta="Fecha" valor={fecha(resumen.fecha)} />
                        <Dato etiqueta="Pedido" valor={texto(resumen.pedido)} mono />
                        <Dato etiqueta="Asiento" valor={texto(resumen.asiento)} mono />
                        <Dato etiqueta="Estado en el ERP" valor={texto(resumen.estado_erp)} />
                        <Dato etiqueta="Total de la factura" valor={euros(resumen.total)} />
                        <Dato etiqueta="Importe del asiento" valor={euros(resumen.importe_erp)} />
                        <Dato
                            etiqueta="Desvío"
                            valor={
                                resumen.desvio_importe === null ||
                                resumen.desvio_importe === undefined
                                    ? SIN_DATO
                                    : eurosConSigno(resumen.desvio_importe)
                            }
                            className={claseDesvio(resumen.desvio_importe)}
                        />
                    </dl>
                </CardContent>
            </Card>

            {!detalle.identificacion_fiable ? (
                <Alert className="border-amber-600/40 bg-amber-500/5 dark:border-amber-400/40 dark:bg-amber-400/5">
                    <CircleAlert className="text-amber-700 dark:text-amber-300" />
                    <AlertTitle className="text-amber-800 dark:text-amber-200">
                        El motor no pudo identificar al proveedor
                    </AlertTitle>
                    <AlertDescription className="text-amber-800/80 dark:text-amber-200/70">
                        Los datos de identidad de arriba no se leyeron del documento: o están
                        vacíos, o se heredaron del ERP. Trátalos como pistas y no como hechos.
                        Cuando la identidad no es fiable, casi todo lo demás deja de serlo también.
                    </AlertDescription>
                </Alert>
            ) : null}
        </div>
    );
}

/**
 * Los motivos de la decision.
 *
 * `motivos: []` no es un dato que falte: es que no hay ninguno. Se dice con
 * palabras y sin alarma, porque en 448 de las 500 facturas es exactamente eso.
 */
function Motivos({ motivos }: { motivos: readonly string[] }) {
    if (motivos.length === 0) {
        return (
            <p className="rounded-lg bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
                Ninguna regla ha dado un motivo. Esta factura no necesita que nadie la revise.
            </p>
        );
    }

    return (
        <div>
            <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                {motivos.length === 1 ? "Motivo" : `Motivos (${motivos.length})`}
            </h3>
            <ul className="mt-1.5 space-y-1">
                {motivos.map((motivo, indice) => (
                    <li
                        key={`${indice}-${motivo}`}
                        className="flex gap-2 text-sm before:mt-2 before:size-1 before:shrink-0 before:rounded-full before:bg-foreground/40 before:content-['']"
                    >
                        {motivo}
                    </li>
                ))}
            </ul>
        </div>
    );
}

function Dato({
    etiqueta,
    valor,
    mono,
    className,
}: {
    etiqueta: string;
    valor: string;
    mono?: boolean;
    className?: string;
}) {
    return (
        <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">{etiqueta}</dt>
            <dd className={cn("truncate text-sm", mono && "font-mono text-xs", className)}>
                {valor}
            </dd>
        </div>
    );
}
