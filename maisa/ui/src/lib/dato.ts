/**
 * Como se pinta un par clave/valor de un registro abierto.
 *
 * Hace falta porque hay **dos** registros abiertos en el contrato (`Hecho.datos`
 * y `CamposFactura`) y los dos tienen el mismo problema: la clave es un
 * identificador de programa (`importe_erp`, `desvio_importe`) y el valor puede
 * ser numero, texto, lista o `null`. Si cada componente se lo resolviera por su
 * cuenta, el mismo `desvio_importe` saldria `0.00` en un sitio y `0,00 €` en
 * otro, y en un panel de conciliacion eso es la diferencia entre dos datos
 * distintos.
 *
 * El criterio de que clave es dinero y cual porcentaje es **por nombre**, porque
 * no hay esquema que consultar: `campos` trae entre 5 y 26 claves segun la
 * factura. Es una heuristica, y por eso esta escrita en un solo sitio y con la
 * lista a la vista: el dia que aparezca una clave nueva que sea dinero, se anade
 * aqui y aparece formateada en las dos pantallas a la vez.
 */

import { euros, porcentaje, valorLegible } from "./formato";

/** Claves cuyo valor son euros, en `campos` y en `hechos.datos`. */
const CLAVES_DINERO = [
    "total",
    "base",
    "importe",
    "importe_erp",
    "desvio",
    "desvio_importe",
    "iva",
    "saldo",
    "cuantia",
];

/** Claves cuyo valor es un tanto por uno (0..1). */
const CLAVES_TANTO = ["calidad", "calidad_lectura", "confianza", "umbral", "similitud"];

function esDeClave(clave: string, lista: readonly string[]): boolean {
    const limpia = clave.toLowerCase();
    return lista.some((candidata) => limpia === candidata || limpia.endsWith(`_${candidata}`));
}

/**
 * Un valor que **ya** viene en tantos por ciento: `"21"` -> `21 %`.
 *
 * Es una funcion distinta de `porcentaje()` y no una con un parametro porque son
 * **dos unidades distintas**, y confundirlas ya dio un numero falso en pantalla:
 * `campos.iva_pct` viene como `"21"` (por ciento) mientras que `calidad_lectura`
 * viene como `0.983` (tanto por uno). Al pasar `"21"` por `porcentaje()`, que
 * multiplica por cien, el expediente enseñaba `IVA pct 2100 %`. En un panel que
 * decide pagos, un porcentaje de IVA inventado no es un detalle de estilo.
 */
function porCiento(valor: number): string {
    return `${Math.round(valor)} %`;
}

/**
 * El valor, ya formateado segun lo que la clave dice que es.
 *
 * El orden importa, y por dos motivos distintos:
 *
 * 1. `iva_pct` es un porcentaje y `iva` son euros, asi que hay que descartar el
 *    porcentaje **antes** de mirar la lista de dinero, o `iva_pct` saldria en
 *    euros.
 * 2. El sufijo `_pct` y las claves de `CLAVES_TANTO` **no son la misma unidad**
 *    (por ciento contra tanto por uno), asi que se comprueban por separado y con
 *    formateadores distintos. Ver `porCiento()`.
 */
export function valorDeDato(clave: string, valor: unknown): string {
    if (valor === null || valor === undefined || valor === "") return valorLegible(valor);

    const limpia = clave.toLowerCase();

    if (limpia.endsWith("_pct")) {
        const convertido = numero(valor);
        if (convertido !== null) return porCiento(convertido);
    }

    if (esDeClave(limpia, CLAVES_TANTO)) {
        const convertido = numero(valor);
        if (convertido !== null) return porcentaje(convertido);
    }

    if (esDeClave(limpia, CLAVES_DINERO)) {
        const convertido = numero(valor);
        if (convertido !== null) return euros(convertido);
    }

    return valorLegible(valor);
}

/** El numero de un valor que puede venir como texto (`"3012.89"`) o como numero. */
function numero(valor: unknown): number | null {
    if (typeof valor === "number") return Number.isFinite(valor) ? valor : null;
    if (typeof valor !== "string") return null;
    const limpio = valor.trim().replace(",", ".");
    if (!limpio) return null;
    const convertido = Number(limpio);
    return Number.isFinite(convertido) ? convertido : null;
}

/**
 * La clave, en castellano y legible.
 *
 * `importe_erp` -> `Importe ERP`. Se conservan en mayusculas las siglas que se
 * usan asi en todo el proyecto (ERP, NIF, IBAN, IVA) porque escribirlas en
 * minusculas en un panel que habla de ellas todo el rato queda mal y confunde.
 */
const SIGLAS = new Set(["erp", "nif", "iban", "iva", "ocr", "pdf", "id", "sha", "po", "as"]);

/**
 * Las palabras que el generico no puede acentuar.
 *
 * `claveLegible` no sabe castellano: parte por guiones, pasa todo a minusculas y
 * pone mayuscula la primera. Con eso, `ordenes` sale "Ordenes" y `desvio` sale
 * "Desvio", que estan **mal escritos**, y no hay regla que lo arregle: la tilde
 * depende de la palabra, no de su posicion. Las claves son un conjunto cerrado
 * que sale del motor (44 en la traza congelada, contadas sobre los ficheros), asi
 * que se listan aqui las tres palabras que la llevan. El dia que el motor saque
 * una clave nueva con tilde aparecera sin ella, y se anade aqui.
 */
const ACENTOS: Record<string, string> = {
    ordenes: "órdenes",
    metodo: "método",
    desvio: "desvío",
};

export function claveLegible(clave: string): string {
    const palabras = clave.replace(/^_+/, "").split("_").filter(Boolean);
    if (palabras.length === 0) return clave;

    const texto = palabras
        .map((palabra) => {
            const minuscula = palabra.toLowerCase();
            if (SIGLAS.has(minuscula)) return palabra.toUpperCase();
            return ACENTOS[minuscula] ?? minuscula;
        })
        .join(" ");

    return texto.charAt(0).toUpperCase() + texto.slice(1);
}

/**
 * Si una clave merece ensenarse arriba o es ruido.
 *
 * `file_id` y `metodo_lectura` estan en `campos` pero ya se ven en la cabecera
 * del detalle: repetirlos en la tabla de la lectura cruda ocupa sitio y no anade
 * nada. Se descartan a proposito y no por accidente.
 */
const CLAVES_YA_VISIBLES = new Set(["file_id", "metodo_lectura"]);

export function claveRepetida(clave: string): boolean {
    return CLAVES_YA_VISIBLES.has(clave.toLowerCase());
}
