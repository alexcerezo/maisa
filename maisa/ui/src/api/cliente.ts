/**
 * Las llamadas a `albertitos-api`, con las trampas del camino ya resueltas aqui
 * dentro y no repartidas por las pantallas.
 *
 * La API tiene cuatro comportamientos que sorprenden y que, dejados a la vista de
 * cada componente, acabarian en cuatro versiones distintas del mismo error:
 *
 * 1. **Nunca devuelve HTML, pero el despliegue estatico si.** `vercel.json`
 *    reescribe cualquier ruta desconocida a `/index.html` con **200**. Un detalle
 *    que falte en el congelado no da 404: da 200 con `text/html` y `res.json()`
 *    revienta con `Unexpected token '<'`. Aqui se mira el `content-type` **antes**
 *    de convertir, y el fallo dice que falta el fichero.
 *
 * 2. **`limit` se recorta en silencio.** `?limit=99999` no es un error: devuelve
 *    `limit: 500`. Si el panel diese por hecho que le devuelven todo lo que pide,
 *    ensenaria 500 de 1200 filas sin decir nada. Por eso `pedirTodasLasFacturas`
 *    pagina de verdad y comprueba lo que le han dado.
 *
 * 3. **`q` y `proveedor` tienen `max_length=64`.** Pasarse no da cero resultados:
 *    da un **422**. Se recorta en el cliente (en `filtros.ts`).
 *
 * 4. **Los errores son uniformes**: `{error: {codigo, mensaje, detalle}}` y el
 *    `mensaje` ya viene redactado en castellano para ensenarlo tal cual. Asi que
 *    `ErrorPeticion.mensaje` es lo que se pinta, sin traducir codigos.
 *
 * Nada de esto sale de la documentacion: esta comprobado contra la API desplegada.
 */

import { esJson } from "./config";
import { parametrosDeConsulta, type FiltrosApi } from "./filtros";
import type {
    Anclajes,
    CampoAnclable,
    Correcciones,
    ErrorApi,
    Estadisticas,
    FacturaDetalle,
    FacturaResumen,
    Meta,
    Pagina,
    Salud,
    Snapshot,
} from "./types";

/** Con quien hay que hablar. Sale de `config.ts`. */
export interface AccesoApi {
    api: string;
    /** `null` = no mandar `X-API-Key`. Es el caso de hoy. */
    apiKey: string | null;
}

/**
 * Un fallo al pedir datos, ya interpretado.
 *
 * `sinRespuesta` es la distincion que decide si se tira del congelado: `true`
 * significa que la API no ha contestado (red, DNS, TLS, tiempo agotado) o ha
 * contestado algo que no se puede usar; `false` significa que la API ha
 * contestado y ha dicho que no (`404`, `401`...), que es informacion, no averia.
 */
export class ErrorPeticion extends Error {
    readonly codigo: string | null;
    readonly estado: number | null;
    readonly ruta: string;
    readonly sinRespuesta: boolean;

    constructor(
        mensaje: string,
        opciones: {
            ruta: string;
            codigo?: string | null;
            estado?: number | null;
            sinRespuesta?: boolean;
            causa?: unknown;
        },
    ) {
        super(mensaje, { cause: opciones.causa });
        this.name = "ErrorPeticion";
        this.codigo = opciones.codigo ?? null;
        this.estado = opciones.estado ?? null;
        this.ruta = opciones.ruta;
        this.sinRespuesta = opciones.sinRespuesta ?? false;
    }
}

/**
 * El listado entero entra en una peticion. El tope de la API es 500
 * (`MAX_LIMIT`), asi que pedir 500 es pedir el maximo sin mentir.
 */
export const TAMANO_PAGINA = 500;

/**
 * Freno de seguridad del bucle de paginacion. Con 500 por vuelta son 20.000
 * facturas: muy por encima de lo real, y evita que un servidor que ignore
 * `offset` deje la pestana girando para siempre.
 */
const MAX_VUELTAS = 40;

const TIEMPO_POR_DEFECTO = 15000;

/**
 * El corte de la comprobacion de fuente. Corto a proposito: esto se ejecuta al
 * abrir la pantalla y de su resultado depende que se vea algo. Mas vale caer al
 * congelado en 5 segundos que dejar al que mira delante de una pantalla vacia.
 */
export const TIEMPO_COMPROBACION = 5000;

interface OpcionesPeticion {
    api: string;
    apiKey: string | null;
    tiempoMs?: number;
    acepta?: string;
    /**
     * `GET` si se omite. Solo lo usan las correcciones, que son lo unico que el
     * panel escribe.
     */
    metodo?: "GET" | "PUT" | "DELETE";
    /**
     * Se manda como JSON. `undefined` = sin cuerpo: un `DELETE` de todo no lleva
     * ninguno, y mandar `"null"` o `{}` seria inventarse una peticion distinta.
     */
    cuerpo?: unknown;
}

/**
 * Una peticion, con tiempo maximo y con los fallos ya traducidos.
 *
 * El tiempo maximo se hace con `AbortController` en vez de `AbortSignal.timeout`
 * para poder decir **si** el fallo fue por agotarse el tiempo, que es un mensaje
 * util, o por no haber red, que es otro.
 */
async function pedir(ruta: string, opciones: OpcionesPeticion): Promise<Response> {
    const url = opciones.api + ruta;
    const tiempoMs = opciones.tiempoMs ?? TIEMPO_POR_DEFECTO;
    const control = new AbortController();
    let agotado = false;
    const reloj = window.setTimeout(() => {
        agotado = true;
        control.abort();
    }, tiempoMs);

    const cabeceras: Record<string, string> = { Accept: opciones.acepta ?? "application/json" };
    // La regla que permite cambiar de estrategia sin tocar codigo: sin clave
    // configurada NO se manda la cabecera. Si algun dia hay un proxy delante que
    // la inyecte, este fichero no se entera y sigue funcionando.
    if (opciones.apiKey) cabeceras["X-API-Key"] = opciones.apiKey;

    const metodo = opciones.metodo ?? "GET";
    let cuerpo: string | undefined;
    if (opciones.cuerpo !== undefined) {
        cabeceras["Content-Type"] = "application/json";
        cuerpo = JSON.stringify(opciones.cuerpo);
    }

    let respuesta: Response;
    try {
        respuesta = await fetch(url, {
            method: metodo,
            headers: cabeceras,
            body: cuerpo,
            signal: control.signal,
        });
    } catch (exc) {
        throw new ErrorPeticion(
            agotado
                ? `La API no ha contestado en ${Math.round(tiempoMs / 1000)} s (${url}).`
                : `No he podido conectar con la API en ${url}.`,
            { ruta, sinRespuesta: true, causa: exc },
        );
    } finally {
        window.clearTimeout(reloj);
    }

    if (!respuesta.ok) {
        const { codigo, mensaje } = await leerError(respuesta, url);
        throw new ErrorPeticion(mensaje, { ruta, codigo, estado: respuesta.status });
    }
    return respuesta;
}

/** Saca el mensaje de la API si lo trae; si no, uno generico con el codigo HTTP. */
async function leerError(
    respuesta: Response,
    url: string,
): Promise<{ codigo: string | null; mensaje: string }> {
    if (esJson(respuesta)) {
        try {
            const cuerpo = (await respuesta.json()) as Partial<ErrorApi>;
            if (cuerpo.error?.mensaje) {
                return { codigo: cuerpo.error.codigo ?? null, mensaje: cuerpo.error.mensaje };
            }
        } catch {
            // El cuerpo no era el error uniforme. Se cae al mensaje generico.
        }
    }
    return { codigo: null, mensaje: `La API ha respondido ${respuesta.status} en ${url}.` };
}

/** Convierte a JSON comprobando antes el tipo. Ver la trampa 1 de la cabecera. */
async function leerJson<T>(respuesta: Response, ruta: string): Promise<T> {
    const tipo = respuesta.headers.get("content-type") ?? "";
    if (!esJson(respuesta)) {
        throw new ErrorPeticion(
            `Esperaba JSON de ${ruta} y ha llegado "${tipo || "sin tipo"}".`,
            { ruta, estado: respuesta.status },
        );
    }
    try {
        return (await respuesta.json()) as T;
    } catch (exc) {
        throw new ErrorPeticion(`El JSON de ${ruta} no se puede leer.`, {
            ruta,
            estado: respuesta.status,
            causa: exc,
        });
    }
}

/**
 * Los contadores de la cabecera. **Esta es tambien la comprobacion de fuente**:
 * es la primera llamada de la pantalla y su exito decide si los datos salen de
 * la API o del congelado.
 *
 * A proposito **no** se usa `/health/ready` para eso, aunque parezca lo natural:
 * `CRITICAL_DEPS` incluye el OCR y devuelve 503 cuando el OCR esta caido, pero el
 * listado se lee de fichero y funciona igual. Un panel que no arranca porque el
 * OCR no esta seria un panel roto por un motivo que no le afecta.
 */
export async function pedirEstadisticas(
    acceso: AccesoApi,
    tiempoMs = TIEMPO_COMPROBACION,
): Promise<Estadisticas> {
    const ruta = "/api/estadisticas";
    const respuesta = await pedir(ruta, { ...acceso, tiempoMs });
    return leerJson<Estadisticas>(respuesta, ruta);
}

/**
 * Las descargas del ERP registradas.
 *
 * Devuelve la lista entera y no una por `_id` porque hoy hay una sola descarga
 * vigente y el panel enseña esa: pedir "la vigente" seria una ruta que la API no
 * tiene, y el dia que haya historico el panel lo quiere entero para poder decir
 * que hubo antes.
 */
export async function pedirSnapshots(
    acceso: AccesoApi,
    tiempoMs = TIEMPO_COMPROBACION,
): Promise<Pagina<Snapshot>> {
    const ruta = "/api/snapshots";
    const respuesta = await pedir(ruta, { ...acceso, tiempoMs });
    return leerJson<Pagina<Snapshot>>(respuesta, ruta);
}

/**
 * El tiempo maximo de `/health`. Mas largo que el de la comprobacion de fuente
 * porque esta ruta **no** decide de donde salen los datos: recorre las
 * dependencias una a una y una que este lenta no debe cortar la pantalla. Si se
 * agota, la seccion de estado lo dice y el resto de la pagina sigue contando lo
 * que sabe.
 */
const TIEMPO_SALUD = 8000;

/**
 * La salud de las dependencias: Mongo, el OCR y el bucket de escritura.
 *
 * Aqui **no** se comprueba nada despues de leerla, y es lo correcto: `/health`
 * contesta 200 aunque haya una dependencia caida (el 503 vive en
 * `/health/ready`). O sea que `ok: false` no es un fallo de la peticion, es el
 * dato que se ha venido a buscar. Tratarlo como error dejaria la seccion de
 * estado en blanco justo cuando importa.
 */
export async function pedirSalud(acceso: AccesoApi, tiempoMs = TIEMPO_SALUD): Promise<Salud> {
    const ruta = "/health";
    const respuesta = await pedir(ruta, { ...acceso, tiempoMs });
    return leerJson<Salud>(respuesta, ruta);
}

/**
 * Que version es esto y como esta montado.
 *
 * Trae la version de la API y la de la norma con la que se juzgaron las
 * facturas, que es la mitad de la trazabilidad: sin saber con que norma se
 * decidio, el resultado de una factura no se puede reproducir.
 */
export async function pedirMeta(acceso: AccesoApi, tiempoMs = TIEMPO_COMPROBACION): Promise<Meta> {
    const ruta = "/api/meta";
    const respuesta = await pedir(ruta, { ...acceso, tiempoMs });
    return leerJson<Meta>(respuesta, ruta);
}

/** Una pagina del listado. Normalmente no se llama directamente. */
export async function listarFacturas(
    acceso: AccesoApi,
    filtros: FiltrosApi,
    pagina: { limit?: number; offset?: number } = {},
): Promise<Pagina<FacturaResumen>> {
    const parametros = parametrosDeConsulta(filtros);
    if (pagina.limit !== undefined) parametros.set("limit", String(pagina.limit));
    if (pagina.offset !== undefined) parametros.set("offset", String(pagina.offset));
    const consulta = parametros.toString();
    const ruta = consulta ? `/api/facturas?${consulta}` : "/api/facturas";
    const respuesta = await pedir(ruta, acceso);
    return leerJson<Pagina<FacturaResumen>>(respuesta, ruta);
}

/**
 * Todo lo que casa con el filtro, paginando de verdad.
 *
 * Se pagina en vez de pedir `?limit=99999` porque el tope se recorta **sin
 * avisar**: con 500 facturas una vuelta basta, pero el dia que la traza crezca a
 * 1200 el panel tiene que seguir ensenandolas todas y no 500 sin decirlo.
 */
export async function pedirTodasLasFacturas(
    acceso: AccesoApi,
    filtros: FiltrosApi,
    opciones: { tamanoPagina?: number } = {},
): Promise<Pagina<FacturaResumen>> {
    const tamano = opciones.tamanoPagina ?? TAMANO_PAGINA;
    const items: FacturaResumen[] = [];
    let total = 0;
    let limite = tamano;
    let offset = 0;

    for (let vuelta = 0; vuelta < MAX_VUELTAS; vuelta++) {
        const pagina = await listarFacturas(acceso, filtros, { limit: tamano, offset });
        total = pagina.total;
        limite = pagina.limit;
        items.push(...pagina.items);
        offset += pagina.devueltas;
        // Se para al haber pedido todo. `devueltas` a cero tambien corta: un
        // servidor que ignorase `offset` devolveria lo mismo en cada vuelta y
        // esto no acabaria nunca.
        if (pagina.devueltas <= 0 || offset >= pagina.total) break;
    }

    return { total, limit: limite, offset: 0, devueltas: items.length, items };
}

/**
 * El expediente de una factura.
 *
 * `encodeURIComponent` no es opcional: 65 de los 500 `file_id` llevan acentos y
 * el parametro de ruta de la API valida contra un patron que los admite, pero
 * solo si llegan bien codificados.
 */
export async function pedirDetalle(acceso: AccesoApi, fileId: string): Promise<FacturaDetalle> {
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}`;
    const respuesta = await pedir(ruta, acceso);
    return leerJson<FacturaDetalle>(respuesta, ruta);
}

/**
 * El PDF original, como `Blob`.
 *
 * Devuelve un `Blob` y no una URL a proposito. Un `<iframe src=".../pdf">` no
 * puede mandar la cabecera `X-API-Key`, asi que el dia que la clave se active el
 * PDF dejaria de verse. Con el `Blob` la peticion la hace `fetch` (que si puede
 * mandar cabeceras) y el `iframe` apunta a una URL local del propio navegador.
 * Quien lo use tiene que soltar la URL con `URL.revokeObjectURL` al desmontar.
 */
export async function pedirPdf(acceso: AccesoApi, fileId: string, tiempoMs?: number): Promise<Blob> {
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}/pdf`;
    const respuesta = await pedir(ruta, { ...acceso, acepta: "application/pdf", tiempoMs });
    const tipo = respuesta.headers.get("content-type") ?? "";
    if (!tipo.toLowerCase().includes("pdf")) {
        throw new ErrorPeticion(
            `Esperaba un PDF de ${ruta} y ha llegado "${tipo || "sin tipo"}".`,
            { ruta, estado: respuesta.status },
        );
    }
    return await respuesta.blob();
}

/**
 * Donde esta escrito cada dato dentro del documento.
 *
 * Devuelve **dos cosas distintas segun el origen** y hay que tratarlas como
 * tales: en `capa_texto` trae los `tokens` para que los busque el navegador en el
 * texto real del PDF, y en `ocr` trae las `anclas` ya resueltas, porque ahi no
 * hay texto que buscar. Las 471 con capa de texto llegan con `anclas: []`; las 29
 * escaneadas, con `tokens` que solo sirven de pista.
 */
export async function pedirAnclajes(acceso: AccesoApi, fileId: string): Promise<Anclajes> {
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}/anclajes`;
    const respuesta = await pedir(ruta, acceso);
    return leerJson<Anclajes>(respuesta, ruta);
}

/** Las correcciones que ya hay guardadas para una factura. */
export async function pedirCorrecciones(acceso: AccesoApi, fileId: string): Promise<Correcciones> {
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}/correcciones`;
    const respuesta = await pedir(ruta, acceso);
    return leerJson<Correcciones>(respuesta, ruta);
}

/**
 * Guarda campos corregidos a mano.
 *
 * **Fusiona, no reemplaza**: se puede mandar un solo campo y los que ya estaban
 * se quedan como estaban. Es lo que permite guardar campo a campo desde el
 * formulario sin llevarse por delante lo corregido antes.
 *
 * La respuesta es el estado completo ya guardado, asi que quien llama no tiene
 * que recomponerlo por su cuenta ni volver a pedirlo.
 */
export async function guardarCorrecciones(
    acceso: AccesoApi,
    fileId: string,
    campos: Record<string, { valor: string; nota?: string }>,
    autor?: string,
): Promise<Correcciones> {
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}/correcciones`;
    const cuerpo: { campos: typeof campos; autor?: string } = { campos };
    // El `autor` vacio se omite en vez de mandarse en blanco: la API lo guarda
    // como `null` y "sin autor" es la ausencia del campo, no una cadena vacia.
    if (autor && autor.trim()) cuerpo.autor = autor.trim();
    const respuesta = await pedir(ruta, { ...acceso, metodo: "PUT", cuerpo });
    return leerJson<Correcciones>(respuesta, ruta);
}

/** Deshace una correccion, o todas si se omite `campo`. */
export async function borrarCorrecciones(
    acceso: AccesoApi,
    fileId: string,
    campo?: CampoAnclable,
): Promise<Correcciones> {
    const sufijo = campo ? `?campo=${encodeURIComponent(campo)}` : "";
    const ruta = `/api/facturas/${encodeURIComponent(fileId)}/correcciones${sufijo}`;
    const respuesta = await pedir(ruta, { ...acceso, metodo: "DELETE" });
    return leerJson<Correcciones>(respuesta, ruta);
}
