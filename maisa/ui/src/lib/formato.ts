/**
 * Como se enseña un dato. Un solo sitio para las reglas que se repiten en toda
 * la pantalla, porque si cada componente formatea a su manera acabas con el
 * mismo importe escrito de dos formas distintas en la tabla y en el detalle.
 *
 * Tres decisiones que estan aqui y no en los componentes:
 *
 * 1. **Un nulo no es un cero.** En el corpus 8 de las 500 facturas no tienen ni
 *    proveedor ni importe, y 8 no tienen fecha. Pintar `0,00 €` donde no hay dato
 *    es peor que un hueco: en un panel que decide pagos es una cifra inventada
 *    que alguien puede dar por buena. Por eso todas las funciones aceptan `null`
 *    y devuelven una raya.
 *
 * 2. **Las fechas no pasan por `Date`.** `new Date("2026-01-08")` se interpreta
 *    como medianoche **UTC**, y al formatearla en un huso negativo se enseña el
 *    dia 7. Como las fechas del motor ya vienen en `YYYY-MM-DD`, se parten por el
 *    guion: no hay conversion, luego no hay desfase posible. Es la misma razon
 *    por la que `filtros.ts` compara fechas como texto.
 *
 * 3. **El desvio lleva signo explicito.** `+12,30 €` y `-12,30 €` se leen de un
 *    golpe; `12,30 €` obliga a mirar de que columna es.
 */

/** La raya con la que se pinta cualquier dato que no esta. */
export const SIN_DATO = "—";

const EUROS = new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
});

const ENTERO = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 });

const MOMENTO = new Intl.DateTimeFormat("es-ES", { dateStyle: "short", timeStyle: "short" });

/** `1234.5` -> `1.234,50 €`. Un nulo es una raya, nunca `0,00 €`. */
export function euros(valor: number | null | undefined): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    return EUROS.format(valor);
}

/**
 * El desvio con el signo delante, incluido el `+`.
 *
 * `Intl` no pone el `+` en los positivos, y sin el, una columna de desvios se
 * lee como una columna de importes. El cero se deja sin signo: `0,00 €` ya dice
 * que no hay desvio.
 */
export function eurosConSigno(valor: number | null | undefined): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    if (valor === 0) return EUROS.format(0);
    return `${valor > 0 ? "+" : "−"}${EUROS.format(Math.abs(valor))}`;
}

/** `2026-01-08` -> `08/01/2026`. Sin `Date`: ver la cabecera del fichero. */
export function fecha(iso: string | null | undefined): string {
    if (!iso) return SIN_DATO;
    const partes = iso.split("-");
    if (partes.length !== 3) return iso;
    const [ano, mes, dia] = partes;
    return `${dia}/${mes}/${ano}`;
}

/**
 * `2026-09-20T01:05:33+00:00` -> `20/09/2026, 01:05`.
 *
 * Aqui si se usa `Date`, al contrario que en `fecha()`, y no es una
 * contradiccion: esto es un **instante** con su huso explicito (el momento en
 * que se congelaron los datos), y enseñarlo en la hora del que mira es lo
 * correcto. El problema de `fecha()` es distinto: alli la cadena es un dia
 * suelto sin hora, y pasarlo por `Date` lo convierte en medianoche UTC, que en
 * un huso negativo es el dia anterior.
 */
export function fechaHora(iso: string | null | undefined): string {
    if (!iso) return SIN_DATO;
    const momento = new Date(iso);
    if (Number.isNaN(momento.getTime())) return iso;
    return MOMENTO.format(momento);
}

/** `0.983` -> `98 %`. La calidad de lectura se lee mejor como porcentaje. */
export function porcentaje(valor: number | null | undefined): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    return `${Math.round(valor * 100)} %`;
}

/** Miles con separador de es-ES. Para contadores de cabecera. */
export function entero(valor: number | null | undefined): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    return ENTERO.format(valor);
}

/** Un texto vacio o ausente es una raya, igual que un nulo numerico. */
export function texto(valor: string | null | undefined): string {
    const limpio = (valor ?? "").trim();
    return limpio || SIN_DATO;
}

/**
 * El primer trozo de una huella. Un `sha256` entero no cabe en una fila y los
 * 12 primeros caracteres ya identifican el fichero de un vistazo.
 */
export function huellaCorta(sha256: string | null | undefined): string {
    if (!sha256) return SIN_DATO;
    return sha256.slice(0, 12);
}

/**
 * Cualquier valor de `campos` como texto.
 *
 * `campos` no tiene esquema fijo: trae entre 5 y 26 claves por factura y los
 * valores pueden ser cadena, numero, booleano, lista de cadenas o `null`. Esto
 * lo reduce a algo que se pueda pintar sin inventarse el tipo.
 */
export function valorLegible(valor: unknown): string {
    if (valor === null || valor === undefined) return SIN_DATO;
    if (typeof valor === "boolean") return valor ? "sí" : "no";
    if (typeof valor === "number") return String(valor);
    if (typeof valor === "string") return valor.trim() || SIN_DATO;
    if (Array.isArray(valor)) {
        const trozos = valor.map((elemento) => valorLegible(elemento)).filter((t) => t !== SIN_DATO);
        return trozos.length ? trozos.join(" · ") : SIN_DATO;
    }
    // Un objeto anidado se enseña tal cual en vez de `[object Object]`, que no
    // dice nada: hoy no hay ninguno, pero `campos` es abierto a proposito.
    return JSON.stringify(valor);
}

/**
 * `campos.<clave>` a numero, o `null`.
 *
 * Hace falta porque los importes de `campos` son **texto** (`"3012.89"`) y los
 * del resumen son `number`: es el mismo dato con dos representaciones (ver la
 * cabecera de `types.ts`). Sin esto, sumar o comparar un importe de `campos`
 * concatenaria cadenas.
 */
export function numeroDeTexto(valor: unknown): number | null {
    if (typeof valor === "number") return Number.isFinite(valor) ? valor : null;
    if (typeof valor !== "string") return null;
    const limpio = valor.trim().replace(",", ".");
    if (!limpio) return null;
    const numero = Number(limpio);
    return Number.isFinite(numero) ? numero : null;
}

/** `PAGAR` -> `Pagar`. Para textos de interfaz, no para claves. */
export function humano(valor: string | null | undefined): string {
    if (!valor) return SIN_DATO;
    const conEspacios = valor.replace(/_/g, " ").toLowerCase();
    return conEspacios.charAt(0).toUpperCase() + conEspacios.slice(1);
}
