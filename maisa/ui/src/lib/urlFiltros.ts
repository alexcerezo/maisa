/**
 * Los filtros, en la barra de direcciones.
 *
 * El proyecto ya decidio donde vive el estado: "el detalle vive en la URL y no
 * en un estado interno de la pantalla". Los filtros son el mismo caso y por la
 * misma razon — una tabla filtrada se comparte, se recarga y se manda por chat,
 * y con los filtros en un `useState` el enlace que se manda abre la tabla
 * entera. En una demo eso es la diferencia entre enseñar el caso y describirlo.
 *
 * Aqui solo vive la traduccion entre `URLSearchParams` y `FiltrosVista`. La
 * pantalla no toca cadenas de consulta y esta capa no sabe nada de React, asi que
 * se puede razonar sobre ella sin montar un navegador.
 */

import { RESULTADOS, type Resultado } from "@/api/types";
import { limpiarTexto, type FiltrosVista } from "@/api/filtros";

/** Los nombres de los parametros. Cortos porque van en enlaces que se comparten. */
export const CLAVES = {
    q: "q",
    resultado: "resultado",
    lote: "lote",
    nif: "nif",
    desde: "desde",
    hasta: "hasta",
    pagina: "pagina",
} as const;

/**
 * El unico valor de `resultado` que se acepta de la URL.
 *
 * Cualquier otra cosa se descarta **sin avisar y sin romper**: la direccion la
 * escribe cualquiera, y un `?resultado=PAGAR_TODO` a mano no puede dejar la
 * pantalla en blanco ni filtrar por un valor que el motor no produce. La lista
 * sale de `RESULTADOS`, que es la constante cerrada del contrato.
 */
function resultadoValido(valor: string | null): Resultado | null {
    if (!valor) return null;
    return RESULTADOS.find((conocido) => conocido === valor) ?? null;
}

/** `?lote=1` -> `1`. Un lote no numerico se ignora, no se convierte en `NaN`. */
function loteValido(valor: string | null): number | null {
    if (!valor) return null;
    const numero = Number(valor);
    return Number.isInteger(numero) ? numero : null;
}

/** Una fecha `YYYY-MM-DD` o nada. El `<input type="date">` ya solo da eso. */
function fechaValida(valor: string | null): string | null {
    if (!valor) return null;
    return /^\d{4}-\d{2}-\d{2}$/.test(valor) ? valor : null;
}

/** Lo que dice la direccion, ya saneado. */
export function filtrosDeUrl(params: URLSearchParams): FiltrosVista {
    return {
        q: limpiarTexto(params.get(CLAVES.q)),
        resultado: resultadoValido(params.get(CLAVES.resultado)),
        lote: loteValido(params.get(CLAVES.lote)),
        nif: limpiarTexto(params.get(CLAVES.nif)),
        fechaDesde: fechaValida(params.get(CLAVES.desde)),
        fechaHasta: fechaValida(params.get(CLAVES.hasta)),
    };
}

/**
 * La pagina actual, 1-based y nunca menor que 1.
 *
 * Se pagina en el cliente porque `cargarFacturas` ya trae **todo** lo que casa
 * con el filtro (la API se pagina sola por dentro hasta agotar el freno). Un
 * `?pagina=0` o un `?pagina=-3` escrito a mano daria un `slice` con indices
 * negativos y una tabla que empieza por el final, asi que se acota aqui.
 */
export function paginaDeUrl(params: URLSearchParams): number {
    const numero = Number(params.get(CLAVES.pagina) ?? "1");
    return Number.isInteger(numero) && numero >= 1 ? numero : 1;
}

/**
 * Escribe los filtros en los parametros. **No muta** los que recibe.
 *
 * Lo que no hay se **borra** del enlace en vez de quedarse vacio (`?q=&nif=`):
 * una direccion con parametros vacios se ve sucia al compartirla y hace que dos
 * enlaces con los mismos filtros no sean la misma cadena.
 */
export function escribirFiltros(filtros: FiltrosVista, pagina: number): URLSearchParams {
    const params = new URLSearchParams();

    const q = limpiarTexto(filtros.q);
    if (q) params.set(CLAVES.q, q);

    if (filtros.resultado) params.set(CLAVES.resultado, filtros.resultado);
    if (filtros.lote !== null && filtros.lote !== undefined) {
        params.set(CLAVES.lote, String(filtros.lote));
    }

    const nif = limpiarTexto(filtros.nif);
    if (nif) params.set(CLAVES.nif, nif);

    const desde = fechaValida(filtros.fechaDesde ?? null);
    if (desde) params.set(CLAVES.desde, desde);

    const hasta = fechaValida(filtros.fechaHasta ?? null);
    if (hasta) params.set(CLAVES.hasta, hasta);

    // La primera pagina no se escribe: `?pagina=1` y nada son la misma vista, y
    // asi el enlace limpio de la primera pantalla se queda corto.
    if (pagina > 1) params.set(CLAVES.pagina, String(pagina));

    return params;
}

/** Cuantos filtros hay puestos. Es el numero del boton de limpiar. */
export function cuantosFiltros(filtros: FiltrosVista): number {
    let cuantos = 0;
    if (limpiarTexto(filtros.q)) cuantos += 1;
    if (filtros.resultado) cuantos += 1;
    if (filtros.lote !== null && filtros.lote !== undefined) cuantos += 1;
    if (limpiarTexto(filtros.nif)) cuantos += 1;
    if (filtros.fechaDesde) cuantos += 1;
    if (filtros.fechaHasta) cuantos += 1;
    return cuantos;
}
