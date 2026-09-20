/**
 * La puerta unica a los datos: da igual que vengan de la API viva o del
 * congelado, y quien llama no tiene por que saberlo.
 *
 * Este fichero es el que hace posible el plan B. Las pantallas llaman a
 * `cargarFacturas` y reciben lo mismo si detras esta la VM o los ficheros de
 * `public/data/`, asi que "se ha caido la API" deja de ser una pantalla en blanco
 * y pasa a ser un dato mas que se ensena.
 *
 * La degradacion **no es silenciosa**: quien llama recibe tambien de donde
 * vinieron los datos (`Acceso.fuente`) y el congelado trae su `manifiesto.json`
 * con la fecha de la foto. Un panel que ensena datos de ayer sin decirlo es peor
 * que uno que no arranca.
 */

import {
    ErrorPeticion,
    borrarCorrecciones as borrarCorreccionesApi,
    guardarCorrecciones as guardarCorreccionesApi,
    pedirAnclajes,
    pedirCorrecciones,
    pedirDetalle,
    pedirEstadisticas,
    pedirMeta,
    pedirPdf,
    pedirSalud,
    pedirSnapshots,
    pedirTodasLasFacturas,
} from "./cliente";
import { esJson } from "./config";
import {
    RUTA_ESTADISTICAS,
    RUTA_FACTURAS,
    RUTA_MANIFIESTO,
    RUTA_META,
    RUTA_SALUD,
    RUTA_SNAPSHOTS,
    hayPdfCongelado,
    rutaDetalle,
    rutaPdfCongelado,
    type Manifiesto,
} from "./fixtures";
import {
    aplicarFiltrosDeApi,
    aplicarFiltrosExtra,
    type FiltrosVista,
} from "./filtros";
import type {
    Anclajes,
    CampoAnclable,
    Correcciones,
    Estadisticas,
    FacturaDetalle,
    FacturaResumen,
    Meta,
    Pagina,
    Salud,
    Snapshot,
} from "./types";

/** De donde se esta leyendo. */
export type Fuente = "vivo" | "congelado";

/**
 * Con quien se habla, resuelto de antemano.
 *
 * Es una union discriminada y no dos campos sueltos porque asi el compilador
 * impide pedir `api` en el caso congelado: si `fuente` es `"congelado"` no hay
 * ninguna URL que usar, y eso tiene que ser imposible de escribir, no una regla
 * que haya que recordar.
 */
export type Acceso =
    | { fuente: "vivo"; api: string; apiKey: string | null }
    | { fuente: "congelado"; api: string | null; apiKey: string | null };

/**
 * Todo lo que casa con un filtro, ya sin paginar.
 *
 * `total` viene de la fuente (cuantas coincidencias dice que hay) y `items` es lo
 * que se ha podido traer. Normalmente coinciden; si no, es que se ha tocado el
 * freno de seguridad de la paginacion y la pantalla puede decirlo en vez de
 * ensenar una lista corta como si fuera todo.
 */
export interface ConjuntoFacturas {
    total: number;
    items: FacturaResumen[];
}

/** Lee un JSON del congelado, con el mismo cuidado con el tipo que en la API. */
async function bajarJson<T>(ruta: string, queEs: string): Promise<T> {
    let respuesta: Response;
    try {
        respuesta = await fetch(ruta, { cache: "no-store" });
    } catch (exc) {
        throw new ErrorPeticion(`No he podido leer ${queEs} (${ruta}).`, {
            ruta,
            sinRespuesta: true,
            causa: exc,
        });
    }
    if (!respuesta.ok) {
        throw new ErrorPeticion(`${queEs} responde ${respuesta.status}.`, {
            ruta,
            estado: respuesta.status,
        });
    }
    // Aqui esta la trampa del rewrite de Vercel otra vez: un fichero que falta no
    // da 404, da 200 con el `index.html`. Ver `esJson` en `config.ts`.
    if (!esJson(respuesta)) {
        throw new ErrorPeticion(
            `Falta ${queEs} en el congelado: ${ruta} no devuelve JSON. ` +
            "Vuelve a generarlo con `python3 tools/descargar_fixtures.py`.",
            { ruta, estado: respuesta.status },
        );
    }
    try {
        return (await respuesta.json()) as T;
    } catch (exc) {
        throw new ErrorPeticion(`${queEs} no es JSON valido (${ruta}).`, {
            ruta,
            estado: respuesta.status,
            causa: exc,
        });
    }
}

/** Los contadores de la cabecera. */
export async function cargarEstadisticas(acceso: Acceso): Promise<Estadisticas> {
    if (acceso.fuente === "vivo") {
        return pedirEstadisticas({ api: acceso.api, apiKey: acceso.apiKey });
    }
    return bajarJson<Estadisticas>(RUTA_ESTADISTICAS, "los contadores del congelado");
}

/**
 * Las descargas del ERP, del origen que toque.
 *
 * Va por `Acceso` y no lee el fichero congelado directamente (como si hace
 * `cargarEscalabilidad`, que es un artefacto del build) porque **el dato vive en
 * Mongo**: en vivo lo sirve la API y en congelado hay una foto de lo que sirvio.
 * Sin la rama congelada el panel se quedaria sin la salud del ERP justo cuando
 * mas hace falta que es cuando la API no contesta.
 */
export async function cargarSnapshots(acceso: Acceso): Promise<Pagina<Snapshot>> {
    if (acceso.fuente === "vivo") {
        return pedirSnapshots({ api: acceso.api, apiKey: acceso.apiKey });
    }
    return bajarJson<Pagina<Snapshot>>(RUTA_SNAPSHOTS, "las descargas del ERP del congelado");
}

/**
 * La salud de las dependencias, del origen que toque.
 *
 * Va por `Acceso` por el mismo motivo que `cargarSnapshots`: el estado es un
 * dato vivo y la rama congelada existe para que la seccion de estado no
 * desaparezca justo cuando la API no contesta. Pero lo que se ensena entonces es
 * una **foto**, con la fecha del manifiesto al lado, no un latido.
 */
export async function cargarSalud(acceso: Acceso): Promise<Salud> {
    if (acceso.fuente === "vivo") {
        return pedirSalud({ api: acceso.api, apiKey: acceso.apiKey });
    }
    return bajarJson<Salud>(RUTA_SALUD, "la salud del congelado");
}

/**
 * La version del servicio y la configuracion con la que se juzgo la traza.
 *
 * `versiones_norma` es lo que hace reproducible una decision: la factura que se
 * sigue en el panel se juzgo con `norma_v3.2` y la pantalla lo tiene que poder
 * decir sin abrir el JSON. Por eso se pide aqui y no se da por sabido.
 */
export async function cargarMeta(acceso: Acceso): Promise<Meta> {
    if (acceso.fuente === "vivo") {
        return pedirMeta({ api: acceso.api, apiKey: acceso.apiKey });
    }
    return bajarJson<Meta>(RUTA_META, "la version del servicio en el congelado");
}

/**
 * Las facturas que casan con el filtro.
 *
 * Los dos caminos tienen que dar **el mismo** conjunto, y por eso el filtrado en
 * modo vivo lo hace la API (que es su trabajo y es lo que documenta) y en modo
 * congelado lo hace `filtros.ts`, que es un espejo suyo. Lo que la API no sabe
 * filtrar (fechas y NIF) se aplica en local en los dos casos, asi que tampoco ahi
 * puede haber diferencia.
 *
 * `total` y `items.length` **pueden no coincidir, y es correcto**: `total` es lo
 * que diria la API con ese filtro (que no sabe de fechas ni de NIF) e `items` es
 * lo que se pinta ya con todo aplicado. En modo vivo pasa igual, porque el total
 * lo da ella. La pantalla tiene que ensenar `items.length` en el pie de la tabla
 * y usar `total` solo para el "de N". Poner el total de la fuente en los dos
 * modos es justo lo que se hizo mal al principio: el congelado decia 500 con 43
 * filas en pantalla.
 */
export async function cargarFacturas(
    acceso: Acceso,
    filtros: FiltrosVista,
): Promise<ConjuntoFacturas> {
    if (acceso.fuente === "vivo") {
        const pagina = await pedirTodasLasFacturas(
            { api: acceso.api, apiKey: acceso.apiKey },
            filtros,
        );
        return { total: pagina.total, items: aplicarFiltrosExtra(pagina.items, filtros) };
    }

    const listado = await bajarJson<{ items: FacturaResumen[]; total: number }>(
        RUTA_FACTURAS,
        "el listado del congelado",
    );
    // El espejo del filtro de la API se aplica primero y por separado porque su
    // resultado es el que hace de `total`: el congelado no tiene un servidor que
    // cuente, asi que el numero lo tiene que sacar contando lo mismo que contaria
    // la API.
    const filtradoPorApi = aplicarFiltrosDeApi(listado.items, filtros);
    return { total: filtradoPorApi.length, items: aplicarFiltrosExtra(filtradoPorApi, filtros) };
}

/** El expediente completo de una factura. */
export async function cargarDetalle(acceso: Acceso, fileId: string): Promise<FacturaDetalle> {
    if (acceso.fuente === "vivo") {
        return pedirDetalle({ api: acceso.api, apiKey: acceso.apiKey }, fileId);
    }
    try {
        return await bajarJson<FacturaDetalle>(rutaDetalle(fileId), `el detalle de ${fileId}`);
    } catch (exc) {
        // Se marca con un codigo propio para que la pantalla pueda decir "esta
        // factura no esta en el congelado" en vez de un error generico: el
        // congelado solo guarda los 500 detalles del dia que se genero.
        if (exc instanceof ErrorPeticion) {
            throw new ErrorPeticion(exc.message, {
                ruta: exc.ruta,
                codigo: "congelado_sin_detalle",
                estado: exc.estado,
            });
        }
        throw exc;
    }
}

/**
 * El PDF, como `Blob`.
 *
 * En modo congelado solo hay unos pocos (los casos que se ensenan, ver `PDFS` en
 * el script). Se comprueba antes de pedirlo para no provocar un 404 que el
 * rewrite convertiria en un `index.html` que no es un PDF.
 */
export async function cargarPdf(acceso: Acceso, fileId: string): Promise<Blob> {
    if (acceso.fuente === "vivo") {
        return pedirPdf({ api: acceso.api, apiKey: acceso.apiKey }, fileId);
    }
    if (!hayPdfCongelado(fileId)) {
        throw new ErrorPeticion(
            `El congelado no trae el PDF de ${fileId}: solo guarda los ficheros de ejemplo.`,
            { ruta: rutaPdfCongelado(fileId), codigo: "congelado_sin_pdf" },
        );
    }
    const ruta = rutaPdfCongelado(fileId);
    let respuesta: Response;
    try {
        respuesta = await fetch(ruta);
    } catch (exc) {
        throw new ErrorPeticion(`No he podido leer el PDF congelado de ${fileId}.`, {
            ruta,
            codigo: "congelado_sin_pdf",
            sinRespuesta: true,
            causa: exc,
        });
    }
    const tipo = respuesta.headers.get("content-type") ?? "";
    if (!respuesta.ok || !tipo.toLowerCase().includes("pdf")) {
        throw new ErrorPeticion(
            `El PDF congelado de ${fileId} no esta o no es un PDF ("${tipo || "sin tipo"}").`,
            { ruta, codigo: "congelado_sin_pdf", estado: respuesta.status },
        );
    }
    return await respuesta.blob();
}

/**
 * Donde esta escrito cada dato dentro del PDF.
 *
 * **Sin API no hay resaltado, y se dice.** El anclaje no es un adorno que se
 * pueda adivinar en el cliente: lo decide la API, que es la que tiene la traza y
 * la cache de OCR. Recalcularlo aqui seria tener dos respuestas distintas a la
 * misma pregunta —y la del panel seria la equivocada en cuanto el motor cambie
 * como busca—, asi que el congelado se queda sin resaltado y lo explica en vez
 * de inventarse cajas.
 */
export async function cargarAnclajes(acceso: Acceso, fileId: string): Promise<Anclajes> {
    if (acceso.fuente === "vivo") {
        return pedirAnclajes({ api: acceso.api, apiKey: acceso.apiKey }, fileId);
    }
    throw new ErrorPeticion(
        "Sin API no se puede saber dónde está escrito cada dato: el anclaje sale de la traza, " +
            "que solo tiene el servidor.",
        { ruta: `/api/facturas/${fileId}/anclajes`, codigo: "congelado_sin_anclajes" },
    );
}

/** Las correcciones guardadas de una factura. */
export async function cargarCorrecciones(acceso: Acceso, fileId: string): Promise<Correcciones> {
    if (acceso.fuente === "vivo") {
        return pedirCorrecciones({ api: acceso.api, apiKey: acceso.apiKey }, fileId);
    }
    // En congelado no hay Mongo detras: la respuesta honesta es "no hay ninguna",
    // no un error. El formulario se deshabilita por separado (ver `SIN_ESCRITURA`).
    return { file_id: fileId, campos: [], actualizado_en: null };
}

/**
 * Guarda campos corregidos a mano. **Solo con API**: es lo unico que el panel
 * escribe, y escribe en Mongo.
 */
export async function guardarCorrecciones(
    acceso: Acceso,
    fileId: string,
    campos: Record<string, { valor: string; nota?: string }>,
    autor?: string,
): Promise<Correcciones> {
    if (acceso.fuente !== "vivo") {
        throw new ErrorPeticion(
            "No se puede guardar sin conexión con la API: las correcciones van a la base de datos.",
            { ruta: `/api/facturas/${fileId}/correcciones`, codigo: "congelado_sin_escritura" },
        );
    }
    return guardarCorreccionesApi({ api: acceso.api, apiKey: acceso.apiKey }, fileId, campos, autor);
}

/** Deshace una correccion, o todas si se omite `campo`. */
export async function borrarCorrecciones(
    acceso: Acceso,
    fileId: string,
    campo?: CampoAnclable,
): Promise<Correcciones> {
    if (acceso.fuente !== "vivo") {
        throw new ErrorPeticion(
            "No se puede deshacer sin conexión con la API: las correcciones están en la base de datos.",
            { ruta: `/api/facturas/${fileId}/correcciones`, codigo: "congelado_sin_escritura" },
        );
    }
    return borrarCorreccionesApi({ api: acceso.api, apiKey: acceso.apiKey }, fileId, campo);
}

/**
 * Cuando se congelaron los datos y de donde vinieron.
 *
 * Es best-effort: si falta el manifiesto el panel sigue, solo que no puede decir
 * de cuando son los datos. Por eso devuelve `null` en vez de lanzar.
 */
export async function cargarManifiesto(): Promise<Manifiesto | null> {
    try {
        return await bajarJson<Manifiesto>(RUTA_MANIFIESTO, "el manifiesto del congelado");
    } catch {
        return null;
    }
}
