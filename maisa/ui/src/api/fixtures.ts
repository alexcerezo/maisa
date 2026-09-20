/**
 * Rutas del congelado de `public/data/` y el `slug` que las hace posibles.
 *
 * Esto es el plan B del panel. El plan A es la API viva; cuando
 * `/api/estadisticas` no contesta, la pantalla cambia de fuente y sigue
 * funcionando con estos ficheros, que son copias literales de la API hechas por
 * `tools/descargar_fixtures.py`. La pantalla no se queda en blanco y, sobre
 * todo, **dice que esta congelada** en vez de fingir que es en vivo.
 */

import type { Entrega } from "@/api/types";

/**
 * `file_id` -> nombre de fichero seguro en ASCII.
 *
 * Hace falta porque 65 de las 500 facturas traen acentos
 * (`FA-2116_mensajeria.pdf`, `FA-5590_ofimatica.pdf`). Un fichero en disco con
 * acentos no se puede pedir por URL sin que el servidor lo descodifique: pides
 * `.../FA-2116_mensajer%C3%ADa.pdf.json`, el servidor busca
 * `FA-2116_mensajería.pdf.json` en disco, no coincide con el nombre guardado y
 * responde 404. Escapando el nombre a ASCII se evita el problema de raiz.
 *
 * Todo lo que no sea `[A-Za-z0-9._-]` pasa a `~` + su code point en hexa a 4
 * digitos. Es reversible y no colisiona, porque `~` nunca sobrevive tal cual
 * (se escaparia a `~007e`).
 *
 * **Tiene que dar exactamente el mismo resultado que `slug()` en
 * `tools/descargar_fixtures.py`.** Si se toca una, hay que tocar la otra: el
 * fallo seria un 404 en 65 facturas y solo sin conexion, el peor sitio para
 * enterarse. Se usa el flag `u` para que un emoji (par subrogado) cuente como
 * un solo code point, igual que hace Python con `ord()`.
 */
export function slugFileId(fileId: string): string {
    return fileId.replace(
        /[^A-Za-z0-9._-]/gu,
        (caracter) => "~" + (caracter.codePointAt(0) ?? 0).toString(16).padStart(4, "0"),
    );
}

export const RUTA_ESTADISTICAS = "/data/estadisticas.json";
export const RUTA_FACTURAS = "/data/facturas.json";
export const RUTA_MANIFIESTO = "/data/manifiesto.json";
export const RUTA_SNAPSHOTS = "/data/snapshots.json";

/**
 * La salud de las dependencias y la version del servicio, congeladas.
 *
 * Son las dos unicas copias que **no** son una foto de los datos sino del
 * servicio, y por eso el panel las marca como tales: sin conexion, `latencia_ms`
 * es la que habia el dia del congelado y no la de ahora. Ensenarla sin decirlo
 * seria presentar una medida vieja como si fuera un latido.
 */
export const RUTA_SALUD = "/data/salud.json";
export const RUTA_META = "/data/meta.json";

/** El detalle de una factura, ya congelado. */
export function rutaDetalle(fileId: string): string {
    return `/data/facturas/${slugFileId(fileId)}.json`;
}

/**
 * El PDF congelado. Solo hay unos pocos (ver `PDFS` en el script): los de los
 * casos que se ensenan. Para el resto, sin conexion no hay PDF, y el detalle
 * tiene que decirlo en vez de dejar un `<iframe>` roto.
 *
 * Aqui se usa el slug **sin anadirle nada**, porque ya trae la extension del
 * original (`.` y los alfanumericos no se escapan): `scan_002.pdf` se guarda
 * como `pdfs/scan_002.pdf`. Anadirle un `.pdf` daria `scan_002.pdf.pdf`.
 */
export function rutaPdfCongelado(fileId: string): string {
    return `/data/pdfs/${slugFileId(fileId)}`;
}

/** Si merece la pena intentarlo. Evita un 404 garantizado por cada factura. */
export function hayPdfCongelado(fileId: string): boolean {
    return PDFS_CONGELADOS.has(fileId);
}

/**
 * Los PDF que el script descarga. Copiada de `PDFS` en
 * `tools/descargar_fixtures.py`; si se cambia alli, se cambia aqui. Se prefiere
 * esta lista duplicada a pedir el `manifiesto.json` solo para saberlo: el
 * manifiesto es informativo y esta lista es una decision de renderizado.
 */
export const PDFS_CONGELADOS: ReadonlySet<string> = new Set([
    "2026-01-08_P001.pdf",
    "2026-03-28_P002.pdf",
    "2026-04-08_P007.pdf",
    "2026-0233-A_catering.pdf",
    "2026-06-04_P006.pdf",
    "copia_2026_0518.pdf",
    "FA-2508_consultoría.pdf",
    "2026-07-09_P010.pdf",
    "scan_002.pdf",
    "e02_P002.pdf",
]);

/**
 * Lo que dice `public/data/manifiesto.json`: de cuando es el congelado y de
 * donde salio. El panel lo ensena cuando esta congelado, porque decir "estos
 * datos son del dia X" es la diferencia entre degradar y enganar.
 */
export interface Manifiesto {
    congelado_en: string;
    origen: string;
    facturas_en_el_listado: number;
    detalles_guardados: number;
    pdfs: string[];
    por_resultado: Record<string, number> | null;
    asientos_vigentes: number | null;
    entrega: Entrega | null;
    /** Cuantas descargas del ERP trae `snapshots.json`. `null` si no se pudo bajar. */
    snapshots: number | null;
    generado_por: string;
}
