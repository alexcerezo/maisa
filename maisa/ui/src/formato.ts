/**
 * Como se escribe un numero o una fecha en pantalla.
 *
 * Existe por un motivo concreto y no es estetico: en este panel **casi todo puede
 * ser nulo**. Ocho de las 500 facturas no tienen ni proveedor ni importe (son
 * hojas sueltas que no casan con ningun pedido), y `desvio_importe` es nulo en
 * cuanto falte cualquiera de los dos lados de la resta. Un `valor ?? 0` en la
 * tabla pintaria "0,00 €" donde no hay dato, y en un panel que decide pagos eso
 * no es un detalle de formato: es una cifra inventada con el mismo aspecto que
 * una de verdad.
 *
 * Asi que la regla es una sola, y se cumple en todos los ayudantes de aqui: **lo
 * que no hay se escribe "—", nunca cero**.
 */

const EUROS = new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
});

const PORCENTAJE = new Intl.NumberFormat("es-ES", {
    style: "percent",
    maximumFractionDigits: 0,
});

/** Un numero que puede faltar: ni `null`, ni `NaN` ni infinito son un importe. */
function hayNumero(valor: number | null | undefined): valor is number {
    return valor !== null && valor !== undefined && Number.isFinite(valor);
}

/** Importe en euros. `null` no es 0 €, es "—". */
export function euros(valor: number | null | undefined): string {
    return hayNumero(valor) ? EUROS.format(valor) : "—";
}

/**
 * Importe con signo, para el desvio contra el ERP.
 *
 * El cero se escribe sin signo a proposito: un "+0,00 €" sugiere que la factura
 * se desvia hacia arriba, y lo que significa es que no se desvia. Se usa el signo
 * menos tipografico (−, U+2212) y no el guion, que al lado de una cifra se lee
 * como un guion de texto.
 */
export function eurosConSigno(valor: number | null | undefined): string {
    if (!hayNumero(valor)) return "—";
    const base = EUROS.format(Math.abs(valor));
    if (valor > 0) return `+${base}`;
    if (valor < 0) return `\u2212${base}`;
    return base;
}

/** 0..1 a porcentaje sin decimales: `0.94` -> "94 %". */
export function porcentaje(valor: number | null | undefined): string {
    return hayNumero(valor) ? PORCENTAJE.format(valor) : "—";
}

/**
 * Segundos de lectura, con dos decimales.
 *
 * La coma decimal se pone a mano (`toFixed` da un punto) porque estas cifras
 * salen al lado de importes en euros y mezclar "1.23 s" con "301,00 €" en la
 * misma columna canta.
 */
export function segundos(valor: number | null | undefined): string {
    return hayNumero(valor) ? `${valor.toFixed(2).replace(".", ",")} s` : "—";
}

/**
 * `YYYY-MM-DD` -> `DD/MM/YYYY`.
 *
 * Se parte la cadena a mano en vez de usar `new Date(iso)`, y no es mania:
 * `new Date("2026-01-01")` interpreta la fecha como **UTC**, asi que al pintarla
 * en hora local se convierte en el 31/12/2025 en cualquier huso al oeste de
 * Greenwich. En una factura, un dia de mas o de menos es un dato falso.
 */
export function fecha(iso: string | null | undefined): string {
    if (!iso) return "—";
    const partes = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    // Si viene en otro formato se devuelve tal cual: mejor un dato raro a la vista
    // que uno inventado por nosotros.
    if (!partes) return iso;
    return `${partes[3]}/${partes[2]}/${partes[1]}`;
}

/**
 * Marca de tiempo completa, como la del manifiesto (`2026-09-20T01:05:33+00:00`).
 *
 * Aqui si se convierte a la hora del navegador, porque es una marca de tiempo y no
 * una fecha de factura: el dia no cambia de significado. Pero se le pone el huso a
 * la vista, y no es adorno: el manifiesto dice `01:05+00:00` y en Madrid eso es las
 * 03:05, asi que sin el "GMT+2" la misma foto parecia tener dos horas distintas
 * segun donde se mirase. Quien quiera cotejarlo con la traza lo necesita.
 */
export function fechaHora(iso: string | null | undefined): string {
    if (!iso) return "—";
    const cuando = new Date(iso);
    if (Number.isNaN(cuando.getTime())) return iso;
    return cuando.toLocaleString("es-ES", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
    });
}

/** Un texto que puede faltar. `null` y `""` se ven igual, porque significan lo mismo. */
export function texto(valor: string | null | undefined): string {
    const limpio = (valor ?? "").trim();
    return limpio === "" ? "—" : limpio;
}

/** Hasta 12 caracteres de una huella: bastan para comparar dos a ojo. */
export function huellaCorta(sha: string | null | undefined): string {
    if (!sha) return "—";
    return sha.length > 12 ? `${sha.slice(0, 12)}…` : sha;
}

/**
 * Un valor cualquiera de `campos` o de `hecho.datos`, convertido a texto legible.
 *
 * Los dos vienen sin esquema fijo (entre 5 y 26 claves segun la factura, y
 * `datos` cambia de forma en cada regla), asi que hay que aceptar lo que llegue:
 * cadenas, numeros, booleanos, listas y objetos. Un `String(valor)` a secas
 * pintaria `[object Object]`, que en un detalle forense es una perdida de
 * informacion.
 */
export function valorLegible(valor: unknown): string {
    if (valor === null || valor === undefined) return "—";
    if (typeof valor === "string") return valor.trim() === "" ? "—" : valor;
    if (typeof valor === "number") return Number.isFinite(valor) ? String(valor) : "—";
    if (typeof valor === "boolean") return valor ? "si" : "no";
    if (Array.isArray(valor)) {
        return valor.length === 0 ? "—" : valor.map(valorLegible).join(" · ");
    }
    if (typeof valor === "object") {
        return Object.entries(valor as Record<string, unknown>)
            .map(([clave, dato]) => `${clave}: ${valorLegible(dato)}`)
            .join(" · ");
    }
    return String(valor);
}

/** Un booleano como distintivo de texto corto, para los flags de un `Hecho`. */
export function siNo(valor: boolean): string {
    return valor ? "si" : "no";
}
