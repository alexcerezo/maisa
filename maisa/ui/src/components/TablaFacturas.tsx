/**
 * La tabla de facturas.
 *
 * Es la pantalla que se mira de verdad: 500 filas y una pregunta — "cuales hay
 * que mirar". Todo lo de aqui esta puesto para responderla rapido.
 *
 * Cuatro decisiones que no son de gusto:
 *
 * 1. **La columna que manda es la decision, y va primera.** Ordenadas por
 *    `file_id` (que es como las devuelve el motor, y asi lo dice `filtros.ts`),
 *    la primera columna es la que se recorre con el ojo de arriba abajo. Poner
 *    primero el nombre del fichero obligaria a leer dos columnas en cada fila.
 *
 * 2. **El motivo va debajo del fichero, no en columna propia.** El motivo es
 *    texto largo y solo lo traen 52 de las 500 filas; una columna propia seria
 *    una franja vacia en el 90 % de la tabla y estrecharia lo que si se lee.
 *    Debajo, ocupa el hueco que ya hay y solo cuando hay algo que decir.
 *
 * 3. **El desvio se pinta solo si existe.** `total - importe_erp` es `null` en
 *    las facturas sin importe del ERP, y `0.00` en las que cuadran. Ensenar
 *    "+0,00 €" en 440 filas es ruido; ensenarlo en ambar cuando no es cero es la
 *    senal.
 *
 * 4. **Las columnas secundarias desaparecen en pantalla estrecha.** Un panel de
 *    conciliacion se mira tambien desde el movil en una feria, y 7 columnas en
 *    400 px no se leen: se esconden las de contexto (proveedor, fecha, pedido) y
 *    se quedan las tres que deciden (resultado, factura, importe).
 */

import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

import { EtiquetaResultado } from "@/components/Etiquetas";
import { Skeleton } from "@/components/ui/skeleton";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { FacturaResumen } from "@/api/types";
import { euros, eurosConSigno, fecha, porcentaje, SIN_DATO, texto } from "@/lib/formato";
import { claseDesvio, ICONO_ESCALON } from "@/theme";
import { cn } from "@/lib/utils";

export function TablaFacturas({
    items,
    cargando,
    atenuada,
    fileIdActivo,
    parametrosLista = "",
}: {
    items: readonly FacturaResumen[];
    /** La primera carga. Con datos ya en pantalla, `atenuada` en vez de esqueleto. */
    cargando: boolean;
    /** Hay una peticion en vuelo pero la tabla ya tiene filas. */
    atenuada: boolean;
    /** El `fileId` de la fila de la que se viene, para poder volver a ella. */
    fileIdActivo?: string | null;
    /**
     * El `?` de la lista, tal cual (`location.search`), para pegarlo al enlace del
     * expediente. Sin esto, volver de una factura devuelve a la lista sin filtros
     * ni pagina: se pierde el sitio en el que estabas, que es justo lo que se
     * quiere conservar cuando se revisan 40 escaladas una a una. Va en la URL y no
     * en memoria para que el enlace se pueda compartir y recargar.
     */
    parametrosLista?: string;
}) {
    if (cargando && items.length === 0) {
        return (
            <div className="space-y-2 rounded-xl bg-card p-4 ring-1 ring-foreground/10">
                {Array.from({ length: 8 }, (_, indice) => (
                    <Skeleton key={indice} className="h-9 rounded-md" />
                ))}
            </div>
        );
    }

    return (
        <div
            className={cn(
                "overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10 transition-opacity",
                // Se atenua en vez de vaciarse: al filtrar, la tabla anterior
                // sigue debajo y el cambio se lee como una transicion y no como
                // un parpadeo a blanco. Con 500 filas, ese parpadeo en cada
                // pulsacion de tecla del buscador es lo que hace insufrible una
                // tabla de datos.
                atenuada && "opacity-60",
            )}
            aria-busy={cargando || atenuada}
        >
            <div className="overflow-x-auto">
                <Table>
                    <TableHeader>
                        <TableRow className="hover:bg-transparent">
                            <TableHead className="w-32 pl-4">Decisión</TableHead>
                            <TableHead>Factura</TableHead>
                            <TableHead className="hidden md:table-cell">Proveedor</TableHead>
                            <TableHead className="hidden lg:table-cell">Fecha</TableHead>
                            <TableHead className="text-right">Total</TableHead>
                            <TableHead className="hidden text-right sm:table-cell">
                                Desvío
                            </TableHead>
                            <TableHead className="hidden xl:table-cell">Pedido / asiento</TableHead>
                            <TableHead className="hidden w-24 xl:table-cell">Lectura</TableHead>
                            <TableHead className="w-8 pr-4" />
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {items.map((factura) => (
                            <FilaFactura
                                key={factura.file_id}
                                factura={factura}
                                activa={fileIdActivo === factura.file_id}
                                parametrosLista={parametrosLista}
                            />
                        ))}
                    </TableBody>
                </Table>
            </div>
        </div>
    );
}

/**
 * Una fila.
 *
 * El enlace de verdad es el nombre del fichero, y no toda la fila. Envolver la
 * fila entera en un `<a>` haria que el texto de dentro no se pudiera seleccionar
 * ni copiar, y en un panel donde se copia un `file_id` para buscarlo en el ERP
 * eso se nota el primer dia.
 */
function FilaFactura({
    factura,
    activa,
    parametrosLista,
}: {
    factura: FacturaResumen;
    activa: boolean;
    parametrosLista: string;
}) {
    const Escalon = ICONO_ESCALON[factura.escalon_lectura];
    const destino = `/facturas/${encodeURIComponent(factura.file_id)}${parametrosLista}`;

    return (
        <TableRow
            className={cn(
                "group",
                // La fila de la que se viene se marca: al volver del detalle, lo
                // primero que se busca es "donde estaba".
                activa && "bg-muted/60",
            )}
        >
            <TableCell className="pl-4">
                <EtiquetaResultado resultado={factura.resultado} />
            </TableCell>

            <TableCell className="max-w-72">
                <Link
                    to={destino}
                    className="font-mono text-xs font-medium underline-offset-4 group-hover:underline focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                    {factura.file_id}
                </Link>
                {factura.motivo_principal ? (
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                        {factura.motivo_principal}
                    </p>
                ) : null}
            </TableCell>

            <TableCell className="hidden md:table-cell">
                <span className="block max-w-52 truncate">{texto(factura.proveedor)}</span>
                {factura.nif ? (
                    <span className="block font-mono text-xs text-muted-foreground">
                        {factura.nif}
                    </span>
                ) : null}
            </TableCell>

            <TableCell className="hidden text-muted-foreground tabular-nums lg:table-cell">
                {fecha(factura.fecha)}
            </TableCell>

            <TableCell className="text-right tabular-nums">
                <span className="block font-medium">{euros(factura.total)}</span>
                {factura.importe_erp !== null ? (
                    <span className="block text-xs text-muted-foreground">
                        ERP {euros(factura.importe_erp)}
                    </span>
                ) : (
                    // Distinguir "no cuadra" de "no hay contra que comparar" es
                    // justo lo que decide si alguien tiene que mirar la factura.
                    <span className="block text-xs text-muted-foreground">sin asiento</span>
                )}
            </TableCell>

            <TableCell
                className={cn("hidden text-right tabular-nums sm:table-cell", claseDesvio(factura.desvio_importe))}
            >
                {factura.desvio_importe === null || factura.desvio_importe === undefined
                    ? SIN_DATO
                    : eurosConSigno(factura.desvio_importe)}
            </TableCell>

            <TableCell className="hidden font-mono text-xs text-muted-foreground xl:table-cell">
                <span className="block">{texto(factura.pedido)}</span>
                <span className="block">{texto(factura.asiento)}</span>
            </TableCell>

            <TableCell className="hidden xl:table-cell">
                <Tooltip>
                    <TooltipTrigger asChild>
                        <span className="inline-flex cursor-help items-center gap-1.5 text-xs text-muted-foreground">
                            <Escalon className="size-3.5 shrink-0" />
                            {porcentaje(factura.calidad_lectura)}
                        </span>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                        <p className="font-medium">
                            {factura.metodo_lectura === "vision_ocr"
                                ? "Leída con OCR (el PDF no traía texto)"
                                : "Leída del texto del PDF"}
                        </p>
                        <p className="text-muted-foreground">
                            {factura.escalon_lectura === "cache_ocr"
                                ? "El texto salió de la caché de OCR."
                                : "El texto salió de la capa de texto del PDF."}{" "}
                            Confianza de la lectura {porcentaje(factura.calidad_lectura)} en{" "}
                            {factura.segundos_lectura.toFixed(1)} s. Es la confianza en lo que se
                            leyó, no en la decisión.
                        </p>
                        {!factura.identificacion_fiable ? (
                            <p className="mt-1 font-medium">
                                Ojo: el motor no pudo identificar al proveedor con seguridad, así que
                                los datos de identidad son de fiar poco.
                            </p>
                        ) : null}
                    </TooltipContent>
                </Tooltip>
            </TableCell>

            <TableCell className="pr-4">
                <Link
                    to={destino}
                    aria-label={`Abrir el expediente de ${factura.file_id}`}
                    className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                    <ChevronRight className="size-4" />
                </Link>
            </TableCell>
        </TableRow>
    );
}
