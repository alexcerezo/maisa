/**
 * La barra de filtros.
 *
 * Tres decisiones que no se ven en el dibujo:
 *
 * 1. **El buscador y el NIF son cuadros distintos, y no es un capricho.** El
 *    parametro `q` de la API busca en `file_id`, `proveedor`, `pedido` y
 *    `asiento` — y **no** en el NIF (ver `CAMPOS_BUSQUEDA` en `filtros.ts`). Como
 *    el NIF es justo el dato con el que se caza a un proveedor suplantado, dejarlo
 *    fuera del buscador sin decirlo seria una trampa: alguien escribiria un NIF en
 *    el cuadro de busqueda, no saldria nada, y no tendria forma de saber que el
 *    filtro correcto es otro. Por eso el NIF tiene su propio cuadro y el de
 *    busqueda dice en el texto de ayuda lo que SI mira.
 *
 * 2. **El texto se confirma con retardo** (ver `useTextoConRetardo`), para no
 *    lanzar una consulta por tecla.
 *
 * 3. **Las fechas son `<input type="date">`**, que da `YYYY-MM-DD`. Es
 *    exactamente el formato que compara `aplicarFiltrosExtra`, que lo hace como
 *    texto: con ISO el orden alfabetico es el cronologico, asi que no hay que
 *    convertir nada y no hay desfase de zona horaria posible.
 */

import type { FiltrosVista } from "../api/filtros";
import { LIMITE_TEXTO } from "../api/filtros";
import { useTextoConRetardo } from "../hooks/useTextoConRetardo";
import { BOTON, CAMPO, ETIQUETA_CAMPO } from "../theme";

/** Un campo de texto de los tres que hay, para no repetir el mismo bloque tres veces. */
function CampoTexto({
    etiqueta,
    ayuda,
    valor,
    sitio,
    alCambiar,
    maxLongitud = LIMITE_TEXTO,
}: {
    etiqueta: string;
    ayuda?: string;
    valor: string;
    sitio: string;
    alCambiar: (nuevo: string) => void;
    maxLongitud?: number;
}) {
    const [borrador, setBorrador] = useTextoConRetardo(valor, alCambiar);
    return (
        <label className="block">
            <span className={ETIQUETA_CAMPO}>{etiqueta}</span>
            <input
                type="search"
                className={CAMPO}
                value={borrador}
                placeholder={sitio}
                // El tope se pone tambien en el cuadro: la API responde 422 a un
                // `q` de mas de 64 caracteres, no cero resultados.
                maxLength={maxLongitud}
                onChange={(evento) => setBorrador(evento.target.value)}
            />
            {ayuda ? <span className="mt-1 block text-xs text-slate-400">{ayuda}</span> : null}
        </label>
    );
}

export function BarraFiltros({
    filtros,
    puestos,
    cambiar,
    limpiar,
}: {
    filtros: FiltrosVista;
    puestos: number;
    cambiar: (cambios: Record<string, string | null>) => void;
    limpiar: () => void;
}) {
    const q = filtros.q ?? "";
    const proveedor = filtros.proveedor ?? "";
    const nif = filtros.nif ?? "";

    return (
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <CampoTexto
                    etiqueta="Buscar"
                    ayuda="Mira en fichero, proveedor, pedido y asiento. No mira el NIF."
                    valor={q}
                    sitio="catering, PO-2026-0096, AS-00096…"
                    alCambiar={(nuevo) => cambiar({ q: nuevo })}
                />
                <CampoTexto
                    etiqueta="Proveedor"
                    valor={proveedor}
                    sitio="parte del nombre"
                    alCambiar={(nuevo) => cambiar({ proveedor: nuevo })}
                />
                <CampoTexto
                    etiqueta="NIF"
                    valor={nif}
                    sitio="parte del NIF"
                    alCambiar={(nuevo) => cambiar({ nif: nuevo })}
                />
                <label className="block">
                    <span className={ETIQUETA_CAMPO}>Fecha desde</span>
                    <input
                        type="date"
                        className={CAMPO}
                        value={filtros.fechaDesde ?? ""}
                        onChange={(evento) => cambiar({ desde: evento.target.value })}
                    />
                </label>
                <label className="block">
                    <span className={ETIQUETA_CAMPO}>Fecha hasta</span>
                    <input
                        type="date"
                        className={CAMPO}
                        value={filtros.fechaHasta ?? ""}
                        onChange={(evento) => cambiar({ hasta: evento.target.value })}
                    />
                </label>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                {puestos > 0 ? (
                    <>
                        <button type="button" onClick={limpiar} className={BOTON}>
                            Limpiar {puestos === 1 ? "el filtro" : `los ${puestos} filtros`}
                        </button>
                        {/*
                         * Este aviso evita un susto real: ocho facturas del lote no
                         * tienen fecha porque son documentos que no casan con ningun
                         * pedido. Al filtrar por fechas desaparecen, y sin decirlo
                         * parece que el panel ha perdido datos.
                         */}
                        {filtros.fechaDesde || filtros.fechaHasta ? (
                            <span className="text-xs text-slate-500">
                                Las facturas sin fecha no salen al filtrar por fecha.
                            </span>
                        ) : null}
                    </>
                ) : (
                    <span className="text-xs text-slate-500">
                        Sin filtros: se esta ensenando el lote completo.
                    </span>
                )}
            </div>
        </section>
    );
}
