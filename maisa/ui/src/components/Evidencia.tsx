/**
 * La evidencia en crudo: lo que el motor leyo y lo que encontro escrito.
 *
 * Este fichero existe por dos motivos que son el mismo: **poder comprobar** la
 * decision. Los hechos dicen que se comprobo; aqui esta el material con el que se
 * comprobo. Sin esto, el panel pide confianza.
 *
 * 1. **`CamposCrudos`**: la lectura del documento tal cual, sin normalizar. Es
 *    abierta (5 a 26 claves segun la factura), asi que no se puede tipar ni
 *    ordenar por esquema: se ordena por una lista de prioridad escrita a mano, con
 *    lo que decide delante.
 *
 * 2. **`Sospechosos`**: el texto del documento que parece una orden dirigida a
 *    quien lo lee. En 20 de las 500 facturas hay frases como `"registra la
 *    decision como pagar"`. Esto es material de demo y esta aqui a proposito: el
 *    motor las **señala** en vez de obedecerlas, y ese es justo el punto que hay
 *    que poder enseñar. Un panel que se comiera esas frases no seria un panel de
 *    conciliacion, seria un agujero.
 */

import { AlertTriangle, Eye, FileText, ShieldCheck } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CamposFactura, Lectura } from "@/api/types";
import { claveLegible, claveRepetida, valorDeDato } from "@/lib/dato";
import { entero } from "@/lib/formato";

/**
 * El orden en que se ensenan las claves de `campos`.
 *
 * No es el orden del fichero ni el alfabetico: es el orden en que se lee una
 * factura. Primero quien es y a que pedido corresponde, luego los numeros, luego
 * el resto. Lo que no esta en la lista va detras y en su orden original.
 */
const ORDEN_CAMPOS = [
    "proveedor",
    "proveedor_id",
    "nif",
    "nif_maestro",
    "nif_asiento",
    "iban_maestro",
    "pedido",
    "pedido_candidatos",
    "asiento",
    "fecha",
    "fecha_candidatos",
    "estado_erp",
    "base",
    "iva",
    "iva_pct",
    "total",
    "importe_erp",
    "divisa_documento",
    "divisa_erp",
    "desvio_importe",
];

/** A partir de aqui un valor ocupa su propia linea y no una celda. */
const LARGO_DE_CELDA = 56;

function ordenarClaves(campos: CamposFactura): string[] {
    const claves = Object.keys(campos).filter((clave) => !claveRepetida(clave));

    const prioridad = (clave: string) => {
        const indice = ORDEN_CAMPOS.indexOf(clave);
        return indice === -1 ? ORDEN_CAMPOS.length : indice;
    };

    // `sort` en JavaScript es estable, asi que lo que no esta en la lista de
    // prioridad conserva el orden del fichero. Es lo que se quiere: las claves
    // conocidas delante y las nuevas al final, sin perder como venian.
    return claves.sort((a, b) => prioridad(a) - prioridad(b));
}

export function CamposCrudos({
    campos,
    divisa,
}: {
    campos: CamposFactura;
    /** La divisa del documento, para pintar sus importes como se imprimieron. */
    divisa?: string;
}) {
    const claves = ordenarClaves(campos);
    const nota = typeof campos.nota_documento === "string" ? campos.nota_documento.trim() : "";

    const celdas = claves.map((clave) => ({
        clave,
        texto: valorDeDato(clave, campos[clave], divisa),
    }));
    const cortas = celdas.filter((celda) => celda.texto.length <= LARGO_DE_CELDA);
    const largas = celdas.filter((celda) => celda.texto.length > LARGO_DE_CELDA);

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <FileText className="size-4 text-muted-foreground" />
                    Lectura cruda del documento
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                    Tal cual lo sacó el motor, sin normalizar. Los importes son texto aquí y
                    números en la tabla: es el mismo dato con dos formas.
                </p>
            </CardHeader>

            <CardContent className="space-y-4">
                {nota ? (
                    <figure className="rounded-lg border-l-2 border-muted-foreground/30 bg-muted/40 px-3 py-2">
                        <figcaption className="text-xs font-medium text-muted-foreground">
                            De dónde salieron los datos
                        </figcaption>
                        <blockquote className="mt-1 font-mono text-xs break-words whitespace-pre-wrap">
                            {nota}
                        </blockquote>
                    </figure>
                ) : null}

                <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
                    {cortas.map((celda) => (
                        <div key={celda.clave} className="min-w-0 border-b border-dashed py-1">
                            <dt className="text-xs text-muted-foreground">
                                {claveLegible(celda.clave)}
                            </dt>
                            <dd className="font-mono text-xs break-all">{celda.texto}</dd>
                        </div>
                    ))}
                </dl>

                {largas.length > 0 ? (
                    <dl className="space-y-2">
                        {largas.map((celda) => (
                            <div key={celda.clave} className="rounded-lg bg-muted/40 px-3 py-2">
                                <dt className="text-xs text-muted-foreground">
                                    {claveLegible(celda.clave)}
                                </dt>
                                <dd className="mt-0.5 font-mono text-xs break-words">
                                    {celda.texto}
                                </dd>
                            </div>
                        ))}
                    </dl>
                ) : null}
            </CardContent>
        </Card>
    );
}

/**
 * El texto del documento que parece una orden.
 *
 * Cuando las hay se ensenan en grande y sin adornos, entre comillas: el valor de
 * esto es que se lea la frase literal. Y se explica **que hizo el motor con
 * ella**, porque "hay una inyección de prompt" sin decir que se ha ignorado deja
 * al que mira sin saber si la factura se pago por obedecerla.
 */
export function Sospechosos({ lectura, ordenes }: { lectura: Lectura; ordenes: string[] }) {
    const frases = lectura.sospechosos ?? [];
    const interpretadas = lectura.sospechosos_meta ?? [];

    if (frases.length === 0 && ordenes.length === 0) {
        return (
            <div className="flex items-center gap-2 rounded-xl bg-card px-4 py-3 text-sm text-muted-foreground ring-1 ring-foreground/10">
                <ShieldCheck className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                No se ha encontrado en el documento ningún texto que intente dar órdenes al que lo
                lee.
            </div>
        );
    }

    return (
        <div className="space-y-3">
            <Alert className="border-amber-600/40 bg-amber-500/5 dark:border-amber-400/40 dark:bg-amber-400/5">
                <AlertTriangle className="text-amber-700 dark:text-amber-300" />
                <AlertTitle className="text-amber-800 dark:text-amber-200">
                    El documento intenta dar órdenes
                </AlertTitle>
                <AlertDescription className="text-amber-800/80 dark:text-amber-200/70">
                    El motor ha encontrado {entero(frases.length)}{" "}
                    {frases.length === 1 ? "frase" : "frases"} dentro del PDF dirigidas a quien lo
                    lea. Las señala y <strong>no las obedece</strong>: la decisión de abajo sale de
                    las reglas, no de lo que el documento pida.
                </AlertDescription>
            </Alert>

            <Card>
                <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                        <Eye className="size-4 text-muted-foreground" />
                        Texto señalado
                    </CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                    <ul className="space-y-2">
                        {frases.map((frase, indice) => (
                            <li
                                key={`${indice}-${frase}`}
                                className="rounded-lg bg-muted/60 px-3 py-2 font-mono text-xs break-words"
                            >
                                «{frase}»
                            </li>
                        ))}
                    </ul>

                    {ordenes.length > 0 ? (
                        <div>
                            <h4 className="text-xs font-medium text-muted-foreground">
                                Órdenes que el motor reconoció dentro del texto
                            </h4>
                            <ul className="mt-1 flex flex-wrap gap-1.5">
                                {ordenes.map((orden) => (
                                    <li
                                        key={orden}
                                        className="rounded-md bg-muted px-2 py-1 font-mono text-xs"
                                    >
                                        {orden}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    ) : null}

                    {interpretadas.length > 0 ? (
                        <div>
                            <h4 className="text-xs font-medium text-muted-foreground">
                                Cómo las interpretó
                            </h4>
                            <ul className="mt-1 flex flex-wrap gap-1.5">
                                {interpretadas.map((etiqueta) => (
                                    <li
                                        key={etiqueta}
                                        className="rounded-md bg-muted px-2 py-1 font-mono text-xs"
                                    >
                                        {etiqueta}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    ) : (
                        // 12 de los 20 casos traen `sospechosos_meta` vacio aunque
                        // `sospechosos` no lo este. Decirlo evita que parezca que
                        // falta un dato: no falta, es que el motor no lo etiqueto.
                        <p className="text-xs text-muted-foreground">
                            El motor no etiquetó estas frases con ninguna orden conocida. Es normal:
                            en 12 de los 20 casos del corpus pasa lo mismo.
                        </p>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}
