/**
 * El pie de la tabla: cuantas se estan viendo y como moverse.
 *
 * La cuenta se hace sobre lo que hay **en pantalla**, no sobre el total de la
 * fuente, y ese detalle importa. `total` es lo que diria la API con ese filtro
 * (que no sabe de fechas ni de NIF) y `facturas.length` es lo que se pinta con
 * todo aplicado; cuando los filtros de fecha estan puestos, los dos numeros
 * difieren a proposito y el texto lo explica en vez de ensenar una cifra que no
 * cuadra con las filas.
 */

import { BOTON } from "../theme";

export function Paginacion({
    pagina,
    porPagina,
    total,
    alIr,
}: {
    /** Ya acotada a una pagina que existe. */
    pagina: number;
    porPagina: number;
    /** Cuantas filas hay que paginar (las que se van a pintar). */
    total: number;
    alIr: (pagina: number) => void;
}) {
    const paginas = Math.max(1, Math.ceil(total / porPagina));
    const primera = total === 0 ? 0 : (pagina - 1) * porPagina + 1;
    const ultima = Math.min(total, pagina * porPagina);

    return (
        <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-slate-500">
                Mostrando <span className="font-medium text-slate-700">{primera}</span>–
                <span className="font-medium text-slate-700">{ultima}</span> de{" "}
                <span className="font-medium text-slate-700">{total}</span> facturas
            </p>

            {paginas > 1 ? (
                <nav className="flex items-center gap-2" aria-label="Paginacion">
                    <button
                        type="button"
                        onClick={() => alIr(pagina - 1)}
                        disabled={pagina <= 1}
                        className={BOTON}
                    >
                        ← Anterior
                    </button>
                    <span className="text-xs text-slate-600 tabular-nums">
                        Pagina {pagina} de {paginas}
                    </span>
                    <button
                        type="button"
                        onClick={() => alIr(pagina + 1)}
                        disabled={pagina >= paginas}
                        className={BOTON}
                    >
                        Siguiente →
                    </button>
                </nav>
            ) : null}
        </div>
    );
}
