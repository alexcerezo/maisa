/**
 * Ruta `/facturas/:fileId` — el detalle: el expediente de una factura.
 *
 * El `fileId` viene de la URL y es el nombre del PDF, tal cual
 * (`2026-01-08_P001.pdf`): es la clave primaria de todo el sistema, la misma
 * que usan `/api/facturas/{file_id}` y el `/pdf`. Eso permite recargar la ficha
 * y compartir el enlace, que es justo lo que hace falta cuando alguien pregunta
 * "¿por que esta no se paga?".
 *
 * La pantalla contesta tres preguntas y en este orden, porque es el orden en que
 * se las hace quien la abre:
 *
 * 1. **Que se ha decidido** (la cabecera).
 * 2. **Si el documento intento cambiar esa decision** (el bloque ambar de
 *    sospechosos, que va arriba del todo cuando existe).
 * 3. **Por que**: los motivos, las seis reglas y la lectura cruda.
 *
 * Los hechos se ordenan con `ordenarHechos`, que pone delante lo que hay que
 * mirar. El orden de la norma (R1..R6) es el de la ley, no el de la urgencia:
 * quien abre esta pantalla viene a ver que ha fallado, y con seis reglas evaluadas
 * casi siempre hay tres o cuatro `ok: true` que estorban al principio.
 */

import { Link, useLocation, useParams } from "react-router-dom";

import { useDetalle, useFuente } from "../api/hooks";
import { ordenarHechos } from "../api/severidad";
import { Campos } from "../components/Campos";
import { DistintivoNeutro, DistintivoResultado } from "../components/Distintivos";
import { Cargando, Fallo } from "../components/Estados";
import { FichaHecho } from "../components/FichaHecho";
import { LectorPdf } from "../components/LectorPdf";
import { Sospechosos } from "../components/Sospechosos";
import { Dato, Datos, Tarjeta } from "../components/Tarjeta";
import {
    euros,
    eurosConSigno,
    fecha,
    huellaCorta,
    porcentaje,
    segundos,
    texto,
} from "../formato";
import { APUNTE, DESCRIPCION_RESULTADO, ENLACE_BOTON, ETIQUETA_METODO } from "../theme";

/** Lo que el detalle espera recibir en el estado de la navegacion. */
interface EstadoNavegacion {
    volverA?: string;
}

export default function FacturaDetallePage() {
    const { fileId } = useParams<{ fileId: string }>();
    const ubicacion = useLocation();
    const volverA = (ubicacion.state as EstadoNavegacion | null)?.volverA ?? "/facturas";

    const { estado, error: errorFuente, reintentar } = useFuente();
    const { datos, error, cargando } = useDetalle(fileId);

    const enlaceDeVuelta = (
        <Link to={volverA} className={ENLACE_BOTON}>
            ← Volver a la lista
        </Link>
    );

    if (errorFuente) {
        return (
            <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
                {enlaceDeVuelta}
                <Fallo error={errorFuente} alReintentar={reintentar} />
            </main>
        );
    }

    if (error) {
        return (
            <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
                {enlaceDeVuelta}
                <Fallo error={error} alReintentar={reintentar}>
                    {estado?.fuente === "congelado"
                        ? "Estas leyendo el congelado, que lleva los 500 detalles. Si esta factura no aparece, el nombre del fichero no es el que se congelo."
                        : "Si la ruta del navegador lleva el nombre codificado a mano, corrigelo: el identificador es el nombre exacto del PDF."}
                </Fallo>
            </main>
        );
    }

    if (cargando && !datos) {
        return (
            <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
                {enlaceDeVuelta}
                <Cargando queEs="la factura" />
            </main>
        );
    }

    if (!datos) {
        return (
            <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
                {enlaceDeVuelta}
                <p className={APUNTE}>Sin datos de esta factura.</p>
            </main>
        );
    }

    const resumen = datos.resumen;
    const hechos = ordenarHechos(datos.hechos);

    return (
        <main className="mx-auto max-w-[110rem] space-y-4 px-4 py-6 sm:px-6 lg:px-8">
            <div className="flex flex-wrap items-center justify-between gap-3">{enlaceDeVuelta}</div>

            <header className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
                <div className="flex flex-wrap items-center gap-2">
                    <h1 className="font-mono text-lg font-semibold break-all text-slate-900">
                        {datos.file_id}
                    </h1>
                    <DistintivoResultado resultado={datos.resultado} />
                    {!datos.identificacion_fiable ? (
                        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800">
                            sin identificar
                        </span>
                    ) : null}
                    <DistintivoNeutro>{datos.version_norma}</DistintivoNeutro>
                    <DistintivoNeutro>{`lote ${datos.lote}`}</DistintivoNeutro>
                </div>

                <p className="mt-3 text-sm text-slate-700">
                    {datos.motivo_principal ?? DESCRIPCION_RESULTADO[datos.resultado]}
                </p>
                {datos.motivo_principal ? (
                    <p className={`${APUNTE} mt-1`}>{DESCRIPCION_RESULTADO[datos.resultado]}</p>
                ) : null}
            </header>

            {/*
             * Los intentos de manipulacion van antes que nada, y solo existen en 20
             * de las 500. Si estan, son lo primero que hay que ver; si no estan, el
             * componente no pinta nada.
             */}
            <Sospechosos lectura={datos.lectura} ordenes={datos.campos.ordenes_resultado ?? []} />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
                <div className="space-y-4">
                    <Tarjeta
                        titulo="Motivos"
                        acciones={
                            <span className={APUNTE}>
                                {datos.motivos.length === 1
                                    ? "1 motivo"
                                    : `${datos.motivos.length} motivos`}
                            </span>
                        }
                    >
                        {datos.motivos.length === 0 ? (
                            <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
                                Sin ningun motivo. En este lote no es un dato que falte: es el caso
                                bueno, y son 448 de las 500 facturas.
                            </p>
                        ) : (
                            <ul className="space-y-2">
                                {datos.motivos.map((motivo, indice) => (
                                    <li
                                        key={`${motivo}-${indice}`}
                                        className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
                                    >
                                        {motivo}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </Tarjeta>

                    <section className="space-y-3">
                        <h2 className="text-sm font-semibold text-slate-800">
                            Reglas evaluadas
                            <span className={`${APUNTE} ml-2 font-normal`}>
                                {hechos.length} comprobaciones, de lo que bloquea a lo que se cumple
                            </span>
                        </h2>
                        <p className={APUNTE}>
                            No son seis fichas fijas. R1 puede salir dos veces, una por el NIF del
                            emisor y otra por el IBAN, porque son dos comprobaciones distintas
                            contra el maestro. R6 solo aparece cuando hay una anomalia que un
                            humano debe mirar. Por eso el numero de fichas cambia de una factura a
                            otra y ninguna lista fija valdria para todas.
                        </p>
                        {hechos.map((hecho, indice) => (
                            // La clave no puede ser la regla: R1_identidad viene repetida.
                            <FichaHecho key={`${hecho.regla}-${indice}`} hecho={hecho} />
                        ))}
                    </section>

                    <Campos campos={datos.campos} />
                </div>

                <div className="space-y-4">
                    <LectorPdf fileId={datos.file_id} />

                    <Tarjeta titulo="Ficha">
                        <Datos>
                            <Dato etiqueta="Proveedor">{texto(resumen.proveedor)}</Dato>
                            <Dato etiqueta="NIF" mono>
                                {texto(resumen.nif)}
                            </Dato>
                            <Dato etiqueta="Fecha">{fecha(resumen.fecha)}</Dato>
                            <Dato etiqueta="Total leido">{euros(resumen.total)}</Dato>
                            <Dato etiqueta="Importe del ERP">{euros(resumen.importe_erp)}</Dato>
                            <Dato etiqueta="Desvio" mono={false}>
                                {resumen.desvio_importe === null ? (
                                    <span className="text-slate-400">—</span>
                                ) : (
                                    <span
                                        className={
                                            resumen.desvio_importe === 0
                                                ? "text-slate-500"
                                                : "font-medium text-amber-800"
                                        }
                                    >
                                        {eurosConSigno(resumen.desvio_importe)}
                                    </span>
                                )}
                            </Dato>
                            <Dato etiqueta="Pedido" mono>
                                {texto(resumen.pedido)}
                            </Dato>
                            <Dato etiqueta="Asiento" mono>
                                {texto(resumen.asiento)}
                            </Dato>
                            <Dato etiqueta="Estado ERP">
                                <span
                                    className={
                                        resumen.estado_erp === "PENDIENTE"
                                            ? "text-slate-700"
                                            : "font-medium text-rose-700"
                                    }
                                >
                                    {texto(resumen.estado_erp)}
                                </span>
                            </Dato>
                            <Dato etiqueta="Lectura">
                                {ETIQUETA_METODO[datos.lectura.metodo_lectura]}
                            </Dato>
                            <Dato etiqueta="Confianza">{porcentaje(datos.lectura.calidad_lectura)}</Dato>
                            <Dato etiqueta="Tiempo">{segundos(datos.lectura.segundos_lectura)}</Dato>
                            <Dato etiqueta="Huella" mono>
                                <span title={datos.sha256}>{huellaCorta(datos.sha256)}</span>
                            </Dato>
                            <Dato etiqueta="Identificacion">
                                <span
                                    className={
                                        datos.identificacion_fiable
                                            ? "text-slate-700"
                                            : "font-medium text-amber-800"
                                    }
                                >
                                    {datos.identificacion_fiable
                                        ? "leida del documento"
                                        : "heredada del ERP"}
                                </span>
                            </Dato>
                        </Datos>
                    </Tarjeta>
                </div>
            </div>
        </main>
    );
}
