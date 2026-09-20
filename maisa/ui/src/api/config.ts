/**
 * De donde sale la URL de la API, decidido en el navegador y no dentro del
 * bundle.
 *
 * Vite sustituye `import.meta.env.VITE_*` al empaquetar, asi que una variable de
 * entorno queda cocida dentro del `.js` del build. Eso obliga a **volver a
 * desplegar** cada vez que cambia la URL, y aqui la URL es precisamente lo que
 * mas se mueve: la API vive en la VM de Oracle y su nombre (`sslip.io`) deriva
 * de la IP, de modo que si Oracle la cambia hay que corregirlo en caliente.
 *
 * Por eso se lee al arrancar, por este orden:
 *
 *   1. `?api=https://otra` en la barra de direcciones. Sirve para apuntar a otro
 *      backend sin tocar ficheros ni desplegar. Se guarda y sobrevive a la
 *      recarga, para que un enlace compartido y el `localStorage` digan lo mismo.
 *   2. `localStorage` (lo que guardo el paso 1).
 *   3. `public/config.json`, que es el valor de verdad del despliegue.
 *
 * Si ninguno da una URL utilizable el panel **no revienta**: se queda sin API y
 * `fuente.ts` decide tirar del congelado. Por eso `api` es `string | null` y no
 * se lanza ninguna excepcion desde aqui: quedarse sin API es un estado previsto,
 * no un error.
 */

/** La forma de `public/config.json`. */
export interface ConfigFichero {
    /** URL base de la API. Tiene que ser absoluta y con esquema `http(s)://`. */
    api: string;
    /**
     * Valor de la cabecera `X-API-Key`. Es **opcional a proposito**: mientras
     * falte, el cliente no manda la cabecera y la API contesta en modo abierto
     * (que es como esta hoy). El dia que se active la clave, puesta aqui y
     * recargar: no hay que tocar ni una linea de codigo.
     *
     * Ojo con lo que significa traerla en este fichero: `config.json` se sirve
     * como un fichero estatico mas, asi que la clave es **publica**. Con la
     * clave publica lo unico que protege de verdad contra escrituras es
     * `SUBIDAS_HABILITADAS=0` en el servidor. Si se quiere la clave sin
     * publicarla, la via es un proxy (una `rewrite` de Vercel que inyecte la
     * cabecera desde una variable de entorno): para eso el cliente ya esta
     * preparado, porque sin `apiKey` simplemente no manda la cabecera.
     */
    apiKey?: string;
}

/** De donde ha salido la URL finalmente. Se ensena al lado del distintivo de fuente. */
export type OrigenConfig = "consulta" | "localStorage" | "config.json" | "ninguno";

export interface ConfigResuelta {
    /** URL base, sin barra final. `null` si no hay ninguna utilizable. */
    api: string | null;
    /** Clave para `X-API-Key`. `null` significa "no mandes la cabecera". */
    apiKey: string | null;
    origen: OrigenConfig;
    /** Si no hay URL, por que. Texto listo para la pantalla. `null` si todo bien. */
    problema: string | null;
}

export const RUTA_CONFIG = "/config.json";

/** Prefijadas para no pisar nada de la pagina anfitriona. */
const CLAVE_API = "albertitos.api";
const CLAVE_APIKEY = "albertitos.apiKey";

let cache: ConfigResuelta | null = null;
let enCurso: Promise<ConfigResuelta> | null = null;

/**
 * Si una respuesta trae JSON de verdad.
 *
 * Esto no es paranoia: el `rewrites` de `vercel.json` manda **cualquier** ruta
 * desconocida a `/index.html` y lo hace con **200**. Un `/config.json` o un
 * `/data/facturas/x.json` que no existan no dan 404, dan 200 con `text/html`, y
 * entonces `res.json()` falla con un `Unexpected token '<'` que no explica nada.
 * Mirando el tipo antes de convertir, el fallo dice lo que pasa.
 */
export function esJson(respuesta: Response): boolean {
    const tipo = respuesta.headers.get("content-type") ?? "";
    return tipo.toLowerCase().includes("json");
}

/**
 * Valida y normaliza una URL de API.
 *
 * `new URL()` sin base rechaza las rutas relativas (`/api`) y las
 * protocolo-relativas (`//host`), que es justo lo que hay que rechazar: puestas
 * en `config.json` no apuntarian a la API, apuntarian al propio panel, y el
 * fallo se veria como "la API devuelve HTML" en vez de "la URL esta mal".
 */
function normalizarApi(valor: unknown): string | null {
    if (typeof valor !== "string") return null;
    const limpio = valor.trim().replace(/\/+$/, "");
    if (!limpio) return null;
    let url: URL;
    try {
        url = new URL(limpio);
    } catch {
        return null;
    }
    return url.protocol === "http:" || url.protocol === "https:" ? limpio : null;
}

/** `localStorage` puede lanzar (modo privado, cookies de terceros bloqueadas). */
function leerGuardado(clave: string): string | null {
    try {
        return window.localStorage.getItem(clave);
    } catch {
        return null;
    }
}

function guardar(clave: string, valor: string | null): void {
    try {
        if (valor === null) window.localStorage.removeItem(clave);
        else window.localStorage.setItem(clave, valor);
    } catch {
        // Almacenamiento bloqueado. La config sigue valida mientras dure la
        // pestana; simplemente no se recuerda.
    }
}

/** Descarga `config.json`. Devuelve el motivo en texto cuando no se puede usar. */
async function bajarConfig(): Promise<{ fichero: ConfigFichero | null; problema: string | null }> {
    let respuesta: Response;
    try {
        // `no-store` para que un redespliegue no quede tapado por la cache del
        // navegador: la gracia de este fichero es poder cambiarlo sin recompilar.
        respuesta = await fetch(RUTA_CONFIG, { cache: "no-store" });
    } catch (exc) {
        return { fichero: null, problema: `No he podido leer ${RUTA_CONFIG} (${describir(exc)}).` };
    }
    if (!respuesta.ok) {
        return { fichero: null, problema: `${RUTA_CONFIG} responde ${respuesta.status}.` };
    }
    if (!esJson(respuesta)) {
        return { fichero: null, problema: `${RUTA_CONFIG} no devuelve JSON (¿falta el fichero?).` };
    }
    try {
        return { fichero: (await respuesta.json()) as ConfigFichero, problema: null };
    } catch {
        return { fichero: null, problema: `${RUTA_CONFIG} no es JSON valido.` };
    }
}

function describir(exc: unknown): string {
    return exc instanceof Error ? exc.message : String(exc);
}

async function calcularConfig(): Promise<ConfigResuelta> {
    const parametros = new URLSearchParams(window.location.search);
    const apiDeConsulta = parametros.get("api");
    const claveDeConsulta = parametros.get("apiKey");
    const apiGuardada = leerGuardado(CLAVE_API);

    // El fichero se lee siempre, aunque la URL venga por la consulta: la clave
    // puede estar solo aqui, y leerlo es un fichero estatico pequeno.
    const { fichero, problema: problemaFichero } = await bajarConfig();

    let api = normalizarApi(apiDeConsulta);
    let origen: OrigenConfig = "consulta";
    if (api) {
        guardar(CLAVE_API, api);
    } else {
        api = normalizarApi(apiGuardada);
        origen = "localStorage";
        if (!api) {
            api = normalizarApi(fichero?.api);
            origen = "config.json";
            if (!api) origen = "ninguno";
        }
    }

    const clave =
        (claveDeConsulta ?? "").trim() ||
        (leerGuardado(CLAVE_APIKEY) ?? "").trim() ||
        (fichero?.apiKey ?? "").trim() ||
        null;

    const motivos: string[] = [];
    if (apiDeConsulta && !normalizarApi(apiDeConsulta)) {
        motivos.push("El `?api=` de la direccion no es una URL http o https.");
    }
    if (apiGuardada && !normalizarApi(apiGuardada)) {
        motivos.push("La URL guardada en este navegador ya no vale.");
    }
    if (problemaFichero) motivos.push(problemaFichero);

    return {
        api,
        apiKey: clave,
        origen,
        problema:
            api !== null
                ? null
                : motivos.join(" ") ||
                `No hay ninguna API configurada: falta el campo "api" en ${RUTA_CONFIG}.`,
    };
}

/**
 * La configuracion, resuelta una sola vez por pestana.
 *
 * Memoiza la **promesa**, no el resultado, para que dos consumidores que
 * pregunten a la vez (React en modo estricto monta los efectos dos veces en
 * desarrollo) compartan una sola descarga de `config.json`.
 */
export function resolverConfig(): Promise<ConfigResuelta> {
    if (cache) return Promise.resolve(cache);
    if (!enCurso) {
        enCurso = calcularConfig()
            .then((config) => {
                cache = config;
                return config;
            })
            .finally(() => {
                enCurso = null;
            });
    }
    return enCurso;
}

/** Apunta a otra API y lo recuerda. La recarga no es opcional: invalida la cache. */
export function setApiBase(url: string): boolean {
    const limpio = normalizarApi(url);
    if (!limpio) return false;
    guardar(CLAVE_API, limpio);
    cache = null;
    enCurso = null;
    return true;
}

/** Fija la clave para `X-API-Key`. Cadena vacia para volver a no mandarla. */
export function setApiKey(clave: string | null): void {
    guardar(CLAVE_APIKEY, (clave ?? "").trim() || null);
    cache = null;
    enCurso = null;
}

/** Vuelve a `config.json` como unica fuente. Util para dejar de perseguir un override. */
export function olvidarAjustesLocales(): void {
    guardar(CLAVE_API, null);
    guardar(CLAVE_APIKEY, null);
    cache = null;
    enCurso = null;
}
