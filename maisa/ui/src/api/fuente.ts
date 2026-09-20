/**
 * Decide si los datos salen de la API viva o del congelado. Es la unica
 * decision de este fichero.
 *
 * La comprobacion es **una peticion de datos de verdad** (`/api/estadisticas`),
 * no un latido. Eso importa por dos motivos:
 *
 * - Un `/health` que responde 200 no garantiza que el listado funcione: la API
 *   puede estar viva con la traza sin cargar y devolver 503 en las facturas. Si
 *   la comprobacion fuese un latido, el panel elegiria "vivo" y luego no habria
 *   nada que ensenar, que es justo el fallo que el congelado viene a evitar.
 * - `/health/ready` seria peor todavia: da 503 cuando el OCR esta caido, pero el
 *   listado se lee de fichero y funciona igual. Tirar del congelado por el OCR
 *   seria degradar por un motivo que no afecta.
 *
 * Un matiz que se decide aqui y no en la pantalla: **quedarse sin API no es un
 * error, es un estado**. El panel tiene que arrancar igual y decir por que esta
 * congelado, asi que `motivo` es texto para ensenar, no una excepcion.
 */

import { ErrorPeticion, pedirEstadisticas } from "./cliente";
import { resolverConfig, type ConfigResuelta } from "./config";
import { cargarEstadisticas, cargarManifiesto, type Acceso, type Fuente } from "./datos";
import type { Manifiesto } from "./fixtures";
import type { Estadisticas } from "./types";

export interface EstadoFuente {
    fuente: Fuente;
    /** Lo que hay que pasar a `datos.ts` en cada peticion. */
    acceso: Acceso;
    /**
     * Los contadores, ya leidos. En vivo son los que trajo la propia
     * comprobacion (no se piden dos veces); en congelado, los del fichero.
     * `null` si el congelado esta incompleto y no hay ni contadores.
     */
    estadisticas: Estadisticas | null;
    /** Por que se ha caido al congelado, en texto para la pantalla. `null` en vivo. */
    motivo: string | null;
    /** Solo en congelado: cuando y de donde son los datos. */
    manifiesto: Manifiesto | null;
    /** La config tal cual se resolvio, para poder ensenar que URL se esta usando. */
    config: ConfigResuelta;
}

/**
 * `?fuente=congelado` fuerza el plan B sin tocar nada.
 *
 * Existe para poder **ensenar** la degradacion: en una demo no se puede apagar la
 * VM a proposito, y "esto sigue funcionando sin la API" solo se cree si se ve. Con
 * este parametro el congelado se ensena cuando se quiere, y ademas sirve para
 * comprobar los dos caminos sin depender de la red.
 */
const FORZAR_CONGELADO = new URLSearchParams(window.location.search).get("fuente") === "congelado";

/**
 * Memoiza solo la comprobacion **en vuelo**, no el resultado.
 *
 * React en modo estricto monta los efectos dos veces en desarrollo, y sin esto
 * cada montaje seria una peticion de mas. Pero guardar el resultado impediria
 * volver a comprobar (el boton de reintentar, o volver de una caida), asi que en
 * cuanto termina se olvida.
 */
let enCurso: Promise<EstadoFuente> | null = null;

/** Comprueba la fuente y devuelve el estado. Se puede volver a llamar para reintentar. */
export function elegirFuente(): Promise<EstadoFuente> {
    if (!enCurso) {
        enCurso = comprobar().finally(() => {
            enCurso = null;
        });
    }
    return enCurso;
}

async function comprobar(): Promise<EstadoFuente> {
    const config = await resolverConfig();

    if (config.api === null) {
        return await congelado(config, config.problema ?? "No hay ninguna API configurada.", null);
    }
    if (FORZAR_CONGELADO) {
        return await congelado(config, "Forzado con `?fuente=congelado` en la direccion.", config.api);
    }

    try {
        const estadisticas = await pedirEstadisticas({ api: config.api, apiKey: config.apiKey });
        return {
            fuente: "vivo",
            acceso: { fuente: "vivo", api: config.api, apiKey: config.apiKey },
            estadisticas,
            motivo: null,
            manifiesto: null,
            config,
        };
    } catch (exc) {
        return await congelado(config, describirFallo(exc, config.api), config.api);
    }
}

/** Carga el congelado y su manifiesto. Si el congelado tambien falta, se notara al pintar. */
async function congelado(
    config: ConfigResuelta,
    motivo: string,
    api: string | null,
): Promise<EstadoFuente> {
    const acceso: Acceso = { fuente: "congelado", api, apiKey: config.apiKey };
    const [manifiesto, estadisticas] = await Promise.all([
        cargarManifiesto(),
        cargarEstadisticas(acceso).catch(() => null),
    ]);
    return { fuente: "congelado", acceso, estadisticas, motivo, manifiesto, config };
}

/**
 * El motivo en texto, para la banda de "estas viendo el congelado".
 *
 * El caso que de verdad va a pasar es `no_autorizado`: el dia que se active
 * `API_KEY`, el panel dejara de poder leer. Ese mensaje tiene que decir que hacer,
 * no solo que algo ha ido mal.
 */
function describirFallo(exc: unknown, api: string): string {
    if (!(exc instanceof ErrorPeticion)) {
        return exc instanceof Error ? exc.message : String(exc);
    }
    if (exc.codigo === "no_autorizado") {
        return (
            `La API de ${api} exige clave (X-API-Key) y no tengo ninguna valida. ` +
            "Ponla en public/config.json, o entra con ?apiKey=... en la direccion."
        );
    }
    // El resto de mensajes ya vienen redactados en castellano desde la API, asi
    // que se ensenan tal cual en vez de traducir codigos.
    return exc.message;
}
