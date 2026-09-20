/**
 * Las reglas evaluadas: el "por que" de la decision.
 *
 * Esta es la pantalla que convierte el panel en un panel de auditoria. Ensenar
 * "NO_PAGAR" sin los hechos que lo sostienen es pedir que se confie; ensenar los
 * seis hechos con la regla que los produce es poder comprobarlo. Y los hechos
 * vienen del motor, no se calculan aqui: lo unico que hace este fichero es
 * ordenarlos y pintarlos.
 *
 * Tres decisiones que vienen de `severidad.ts` y no se repiten aqui:
 *
 * 1. **Se ordenan por gravedad** (`ordenarHechos`), no por numero de regla. Lo
 *    que bloquea el pago va arriba. Ordenados por `R1..R6`, el hecho que importa
 *    cae en medio de cinco que dicen "Cumple".
 *
 * 2. **`ok` no llama la atencion.** 448 de las 500 facturas cumplen las seis
 *    reglas. Pintar cada "Cumple" en verde fuerte llenaria la pantalla de color y
 *    el ojo dejaria de ir a lo que falla. Los que cumplen van en gris y ademas se
 *    pueden plegar, porque en una factura limpia son los seis.
 *
 * 3. **`bloquea` y `anomalia` no son lo mismo.** Solo R5 produce `duro: true`, y
 *    solo eso significa "no se paga". Una anomalia (`ok: false` y nada mas) se
 *    ensena en azul y no en rojo, porque el motor la reporta sin concluir y
 *    pintarla de rojo seria inventarse una decision que no se ha tomado.
 */

import { useState } from "react";
import { ChevronDown, Scale } from "lucide-react";

import { EtiquetaSeveridad } from "@/components/Etiquetas";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ordenarHechos, severidadHecho, type Severidad } from "@/api/severidad";
import type { Hecho } from "@/api/types";
import { claveLegible, valorDeDato } from "@/lib/dato";
import { entero } from "@/lib/formato";
import { ETIQUETA_REGLA, EXPLICACION_REGLA, FILETE_SEVERIDAD } from "@/theme";
import { cn } from "@/lib/utils";

/** A partir de aqui un valor ocupa su propia linea en vez de ir en una ficha. */
const LARGO_DE_FICHA = 48;

export function PanelHechos({ hechos }: { hechos: readonly Hecho[] }) {
    const [verCumplen, setVerCumplen] = useState(false);

    const ordenados = ordenarHechos(hechos);
    const pendientes = ordenados.filter((hecho) => severidadHecho(hecho) !== "ok");
    const cumplen = ordenados.filter((hecho) => severidadHecho(hecho) === "ok");

    if (hechos.length === 0) {
        return (
            <Card>
                <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                        <Scale className="size-4 text-muted-foreground" />
                        Reglas evaluadas
                    </CardTitle>
                </CardHeader>
                <CardContent className="text-sm text-muted-foreground">
                    El motor no ha dejado ni un hecho para esta factura. Eso no es lo mismo que
                    "todo bien": sin hechos no hay nada que enseñe por qué se decidió lo que se
                    decidió, y esta pantalla no puede respaldar la decisión.
                </CardContent>
            </Card>
        );
    }

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Scale className="size-4 text-muted-foreground" />
                    Reglas evaluadas
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                    {resumen(pendientes, cumplen.length)}
                </p>
            </CardHeader>

            <CardContent className="space-y-3">
                {pendientes.length === 0 ? (
                    <p className="rounded-lg bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
                        Las {entero(cumplen.length)} reglas se cumplen. No hay nada que revisar en
                        esta factura.
                    </p>
                ) : (
                    pendientes.map((hecho) => <FilaHecho key={hecho.regla} hecho={hecho} />)
                )}

                {cumplen.length > 0 && pendientes.length > 0 ? (
                    <div className="space-y-3 border-t pt-3">
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setVerCumplen((previo) => !previo)}
                            aria-expanded={verCumplen}
                            className="text-muted-foreground"
                        >
                            <ChevronDown
                                data-icon="inline-start"
                                className={cn("transition-transform", verCumplen && "rotate-180")}
                            />
                            {verCumplen ? "Ocultar" : "Ver"} las {entero(cumplen.length)} reglas que
                            se cumplen
                        </Button>

                        {verCumplen
                            ? cumplen.map((hecho) => <FilaHecho key={hecho.regla} hecho={hecho} />)
                            : null}
                    </div>
                ) : null}
            </CardContent>
        </Card>
    );
}

/** La frase del encabezado: cuantas hay que mirar y por que. */
function resumen(pendientes: readonly Hecho[], cumplen: number): string {
    const total = pendientes.length + cumplen;

    if (pendientes.length === 0) {
        return `${entero(total)} reglas evaluadas, todas se cumplen.`;
    }

    const cuantos = (severidad: Severidad) =>
        pendientes.filter((hecho) => severidadHecho(hecho) === severidad).length;

    const partes: string[] = [];
    const bloquean = cuantos("bloquea");
    const avisos = cuantos("aviso");
    const anomalias = cuantos("anomalia");

    if (bloquean > 0) partes.push(`${entero(bloquean)} bloquea el pago`);
    if (avisos > 0) partes.push(`${entero(avisos)} ${avisos === 1 ? "aviso" : "avisos"}`);
    if (anomalias > 0) {
        partes.push(`${entero(anomalias)} ${anomalias === 1 ? "anomalía" : "anomalías"}`);
    }

    return `${entero(total)} reglas evaluadas: ${partes.join(", ")} y ${entero(cumplen)} que ${
        cumplen === 1 ? "cumple" : "cumplen"
    }.`;
}

/**
 * Un hecho.
 *
 * El filete de color de la izquierda es lo que hace que la lista se pueda leer en
 * vertical: con seis hechos y textos de distinta longitud, el color es lo unico
 * que dice de un vistazo cuales hay que leer. Va como pseudo-elemento y no como
 * un `<div>` de adorno para que no exista en el arbol de accesibilidad — el
 * color no es informacion, la etiqueta de severidad si.
 */
function FilaHecho({ hecho }: { hecho: Hecho }) {
    const severidad = severidadHecho(hecho);
    const datos = Object.entries(hecho.datos ?? {});
    const cortos = datos.filter(([clave, valor]) => valorDeDato(clave, valor).length <= LARGO_DE_FICHA);
    const largos = datos.filter(([clave, valor]) => valorDeDato(clave, valor).length > LARGO_DE_FICHA);

    return (
        <article
            className={cn(
                "relative rounded-lg py-2 pr-3 pl-4 before:absolute before:inset-y-0 before:left-0 before:w-1 before:rounded-full before:content-['']",
                FILETE_SEVERIDAD[severidad],
                severidad === "ok" && "opacity-70",
            )}
            title={EXPLICACION_REGLA[hecho.regla]}
        >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h4 className="text-sm font-medium">{ETIQUETA_REGLA[hecho.regla]}</h4>
                <EtiquetaSeveridad severidad={severidad} />
                {hecho.nombre ? (
                    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                        {hecho.nombre}
                    </code>
                ) : null}
            </div>

            <p className="mt-1 text-sm text-muted-foreground">{hecho.motivo}</p>

            {cortos.length > 0 ? (
                <dl className="mt-2 flex flex-wrap gap-1.5">
                    {cortos.map(([clave, valor]) => (
                        <div
                            key={clave}
                            className="flex items-baseline gap-1.5 rounded-md bg-muted/60 px-2 py-1 text-xs"
                        >
                            <dt className="text-muted-foreground">{claveLegible(clave)}</dt>
                            <dd className="font-medium break-all">{valorDeDato(clave, valor)}</dd>
                        </div>
                    ))}
                </dl>
            ) : null}

            {largos.length > 0 ? (
                <dl className="mt-2 space-y-1.5">
                    {largos.map(([clave, valor]) => (
                        <div key={clave} className="rounded-md bg-muted/60 px-2 py-1.5 text-xs">
                            <dt className="text-muted-foreground">{claveLegible(clave)}</dt>
                            <dd className="mt-0.5 break-words">{valorDeDato(clave, valor)}</dd>
                        </div>
                    ))}
                </dl>
            ) : null}
        </article>
    );
}
