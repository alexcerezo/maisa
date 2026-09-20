/**
 * `/facturas` — el explorador: la cabecera, los filtros y la tabla.
 *
 * El orden de los bloques es el de las preguntas que se hace quien mira la
 * pantalla, y por eso la banda de fuente va **antes** que los numeros: lo primero
 * que hay que saber es si esto son los datos de ahora o los del ultimo volcado.
 * Un contador correcto de datos congelados sigue siendo un dato viejo, y de nada
 * sirve ensenarlo bien si no se dice de cuando es.
 *
 * La tabla se ordena y se pagina aqui, en el navegador, porque la API devuelve
 * siempre lo mismo ordenado por `file_id` (500 filas caben de sobra en memoria).
 * Lo que si se le pide a la API es el filtrado: los filtros que ella entiende
 * (`resultado`, `proveedor`, `q`, `lote`) los aplica el servidor, y los que no
 * (fechas y NIF) se aplican aqui en los dos modos, para que el resultado sea el
 * mismo venga de donde venga. Ver `api/filtros.ts`.
 */

import { useMemo } from "react";

import { useFuente, useFacturas } from "../api/hooks";
import { BarraFiltros } from "../components/BarraFiltros";
import { Cargando, Fallo, Vacio } from "../components/Estados";
import { Paginacion } from "../components/Paginacion";
import { PanelEstadisticas } from "../components/PanelEstadisticas";
import { TablaFacturas } from "../components/TablaFacturas";
import { useFiltrosUrl } from "../hooks/useFiltrosUrl";
import { aplicarOrden } from "../orden";

/** Filas por pagina. Cabe comodo en una pantalla y no obliga a bajar rodando. */
const POR_PAGINA = 25;

export default function FacturasPage() {
    const { estado, cargando: comprobando, error: errorFuente, reintentar } = useFuente();
    const {
        filtros,
        orden,
        pagina,
        puestos,
        cambiar,
        ponerResultado,
        alternarCampo,
        irAPagina,
        limpiar,
        direccionConFiltros,
    } = useFiltrosUrl();
    const { datos, error, cargando } = useFacturas(filtros);

    const ordenadas = useMemo(() => (datos ? aplicarOrden(datos.items, orden) : []), [datos, orden]);

    // La pagina se acota aqui y no con un efecto que reescriba la direccion: si el
    // filtro deja menos paginas de las que dice la URL, es mas simple ensenar la
    // ultima que existe que provocar una navegacion desde un efecto.
    const paginas = Math.max(1, Math.ceil(ordenadas.length / POR_PAGINA));
    const paginaEfectiva = Math.min(pagina, paginas);
    const visibles = useMemo(
        () => ordenadas.slice((paginaEfectiva - 1) * POR_PAGINA, paginaEfectiva * POR_PAGINA),
        [ordenadas, paginaEfectiva],
    );

    /** Que ensenar donde va la tabla. Los cuatro casos no son exhaustivos por gusto. */
    let contenido;
    if (errorFuente) {
        contenido = <Fallo error={errorFuente} alReintentar={reintentar} />;
    } else if (error) {
        contenido = (
            <Fallo error={error} alReintentar={reintentar}>
                {estado?.fuente === "congelado"
                    ? "Estas leyendo el congelado. Si el fichero que falta no esta en el despliegue, hay que regenerarlo con el script de fixtures."
                    : null}
            </Fallo>
        );
    } else if (comprobando || (cargando && !datos)) {
        contenido = <Cargando queEs="las facturas" />;
    } else if (datos && datos.items.length === 0) {
        contenido = (
            <Vacio
                titulo="Ninguna factura casa con estos filtros"
                accion={puestos > 0 ? { etiqueta: "Quitar los filtros", alPulsar: limpiar } : undefined}
            >
                {puestos > 0 ? (
                    <>
                        Hay {puestos} {puestos === 1 ? "filtro puesto" : "filtros puestos"}. Recuerda
                        que la busqueda de texto no mira el NIF y que las facturas sin fecha no
                        salen al filtrar por fecha.
                    </>
                ) : (
                    <>El lote esta vacio: ni la API ni el congelado traen facturas.</>
                )}
            </Vacio>
        );
    } else {
        contenido = (
            <>
                {/*
                 * Cuando los filtros de fecha o de NIF estan puestos, el total de la
                 * API y las filas en pantalla no coinciden, y es correcto: la API no
                 * sabe hacer esos dos filtros. Se explica en vez de dejar dos cifras
                 * que parecen contradecirse.
                 */}
                {datos && datos.items.length < datos.total ? (
                    <p className="mb-2 text-xs text-slate-500">
                        {datos.total} coincidencias en el origen, {datos.items.length} en pantalla.
                        Los filtros de fecha y NIF se aplican en el navegador porque la API no los
                        conoce.
                    </p>
                ) : null}
                <TablaFacturas
                    facturas={visibles}
                    orden={orden}
                    alOrdenar={alternarCampo}
                    direccionDeVuelta={direccionConFiltros}
                />
            </>
        );
    }

    return (
        <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
            <header className="flex flex-wrap items-end justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
                        Conciliacion de facturas
                    </h1>
                    <p className="mt-1 text-sm text-slate-500">
                        La norma v3.1 aplicada al lote completo: que se paga, que no y por que.
                    </p>
                </div>
                {cargando && datos ? (
                    <span className="inline-flex items-center gap-2 text-xs text-slate-500">
                        <span
                            aria-hidden="true"
                            className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600"
                        />
                        Actualizando
                    </span>
                ) : null}
            </header>

            <PanelEstadisticas
                estadisticas={estado?.estadisticas ?? null}
                comprobando={comprobando}
                activo={filtros.resultado ?? null}
                alElegir={ponerResultado}
            />

            <BarraFiltros filtros={filtros} puestos={puestos} cambiar={cambiar} limpiar={limpiar} />

            {contenido}

            {datos && datos.items.length > 0 ? (
                <Paginacion
                    pagina={paginaEfectiva}
                    porPagina={POR_PAGINA}
                    total={ordenadas.length}
                    alIr={irAPagina}
                />
            ) : null}
        </main>
    );
}
