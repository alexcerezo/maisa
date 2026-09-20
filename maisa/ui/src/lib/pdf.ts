/**
 * El motor de PDF del navegador, con `pdf.js`, y la busqueda del dato dentro del
 * texto de la pagina.
 *
 * Por que `pdf.js` y no el `<iframe>` que habia: un `<iframe>` pinta el PDF que
 * trae el navegador y no deja tocar nada de dentro. Para **resaltar** un dato hay
 * que saber donde esta, y eso solo se sabe si el PDF se ha interpretado aqui. El
 * `<iframe>` no se puede sustituir por CSS ni por un overlay: no hay forma de
 * preguntarle donde ha puesto el texto.
 *
 * Aqui vive la parte que no es React: normalizar, indexar el texto de una pagina
 * y buscar un token en el. La parte de pintar esta en `VisorPdf.tsx`.
 *
 * **Este fichero es un espejo de `api/app/anclajes.py`, no una copia libre.**
 * `normaliza` tiene que dar exactamente lo mismo que la de la API o el token que
 * manda la API no casa con el texto que indexa el navegador. Las dos quitan
 * acentos y todo lo que no sea `[0-9a-z]`, en ese orden, y hay una prueba que lo
 * comprueba con los 500 textos de la traza.
 */

import * as pdfjs from "pdfjs-dist";
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";

/*
 * El worker, obligatorio: sin el, `pdf.js` avisa de que va a interpretar el PDF
 * en el hilo de la interfaz y el panel se congela mientras se abre uno grande.
 *
 * La URL se importa con `?url` y no se escribe a mano porque Vite le pone un
 * hash al nombre del fichero al empaquetar. Con la ruta a pelo, en `dev`
 * funcionaria y en el `build` daria un 404 que solo se ve en produccion.
 */
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

export { pdfjs };

/** Los tipos del documento, reexportados para no importar `pdfjs` dos veces. */
export type DocumentoPdf = pdfjs.PDFDocumentProxy;
export type TareaDeCarga = pdfjs.PDFDocumentLoadingTask;
export type PaginaPdf = pdfjs.PDFPageProxy;
export type TareaDePintado = pdfjs.RenderTask;
export type CapaDeTexto = pdfjs.TextLayer;

/**
 * Reduce un texto a alfanumericos en minusculas, sin acentos ni separadores.
 *
 * Es la operacion que hace comparables el valor que leyo el motor y el texto
 * escrito en el papel. Se quitan los acentos porque el OCR los pierde con
 * frecuencia (`Factura` / `Fáctura`) y quedarse con ellos rompe la comparacion.
 *
 * Espejo exacto de `normaliza()` en `api/app/anclajes.py`. Ojo con el orden:
 * primero se descompone, luego se quitan los acentos, luego se baja a minusculas
 * y solo al final se tira lo que no sea alfanumerico.
 */
export function normaliza(texto: string): string {
    return texto
        .normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .replace(/[^0-9a-z]+/g, "");
}

/**
 * El texto de una pagina, todo seguido, con la vuelta al trozo del que salio.
 *
 * `pdf.js` entrega el texto troceado como quiere (`"Base imponible"`,
 * `"1.025,49"`, `" "`), y un dato puede caer a caballo de dos trozos: el NIF
 * `B98120774` sale partido en `"NIF:B"` + `"98120774"` mas veces de las que
 * parece. Buscar en cada trozo por separado perderia justo esos casos, asi que se
 * pega todo y se guarda, por cada caracter, de que trozo vino. Asi se puede
 * buscar en el texto entero y volver a los trozos que hay que rodear.
 */
export interface IndiceTexto {
    /** El texto de la pagina entera, normalizado. */
    plano: string;
    /** `deQuien[i]` = trozo del que salio el caracter `i` de `plano`. */
    deQuien: number[];
    /** Los trozos ya normalizados, en el orden en que los dio `pdf.js`. */
    trozos: string[];
}

export function indexaTexto(brutos: readonly string[]): IndiceTexto {
    const trozos = brutos.map(normaliza);
    let plano = "";
    const deQuien: number[] = [];

    trozos.forEach((trozo, indice) => {
        plano += trozo;
        // Un `push` por caracter es lo mas directo y aqui no duele: una pagina
        // tiene unos pocos miles de caracteres y esto se hace una vez por pagina.
        for (let i = 0; i < trozo.length; i++) deQuien.push(indice);
    });

    return { plano, deQuien, trozos };
}

/**
 * Donde aparece un token dentro de una pagina.
 *
 * `items` son los trozos de texto que hay que rodear y `conPista` dice si al lado
 * del dato aparece una de las palabras que suelen acompanarlo (`nif`, `cif`,
 * `pedido`). La pista no es decorativa: distingue el dato de una cifra que se le
 * parece, y es la misma distincion que hace la API al dar 0.95 o 0.7 de
 * confianza. Aqui se ensena, no se descarta: si un NIF sale dos veces, el que
 * lleva `NIF` al lado se marca fuerte y el otro queda como duda.
 */
export interface Coincidencia {
    items: number[];
    conPista: boolean;
}

/**
 * Cuantas veces se resalta el mismo dato. El mismo numero que `MAX_ANCLAS` en la
 * API: un NIF sale en la cabecera y en el pie, y a partir de ahi es ruido.
 */
export const MAX_COINCIDENCIAS = 4;

/**
 * Busca, por orden, el primero de los `tokens` que aparezca en la pagina.
 *
 * Se para en el primer token que casa **a proposito**: los tokens vienen
 * ordenados por probabilidad desde la API (para la fecha, primero el numerico
 * `08032026` y despues las formas largas `8demarzode2026`), asi que el primero
 * que aparece es el que se quiere. Seguir buscando los demas llenaria el
 * documento de cajas para el mismo dato.
 */
export function buscaEnPagina(
    indice: IndiceTexto,
    tokens: readonly string[],
    pistas: readonly string[],
    maximo: number = MAX_COINCIDENCIAS,
): Coincidencia[] {
    for (const token of tokens) {
        if (token.length < 1) continue;

        const encontradas: Coincidencia[] = [];
        let desde = 0;
        while (encontradas.length < maximo) {
            const posicion = indice.plano.indexOf(token, desde);
            if (posicion === -1) break;

            const items = new Set<number>();
            for (let i = posicion; i < posicion + token.length; i++) {
                items.add(indice.deQuien[i]);
            }
            const ordenados = [...items].sort((a, b) => a - b);
            encontradas.push({ items: ordenados, conPista: hayPista(indice, ordenados, pistas) });

            // Se avanza el token entero y no un caracter: dos apariciones del
            // mismo dato no se solapan, y avanzando de uno en uno un texto
            // periodico (`ES44ES44...`) daria cuatro cajas de lo mismo.
            desde = posicion + token.length;
        }

        if (encontradas.length > 0) return encontradas;
    }
    return [];
}

/**
 * Si el dato lleva al lado una de las palabras que lo suelen acompanar.
 *
 * Se mira el trozo donde esta el dato y sus dos vecinos, que es donde caen los
 * rotulos: en `"NIF:B98120774.IBAN:..."` la pista esta **en el mismo** trozo, y
 * en una tabla la etiqueta suele venir en el trozo de al lado. Tres trozos
 * cubren los dos casos sin llegar a arrastrar el texto de otra linea.
 */
function hayPista(
    indice: IndiceTexto,
    items: readonly number[],
    pistas: readonly string[],
): boolean {
    if (pistas.length === 0 || items.length === 0) return false;

    const desde = Math.max(0, items[0] - 1);
    const hasta = Math.min(indice.trozos.length - 1, items[items.length - 1] + 1);

    let contexto = "";
    for (let i = desde; i <= hasta; i++) contexto += indice.trozos[i];

    return pistas.some((pista) => pista.length > 0 && contexto.includes(pista));
}

/**
 * Abre un PDF que ya esta en memoria.
 *
 * Devuelve la **tarea de carga** y no el documento porque lo que hay que soltar
 * cuando el visor se desmonta es la tarea: `PDFDocumentProxy` no tiene
 * `destroy()`, y soltar el documento sin cerrar el worker dejaria un hilo vivo
 * por cada factura que se ha mirado. Quien la llame espera `tarea.promise`.
 */
export function abrirPdf(datos: ArrayBuffer): TareaDeCarga {
    return pdfjs.getDocument({ data: datos });
}
