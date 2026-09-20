/**
 * Los filtros, en un solo sitio y con dos usos: construir la consulta para la
 * API y aplicarlos en local cuando no hay API.
 *
 * Este fichero es un **espejo de `maisa/api/app/traza.py`**, y esa es su razon
 * de ser. El panel puede leer los datos de dos sitios (la API viva o el
 * congelado de `public/data/`) y la misma consulta tiene que dar exactamente el
 * mismo resultado en los dos. Si la fuente de datos cambiara lo que devuelve una
 * busqueda, el panel ensenaria una tabla distinta segun el dia, y eso en un panel
 * de conciliacion es peor que un fallo: es una mentira que nadie ve.
 *
 * De `traza.py` se copian **tres** decisiones, no una:
 *
 * 1. `buscar()` compara `resultado` y `lote` por igualdad, y `proveedor` y `q`
 *    como subcadena literal sin distinguir mayusculas. Nada de "empieza por" ni
 *    de ordenar por relevancia.
 *
 * 2. `compilar_busqueda()` recorta el texto a `MAX_QUERY_LEN` (64) y usa
 *    `re.escape`, o sea que el texto es **literal**: buscar `(a+)+` busca esos
 *    caracteres, no una expresion regular. Aqui se hace con `includes`, que es
 *    literal por naturaleza. El tope de 64 hay que aplicarlo en el cliente
 *    porque los parametros `q` y `proveedor` tienen `max_length=64` en la API:
 *    pasar de ahi no da cero resultados, da un **422**.
 *
 * 3. `q` busca solo en `file_id`, `proveedor`, `pedido` y `asiento`
 *    (`CAMPOS_BUSQUEDA`). **No busca en el NIF**, y eso sorprende: por eso el
 *    filtro por NIF existe aparte y se aplica siempre en local.
 *
 * Ademas, `buscar()` **ordena por `file_id` antes de paginar**, asi que el
 * congelado tiene que ordenar igual o la paginacion no cuadraria. Se ordena por
 * comparacion de cadenas: en Python por puntos de codigo y en JavaScript por
 * unidades UTF-16. Para los `file_id` reales (ASCII y acentos del BMP) coinciden.
 *
 * Si se toca `traza.py`, hay que tocar esto. La comprobacion de que los dos
 * lados dicen lo mismo esta hecha sobre las 500 facturas: para cada filtro, el
 * conjunto de `file_id` que devuelve la API y el que devuelve esto tienen que
 * ser identicos.
 */

import { estadoCola, type EstadoCola, type FacturaResumen, type Resultado } from "./types";

/**
 * Tope de caracteres de una busqueda. Espejo de `MAX_QUERY_LEN` en
 * `maisa/api/app/config.py` y del `max_length=64` de los parametros.
 *
 * Recortar aqui no es lo mismo que dejar que la API recorte: la API **rechaza**
 * con 422 lo que pase de 64, no lo trunca. Truncando en el cliente, escribir de
 * mas da resultados en vez de un error.
 */
export const LIMITE_TEXTO = 64;

/** Espejo de `CAMPOS_BUSQUEDA` en `traza.py`. El NIF no esta, a proposito. */
export const CAMPOS_BUSQUEDA = ["file_id", "proveedor", "pedido", "asiento"] as const;

/** Los filtros que la API entiende, tal cual aparecen en la documentacion. */
export interface FiltrosApi {
    resultado?: Resultado | null;
    lote?: number | null;
    proveedor?: string | null;
    q?: string | null;
}

/**
 * Los filtros que ofrece la pantalla. Son los de la API **mas** los que la API no
 * sabe hacer, que se aplican siempre en el navegador.
 *
 * Se declaran aqui, en la capa de datos, y no en un componente: si estuvieran en
 * la tabla, el congelado no podria aplicarlos y volveriamos a tener dos paneles
 * distintos segun la fuente.
 */
export interface FiltrosVista extends FiltrosApi {
    /** `YYYY-MM-DD`. Comparacion por texto, que para ISO es orden cronologico. */
    fechaDesde?: string | null;
    fechaHasta?: string | null;
    /** Subcadena del NIF. El motor lo saca del maestro o del asiento. */
    nif?: string | null;
    /**
     * En que ha quedado la segunda lectura.
     *
     * Es un filtro de **vista** y no de API: `buscar()` en `traza.py` filtra por
     * `resultado`, `lote`, `proveedor` y `q`, y no sabe nada de la cola. Pedirlo
     * al servidor devolveria el listado entero sin filtrar y el panel enseñaria
     * 500 filas donde dice que hay 4. Se aplica siempre en local, como las fechas.
     *
     * El valor es un `EstadoCola` y no un booleano porque las tres respuestas
     * ("se cierra", "hay desvio", "no concluye") son tres trabajos distintos, y
     * "hay segunda lectura" a secas no dice cual toca.
     */
    segundaLectura?: EstadoCola | null;
}

/**
 * Limpia un texto de busqueda: recorta espacios, descarta lo vacio y trunca al
 * tope. Devolver `null` (y no cadena vacia) es lo que permite distinguir "no hay
 * filtro" de "filtro que no casa con nada", que en la pantalla se ven distinto.
 */
export function limpiarTexto(valor: string | null | undefined): string | null {
    const limpio = (valor ?? "").trim();
    if (!limpio) return null;
    return limpio.slice(0, LIMITE_TEXTO);
}

/**
 * Subcadena literal sin distinguir mayusculas, como `re.escape(...) + IGNORECASE`.
 *
 * El texto a buscar llega en crudo y se pasa a minusculas **aqui dentro**: asi no
 * hay forma de olvidarse en una llamada y que el filtro deje de casar sin que
 * nadie se entere (con 500 filas, un filtro roto se ve como "no hay resultados").
 */
function contiene(valor: string | null | undefined, aguja: string): boolean {
    return (valor ?? "").toLowerCase().includes(aguja.toLowerCase());
}

/**
 * Los parametros de consulta de los filtros que la API entiende.
 *
 * Los vacios **no se envian**: un `q=` vacio llegaria a `texto_busqueda` y se
 * convertiria en "sin filtro", que es lo mismo, pero ensucia el ETag y hace que
 * dos consultas iguales no compartan cache.
 */
export function parametrosDeConsulta(filtros: FiltrosApi): URLSearchParams {
    const parametros = new URLSearchParams();
    if (filtros.resultado) parametros.set("resultado", filtros.resultado);
    if (filtros.lote !== null && filtros.lote !== undefined) {
        parametros.set("lote", String(filtros.lote));
    }
    const proveedor = limpiarTexto(filtros.proveedor);
    if (proveedor) parametros.set("proveedor", proveedor);
    const q = limpiarTexto(filtros.q);
    if (q) parametros.set("q", q);
    return parametros;
}

/**
 * Los filtros de la API aplicados en local. Es el camino del **congelado**.
 *
 * Reproduce `TrazaStore.buscar()` entero: los cuatro filtros, el orden y nada de
 * paginacion (el congelado es el listado completo y quien pinta pagina despues).
 */
export function aplicarFiltrosDeApi(
    items: readonly FacturaResumen[],
    filtros: FiltrosApi,
): FacturaResumen[] {
    const proveedor = limpiarTexto(filtros.proveedor);
    const q = limpiarTexto(filtros.q);

    const coincidencias = items.filter((item) => {
        if (filtros.resultado && item.resultado !== filtros.resultado) return false;
        if (filtros.lote !== null && filtros.lote !== undefined && item.lote !== filtros.lote) {
            return false;
        }
        if (proveedor && !contiene(item.proveedor, proveedor)) return false;
        if (q) {
            // La lista de campos sale de la constante de arriba a proposito: si
            // manana la API anade un campo a `CAMPOS_BUSQUEDA`, el espejo cambia
            // solo y no puede quedarse atras en silencio.
            if (!CAMPOS_BUSQUEDA.some((campo) => contiene(item[campo], q))) return false;
        }
        return true;
    });

    // Se ordena **despues** de filtrar y **antes** de paginar, como `buscar()`.
    // El listado de la API ya viene ordenado, asi que en modo vivo esto no cambia
    // nada; en modo congelado es lo que hace que la paginacion cuadre.
    return [...coincidencias].sort((a, b) => (a.file_id < b.file_id ? -1 : a.file_id > b.file_id ? 1 : 0));
}

/**
 * Los filtros que la API no sabe hacer. Se aplican **siempre** en local, tambien
 * en modo vivo, sobre lo que ya devolvio el servidor.
 *
 * Dos avisos que importan en pantalla:
 *
 * - Ocho de las 500 facturas no tienen fecha (documentos sin pedido que no casan
 *   con nada). Al poner un filtro de fechas **desaparecen**, y eso es correcto
 *   pero parece perdida de datos. La pantalla deberia poder decirlo.
 * - `fechaDesde`/`fechaHasta` se comparan como texto. Con `YYYY-MM-DD` el orden
 *   alfabetico es el cronologico, asi que no hace falta convertir a `Date` ni
 *   arriesgarse a un desfase de zona horaria.
 */
export function aplicarFiltrosExtra(
    items: readonly FacturaResumen[],
    filtros: FiltrosVista,
): FacturaResumen[] {
    const desde = (filtros.fechaDesde ?? "").trim();
    const hasta = (filtros.fechaHasta ?? "").trim();
    const nif = limpiarTexto(filtros.nif);
    const cola = filtros.segundaLectura ?? null;
    if (!desde && !hasta && !nif && !cola) return [...items];

    return items.filter((item) => {
        if (desde || hasta) {
            if (!item.fecha) return false;
            if (desde && item.fecha < desde) return false;
            if (hasta && item.fecha > hasta) return false;
        }
        if (nif && !contiene(item.nif, nif)) return false;
        // `estadoCola()` devuelve `null` cuando nadie la ha releido, y `null` no es
        // un cuarto estado: dice "no hay segunda lectura", que es distinto de "la
        // relectura no concluyo". Por eso el filtro compara contra `EstadoCola` y no
        // ofrece una opcion "sin segunda lectura": pedir eso no es una pregunta sobre
        // trabajo pendiente, es mirar el listado entero.
        if (cola && estadoCola(item.segunda_lectura) !== cola) return false;
        return true;
    });
}
