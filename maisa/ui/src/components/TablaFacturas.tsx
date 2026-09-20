/**
 * La tabla de facturas.
 *
 * Tres cosas que se han decidido aqui y conviene no deshacer sin querer:
 *
 * 1. **El importe que falta se ensena "—", no "0,00 €".** Ocho de las 500
 *    facturas no tienen proveedor ni importe, y `desvio_importe` falta siempre que
 *    falte un lado de la resta. Un cero ahi no es un hueco: es una cifra falsa con
 *    el mismo aspecto que una buena.
 *
 * 2. **`PAGAR` sin motivo no es un dato que falte.** 448 de las 500 no tienen
 *    ningun motivo, y eso es exactamente el caso bueno. Por eso dice "sin motivos"
 *    en gris y no un guion, que se leeria como informacion perdida.
 *
 * 3. **Las columnas del motor se pueden ordenar, y el orden se anade al de la
 *    API, no lo sustituye.** `file_id` se compara a pelo para que el orden por
 *    defecto sea identico al que devuelve el servidor (ver `orden.ts`); si aqui se
 *    usara `localeCompare`, las 65 facturas con acentos se colocarian de otra
 *    forma que en la API y el mismo panel ensenaria dos ordenes distintos.
 */

import { Link } from "react-router-dom";

import type { FacturaResumen } from "../api/types";
import { euros, eurosConSigno, fecha, texto } from "../formato";
import type { CampoOrden, Orden } from "../orden";
import { DistintivoResultado } from "./Distintivos";

/** Una cabecera de columna. Si tiene `campo`, se puede pulsar para ordenar. */
function Cabecera({
    titulo,
    campo,
    orden,
    alOrdenar,
    aLaDerecha = false,
}: {
    titulo: string;
    campo?: CampoOrden;
    orden: Orden;
    alOrdenar: (campo: CampoOrden) => void;
    aLaDerecha?: boolean;
}) {
    if (!campo) {
        return (
            <th
                scope="col"
                className={`whitespace-nowrap px-3 py-2 ${aLaDerecha ? "text-right" : "text-left"}`}
            >
                {titulo}
            </th>
        );
    }
    const activa = orden.campo === campo;
    return (
        <th
            scope="col"
            className={`whitespace-nowrap px-3 py-2 ${aLaDerecha ? "text-right" : "text-left"}`}
            aria-sort={activa ? (orden.ascendente ? "ascending" : "descending") : "none"}
        >
            <button
                type="button"
                onClick={() => alOrdenar(campo)}
                className={`inline-flex items-center gap-1 rounded transition hover:text-slate-900 ${activa ? "font-semibold text-slate-900" : ""
                    }`}
                title={`Ordenar por ${titulo.toLowerCase()}`}
            >
                {titulo}
                <span aria-hidden="true" className={activa ? "" : "text-slate-300"}>
                    {activa && !orden.ascendente ? "↓" : "↑"}
                </span>
            </button>
        </th>
    );
}

export function TablaFacturas({
    facturas,
    orden,
    alOrdenar,
    direccionDeVuelta,
}: {
    facturas: readonly FacturaResumen[];
    orden: Orden;
    alOrdenar: (campo: CampoOrden) => void;
    /** Los filtros que hay puestos, para devolverlos al volver del detalle. */
    direccionDeVuelta: string;
}) {
    const comun = { orden, alOrdenar };

    return (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
            <table className="w-full min-w-[68rem] border-collapse text-sm">
                <thead>
                    <tr className="border-b border-slate-200 bg-slate-50 text-xs font-medium text-slate-600">
                        <Cabecera titulo="Factura" campo="file_id" {...comun} />
                        <Cabecera titulo="Fecha" campo="fecha" {...comun} />
                        <Cabecera titulo="Proveedor" campo="proveedor" {...comun} />
                        <Cabecera titulo="Pedido / asiento" {...comun} />
                        <Cabecera titulo="Total / ERP" campo="total" aLaDerecha {...comun} />
                        <Cabecera titulo="Desvio" campo="desvio_importe" aLaDerecha {...comun} />
                        <Cabecera titulo="Estado ERP" {...comun} />
                        <Cabecera titulo="Decision" {...comun} />
                        <Cabecera titulo="Motivo" {...comun} />
                    </tr>
                </thead>
                <tbody>
                    {facturas.map((factura) => {
                        const desvio = factura.desvio_importe;
                        return (
                            <tr
                                key={factura.file_id}
                                className="border-b border-slate-100 align-top last:border-b-0 hover:bg-slate-50/80"
                            >
                                <td className="px-3 py-2">
                                    <Link
                                        to={`/facturas/${factura.file_id}`}
                                        state={{ volverA: direccionDeVuelta }}
                                        className="font-mono text-xs break-all text-blue-700 hover:underline"
                                    >
                                        {factura.file_id}
                                    </Link>
                                    {!factura.identificacion_fiable ? (
                                        <span
                                            className="mt-1 block text-xs text-amber-700"
                                            title="El motor no ha podido identificar de quien es la factura: casi todo lo de esta fila es heredado del ERP o viene vacio."
                                        >
                                            sin identificar
                                        </span>
                                    ) : null}
                                </td>
                                <td className="px-3 py-2 whitespace-nowrap text-slate-700 tabular-nums">
                                    {fecha(factura.fecha)}
                                </td>
                                <td className="px-3 py-2 text-slate-700">
                                    <span className="block max-w-[16rem] truncate" title={texto(factura.proveedor)}>
                                        {texto(factura.proveedor)}
                                    </span>
                                    <span className="block font-mono text-xs text-slate-400">
                                        {texto(factura.nif)}
                                    </span>
                                </td>
                                <td className="px-3 py-2 font-mono text-xs text-slate-600">
                                    <span className="block">{texto(factura.pedido)}</span>
                                    <span className="block text-slate-400">{texto(factura.asiento)}</span>
                                </td>
                                <td className="px-3 py-2 text-right whitespace-nowrap tabular-nums">
                                    <span className="block font-medium text-slate-900">
                                        {euros(factura.total)}
                                    </span>
                                    <span className="block text-xs text-slate-400">
                                        {euros(factura.importe_erp)}
                                    </span>
                                </td>
                                <td className="px-3 py-2 text-right whitespace-nowrap tabular-nums">
                                    {/*
                                     * Tres casos y no dos, porque cero no es un hueco. "Cuadra
                                     * con el ERP" es el mejor valor posible de esta columna y
                                     * tiene que poder distinguirse de "no se puede comparar";
                                     * pintar los dos como un guion haria que las 448 facturas
                                     * limpias pareciesen tener un dato perdido.
                                     */}
                                    {desvio === null ? (
                                        <span
                                            className="text-slate-300"
                                            title="La factura o el ERP no traen importe, asi que no hay nada que comparar."
                                        >
                                            —
                                        </span>
                                    ) : desvio === 0 ? (
                                        <span className="text-slate-400" title="Cuadra con el ERP.">
                                            {eurosConSigno(desvio)}
                                        </span>
                                    ) : (
                                        <span
                                            className="text-amber-800"
                                            title="Diferencia entre lo que dice la factura y lo que dice el ERP."
                                        >
                                            {eurosConSigno(desvio)}
                                        </span>
                                    )}
                                </td>
                                <td className="px-3 py-2 whitespace-nowrap">
                                    <span
                                        className={
                                            factura.estado_erp === "PENDIENTE"
                                                ? "text-xs text-slate-500"
                                                : "text-xs font-medium text-rose-700"
                                        }
                                    >
                                        {texto(factura.estado_erp)}
                                    </span>
                                </td>
                                <td className="px-3 py-2">
                                    <DistintivoResultado resultado={factura.resultado} />
                                </td>
                                <td className="px-3 py-2">
                                    {factura.motivo_principal ? (
                                        <span
                                            className="block max-w-[22rem] text-xs text-slate-600"
                                            title={factura.motivo_principal}
                                        >
                                            {factura.motivo_principal}
                                        </span>
                                    ) : factura.resultado === "PAGAR" ? (
                                        <span className="text-xs text-slate-300">sin motivos</span>
                                    ) : (
                                        <span className="text-xs text-slate-300">—</span>
                                    )}
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}
