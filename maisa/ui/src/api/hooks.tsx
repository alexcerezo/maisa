/**
 * Los hooks con los que las pantallas piden datos. Nada de red aqui: toda la
 * logica esta en `datos.ts`, y esto solo la ata al ciclo de vida de React.
 *
 * Lo que se resuelve en este fichero y no en cada pantalla:
 *
 * 1. **Esperar a saber la fuente.** La mayoria de los hooks no pueden pedir nada
 *    hasta que `fuente.ts` ha decidido si se lee de la API o del congelado. Si
 *    cada pantalla lo hiciera por su cuenta, cada una tendria su propio
 *    `useState`, su propio `useEffect` y su propia carrera.
 *
 * 2. **Descartar respuestas viejas.** Al escribir en un buscador se disparan
 *    peticiones en cadena; sin descartar las antiguas, gana la que llegue ultima y
 *    no la que se pidio ultima. La tabla ensenaria resultados de una busqueda que
 *    ya no esta en pantalla.
 *
 * 3. **Soltar los blobs del PDF.** Un `<iframe src="blob:...">` filtra memoria
 *    hasta recargar la pestana si nadie revoca la URL. Es un fallo que no se ve
 *    hasta que se abre la factura 400 y la pestana va a tirones.
 */

import {
    createContext,
    useContext,
    useEffect,
    useMemo,
    useRef,
    useState,
    type ReactNode,
} from "react";

import {
    cargarDetalle,
    cargarFacturas,
    cargarPdf,
    cargarSnapshots,
    type ConjuntoFacturas,
} from "./datos";
import { elegirFuente, type EstadoFuente } from "./fuente";
import type { FiltrosVista } from "./filtros";
import type { FacturaDetalle, Snapshot } from "./types";

export interface ResultadoPeticion<T> {
    datos: T | null;
    error: Error | null;
    cargando: boolean;
    /**
     * Vuelve a pedir lo mismo. Sin esto, un fallo de red deja la pantalla muerta:
     * el aviso de error ensena un boton de reintentar y no habia a que atarlo.
     */
    reintentar: () => void;
}

interface ValorFuente {
    /** `null` mientras se comprueba. */
    estado: EstadoFuente | null;
    cargando: boolean;
    /** Solo si la comprobacion en si falla; quedarse congelado NO es un error. */
    error: string | null;
    reintentar: () => void;
}

const ContextoFuente = createContext<ValorFuente | null>(null);

/**
 * Envuelve la aplicacion entera. Como la comprobacion se hace una vez y el
 * resultado vive en el contexto, las pantallas comparten la misma respuesta y no
 * se repite la peticion al navegar entre la tabla y el detalle.
 */
export function ProveedorFuente({ children }: { children: ReactNode }) {
    const [estado, setEstado] = useState<EstadoFuente | null>(null);
    const [cargando, setCargando] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [intento, setIntento] = useState(0);

    useEffect(() => {
        let vivo = true;
        setCargando(true);
        setError(null);
        elegirFuente()
            .then((nuevo) => {
                if (vivo) setEstado(nuevo);
            })
            .catch((exc) => {
                if (vivo) setError(exc instanceof Error ? exc.message : String(exc));
            })
            .finally(() => {
                if (vivo) setCargando(false);
            });
        return () => {
            vivo = false;
        };
    }, [intento]);

    const valor = useMemo<ValorFuente>(
        () => ({ estado, cargando, error, reintentar: () => setIntento((n) => n + 1) }),
        [estado, cargando, error],
    );

    return <ContextoFuente.Provider value={valor}>{children}</ContextoFuente.Provider>;
}

/** El estado de la fuente. Sirve para el distintivo de "vivo" / "congelado". */
export function useFuente(): ValorFuente {
    const valor = useContext(ContextoFuente);
    if (!valor) {
        throw new Error("Falta <ProveedorFuente> envolviendo la aplicacion.");
    }
    return valor;
}

/**
 * Una peticion atada al ciclo de vida de React.
 *
 * `clave` es la identidad de la peticion: si cambia, se vuelve a pedir. Se pasa
 * como cadena y no como lista de dependencias porque la funcion que pide datos se
 * construye en cada render (cierra sobre los filtros) y usarla como dependencia
 * provocaria una peticion por render.
 */
function usePeticion<T>(clave: string | null, ejecutar: () => Promise<T>): ResultadoPeticion<T> {
    const [datos, setDatos] = useState<T | null>(null);
    const [error, setError] = useState<Error | null>(null);
    const [cargando, setCargando] = useState(false);
    const [intento, setIntento] = useState(0);

    const ultima = useRef(ejecutar);
    ultima.current = ejecutar;

    useEffect(() => {
        if (clave === null) {
            setDatos(null);
            setError(null);
            setCargando(false);
            return;
        }
        let vivo = true;
        setCargando(true);
        setError(null);
        ultima
            .current()
            .then((resultado) => {
                if (vivo) {
                    setDatos(resultado);
                    setError(null);
                }
            })
            .catch((exc) => {
                if (vivo) {
                    setDatos(null);
                    setError(exc instanceof Error ? exc : new Error(String(exc)));
                }
            })
            .finally(() => {
                if (vivo) setCargando(false);
            });
        return () => {
            vivo = false;
        };
    }, [clave, intento]);

    return { datos, error, cargando, reintentar: () => setIntento((n) => n + 1) };
}

/**
 * Clave canonica de un filtro.
 *
 * Se construye campo a campo en vez de con `JSON.stringify` porque el orden de
 * las claves de un objeto depende de como se escribio, y dos filtros iguales
 * escritos en distinto orden darian claves distintas: se volveria a pedir lo
 * mismo y la tabla parpadearia sin motivo.
 *
 * **Todo filtro que cambie lo que se devuelve tiene que estar aqui.** Si falta uno,
 * `usePeticion` devuelve el resultado de la consulta anterior —la clave no ha
 * cambiado, luego "es la misma peticion"— y la tabla se queda con las filas de
 * antes mientras la URL y los contadores dicen otra cosa. Es el fallo que tuvo el
 * filtro de cola: se anadio a `aplicarFiltrosExtra` y no aqui, y filtrar por
 * desvios dejaba las 25 filas de siempre.
 */
function claveDeFiltros(filtros: FiltrosVista): string {
    const campos = [
        filtros.resultado ?? "",
        filtros.lote ?? "",
        filtros.proveedor ?? "",
        filtros.q ?? "",
        filtros.fechaDesde ?? "",
        filtros.fechaHasta ?? "",
        filtros.nif ?? "",
        filtros.segundaLectura ?? "",
    ];
    return campos.join("\u0001");
}

/**
 * Las facturas que casan con el filtro, del origen que toque.
 *
 * `activo` existe para el caso de la pantalla de la tabla: para poder decir
 * cuantas facturas se quedan fuera por no tener fecha hace falta consultar el
 * mismo filtro **sin** las fechas, y eso es una segunda peticion. Con `activo` en
 * `false` no se pide nada (`usePeticion` recibe clave `null`), asi que la segunda
 * consulta solo se lanza cuando de verdad hay un filtro de fechas puesto. Sin
 * esto, cada carga de la tabla pediria el listado dos veces para nada.
 */
export function useFacturas(
    filtros: FiltrosVista,
    activo: boolean = true,
): ResultadoPeticion<ConjuntoFacturas> {
    const { estado } = useFuente();
    const clave = estado && activo ? `${estado.fuente}\u0001${claveDeFiltros(filtros)}` : null;
    return usePeticion(clave, async () => {
        if (!estado) throw new Error("Se ha pedido el listado antes de saber la fuente.");
        return cargarFacturas(estado.acceso, filtros);
    });
}

/**
 * La salud de la descarga del ERP.
 *
 * Si espera a `useFuente`, al contrario que `useEscalabilidad`: los snapshots
 * salen de Mongo y en congelado se leen de `public/data/snapshots.json`, asi que
 * cual de los dos origenes se usa depende de la comprobacion de la fuente. La
 * clave lleva `estado.fuente` delante por eso mismo: si el panel cae de vivo a
 * congelado, el numero que se enseña es otro y hay que volver a pedirlo.
 */
export function useSnapshots(): ResultadoPeticion<Snapshot[]> {
    const { estado } = useFuente();
    const clave = estado ? `${estado.fuente}\u0001snapshots` : null;
    return usePeticion(clave, async () => {
        if (!estado) throw new Error("Se ha pedido la salud del ERP antes de saber la fuente.");
        const pagina = await cargarSnapshots(estado.acceso);
        return pagina.items;
    });
}

/** El expediente de una factura. */
export function useDetalle(fileId: string | undefined): ResultadoPeticion<FacturaDetalle> {
    const { estado } = useFuente();
    const clave = estado && fileId ? `${estado.fuente}\u0001detalle\u0001${fileId}` : null;
    return usePeticion(clave, async () => {
        if (!estado || !fileId) throw new Error("Se ha pedido un detalle sin factura.");
        return cargarDetalle(estado.acceso, fileId);
    });
}

export interface PdfCargado {
    /** URL local del navegador (`blob:`), lista para un `<iframe src>`. */
    url: string | null;
    error: Error | null;
    cargando: boolean;
}

/**
 * El PDF como URL local del navegador.
 *
 * Se pide con `fetch` y se convierte a `blob:` en vez de poner la URL de la API
 * directamente en el `<iframe>` por un motivo muy concreto: **un `<iframe>` no
 * puede mandar la cabecera `X-API-Key`**. Hoy la API esta en modo abierto y
 * ponerla directa funcionaria, pero el dia que se active la clave dejaria de
 * verse sin que nadie hubiera tocado el detalle. Pidiendolo aqui, la cabecera la
 * manda `fetch` (que si puede) y el `iframe` lee del navegador.
 */
export function usePdf(fileId: string | undefined): PdfCargado {
    const { estado } = useFuente();
    const [url, setUrl] = useState<string | null>(null);
    const [error, setError] = useState<Error | null>(null);
    const [cargando, setCargando] = useState(false);

    useEffect(() => {
        if (!estado || !fileId) {
            setUrl(null);
            setError(null);
            setCargando(false);
            return;
        }
        let vivo = true;
        let creada: string | null = null;
        setCargando(true);
        setError(null);
        cargarPdf(estado.acceso, fileId)
            .then((blob) => {
                if (!vivo) return;
                creada = URL.createObjectURL(blob);
                setUrl(creada);
            })
            .catch((exc) => {
                if (!vivo) return;
                setUrl(null);
                setError(exc instanceof Error ? exc : new Error(String(exc)));
            })
            .finally(() => {
                if (vivo) setCargando(false);
            });
        return () => {
            vivo = false;
            // Si la respuesta aun no habia llegado, `creada` es `null` y no hay
            // nada que soltar (el `then` ya no va a crear la URL). Si llego, se
            // suelta aqui: al cambiar de factura o al salir del detalle.
            if (creada) URL.revokeObjectURL(creada);
        };
    }, [estado, fileId]);

    return { url, error, cargando };
}
