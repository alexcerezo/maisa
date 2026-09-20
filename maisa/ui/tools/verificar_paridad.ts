/**
 * Comprueba que el congelado y la API viva ensenan lo mismo.
 *
 * Hay dos cosas distintas que pueden desviarse, y las dos rompen el plan B de
 * forma silenciosa:
 *
 * 1. **El congelado se queda viejo.** Si alguien regenera las facturas y no los
 *    fixtures, el panel ensenaria un corpus distinto segun el origen. Se comparan
 *    los registros campo a campo, no solo el numero.
 *
 * 2. **El filtro de `filtros.ts` deja de parecerse al de `traza.py`.** Este es el
 *    peligroso: son dos implementaciones del mismo criterio en dos lenguajes
 *    distintos. El dia que alguien anada un campo a `CAMPOS_BUSQUEDA` en Python y
 *    no aqui (o al reves), el modo vivo y el congelado devolverian conjuntos
 *    distintos para la misma busqueda, y como los dos "funcionan", nadie se
 *    enteraria. Para eso se le pregunta a la API por el mismo filtro y se comparan
 *    los `file_id`.
 *
 * Se ejecuta con Node directamente (v24 sabe leer TypeScript sin compilarlo):
 *
 *     node tools/verificar_paridad.ts
 *     MAISA_API=http://localhost:8010 node tools/verificar_paridad.ts
 *
 * Sale con codigo 1 si algo no cuadra, para poder colgarlo de una tarea.
 */

import { readFile } from "node:fs/promises";

import { aplicarFiltrosDeApi, parametrosDeConsulta, type FiltrosApi } from "../src/api/filtros.ts";
import { rutaDetalle } from "../src/api/fixtures.ts";
import type { FacturaResumen } from "../src/api/types.ts";

const AQUI = new URL(".", import.meta.url);

async function leerJson<T>(relativa: string): Promise<T> {
    return JSON.parse(await readFile(new URL(relativa, AQUI), "utf8")) as T;
}

/**
 * Lee un fichero del congelado a partir de su ruta de la web.
 *
 * Se reutiliza `rutaDetalle` en vez de componer el nombre a mano para que la
 * comprobacion use el mismo `slug` que la aplicacion: si el slug se rompe, esta
 * prueba se rompe con el, que es exactamente lo que se quiere.
 */
async function leerDelCongelado<T>(rutaWeb: string): Promise<T> {
    return leerJson<T>(`../public${rutaWeb}`);
}

interface PaginaApi {
    total: number;
    devueltas: number;
    items: FacturaResumen[];
}

/** El tope real de la API (`MAX_LIMIT`): pedir mas devuelve 500 igualmente. */
const TAMANO_PAGINA = 500;

/** Corte de seguridad: un servidor que ignore `offset` no acaba nunca. */
const MAX_VUELTAS = 100;

const manifiesto = await leerJson<{ origen?: string; congelado_en?: string }>(
    "../public/data/manifiesto.json",
);
const API = process.env.MAISA_API ?? manifiesto.origen ?? "https://82.70.78.22.sslip.io";

console.log(`API:       ${API}`);
console.log(`Congelado: ${manifiesto.congelado_en ?? "sin fecha"}\n`);

let fallos = 0;

/** Compara y anota. No lanza, para poder ver todos los problemas de una pasada. */
function comprobar(queEs: string, correcto: boolean, detalle = ""): void {
    if (correcto) {
        console.log(`  ok   ${queEs}`);
        return;
    }
    fallos += 1;
    console.log(`  FALLA ${queEs}${detalle ? `\n         ${detalle}` : ""}`);
}

/**
 * El valor en JSON con las claves ordenadas, para comparar de verdad.
 *
 * `JSON.stringify` compara el orden en que estan escritas las claves, y en un
 * objeto JSON el orden no significa nada. Aqui si importa porque las dos partes no
 * escriben igual: el script del congelado guarda con las claves ordenadas
 * (`ESCALAR`, `NO_PAGAR`, `PAGAR`) y la API devuelve `por_resultado` en el orden
 * en que lo construyo (`PAGAR`, `NO_PAGAR`, `ESCALAR`). Comparado en crudo eso
 * sale como una diferencia en los contadores, cuando los numeros son identicos.
 *
 * El orden de un **array** si se respeta: ahi el orden es un dato, y `motivos` o
 * `hechos` en otro orden serian otra cosa.
 */
function canonico(valor: unknown): string {
    if (Array.isArray(valor)) return `[${valor.map(canonico).join(",")}]`;
    if (valor !== null && typeof valor === "object") {
        const pares = Object.entries(valor as Record<string, unknown>)
            .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
            .map(([clave, v]) => `${JSON.stringify(clave)}:${canonico(v)}`);
        return `{${pares.join(",")}}`;
    }
    return JSON.stringify(valor) ?? "null";
}

async function pedirFacturas(filtros: FiltrosApi): Promise<FacturaResumen[]> {
    const parametros = parametrosDeConsulta(filtros);
    // Se pagina a 500 —el tope real de la API— hasta agotar el conjunto: la
    // comparacion tiene que ser sobre el corpus entero y no sobre la primera
    // pagina, que ocultaria justo los registros donde suele estar la diferencia.
    // Pedir `?limit=99999` no vale: el servidor lo recorta a 500 en silencio.
    const items: FacturaResumen[] = [];
    let offset = 0;

    for (let vuelta = 0; vuelta < MAX_VUELTAS; vuelta++) {
        parametros.set("limit", String(TAMANO_PAGINA));
        parametros.set("offset", String(offset));
        const respuesta = await fetch(`${API}/api/facturas?${parametros}`);
        if (!respuesta.ok) {
            throw new Error(`La API ha respondido ${respuesta.status} a /api/facturas?${parametros}`);
        }
        const pagina = (await respuesta.json()) as PaginaApi;
        items.push(...pagina.items);
        offset += pagina.devueltas;
        if (pagina.devueltas <= 0 || offset >= pagina.total) break;
    }

    return items;
}

// --------------------------------------------------------------------------
// 1. El congelado, contra la API
// --------------------------------------------------------------------------
console.log("1. El congelado es el mismo corpus que la API");

const congelado = await leerJson<{ total: number; items: FacturaResumen[] }>(
    "../public/data/facturas.json",
);
const vivo = await pedirFacturas({});

comprobar(
    `mismo numero de facturas (${congelado.items.length})`,
    congelado.items.length === vivo.length,
    `congelado ${congelado.items.length}, vivo ${vivo.length}`,
);

const porIdVivo = new Map(vivo.map((f) => [f.file_id, f]));
comprobar(
    "mismos file_id",
    congelado.items.length === porIdVivo.size &&
    congelado.items.every((f) => porIdVivo.has(f.file_id)),
);

// Campo a campo. Se corta la lista de diferencias para que un corpus cambiado
// entero no oculte con ruido cual es el campo que se movio.
const diferencias: string[] = [];
for (const vieja of congelado.items) {
    const nueva = porIdVivo.get(vieja.file_id);
    if (!nueva) continue;
    const claves = new Set([...Object.keys(vieja), ...Object.keys(nueva)]);
    for (const clave of claves) {
        if (canonico(vieja[clave]) !== canonico(nueva[clave])) {
            diferencias.push(`${vieja.file_id}.${clave}: ${JSON.stringify(vieja[clave])} vs ${JSON.stringify(nueva[clave])}`);
        }
    }
    if (diferencias.length > 10) break;
}
comprobar("mismos valores campo a campo", diferencias.length === 0, diferencias.join("\n         "));

// El orden tambien cuenta: la API ordena por `file_id` antes de paginar, asi que
// si el congelado se guardo en ese orden la tabla se ve igual en los dos modos.
const ordenCongelado = congelado.items.map((f) => f.file_id).join("\n");
const ordenVivo = vivo.map((f) => f.file_id).join("\n");
comprobar("mismo orden (la API ordena por file_id)", ordenCongelado === ordenVivo);

// --------------------------------------------------------------------------
// 2. El filtro local, contra el de la API
// --------------------------------------------------------------------------
console.log("\n2. El filtro de filtros.ts hace lo mismo que traza.py");

const casos: { nombre: string; filtros: FiltrosApi }[] = [
    { nombre: "sin filtro", filtros: {} },
    { nombre: "resultado NO_PAGAR", filtros: { resultado: "NO_PAGAR" } },
    { nombre: "resultado ESCALAR", filtros: { resultado: "ESCALAR" } },
    { nombre: "lote 1", filtros: { lote: 1 } },
    { nombre: "lote 99 (no existe)", filtros: { lote: 99 } },
    { nombre: "proveedor catering", filtros: { proveedor: "catering" } },
    { nombre: "proveedor en mayusculas", filtros: { proveedor: "CATERING" } },
    { nombre: "q por numero de pedido", filtros: { q: "P007" } },
    { nombre: "q por file_id completo", filtros: { q: "2026-01-08_P001.pdf" } },
    { nombre: "q en mayusculas", filtros: { q: "CATERING" } },
    { nombre: "q con acento", filtros: { q: "consultoría" } },
    { nombre: "dos filtros a la vez", filtros: { resultado: "PAGAR", q: "catering" } },
    // Este es el caso que separa `re.escape` de un `includes` ingenuo: si alguien
    // buscara con expresion regular en vez de literal, el parentesis abriria un
    // grupo y el patron no casaria con nada (o casaria con todo).
    { nombre: "q con caracteres de regex", filtros: { q: "(2026" } },
    { nombre: "q con puntos", filtros: { q: "P001.pdf" } },
    // 65 de las 500 tienen acentos en el nombre; el filtro no debe comerse esos.
    { nombre: "q solo un guion", filtros: { q: "-" } },
];

for (const caso of casos) {
    const esperado = (await pedirFacturas(caso.filtros))
        .map((f) => f.file_id)
        .sort()
        .join("\n");
    const obtenido = aplicarFiltrosDeApi(congelado.items, caso.filtros)
        .map((f) => f.file_id)
        .sort()
        .join("\n");
    const n = esperado === "" ? 0 : esperado.split("\n").length;
    comprobar(
        `${caso.nombre} -> ${n} facturas`,
        esperado === obtenido,
        `la API devuelve ${esperado === "" ? 0 : esperado.split("\n").length}, el filtro local ${obtenido === "" ? 0 : obtenido.split("\n").length}`,
    );
}

// --------------------------------------------------------------------------
// 3. Los detalles y los contadores
// --------------------------------------------------------------------------
console.log("\n3. Detalles y contadores");

const ids = ["2026-01-08_P001.pdf", "2026-04-08_P007.pdf", "FA-2508_consultoría.pdf", "scan_002.pdf"];
for (const id of ids) {
    const deFichero = await leerDelCongelado<Record<string, unknown>>(rutaDetalle(id)).catch(
        () => null,
    );
    if (!deFichero) {
        comprobar(`detalle de ${id}`, false, "no encuentro el fichero del congelado");
        continue;
    }
    const respuesta = await fetch(`${API}/api/facturas/${encodeURIComponent(id)}`);
    if (!respuesta.ok) {
        comprobar(`detalle de ${id}`, false, `la API responde ${respuesta.status}`);
        continue;
    }
    const deApi = (await respuesta.json()) as Record<string, unknown>;
    // Se comparan las claves del congelado, no al reves: si la API ha anadido un
    // campo nuevo (por ejemplo `revision`, que la version desplegada no trae) eso
    // no es un fallo del congelado, es informacion sobre la version desplegada.
    const distintas = Object.keys(deFichero).filter(
        (clave) => canonico(deFichero[clave]) !== canonico(deApi[clave]),
    );
    const sobraEnApi = Object.keys(deApi).filter((clave) => !(clave in deFichero));
    comprobar(
        `detalle de ${id}`,
        distintas.length === 0,
        distintas.map((c) => `${c}`).join(", ") +
        (sobraEnApi.length ? ` (la API anade: ${sobraEnApi.join(", ")})` : ""),
    );
}

const statsCongeladas = await leerJson<Record<string, unknown>>("../public/data/estadisticas.json");
const statsVivas = (await (await fetch(`${API}/api/estadisticas`)).json()) as Record<string, unknown>;
const statsDistintas = Object.keys(statsCongeladas).filter(
    (clave) => canonico(statsCongeladas[clave]) !== canonico(statsVivas[clave]),
);
comprobar("contadores", statsDistintas.length === 0, statsDistintas.join(", "));

// --------------------------------------------------------------------------
console.log(`\n${fallos === 0 ? "Todo cuadra." : `${fallos} comprobaciones fallan.`}`);
process.exit(fallos === 0 ? 0 : 1);
