/**
 * El documento original, al lado de la decision.
 *
 * Esto es lo que hace que el panel se pueda usar de verdad: cuando alguien duda
 * de un `ESCALAR`, quiere ver la factura. Tenerla al lado y no en otra pestana es
 * la diferencia entre comprobar y no comprobar.
 *
 * El PDF se pide con `usePdf`, que lo convierte a una URL `blob:` del navegador
 * en vez de poner la URL de la API directamente en el visor. El motivo esta
 * explicado en `hooks.tsx`: un `<iframe>` no puede mandar la cabecera
 * `X-API-Key`, y el dia que la API la pida el detalle dejaria de verse sin que
 * nadie hubiera tocado nada. Pidiendolo con `fetch` (que si puede) y sirviendo el
 * `blob:`, funciona con clave y sin ella. El mismo `blob` es ahora el que se le
 * pasa a `pdf.js`, que necesita los bytes, no una URL.
 *
 * Ojo con el caso `congelado_sin_pdf`: el congelado solo guarda unos pocos PDF
 * (los casos que se ensenan). Cuando falta, **los datos estan completos** y lo
 * unico que falta es el documento, y eso se dice tal cual en vez de un error
 * generico. Confundir "no tengo el PDF" con "no tengo los datos" hace que se
 * dude de la tabla entera.
 *
 * Los anclajes se piden aparte y son **opcionales**: el documento se ve igual sin
 * ellos, solo que sin resaltado. Por eso un fallo al pedirlos no tumba el visor;
 * se dice que no se ha podido resaltar y se sigue. Lo que si se conserva del
 * visor anterior son "Abrir en otra pestana" y "Descargar", que siguen usando la
 * URL del blob: para eso el visor del navegador es mejor que este.
 *
 * Los anclajes **llegan por props y no se piden aqui**. Los necesita tambien el
 * formulario de correcciones —para poder contrastar lo que leyo el motor con lo
 * que escribe el operador— y `usePeticion` no guarda cache: pedirlos en los dos
 * sitios serian dos vueltas de red para el mismo dato y la posibilidad de que una
 * llegue y la otra no. Los pide la pagina, que es quien compone las dos piezas.
 */

import { Download, ExternalLink, FileQuestion, FileText, Snowflake } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { VisorPdf } from "@/components/VisorPdf";
import { ErrorPeticion } from "@/api/cliente";
import type { ResultadoPeticion } from "@/api/hooks";
import { usePdf } from "@/api/hooks";
import type { Anclajes, CampoAnclable } from "@/api/types";
import { huellaCorta } from "@/lib/formato";

export function PanelDocumento({
    fileId,
    sha256,
    anclajes,
    campoActivo,
    onCampo,
}: {
    fileId: string;
    sha256: string;
    /** Donde esta escrito cada dato. `datos` a `null` es "sin resaltado". */
    anclajes: ResultadoPeticion<Anclajes>;
    /** El campo que se esta mirando en el formulario, para resaltarlo. */
    campoActivo?: string | null;
    /** Se avisa al pulsar el rotulo de un dato, para llevar al formulario. */
    onCampo?: (campo: CampoAnclable) => void;
}) {
    const { url, blob, error, cargando } = usePdf(fileId);

    return (
        <Card className="flex flex-col">
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <FileText className="size-4 text-muted-foreground" />
                    Documento original
                </CardTitle>
                <p className="font-mono text-xs break-all text-muted-foreground">{fileId}</p>
            </CardHeader>

            <CardContent className="flex flex-1 flex-col gap-3">
                {cargando ? (
                    <Skeleton className="h-[60vh] min-h-80 w-full rounded-lg" />
                ) : error ? (
                    <FaltaElDocumento error={error} />
                ) : blob && url ? (
                    <>
                        <VisorPdf
                            blob={blob}
                            anclajes={anclajes.datos}
                            campoActivo={campoActivo}
                            onCampo={onCampo}
                        />

                        <div className="flex flex-wrap items-center gap-2">
                            <Button asChild variant="outline" size="sm">
                                <a href={url} target="_blank" rel="noreferrer">
                                    <ExternalLink data-icon="inline-start" />
                                    Abrir en otra pestaña
                                </a>
                            </Button>
                            <Button asChild variant="ghost" size="sm">
                                <a href={url} download={fileId}>
                                    <Download data-icon="inline-start" />
                                    Descargar
                                </a>
                            </Button>
                            <span className="ml-auto font-mono text-xs text-muted-foreground">
                                sha256 {huellaCorta(sha256)}
                            </span>
                        </div>
                    </>
                ) : null}
            </CardContent>
        </Card>
    );
}

/**
 * No se ha podido traer el PDF.
 *
 * Se distinguen los dos casos porque llevan a acciones distintas: el congelado no
 * trae los 500 PDF y eso no es un fallo de nadie, mientras que la API sin
 * contestar si es algo que hay que mirar.
 */
function FaltaElDocumento({ error }: { error: Error }) {
    const congelado = error instanceof ErrorPeticion && error.codigo === "congelado_sin_pdf";

    return (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-lg bg-muted/40 px-6 py-12 text-center ring-1 ring-foreground/10">
            {congelado ? (
                <Snowflake className="size-6 text-amber-600 dark:text-amber-400" />
            ) : (
                <FileQuestion className="size-6 text-muted-foreground" />
            )}

            <p className="text-sm font-medium">
                {congelado ? "El congelado no trae este PDF" : "No he podido cargar el PDF"}
            </p>

            <p className="max-w-md text-xs text-muted-foreground">
                {congelado
                    ? "Los datos de la factura están completos: lo único que falta es el documento. El congelado solo guarda los PDF de los casos que se enseñan."
                    : error.message}
            </p>

            {!congelado ? (
                <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
                    Recargar la página
                </Button>
            ) : null}
        </div>
    );
}
