/**
 * El documento original, al lado de la decision.
 *
 * Esto es lo que hace que el panel se pueda usar de verdad: cuando alguien duda
 * de un `ESCALAR`, quiere ver la factura. Tenerla al lado y no en otra pestana es
 * la diferencia entre comprobar y no comprobar.
 *
 * El PDF se pide con `usePdf`, que lo convierte a una URL `blob:` del navegador
 * en vez de poner la URL de la API directamente en el `<iframe>`. El motivo esta
 * explicado en `hooks.tsx`: un `<iframe>` no puede mandar la cabecera
 * `X-API-Key`, y el dia que la API la pida el detalle dejaria de verse sin que
 * nadie hubiera tocado nada. Pidiendolo con `fetch` (que si puede) y sirviendo el
 * `blob:`, funciona con clave y sin ella.
 *
 * Ojo con el caso `congelado_sin_pdf`: el congelado solo guarda unos pocos PDF
 * (los casos que se ensenan). Cuando falta, **los datos estan completos** y lo
 * unico que falta es el documento, y eso se dice tal cual en vez de un error
 * generico. Confundir "no tengo el PDF" con "no tengo los datos" hace que se
 * dude de la tabla entera.
 */

import { Download, ExternalLink, FileQuestion, FileText, Snowflake } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorPeticion } from "@/api/cliente";
import { usePdf } from "@/api/hooks";
import { huellaCorta } from "@/lib/formato";

export function PanelDocumento({ fileId, sha256 }: { fileId: string; sha256: string }) {
    const { url, error, cargando } = usePdf(fileId);

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
                ) : url ? (
                    <>
                        {/*
                         * El `<iframe>` lleva `title` porque sin él un lector de
                         * pantalla anuncia "marco" y nada más. Y `min-h` porque
                         * con altura automática el visor de PDF del navegador se
                         * queda en 150 px y no se lee nada.
                         */}
                        <iframe
                            src={url}
                            title={`Factura ${fileId}`}
                            className="h-[60vh] min-h-80 w-full rounded-lg bg-muted ring-1 ring-foreground/10"
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
