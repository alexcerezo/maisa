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
 * Los formateadores por divisa, memoizados.
 *
 * `Intl.NumberFormat` es caro de construir y la tabla lo pediria una vez por fila.
 * La clave es el codigo ya normalizado, asi que el mapa no puede crecer sin
 * limite: las divisas que declara el corpus son cuatro.
 */
const FORMATOS_DIVISA = new Map<string, Intl.NumberFormat | null>();

function formatoDeDivisa(codigo: string): Intl.NumberFormat | null {
    const cacheado = FORMATOS_DIVISA.get(codigo);
    if (cacheado !== undefined) return cacheado;
    let formato: Intl.NumberFormat | null;
    try {
        formato = new Intl.NumberFormat("es-ES", { style: "currency", currency: codigo });
    } catch {
        // Un codigo que `Intl` no reconoce no puede tumbar el panel: se cae al
        // formato en euros con el codigo detras, que se lee igual de bien.
        formato = null;
    }
    FORMATOS_DIVISA.set(codigo, formato);
    return formato;
}

/**
 * `1234.5` en la divisa que se le diga: `1.234,50 US$`.
 *
 * `euros()` fija `EUR` y por eso no sirve para las cuatro facturas del lote 2 que
 * vienen en USD, JPY o GBP: ensenar `1.560,00 €` un importe impreso en dolares es
 * la misma clase de error que pintar `0,00 €` donde no hay dato. Un `null` sigue
 * siendo una raya, y sin divisa se asume euros, que es lo que hace el motor
 * cuando el documento no declara ninguna.
 */
export function importeEn(
    valor: number | null | undefined,
    divisa: string | null | undefined,
): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    const codigo = divisa?.trim().toUpperCase();
    if (!codigo) return EUROS.format(valor);
    const formato = formatoDeDivisa(codigo);
    return formato ? formato.format(valor) : `${EUROS.format(valor)} ${codigo}`;
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

/**
 * Un numero con decimales fijos y coma de es-ES. `3.69` -> `3,69`.
 *
 * Existe para las cifras de tiempo del banco de medidas, que no son importes ni
 * contadores: `entero()` las redondearia a `4` y perderia justo lo que se esta
 * midiendo (3,69 s contra 3,78 s es la diferencia entre dos configuraciones).
 */
export function decimal(valor: number | null | undefined, decimales: number = 2): string {
    if (valor === null || valor === undefined || !Number.isFinite(valor)) return SIN_DATO;
    return new Intl.NumberFormat("es-ES", {
        minimumFractionDigits: decimales,
        maximumFractionDigits: decimales,
    }).format(valor);
}

/**
 * Una duracion en segundos, ya legible.
 *
 * Los escenarios de la extrapolacion llegan a 3 964 865 s, y eso en segundos no
 * se lee: hay que convertirlo mentalmente a 46 dias. Por eso a partir de una hora
 * se parte en horas y minutos, y de ahi para abajo se dan segundos con dos
 * decimales (que es la precision con la que se midio).
 *
 * `0` se deja como `0 s` y no como `0 h 0 min`: el cero es un dato, no un hueco, y
 * aqui significa "no cuesta nada".
 */
export function duracion(valorSegundos: number | null | undefined): string {
    if (valorSegundos === null || valorSegundos === undefined || !Number.isFinite(valorSegundos)) {
        return SIN_DATO;
    }
    if (valorSegundos >= 3600) {
        const horas = Math.floor(valorSegundos / 3600);
        const minutos = Math.round((valorSegundos % 3600) / 60);
        // Redondear los minutos puede dar 60: se sube a la hora siguiente en vez
        // de enseñar "1 h 60 min".
        if (minutos === 60) return `${horas + 1} h`;
        return minutos === 0 ? `${horas} h` : `${horas} h ${minutos} min`;
    }
    return `${decimal(valorSegundos, valorSegundos < 10 ? 2 : 0)} s`;
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

const SEGUNDO = new Intl.NumberFormat("es-ES", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
});

/**
 * Cuanto tardo el motor en leer el documento. `0.0435` -> `43 ms`.
 *
 * En segundos con un decimal, mas de la mitad del corpus salia `0,0 s`: 302 de
 * las 540 facturas. Eso no es un dato, parece que no se midio, y la latencia es
 * una de las cosas que la rubrica pide enseñar expresamente. La mediana esta en
 * 43 ms, asi que por debajo del segundo se dan milisegundos enteros y de ahi
 * para arriba segundos con un decimal. El corte no es cosmetico: sin el, la
 * cifra que distingue una lectura de texto (4 ms) de una de OCR (299 ms)
 * desaparecia.
 *
 * El nulo sigue siendo una raya. `0 ms` no se llega a pintar: una lectura
 * siempre cuesta algo, asi que el redondeo que daria cero se dice `<1 ms`.
 */
export function latencia(segundos: number | null | undefined): string {
    if (segundos === null || segundos === undefined || !Number.isFinite(segundos)) {
        return SIN_DATO;
    }
    if (segundos >= 1) return `${SEGUNDO.format(segundos)} s`;
    const milisegundos = Math.round(segundos * 1000);
    return milisegundos === 0 ? "<1 ms" : `${ENTERO.format(milisegundos)} ms`;
}
