/**
 * Los filtros viven en la direccion, no en el estado de la pantalla.
 *
 * Es la misma decision que se tomo con el detalle, y por el mismo motivo: una
 * vista filtrada que se puede recargar y compartir. Ademas resuelve un caso que se
 * da todo el rato —filtrar, abrir una factura, volver— porque el enlace de vuelta
 * lleva los filtros consigo. Con el estado en memoria, volver del detalle
 * devolveria la tabla entera y habria que filtrar otra vez.
 *
 * Hay dos parametros que **no** son filtros y no se tocan nunca: `api` y `fuente`.
 * Los leen `config.ts` y `fuente.ts` al arrancar. Por eso todo se hace sobre una
 * copia de los parametros actuales y se borran solo las claves que son nuestras:
 * una pantalla que limpie la direccion entera se lleva por delante la eleccion de
 * API, que es justo lo que hace falta para que haya datos.
 *
 * `replace: true` en cada cambio tambien es deliberado. Sin el, escribir en el
 * buscador dejaria una entrada de historial por pulsacion y el boton de atras
 * tendria que pulsarse veinte veces para salir de la pantalla.
 */

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { limpiarTexto, type FiltrosVista } from "../api/filtros";
import { RESULTADOS, type Resultado } from "../api/types";
import {
    alternarOrden,
    escribirOrden,
    leerOrden,
    type CampoOrden,
    type Orden,
} from "../orden";

/** Las claves que son filtros de la vista. Se listan para poder contarlas y borrarlas. */
const CLAVES_FILTRO = ["q", "resultado", "proveedor", "desde", "hasta", "nif"] as const;

export interface FiltrosDeUrl {
    filtros: FiltrosVista;
    orden: Orden;
    pagina: number;
    /** Cuantos filtros hay puestos. Es lo que decide si el boton de limpiar se ve. */
    puestos: number;
    /** Cambia parametros y vuelve a la primera pagina. `null` borra el parametro. */
    cambiar: (cambios: Record<string, string | null>) => void;
    ponerResultado: (resultado: Resultado | null) => void;
    alternarCampo: (campo: CampoOrden) => void;
    irAPagina: (pagina: number) => void;
    limpiar: () => void;
    /** La direccion de esta vista, para volver del detalle a lo mismo que se estaba viendo. */
    direccionConFiltros: string;
}

/** Un `resultado` de la direccion que no sea uno de los tres se ignora. */
function leerResultado(valor: string | null): Resultado | null {
    if (!valor) return null;
    return RESULTADOS.includes(valor as Resultado) ? (valor as Resultado) : null;
}

function leerPagina(valor: string | null): number {
    const numero = Number(valor);
    return Number.isInteger(numero) && numero >= 1 ? numero : 1;
}

export function useFiltrosUrl(): FiltrosDeUrl {
    const [parametros, setParametros] = useSearchParams();

    const filtros = useMemo<FiltrosVista>(
        () => ({
            q: parametros.get("q"),
            resultado: leerResultado(parametros.get("resultado")),
            proveedor: parametros.get("proveedor"),
            fechaDesde: parametros.get("desde"),
            fechaHasta: parametros.get("hasta"),
            nif: parametros.get("nif"),
        }),
        [parametros],
    );

    const orden = useMemo(() => leerOrden(parametros.get("orden")), [parametros]);
    const pagina = leerPagina(parametros.get("pagina"));

    const cambiar = useCallback(
        (cambios: Record<string, string | null>) => {
            setParametros(
                (previos) => {
                    const siguientes = new URLSearchParams(previos);
                    for (const [clave, valor] of Object.entries(cambios)) {
                        // Cadena vacia y `null` significan lo mismo: quitar el
                        // filtro. Se tratan igual para que un cuadro de texto
                        // borrado a mano no deje un `?q=` colgando.
                        if (valor === null || valor === "") siguientes.delete(clave);
                        else siguientes.set(clave, valor);
                    }
                    // Cambiar un filtro invalida la pagina en la que estabas: la 7
                    // de una busqueda nueva casi nunca existe y la tabla saldria
                    // vacia sin explicar por que.
                    siguientes.delete("pagina");
                    return siguientes;
                },
                { replace: true },
            );
        },
        [setParametros],
    );

    const ponerResultado = useCallback(
        (resultado: Resultado | null) => cambiar({ resultado }),
        [cambiar],
    );

    const alternarCampo = useCallback(
        (campo: CampoOrden) => {
            setParametros(
                (previos) => {
                    const siguientes = new URLSearchParams(previos);
                    const nuevo = escribirOrden(alternarOrden(leerOrden(previos.get("orden")), campo));
                    if (nuevo) siguientes.set("orden", nuevo);
                    else siguientes.delete("orden");
                    return siguientes;
                },
                { replace: true },
            );
        },
        [setParametros],
    );

    const irAPagina = useCallback(
        (destino: number) => {
            setParametros(
                (previos) => {
                    const siguientes = new URLSearchParams(previos);
                    if (destino <= 1) siguientes.delete("pagina");
                    else siguientes.set("pagina", String(destino));
                    return siguientes;
                },
                { replace: true },
            );
        },
        [setParametros],
    );

    const limpiar = useCallback(() => {
        setParametros(
            (previos) => {
                // Se clona y se borran claves sueltas, en vez de crear unos
                // parametros nuevos: asi `api` y `fuente` sobreviven al boton.
                const siguientes = new URLSearchParams(previos);
                for (const clave of CLAVES_FILTRO) siguientes.delete(clave);
                siguientes.delete("pagina");
                return siguientes;
            },
            { replace: true },
        );
    }, [setParametros]);

    // Se cuenta con `limpiarTexto` y no con `!== ""` para que el numero cuadre con
    // lo que se ve: un `?q=%20` (un espacio) no es un filtro puesto, aunque en la
    // direccion ocupe sitio.
    const puestos = useMemo(() => {
        let total = 0;
        if (limpiarTexto(filtros.q)) total += 1;
        if (limpiarTexto(filtros.proveedor)) total += 1;
        if (limpiarTexto(filtros.nif)) total += 1;
        if (filtros.resultado) total += 1;
        if (filtros.fechaDesde) total += 1;
        if (filtros.fechaHasta) total += 1;
        return total;
    }, [filtros]);

    const direccionConFiltros = useMemo(() => {
        const cadena = parametros.toString();
        return cadena ? `/facturas?${cadena}` : "/facturas";
    }, [parametros]);

    return {
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
    };
}
