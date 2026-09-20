/**
 * Las cuentas de la paginacion, sin React.
 *
 * La tabla se pagina **en el cliente** y no contra la API, y eso es una decision
 * y no una comodidad: `cargarFacturas` ya trae todo lo que casa con el filtro (la
 * API se pagina sola por dentro hasta agotar el freno de seguridad). Paginar
 * contra el servidor obligaria a que el total de la API y el filtrado local de
 * fechas y NIF cuadraran, y no cuadran por construccion: `total` es lo que diria
 * la API, que no sabe de fechas ni de NIF.
 *
 * Con las 500 facturas en memoria, paginar aqui es instantaneo y no puede
 * discrepar. Si el dia de manana fueran 500.000, este fichero es lo primero que
 * habria que cambiar.
 */

/** Filas por pagina. 25 entra entero en una pantalla de portatil sin scroll. */
export const TAMANO_TABLA = 25;

/**
 * Los numeros de pagina que se pintan, con huecos.
 *
 * La regla de siempre: la primera y la ultima siempre, la actual con sus
 * vecinas, y un hueco donde falta un tramo. Sin esto, con 20 paginas habria 20
 * botones y con 2000 no cabria ninguno.
 */
export function paginasVisibles(
    actual: number,
    total: number,
    vecinas: number = 1,
): (number | "hueco")[] {
    if (total <= 0) return [];
    if (total === 1) return [1];

    // Con pocas paginas no se gana nada abreviando: se ensenan todas.
    const sinHuecos = vecinas * 2 + 5;
    if (total <= sinHuecos) {
        return Array.from({ length: total }, (_, indice) => indice + 1);
    }

    const primera = Math.max(2, actual - vecinas);
    const ultima = Math.min(total - 1, actual + vecinas);

    const paginas: (number | "hueco")[] = [1];
    if (primera > 2) paginas.push("hueco");
    for (let numero = primera; numero <= ultima; numero += 1) paginas.push(numero);
    if (ultima < total - 1) paginas.push("hueco");
    paginas.push(total);

    return paginas;
}

export interface Tramo {
    /** Indice de la primera fila, 1-based. 0 si el tramo esta vacio. */
    desde: number;
    /** Indice de la ultima fila, 1-based. 0 si el tramo esta vacio. */
    hasta: number;
}

/**
 * Que filas se ven en una pagina, en numeros 1-based **para ensenar**.
 *
 * Se calcula aqui y no con `slice` en la tabla porque el texto de abajo ("filas
 * 26 a 50") tiene que decir exactamente lo mismo que lo que se pinta. Sacar los
 * dos numeros de sitios distintos es como se acaba ensenando "26 a 50" con 24
 * filas en pantalla.
 */
export function tramoDePagina(pagina: number, total: number, tamano: number = TAMANO_TABLA): Tramo {
    if (total <= 0) return { desde: 0, hasta: 0 };
    const desde = (pagina - 1) * tamano + 1;
    const hasta = Math.min(pagina * tamano, total);
    return { desde, hasta };
}

/** Cuantas paginas hay. Siempre al menos 1, aunque no haya filas. */
export function totalDePaginas(total: number, tamano: number = TAMANO_TABLA): number {
    return Math.max(1, Math.ceil(total / tamano));
}
