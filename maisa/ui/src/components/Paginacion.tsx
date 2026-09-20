/**
 * El pie de la tabla: cuantas filas se ven y como cambiar de pagina.
 *
 * Se pinta a mano con los envoltorios de shadcn (`Pagination`,
 * `PaginationContent`, `PaginationItem`) en vez de con `PaginationLink`, por dos
 * motivos concretos:
 *
 * 1. **`PaginationLink` es un `<a>` duro.** En una aplicacion con router, un
 *    `<a href>` recarga la pagina entera: se pierde la tabla, se vuelve a
 *    comprobar la fuente y el scroll salta arriba. Con `Button asChild` + `Link`
 *    la navegacion es de router y la pagina no se recarga.
 *
 * 2. **Trae los textos en ingles** ("Go to previous page", "More pages"). Aqui
 *    todo esta en castellano y una etiqueta en ingles suelta en un panel en
 *    castellano se nota justo en lo que un lector de pantalla lee en voz alta.
 *
 * Los envoltorios si se usan porque son `<nav>` + `<ul>` + `<li>`, que es lo que
 * hace que un lector de pantalla anuncie "navegacion" y cuente las paginas.
 */

import { ChevronLeft, ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
    Pagination,
    PaginationContent,
    PaginationEllipsis,
    PaginationItem,
} from "@/components/ui/pagination";
import { entero } from "@/lib/formato";
import { paginasVisibles, totalDePaginas, tramoDePagina } from "@/lib/paginacion";

export function Paginacion({
    pagina,
    totalFilas,
    totalSinFiltros,
    rutaDePagina,
}: {
    pagina: number;
    /** Filas que quedan despues de todos los filtros. Es lo que se pinta. */
    totalFilas: number;
    /** Filas que habria sin filtros. Solo para el "de N". */
    totalSinFiltros: number;
    /** Construye el `to` del router para una pagina. */
    rutaDePagina: (pagina: number) => string;
}) {
    const tramo = tramoDePagina(pagina, totalFilas);
    const ultimaPagina = totalDePaginas(totalFilas);
    const paginas = paginasVisibles(pagina, ultimaPagina);

    return (
        <div className="flex flex-col-reverse items-center gap-3 sm:flex-row sm:justify-between">
            {/*
             * El recuento se lee de `totalFilas` y no del `total` de la fuente.
             * Los dos numeros pueden diferir a proposito: `total` es lo que diria
             * la API con ese filtro (que no sabe de fechas ni de NIF) y
             * `totalFilas` es lo que hay en pantalla. Ensenar el de la API aqui
             * es el fallo que ya se cometio una vez: el congelado decia 500 con
             * 43 filas pintadas.
             */}
            <p className="text-xs text-muted-foreground tabular-nums">
                {totalFilas === 0 ? (
                    "Ninguna factura"
                ) : (
                    <>
                        Filas <span className="text-foreground">{entero(tramo.desde)}</span>–
                        <span className="text-foreground">{entero(tramo.hasta)}</span> de{" "}
                        <span className="text-foreground">{entero(totalFilas)}</span>
                        {totalSinFiltros !== totalFilas ? (
                            <> (de {entero(totalSinFiltros)} en la traza)</>
                        ) : null}
                    </>
                )}
            </p>

            {ultimaPagina > 1 ? (
                <Pagination className="mx-0 w-auto justify-end">
                    <PaginationContent>
                        <PaginationItem>
                            {pagina > 1 ? (
                                <Button asChild variant="ghost" size="icon" className="pl-1.5!">
                                    <Link
                                        to={rutaDePagina(pagina - 1)}
                                        aria-label="Página anterior"
                                        title="Página anterior"
                                    >
                                        <ChevronLeft />
                                    </Link>
                                </Button>
                            ) : (
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    disabled
                                    className="pl-1.5!"
                                    aria-label="Página anterior"
                                >
                                    <ChevronLeft />
                                </Button>
                            )}
                        </PaginationItem>

                        {paginas.map((numero, indice) =>
                            numero === "hueco" ? (
                                <PaginationItem key={`hueco-${indice}`}>
                                    <PaginationEllipsis />
                                </PaginationItem>
                            ) : (
                                <PaginationItem key={numero}>
                                    <Button
                                        asChild
                                        variant={numero === pagina ? "outline" : "ghost"}
                                        size="icon"
                                    >
                                        <Link
                                            to={rutaDePagina(numero)}
                                            aria-current={numero === pagina ? "page" : undefined}
                                            aria-label={`Página ${numero}`}
                                        >
                                            {numero}
                                        </Link>
                                    </Button>
                                </PaginationItem>
                            ),
                        )}

                        <PaginationItem>
                            {pagina < ultimaPagina ? (
                                <Button asChild variant="ghost" size="icon" className="pr-1.5!">
                                    <Link
                                        to={rutaDePagina(pagina + 1)}
                                        aria-label="Página siguiente"
                                        title="Página siguiente"
                                    >
                                        <ChevronRight />
                                    </Link>
                                </Button>
                            ) : (
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    disabled
                                    className="pr-1.5!"
                                    aria-label="Página siguiente"
                                >
                                    <ChevronRight />
                                </Button>
                            )}
                        </PaginationItem>
                    </PaginationContent>
                </Pagination>
            ) : null}
        </div>
    );
}
