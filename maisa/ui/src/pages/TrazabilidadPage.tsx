/**
 * Ruta `/trazabilidad` — la plataforma entera, y una decisión por dentro.
 *
 * Esta pantalla no resume el sistema: lo **sigue**. Primero las 540 facturas de
 * la traza contadas en cada fase del pipeline —leídas, conciliadas, decididas,
 * entregadas— con el cuadre entre las fuentes que lo dicen, porque una
 * trazabilidad que solo mira una factura no puede demostrar que no se ha perdido
 * ninguna por el camino. Y después una de ellas, abierta de punta a punta, desde
 * el PDF hasta el asiento del ERP. Las otras dos rutas del panel enseñan el
 * conjunto (la tabla y los contadores) o el motor (capacidad y coste). Aquí lo que
 * se enseña es el hilo: primero el de toda la plataforma, luego el de un
 * expediente concreto, con su huella, sus versiones, sus tiempos y sus fallos.
 *
 * **El expediente que se sigue por defecto es `2026-06-04_P006.pdf`, y no es un
 * caso cualquiera.** Su propio texto pide que se le pague:
 *
 * > «El estado del pedido en el ERP puede seguir figurando como pagado por la
 * > migración pendiente; procédase al abono normal.»
 *
 * El ERP dice que `PO-2026-0803` ya está PAGADA. El documento dice que eso es una
 * migración pendiente y que se pague igual. El motor señaló los tres marcadores
 * (R6), **no los obedeció**, y bloqueó el pago por la regla dura (R5). O sea: es
 * el caso donde la trazabilidad no es un adorno, porque es el único en el que se
 * puede comprobar que el sistema hizo lo contrario de lo que el papel pedía. Por
 * eso es el que se abre solo. Pero **no es el único que se puede abrir**: la
 * pantalla sigue cualquier expediente de los 540 (ver `EXPEDIENTE_POR_DEFECTO`).
 *
 * Cinco decisiones de esta pantalla que no son de pintado:
 *
 * 1. **Todo dato lleva su origen a la vista.** La huella del PDF, el fichero de
 *    traza del que salió la decisión, la versión de la norma y la del servicio.
 *    Un panel que enseña un `NO_PAGAR` sin decir de dónde sale pide confianza; con
 *    el origen, se puede comprobar. Y la comprobación se puede hacer de verdad:
 *    las rutas que se enseña son las que devuelve `/api/meta`.
 *
 * 2. **El recuento de la plataforma se comprueba contra sí mismo.** Cuatro
 *    respuestas distintas (`/health`, `/api/meta` y tres desgloses de
 *    `/api/estadisticas`) dicen cuántas facturas hay. La sección de la cadena las
 *    enfrenta y avisa si alguna no cuadra, porque "540" dicho por un solo sitio es
 *    una cifra y dicho por cuatro que se pueden comparar es un dato.
 *
 * 3. **El estado del sistema se enseña aunque vaya todo bien.** Es la misma regla
 *    que sigue la tarjeta de salud del listado: una dependencia caída y una
 *    dependencia que responde se parecen demasiado si la única que habla es la que
 *    va mal. Aquí se enseñan las tres siempre, con su latencia y qué se rompe si
 *    falla.
 *
 * 4. **Un error del sistema y una decisión de bloquear no son lo mismo.** El rojo
 *    de este panel significa "aquí hay una persona obligada", y el `NO_PAGAR` de
 *    esta factura es el motor funcionando bien. Por eso el `NO_PAGAR` se enseña
 *    como decisión (en rojo, sí, porque bloquea un pago) y las dependencias caídas
 *    en ámbar, que es el tono que este panel usa para "hay algo que mirar".
 *
 * 5. **Lo que no cuadra se dice, no se esconde.** `entrega.coincide_con_traza` es
 *    `false`: la traza tiene 540 facturas y la entrega al ERP 500 líneas. Se
 *    explica de dónde sale la diferencia (el lote 2 no está en la entrega) en vez
 *    de omitir el campo, porque un panel que solo enseña lo que cuadra no vale
 *    para auditar nada.
 *
 * Lo que esta pantalla **no** hace: no recalcula la decisión ni mide nada por su
 * cuenta. Los hechos vienen del motor y los tiempos de `/health` y del snapshot
 * del ERP. Lo único que se calcula aquí son restas y sumas que se explican al lado.
 */

import {
    Activity,
    AlertTriangle,
    ArrowRight,
    Ban,
    Clock,
    Database,
    FileText,
    Fingerprint,
    Gauge,
    Layers,
    ListChecks,
    ListTodo,
    RefreshCw,
    Route,
    Search,
    Server,
    ShieldCheck,
    Snowflake,
    Tag,
    Timer,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
    useAnclajes,
    useDetalle,
    useFacturas,
    useFuente,
    useMeta,
    useSalud,
    useSnapshots,
} from "@/api/hooks";
import type { ResultadoPeticion } from "@/api/hooks";
import type { ConjuntoFacturas } from "@/api/datos";
import type { FiltrosVista } from "@/api/filtros";
import { RESULTADOS } from "@/api/types";
import type {
    Dependencia,
    Estadisticas,
    FacturaDetalle,
    Meta,
    Salud,
    Snapshot,
} from "@/api/types";
import { CamposCrudos, Sospechosos } from "@/components/Evidencia";
import { EtiquetaResultado } from "@/components/Etiquetas";
import { FalloDeCarga } from "@/components/Estados";
import { PanelDocumento } from "@/components/PanelDocumento";
import { PanelErp } from "@/components/PanelErp";
import { PanelHechos } from "@/components/PanelHechos";
import { ResumenFactura } from "@/components/ResumenFactura";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
    decimal,
    entero,
    euros,
    fechaHora,
    huellaCorta,
    latencia,
    porcentaje,
    SIN_DATO,
    texto,
} from "@/lib/formato";
import { escribirFiltros } from "@/lib/urlFiltros";
import { cn } from "@/lib/utils";
import {
    CLASE_CIRCUITO,
    CLASE_DEPENDENCIA_CAIDA,
    CLASE_RESULTADO,
    ETIQUETA_CIRCUITO,
    ETIQUETA_DEPENDENCIA,
    ETIQUETA_ESCALON,
    ETIQUETA_METODO,
    EXPLICACION_CIRCUITO,
    EXPLICACION_DEPENDENCIA,
} from "@/theme";

/**
 * El expediente que se sigue cuando la URL no pide otro.
 *
 * Es `2026-06-04_P006.pdf` porque es el caso que mejor enseña para qué sirve una
 * traza: su propio texto pide que se le pague y el motor se niega (ver la cabecera
 * de este fichero). Está en `PDFS_CONGELADOS`, así que funciona igual en vivo y
 * sin conexión.
 *
 * Pero **no es el único expediente que esta pantalla sabe seguir**. Cuál se sigue
 * viene de `?factura=<file_id>` (ver `expedienteDeUrl`), y sin parámetro se abre
 * este. Antes estaba clavado a fuego y la página argumentaba que era "un documento
 * sobre un caso, no un visor de expedientes". Eso dejaba la trazabilidad en una
 * factura de 540, que es justo lo contrario de lo que tiene que demostrar un panel
 * de trazabilidad: que se puede seguir cualquiera. El caso ideal sigue siendo el
 * que se abre solo, pero deja de ser el único.
 */
const EXPEDIENTE_POR_DEFECTO = "2026-06-04_P006.pdf";

/** El nombre del parámetro de la URL con el que se elige el expediente. */
const CLAVE_EXPEDIENTE = "factura";

/**
 * Un `file_id` de la URL, o `null` si no hay ninguno utilizable.
 *
 * **No se valida contra el listado**, y es a propósito. La dirección la escribe
 * cualquiera, y lo honesto cuando llega un `file_id` que no existe no es caer al
 * de por defecto en silencio —eso enseñaría P006 con un enlace que dice otra
 * cosa— sino intentar leerlo y que la pantalla diga cuál no ha podido abrir. Eso
 * ya lo hace `useDetalle` con `FalloDeCarga`.
 *
 * Lo único que se comprueba es que la cadena sea razonable: que no venga vacía y
 * que no sea un párrafo. Un `?factura=` con 2000 caracteres no es una factura, es
 * una dirección mal formada, y no merece ni una vuelta de red.
 */
function expedienteDeUrl(params: URLSearchParams): string | null {
    const bruto = params.get(CLAVE_EXPEDIENTE);
    if (bruto === null) return null;
    const limpio = bruto.trim();
    if (limpio === "" || limpio.length > 200) return null;
    return limpio;
}

/** Los anclajes: la cadena de la plataforma, y luego el hilo de un expediente. */
const ANCLAS = [
    { id: "cadena", etiqueta: "La cadena" },
    { id: "decision", etiqueta: "La decisión" },
    { id: "evidencia", etiqueta: "Evidencia" },
    { id: "versiones", etiqueta: "Versiones" },
    { id: "latencia", etiqueta: "Latencia" },
    { id: "estado", etiqueta: "Estado" },
    { id: "errores", etiqueta: "Errores" },
    { id: "reintentos", etiqueta: "Reintentos" },
    { id: "pendiente", etiqueta: "Trabajo pendiente" },
];

/**
 * Los tres reintentos que sabe hacer el cliente del ERP.
 *
 * Las claves son del cliente (`ora_00600`) y las frases, de
 * `maisa/traces/trazabilidad.md`. Se escriben aquí porque el panel de la tabla ya
 * traduce las claves (`PanelErp`) y esta tabla necesita además **qué hace el
 * cliente con cada una**, que allí no cabe.
 */
const REINTENTOS = [
    {
        clave: "ora_00600",
        titulo: "ORA-00600, error interno de Oracle",
        politica: "Reintento con espera creciente, hasta 3 veces.",
        porque:
            "Es un fallo transitorio del ERP: la misma consulta suele funcionar al segundo intento.",
    },
    {
        clave: "ses_401",
        titulo: "Sesión caducada (401)",
        politica: "Vuelve a identificarse y reintenta una vez.",
        porque:
            "No es un fallo del ERP, es la sesión: reintentar sin renovarla daría otro 401.",
    },
    {
        clave: "erp_429",
        titulo: "Límite del ERP (429)",
        politica: "Espera lo que diga la cabecera `Retry-After`.",
        porque: "Insistir antes de tiempo empeora el límite. Se espera y se vuelve.",
    },
] as const;

export default function TrazabilidadPage() {
    const { estado: fuente } = useFuente();
    const [params] = useSearchParams();
    const seguida = expedienteDeUrl(params) ?? EXPEDIENTE_POR_DEFECTO;
    const detalle = useDetalle(seguida);
    const salud = useSalud();
    const meta = useMeta();
    const snapshots = useSnapshots();

    // El listado entero (los 540) se pide aunque esta pantalla no enseñe la tabla:
    // es lo que alimenta el selector de expediente y lo que permite decir cuántos
    // hay sin fiarse solo de los contadores. En vivo son dos vueltas de paginación
    // (el máximo por página es 500), las mismas que hace `/facturas`.
    const catalogo = useFacturas({});

    const contadores = fuente?.estadisticas ?? null;

    // `usePeticion` no vacía `datos` al cambiar de clave: al elegir otro
    // expediente se sigue viendo el anterior hasta que llega el nuevo. Sin esta
    // comprobación la cabecera diría un `file_id` y el cuerpo enseñaría otro, que
    // en un panel de auditoría es el peor fallo posible —los datos de una factura
    // con el nombre de otra—. Mientras no coincidan, el hilo se queda en esqueleto.
    const factura = detalle.datos?.file_id === seguida ? detalle.datos : null;

    // Un expediente que no se puede abrir **ya no tumba la página**: la cadena de
    // la plataforma no depende de él, y esconderla por una factura mala sería
    // perder la vista de conjunto justo cuando algo va mal. El fallo se enseña en
    // el sitio del hilo, y las secciones que no dependen de la factura siguen.
    return (
        <div className="space-y-10">
            <Cabecera seguida={seguida} factura={factura} catalogo={catalogo} />

            <Cadena
                contadores={contadores}
                meta={meta.datos}
                salud={salud.datos}
                snapshots={snapshots.datos}
                catalogo={catalogo}
            />

            {!factura ? (
                detalle.error ? (
                    <Seccion
                        id="decision"
                        icono={Route}
                        titulo="El expediente que se sigue"
                        descripcion="Lo que no se ha podido abrir. La cadena de arriba no depende de este fichero."
                    >
                        <FalloDeCarga
                            error={detalle.error}
                            alReintentar={detalle.reintentar}
                            queEs={`el expediente de ${seguida}`}
                        />
                    </Seccion>
                ) : (
                    <Esqueleto />
                )
            ) : (
                <>
                    <Decision factura={factura} />

                    <Evidencia factura={factura} meta={meta.datos} />

                    <Versiones
                        factura={factura}
                        meta={meta.datos}
                        salud={salud.datos}
                        snapshots={snapshots.datos}
                    />

                    <Latencia
                        factura={factura}
                        salud={salud.datos}
                        snapshots={snapshots.datos}
                        contadores={contadores}
                    />

                    <Estado
                        salud={salud.datos}
                        error={salud.error}
                        reintentar={salud.reintentar}
                        factura={factura}
                    />

                    <Errores
                        salud={salud.datos}
                        meta={meta.datos}
                        contadores={contadores}
                        factura={factura}
                    />

                    <Reintentos
                        snapshots={snapshots.datos}
                        cargando={snapshots.cargando}
                        error={snapshots.error}
                    />

                    <Pendiente contadores={contadores} meta={meta.datos} factura={factura} />
                </>
            )}
        </div>
    );
}

/* ------------------------------------------------------------------ cabecera */

function Cabecera({
    seguida,
    factura,
    catalogo,
}: {
    seguida: string;
    factura: FacturaDetalle | null;
    catalogo: ResultadoPeticion<ConjuntoFacturas>;
}) {
    return (
        <header className="space-y-4">
            <div className="space-y-2">
                <h1 className="font-heading text-2xl font-semibold tracking-tight sm:text-3xl">
                    Trazabilidad y observabilidad
                </h1>
                <p className="max-w-3xl text-sm text-muted-foreground">
                    Primero la plataforma entera: las 540 facturas de la traza contadas en cada
                    fase del pipeline y el cuadre entre las cuatro respuestas que dicen cuántas
                    hay. Después una de ellas, seguida de punta a punta —el documento, lo que se
                    leyó de él, las reglas que se evaluaron, la decisión que salió y el asiento con
                    el que se comparó—, con la huella del fichero, las versiones con las que se
                    decidió, cuánto tardó cada paso, qué está caído y qué queda por hacer.
                </p>
                <p className="max-w-3xl text-sm text-muted-foreground">
                    {factura ? (
                        <>
                            La que se está siguiendo es{" "}
                            <Link
                                to={`/facturas/${encodeURIComponent(factura.file_id)}`}
                                className="font-mono text-foreground underline decoration-dotted underline-offset-2"
                            >
                                {factura.file_id}
                            </Link>
                            {esCasoIdeal(factura.file_id) ? (
                                <>
                                    , y no es un caso cualquiera: su texto pide que se le pague y el
                                    motor se negó a obedecer. Está más abajo, con las palabras
                                    exactas.
                                </>
                            ) : (
                                <> — cualquiera de las 540 se puede seguir aquí.</>
                            )}
                        </>
                    ) : (
                        <>
                            Se está intentando abrir{" "}
                            <span className="font-mono text-foreground">{seguida}</span>. El hilo de
                            ese expediente no está disponible ahora mismo; la cadena de la
                            plataforma, que no depende de él, sí.
                        </>
                    )}
                </p>
            </div>

            <SelectorExpediente catalogo={catalogo} seguida={seguida} />

            <nav aria-label="Secciones de la página" className="flex flex-wrap gap-2">
                {ANCLAS.map((ancla) => (
                    <a
                        key={ancla.id}
                        href={`#${ancla.id}`}
                        className="rounded-full bg-muted px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                    >
                        {ancla.etiqueta}
                    </a>
                ))}
            </nav>

            <Procedencia />
        </header>
    );
}

/**
 * Los expedientes que se ofrecen a un clic, para no tener que saberse un nombre.
 *
 * Uno de cada `resultado` y uno de cada `metodo_lectura`, porque son las dos
 * preguntas que se hacen al abrir esta pantalla: "¿cómo se ve un `ESCALAR`?" y
 * "¿cómo se ve un documento que hubo que leer con visión?". Están escritos a mano
 * y no se eligen por popularidad porque el interés de cada uno es distinto y no se
 * puede deducir de los contadores: `copia_2026_0518.pdf` es un escaneo sin capa de
 * texto, `scan_002.pdf` es un documento del que no se pudo leer nada.
 *
 * Todos están en el congelado, así que las sugerencias también funcionan sin API.
 */
const SUGERENCIAS: { file_id: string; porQue: string }[] = [
    { file_id: "2026-06-04_P006.pdf", porQue: "el caso ideal: el papel pide pagar y el motor no" },
    { file_id: "2026-01-08_P001.pdf", porQue: "un PAGAR limpio, sin nada que mirar" },
    { file_id: "2026-03-28_P002.pdf", porQue: "un NO_PAGAR por la regla dura" },
    { file_id: "2026-0233-A_catering.pdf", porQue: "un ESCALAR: hay una persona obligada" },
    { file_id: "FA-2508_consultoría.pdf", porQue: "un file_id con acento, que hay que codificar" },
    { file_id: "copia_2026_0518.pdf", porQue: "leído con visión OCR, sin capa de texto" },
    { file_id: "scan_002.pdf", porQue: "un documento ilegible, que acaba en escalado" },
];

/** Si este `file_id` es el caso que la cabecera de este fichero explica. */
function esCasoIdeal(fileId: string): boolean {
    return fileId === EXPEDIENTE_POR_DEFECTO;
}

/**
 * Elegir qué expediente se sigue.
 *
 * Dos maneras, y las dos escriben en la misma dirección (`?factura=`): buscando
 * sobre los 540, que es lo que se hace cuando se viene con un nombre concreto, y
 * a un clic sobre `SUGERENCIAS`, que es lo que se hace cuando se viene a ver cómo
 * se comporta el motor. La búsqueda se hace **en el cliente** sobre el listado que
 * ya está cargado y no contra la API: el listado entero cabe en memoria y filtrar
 * en local responde en el mismo fotograma, mientras que preguntar al servidor por
 * cada tecla enseña resultados de búsquedas que ya no están en la caja.
 *
 * El `setTimeout` de 200 ms no es para ahorrar red —aquí no hay red— sino para no
 * recorrer 540 elementos en cada pulsación cuando se escribe rápido.
 */
function SelectorExpediente({
    catalogo,
    seguida,
}: {
    catalogo: ResultadoPeticion<ConjuntoFacturas>;
    seguida: string;
}) {
    const [consulta, setConsulta] = useState("");
    const [retrasada, setRetrasada] = useState("");

    useEffect(() => {
        const t = setTimeout(() => setRetrasada(consulta.trim().toLowerCase()), 200);
        return () => clearTimeout(t);
    }, [consulta]);

    const items = catalogo.datos?.items ?? [];

    const hallazgos = useMemo(() => {
        if (retrasada.length < 2) return [];
        return items
            .filter((f) => {
                const heno = [f.file_id, f.proveedor, f.pedido, f.nif]
                    .filter((v): v is string => typeof v === "string")
                    .join(" ")
                    .toLowerCase();
                return heno.includes(retrasada);
            })
            .slice(0, 8);
    }, [items, retrasada]);

    return (
        <div className="space-y-3 rounded-lg border bg-card p-3">
            <div className="flex flex-wrap items-center gap-2">
                <label
                    htmlFor="buscador-expediente"
                    className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
                >
                    <Search className="size-3.5" />
                    Seguir otro expediente
                </label>
                <input
                    id="buscador-expediente"
                    type="search"
                    value={consulta}
                    onChange={(evento) => setConsulta(evento.target.value)}
                    placeholder="file_id, proveedor, pedido o NIF…"
                    className="h-8 min-w-56 flex-1 rounded-md border bg-background px-2.5 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
                />
                <span className="text-xs text-muted-foreground">
                    {catalogo.cargando
                        ? "cargando los 540…"
                        : catalogo.error
                          ? "no se ha podido leer el listado"
                          : `${entero(items.length)} expedientes`}
                </span>
            </div>

            {hallazgos.length > 0 ? (
                <ul className="flex flex-wrap gap-1.5">
                    {hallazgos.map((f) => (
                        <li key={f.file_id}>
                            <Link
                                to={direccionExpediente(f.file_id)}
                                className={cn(
                                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors hover:bg-muted",
                                    f.file_id === seguida && "border-foreground/40 bg-muted",
                                )}
                            >
                                <span
                                    aria-hidden
                                    className={cn(
                                        "size-1.5 rounded-full",
                                        CLASE_RESULTADO[f.resultado],
                                    )}
                                />
                                <span className="font-mono">{f.file_id}</span>
                                {f.proveedor ? (
                                    <span className="text-muted-foreground">{f.proveedor}</span>
                                ) : null}
                            </Link>
                        </li>
                    ))}
                </ul>
            ) : retrasada.length >= 2 && !catalogo.cargando ? (
                <p className="text-xs text-muted-foreground">
                    Ninguno de los {entero(items.length)} expedientes cargados coincide con{" "}
                    <span className="font-mono">{retrasada}</span>. Se busca por nombre de fichero,
                    proveedor, pedido y NIF, y hacen falta dos letras.
                </p>
            ) : null}

            <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-muted-foreground">Casos que enseñan algo:</span>
                {SUGERENCIAS.map((s) => (
                    <Tooltip key={s.file_id}>
                        <TooltipTrigger asChild>
                            <Link
                                to={direccionExpediente(s.file_id)}
                                className={cn(
                                    "rounded-full bg-muted px-2.5 py-1 font-mono text-xs text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground",
                                    s.file_id === seguida && "bg-foreground/10 text-foreground",
                                )}
                            >
                                {s.file_id}
                            </Link>
                        </TooltipTrigger>
                        <TooltipContent>{s.porQue}</TooltipContent>
                    </Tooltip>
                ))}
            </div>

            <p className="text-xs text-muted-foreground">
                El expediente va en la dirección —{" "}
                <span className="font-mono">/trazabilidad?factura=&lt;file_id&gt;</span>— y no en el
                estado del componente: así una traza se puede enlazar, mandar por correo y citar en
                un informe. Los <span className="font-mono">file_id</span> pueden llevar acentos y
                espacios ({SUGERENCIAS[4].file_id}), así que van codificados.
            </p>
        </div>
    );
}

/** La dirección de esta misma pantalla con otro expediente. */
function direccionExpediente(fileId: string): string {
    return `/trazabilidad?${CLAVE_EXPEDIENTE}=${encodeURIComponent(fileId)}`;
}

/**
 * De dónde salen los datos que se están viendo.
 *
 * Es la misma información que el distintivo de la cabecera, pero con la fecha del
 * congelado escrita. Aquí importa más que en ninguna otra pantalla, porque **un
 * estado congelado es una foto y no un latido**: si se enseña una latencia de hace
 * tres días sin decir que es de hace tres días, el panel está mintiendo con un dato
 * que parece vivo.
 */
function Procedencia() {
    const { estado, cargando } = useFuente();

    if (cargando || !estado) return <Skeleton className="h-5 w-96" />;

    if (estado.fuente === "vivo") {
        return (
            <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <Activity className="size-3.5 text-emerald-600 dark:text-emerald-400" />
                Todo lo de esta página se está leyendo{" "}
                <strong className="font-medium">en vivo</strong> de{" "}
                <span className="font-mono break-all">{estado.config.api}</span>: el estado, las
                versiones y los tiempos son los de ahora mismo.
            </p>
        );
    }

    return (
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <Snowflake className="size-3.5 text-sky-600 dark:text-sky-400" />
            Esto es el <strong className="font-medium">congelado</strong>
            {estado.manifiesto ? (
                <>
                    , una foto tomada el{" "}
                    <strong className="font-medium">
                        {fechaHora(estado.manifiesto.congelado_en)}
                    </strong>{" "}
                    de <span className="font-mono break-all">{estado.manifiesto.origen}</span>
                </>
            ) : null}
            .{" "}
            <span className="text-amber-700 dark:text-amber-300">
                Las latencias y el estado son los de ese momento, no los de ahora.
            </span>{" "}
            {estado.motivo ? <span>Motivo: {estado.motivo}</span> : null}
        </p>
    );
}

/* --------------------------------------------------------------- la cadena */

/**
 * La plataforma entera, contada por fases, y el cuadre de ese recuento.
 *
 * Esta sección existe porque una traza de una sola factura no demuestra nada: si
 * solo se enseña `2026-06-04_P006.pdf` de punta a punta, se ve que *esa* se puede
 * seguir, no que la plataforma se pueda seguir. Lo que hay que poder decir es
 * cuántas facturas han entrado, cuántas han salido, por dónde se ha ido cada una y
 * si las cuentas de los sitios que las cuentan coinciden. Eso es lo primero de la
 * página, y el expediente concreto es lo segundo.
 *
 * Tres cosas que hace y que no son pintar números:
 *
 * 1. **Cuadra el 540 contra sí mismo.** La misma cifra la dicen cuatro sitios
 *    distintos —`/health`, `/api/meta`, el `total` de `/api/estadisticas`, la suma
 *    de los tres resultados— y una quinta la produce esta pantalla al contar las
 *    filas que ha descargado. Si dos no coinciden, el panel lo dice en vez de
 *    enseñar el número del primero que llegó. Un contador que no se comprueba es
 *    una promesa; uno que se comprueba contra cuatro respuestas independientes es
 *    una medida.
 *
 * 2. **Enseña la resta de la entrega.** `entrega.coincide_con_traza` es `false` y
 *    tiene que seguir siéndolo: 540 en la traza, 500 en la entrega. La sección
 *    explica de dónde salen los 40 (el lote 2 no se entrega) en vez de taparlo,
 *    porque ese hueco es justo lo que un panel de trazabilidad tiene que saber
 *    enseñar.
 *
 * 3. **No espera al expediente.** Se pinta aunque la factura que se está siguiendo
 *    todavía esté cargando, o aunque no se pueda abrir. La vista de conjunto no
 *    depende de un fichero.
 *
 * Se degrada en vez de romperse: si `/health` o `/api/meta` no contestan, los
 * datos que falten salen como `—` y su comprobación como no comprobable. Un panel
 * que se cae cuando una de sus cinco fuentes falla es un panel que falla cinco
 * veces más que el sistema que vigila.
 */
function Cadena({
    contadores,
    meta,
    salud,
    snapshots,
    catalogo,
}: {
    contadores: Estadisticas | null;
    meta: Meta | null;
    salud: Salud | null;
    snapshots: Snapshot[] | null;
    catalogo: ResultadoPeticion<ConjuntoFacturas>;
}) {
    const total = contadores?.total ?? null;
    const porResultado = contadores?.por_resultado ?? null;

    // La suma de los tres resultados **se calcula aquí**, no se lee: es lo que
    // convierte el `total` en un dato comprobado. Si el motor dejara de contar una
    // factura, el total seguiría diciendo 540 y esta suma diría 539.
    const sumaResultados = porResultado
        ? RESULTADOS.reduce((suma, resultado) => suma + porResultado[resultado], 0)
        : null;

    const metodos = contadores?.por_metodo_lectura ?? null;
    const texto = metodos?.texto_determinista ?? null;
    const vision = metodos?.vision_ocr ?? null;
    // Cualquier método que no sea uno de los dos conocidos. Hoy no hay ninguno, y
    // por eso se cuenta aparte en vez de repartirlo: si el motor estrena un tercer
    // camino de lectura, la barra de abajo dejaría de sumar 100 % y se vería.
    const otrosMetodos = metodos
        ? Object.entries(metodos).reduce(
              (suma, [clave, valor]) =>
                  clave === "texto_determinista" || clave === "vision_ocr" ? suma : suma + valor,
              0,
          )
        : null;

    const entregadas = contadores?.entrega.total ?? null;
    const pendientes = contadores?.pendientes_revision ?? null;
    const enListado = catalogo.datos?.items.length ?? null;

    const enTrazaSalud = salud?.datos.traza.facturas ?? null;
    const enTrazaMeta = meta?.motor.facturas_en_traza ?? null;
    const rotasSalud = salud?.datos.traza.lineas_invalidas ?? null;
    const rotasMeta = meta?.motor.lineas_invalidas ?? null;
    const versiones = meta ? Object.keys(meta.motor.versiones_norma) : null;

    const leidas = (texto ?? 0) + (vision ?? 0) + (otrosMetodos ?? 0);
    const pctTexto = leidas > 0 && texto !== null ? (texto / leidas) * 100 : 0;
    const pctVision = leidas > 0 && vision !== null ? (vision / leidas) * 100 : 0;

    return (
        <Seccion
            id="cadena"
            icono={Layers}
            titulo="La cadena de la plataforma"
            descripcion="Las 540 facturas de la traza, contadas en cada fase del pipeline, y el cuadre de esa cifra contra las cuatro respuestas que la dicen. Debajo, una de ellas abierta de punta a punta."
        >
            <div className="space-y-4">
                <Card>
                    <CardContent className="space-y-4">
                        <div className="flex flex-wrap items-stretch gap-2">
                            <Fase
                                icono={Database}
                                etiqueta="Leídas"
                                valor={entero(total)}
                                pie="una línea por factura en la traza"
                            />
                            <Paso />
                            <Fase
                                icono={FileText}
                                etiqueta="Interpretadas"
                                valor={entero(
                                    texto === null && vision === null && otrosMetodos === null
                                        ? null
                                        : leidas,
                                )}
                                pie={`texto ${entero(texto)} · visión ${entero(vision)}`}
                            />
                            <Paso />
                            <Fase
                                icono={Route}
                                etiqueta="Decididas"
                                valor={entero(total)}
                                pie={
                                    porResultado
                                        ? `${entero(porResultado.PAGAR)} pagar · ${entero(porResultado.NO_PAGAR)} no pagar · ${entero(porResultado.ESCALAR)} escalar`
                                        : "sin desglose por resultado"
                                }
                            />
                            <Paso />
                            <Fase
                                icono={Server}
                                etiqueta="Entregadas"
                                valor={entero(entregadas)}
                                pie={
                                    total !== null && entregadas !== null
                                        ? `${entero(entregadas)} de ${entero(total)} al ERP`
                                        : "sin dato de entrega"
                                }
                                aviso={total !== null && entregadas !== null && entregadas !== total}
                            />
                        </div>

                        <div className="space-y-1.5">
                            <p className="text-xs text-muted-foreground">
                                Cómo se leyó cada una:{" "}
                                <strong className="font-medium text-foreground">
                                    {entero(texto)}
                                </strong>{" "}
                                sin salir del texto del PDF,{" "}
                                <strong className="font-medium text-foreground">
                                    {entero(vision)}
                                </strong>{" "}
                                con visión sobre el escaneo
                                {otrosMetodos ? (
                                    <>
                                        , <strong className="font-medium text-foreground">
                                            {entero(otrosMetodos)}
                                        </strong>{" "}
                                        por otros caminos
                                    </>
                                ) : null}
                                .
                            </p>
                            <div className="flex h-1 w-full gap-0.5 overflow-hidden rounded-full">
                                <span
                                    aria-hidden
                                    className="bg-sky-500 dark:bg-sky-400"
                                    style={{ width: `${pctTexto}%` }}
                                />
                                <span
                                    aria-hidden
                                    className="bg-amber-500 dark:bg-amber-400"
                                    style={{ width: `${pctVision}%` }}
                                />
                            </div>
                            <p className="text-xs text-muted-foreground">
                                La visión es 30 de 540 —un 5,6 %— y por eso importa: es la parte
                                del lote donde un error de lectura se convierte en una decisión
                                equivocada sin que nadie lo vea. Se enseña aparte, y no diluida en
                                un total de lecturas, porque el total no dice qué leer mirar.
                            </p>
                        </div>
                    </CardContent>
                </Card>

                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                    <Tarjeta
                        titulo="Trabajo pendiente"
                        valor={entero(pendientes)}
                        pie="escaladas que siguen sin resolverse en el ERP"
                    />
                    <Tarjeta
                        titulo="Líneas rotas de la traza"
                        valor={rotasSalud === null && rotasMeta === null ? SIN_DATO : entero(Math.max(rotasSalud ?? 0, rotasMeta ?? 0))}
                        pie="lo que el motor no ha podido interpretar"
                    />
                    <Tarjeta
                        titulo="Versiones de la norma"
                        valor={versiones === null ? SIN_DATO : entero(versiones.length)}
                        pie={
                            meta
                                ? Object.entries(meta.motor.versiones_norma)
                                      .map(([norma, cuantas]) => `${norma}: ${entero(cuantas)}`)
                                      .join(" · ")
                                : "sin dato del motor"
                        }
                    />
                </div>

                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-sm">
                            <ShieldCheck className="size-4 text-muted-foreground" />
                            El 540, comprobado contra cuatro respuestas
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <p className="text-sm text-muted-foreground">
                            La misma cifra la dicen cuatro sitios distintos. Aquí se enfrentan
                            entre sí: si dos no coinciden, hay una factura que un servicio cuenta y
                            otro no, y eso es exactamente lo que un panel de trazabilidad tiene que
                            enseñar antes de que lo encuentre una auditoría.
                        </p>
                        <ul className="space-y-2">
                            <Comprobacion
                                bien={cuadra(sumaResultados, total)}
                                etiqueta="Los tres resultados suman el total"
                                detalle={
                                    sumaResultados === null || total === null
                                        ? `no comprobable: ${SIN_DATO}`
                                        : `${RESULTADOS.map((r) => `${r} ${entero(porResultado?.[r] ?? null)}`).join(" + ")} = ${entero(sumaResultados)} contra un total de ${entero(total)}`
                                }
                            />
                            <Comprobacion
                                bien={cuadra(enTrazaSalud, total)}
                                etiqueta="/health cuenta las mismas"
                                detalle={
                                    enTrazaSalud === null
                                        ? `no comprobable: ${SIN_DATO}`
                                        : `datos.traza.facturas = ${entero(enTrazaSalud)}`
                                }
                            />
                            <Comprobacion
                                bien={cuadra(enTrazaMeta, total)}
                                etiqueta="/api/meta cuenta las mismas"
                                detalle={
                                    enTrazaMeta === null
                                        ? `no comprobable: ${SIN_DATO}`
                                        : `motor.facturas_en_traza = ${entero(enTrazaMeta)}`
                                }
                            />
                            <Comprobacion
                                bien={cuadra(enListado, total)}
                                etiqueta="Las filas descargadas son las mismas"
                                detalle={
                                    enListado === null
                                        ? catalogo.cargando
                                            ? "cargando el listado…"
                                            : `no comprobable: ${SIN_DATO}`
                                        : `el listado que sostiene el selector trae ${entero(enListado)} facturas`
                                }
                            />
                            <Comprobacion
                                bien={
                                    rotasSalud === null && rotasMeta === null
                                        ? null
                                        : (rotasSalud ?? 0) === 0 && (rotasMeta ?? 0) === 0
                                }
                                etiqueta="Ninguna línea de la traza está rota"
                                detalle={
                                    rotasSalud === null && rotasMeta === null
                                        ? `no comprobable: ${SIN_DATO}`
                                        : `/health dice ${entero(rotasSalud)} y /api/meta ${entero(rotasMeta)}`
                                }
                            />
                            <Comprobacion
                                bien={
                                    contadores
                                        ? contadores.resultados_desconocidos === 0
                                        : null
                                }
                                etiqueta="Ningún resultado fuera del contrato"
                                detalle={
                                    contadores
                                        ? `${entero(contadores.resultados_desconocidos)} facturas con un resultado que el panel no sabe nombrar (el contrato son ${RESULTADOS.join(", ")})`
                                        : `no comprobable: ${SIN_DATO}`
                                }
                            />
                            <Comprobacion
                                bien={contadores ? contadores.entrega.lineas_invalidas === 0 : null}
                                etiqueta="La entrega no trae líneas rotas"
                                detalle={
                                    contadores
                                        ? `${entero(contadores.entrega.lineas_invalidas)} líneas del fichero de entrega sin interpretar`
                                        : `no comprobable: ${SIN_DATO}`
                                }
                            />
                        </ul>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-sm">
                            <AlertTriangle className="size-4 text-amber-600 dark:text-amber-400" />
                            Lo que no cuadra, dicho
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <p className="text-sm text-muted-foreground">
                            {contadores
                                ? `La traza tiene ${entero(total)} facturas y la entrega al ERP ${entero(entregadas)} líneas, y la API lo dice ella misma: `
                                : "La API publica si la entrega cuadra con la traza, y aquí no se puede comprobar: "}
                            {contadores ? (
                                <span className="font-mono text-xs text-foreground">
                                    entrega.coincide_con_traza = {String(contadores.entrega.coincide_con_traza)}
                                </span>
                            ) : (
                                SIN_DATO
                            )}
                            . No es un fallo, es una diferencia con explicación:
                        </p>
                        <ul className="space-y-2">
                            <Comprobacion
                                bien={false}
                                etiqueta="Faltan 40 facturas en la entrega"
                                detalle={
                                    contadores
                                        ? `por lote, la traza son ${Object.entries(contadores.por_lote)
                                              .map(([lote, cuantas]) => `lote ${lote}: ${entero(cuantas)}`)
                                              .join(" y ")}. El lote 2 no entra en la entrega, así que ${entero(total)} − ${entero(contadores.por_lote["2"] ?? null)} = ${entero(entregadas)}.`
                                        : `no comprobable: ${SIN_DATO}`
                                }
                            />
                        </ul>
                        <div className="flex flex-wrap items-center gap-2">
                            <BotonFiltro filtros={{ lote: 2 }}>
                                Ver las {entero(contadores?.por_lote["2"] ?? null)} del lote 2
                            </BotonFiltro>
                            <BotonFiltro filtros={{ resultado: "ESCALAR" }}>
                                Ver las {entero(pendientes)} escaladas
                            </BotonFiltro>
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-sm">
                            <Gauge className="size-4 text-muted-foreground" />
                            Vivo contra congelado
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <p className="text-sm text-muted-foreground">
                            Estas cifras se leen en vivo de la API o del congelado, según lo que
                            haya contestado <span className="font-mono text-xs">/health</span>. Los
                            dos tienen que decir lo mismo; cuando no, la diferencia está en el
                            tiempo, no en el número: el congelado es una foto y puede ser de una
                            traza con menos facturas.
                        </p>
                        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                            <DatoLinea
                                etiqueta="Listado descargado por esta pantalla"
                                valor={entero(enListado)}
                            />
                            <DatoLinea
                                etiqueta="Traza según /health"
                                valor={entero(enTrazaSalud)}
                            />
                            <DatoLinea
                                etiqueta="Traza según /api/meta"
                                valor={entero(enTrazaMeta)}
                            />
                            <DatoLinea
                                etiqueta="Líneas de traza (ficheros)"
                                valor={entero(meta?.configuracion.datos.traza_paths.length ?? null)}
                                mono
                            />
                            <DatoLinea
                                etiqueta="Límite de paginación de la API"
                                valor={
                                    meta
                                        ? `${entero(meta.configuracion.api.limite_paginacion.por_defecto)} por defecto, ${entero(meta.configuracion.api.limite_paginacion.maximo)} como máximo`
                                        : SIN_DATO
                                }
                            />
                            <DatoLinea
                                etiqueta="Descargas del ERP registradas"
                                valor={entero(snapshots?.length ?? null)}
                            />
                            <DatoLinea
                                etiqueta="Asientos vigentes"
                                valor={entero(contadores?.asientos_vigentes ?? null)}
                            />
                            <DatoLinea
                                etiqueta="Mongo responde"
                                valor={
                                    contadores
                                        ? contadores.mongo.ok
                                            ? "sí"
                                            : `no${contadores.mongo.error ? `: ${contadores.mongo.error}` : ""}`
                                        : SIN_DATO
                                }
                            />
                        </dl>
                        <p className="text-xs text-muted-foreground">
                            La API devuelve como máximo{" "}
                            <span className="font-mono">
                                {entero(meta?.configuracion.api.limite_paginacion.maximo ?? null)}
                            </span>{" "}
                            facturas por vuelta y la traza tiene {entero(total)}, así que en vivo el
                            listado se trae en varias vueltas encadenadas; en congelado se lee el
                            fichero entero de una vez. En los dos casos el contador de arriba es{" "}
                            <strong className="font-medium text-foreground">
                                las filas que esta pantalla tiene cargadas
                            </strong>
                            , no el <span className="font-mono">total</span> que anuncia la primera
                            respuesta —que es justo lo que se está comprobando.
                        </p>
                    </CardContent>
                </Card>
            </div>
        </Seccion>
    );
}

/** Un tramo de la cadena: un número, su nombre y de dónde sale. */
function Fase({
    icono: Icono,
    etiqueta,
    valor,
    pie,
    aviso,
}: {
    icono: typeof Activity;
    etiqueta: string;
    valor: string;
    pie: string;
    aviso?: boolean;
}) {
    return (
        <div className="min-w-40 flex-1 rounded-lg border bg-card px-3 py-2">
            <span className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                <Icono className="size-3.5" />
                {etiqueta}
            </span>
            <span className="font-heading text-2xl leading-none font-semibold tabular-nums">
                {valor}
            </span>
            <span
                className={cn(
                    "block text-xs",
                    aviso ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground",
                )}
            >
                {pie}
            </span>
        </div>
    );
}

/** La flecha entre dos tramos de la cadena. Decorativa: el orden lo da el texto. */
function Paso() {
    return (
        <span aria-hidden className="flex items-center justify-center">
            <ArrowRight className="size-4 text-muted-foreground" />
        </span>
    );
}

/**
 * Si dos recuentos coinciden, o `null` si alguno falta.
 *
 * `null` no es `false`: "no cuadra" y "no lo he podido comprobar" se enseñan
 * distinto, porque uno es un hallazgo y el otro una limitación.
 */
function cuadra(a: number | null, b: number | null): boolean | null {
    if (a === null || b === null) return null;
    return a === b;
}

/* ------------------------------------------------------------------ decisión */

/**
 * El hilo: del PDF al asiento, en cinco pasos.
 *
 * Va antes de cualquier explicación porque es la respuesta a "¿qué pasó con esta
 * factura?", y esa pregunta se contesta mejor en orden que en prosa. Cada paso dice
 * de dónde sale su dato; ninguno se calcula aquí.
 */
function Decision({ factura }: { factura: FacturaDetalle }) {
    const resumen = factura.resumen;
    const bloquean = factura.hechos.filter((hecho) => hecho.duro && !hecho.ok);
    const avisan = factura.hechos.filter((hecho) => !hecho.ok && !hecho.duro);
    const cumplen = factura.hechos.length - bloquean.length - avisan.length;

    const pasos = [
        {
            titulo: "El documento",
            dato: factura.file_id,
            pie: `${texto(resumen.proveedor)} · ${euros(resumen.total)} · huella ${huellaCorta(factura.sha256)}`,
            Icono: FileText,
        },
        {
            titulo: "La lectura",
            dato: ETIQUETA_METODO[factura.lectura.metodo_lectura] ?? factura.lectura.metodo_lectura,
            pie: `${ETIQUETA_ESCALON[factura.lectura.escalon_lectura] ?? factura.lectura.escalon_lectura} · confianza ${porcentaje(factura.lectura.calidad_lectura)} · ${latencia(factura.lectura.segundos_lectura)}`,
            Icono: Gauge,
        },
        {
            titulo: "Las reglas",
            dato: `${entero(factura.hechos.length)} evaluadas`,
            pie: `${entero(bloquean.length)} bloquean · ${entero(avisan.length)} avisan · ${entero(cumplen)} cumplen`,
            Icono: ListChecks,
        },
        {
            titulo: "La decisión",
            dato: factura.resultado,
            pie: factura.motivo_principal ?? SIN_DATO,
            Icono: Ban,
        },
        {
            titulo: "El asiento del ERP",
            dato: texto(resumen.pedido),
            pie: `asiento ${texto(resumen.asiento)} · estado ${texto(resumen.estado_erp)}`,
            Icono: Database,
        },
    ];

    return (
        <Seccion
            id="decision"
            icono={Route}
            titulo="La decisión, paso a paso"
            descripcion={`De dónde salió este ${factura.resultado}. Cada paso dice qué dato usó; ninguno se calcula en esta pantalla.`}
        >
            <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
                {pasos.map((paso, indice) => (
                    <li key={paso.titulo}>
                        <Card size="sm" className="h-full gap-0">
                            <CardContent className="flex h-full flex-col gap-1">
                                <span className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                                    <paso.Icono className="size-3.5" />
                                    {indice + 1}. {paso.titulo}
                                </span>
                                <span className="font-heading text-lg leading-tight font-semibold break-all">
                                    {paso.dato}
                                </span>
                                <span className="text-xs text-muted-foreground">{paso.pie}</span>
                            </CardContent>
                        </Card>
                    </li>
                ))}
            </ol>

            <Intercambio factura={factura} />

            <ResumenFactura detalle={factura} />

            <p className="text-xs text-muted-foreground">
                El expediente completo, con el PDF al lado y la lectura cruda, está en{" "}
                <Link
                    to={`/facturas/${encodeURIComponent(factura.file_id)}`}
                    className="text-foreground underline decoration-dotted underline-offset-2"
                >
                    la ficha de la factura
                </Link>
                . Aquí se sigue el hilo; allí se mira el documento.
            </p>
        </Seccion>
    );
}

/**
 * Lo que el documento pidió, al lado de lo que hizo el motor.
 *
 * Es el corazón de la página, y por eso va en dos columnas enfrentadas en vez de
 * repartido entre las secciones: la frase del PDF y la respuesta del motor solo
 * significan algo juntas. Separadas, la primera parece una nota comercial y la
 * segunda un hecho más de la lista.
 */
function Intercambio({ factura }: { factura: FacturaDetalle }) {
    const nota =
        typeof factura.campos.nota_documento === "string" ? factura.campos.nota_documento.trim() : "";
    const marcadores = factura.lectura.sospechosos ?? [];
    const duplicado = factura.hechos.find((hecho) => hecho.nombre === "pago_duplicado");
    const anomalia = factura.hechos.find((hecho) => hecho.regla === "R6_anomalia");

    // Lo que el motor hizo con el resultado, dicho con el resultado y no con el del
    // caso que se abría antes por defecto. "y no pagó" era verdad para P006 y falso
    // para las 467 que sí se pagan.
    const desenlace =
        factura.resultado === "PAGAR"
            ? "y dejó pasar el pago"
            : factura.resultado === "NO_PAGAR"
              ? "y paró el pago"
              : "y lo mandó a una persona en vez de decidir solo";

    // La tarjeta de dos columnas es para cuando el motor **señaló algo**: frases que
    // piden cosas (marcadores de la lectura) o una anomalía de regla. No basta con
    // que el documento traiga una nota, porque la traen cientos y son texto de
    // factura corriente: montar el formato del caso excepcional alrededor de una nota
    // normal daría a entender que el motor vio algo donde no vio nada.
    //
    // Las dos señales no son lo mismo y el texto no puede tratarlas igual: una
    // instrucción es un texto que pide algo, y una anomalía es una regla que no se
    // cumple sin que nadie pida nada. Decir «señaló la instrucción» cuando lo que
    // saltó fue un pedido repetido sería inventarse el documento.
    const instruccion = marcadores.length > 0;
    const haySenal = instruccion || Boolean(anomalia);

    if (!haySenal) {
        const incumplidas = factura.hechos.filter((hecho) => !hecho.ok);
        return (
            <Card size="sm">
                <CardContent className="flex items-start gap-2 pt-4 text-sm text-muted-foreground">
                    <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                    <span>
                        El motor no ha señalado ninguna instrucción dirigida al sistema en este
                        documento, así que no hay nada que obedecer ni que ignorar: la decisión sale
                        de las {entero(factura.hechos.length)} reglas y de nada más.{" "}
                        {incumplidas.length === 0 ? (
                            <>Ninguna dio un motivo.</>
                        ) : (
                            <>
                                Las que no se cumplieron:{" "}
                                {incumplidas.map((hecho) => hecho.motivo).join(" · ")}.
                            </>
                        )}{" "}
                        Resultado: <EtiquetaResultado resultado={factura.resultado} />.
                    </span>
                </CardContent>
            </Card>
        );
    }

    // El color del panel derecho es el del resultado: en rojo solo cuando hay una
    // persona obligada o un pago parado. Un `PAGAR` pintado de rojo diría que algo
    // se ha roto cuando lo que ha pasado es que no había nada que parar.
    const claseMotor =
        factura.resultado === "PAGAR"
            ? {
                  borde: "border-emerald-500/60 bg-emerald-500/5",
                  titulo: "text-emerald-800 dark:text-emerald-200",
                  texto: "text-emerald-900/90 dark:text-emerald-100/80",
              }
            : factura.resultado === "NO_PAGAR"
              ? {
                    borde: "border-red-500/60 bg-red-500/5",
                    titulo: "text-red-800 dark:text-red-200",
                    texto: "text-red-900/90 dark:text-red-100/80",
                }
              : {
                    borde: "border-amber-500/60 bg-amber-500/5",
                    titulo: "text-amber-800 dark:text-amber-200",
                    texto: "text-amber-900/90 dark:text-amber-100/80",
                };

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <ShieldCheck className="size-4 text-muted-foreground" />
                    {instruccion
                        ? "Lo que el documento pidió y lo que hizo el motor"
                        : "Lo que el motor encontró y lo que hizo con ello"}
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                    {instruccion
                        ? "Un texto dentro de un PDF no es una orden. El motor lo señala, lo cuenta como anomalía y decide con las reglas."
                        : "La señal no es una frase marcada en la lectura, sino una regla que salta. El motor la señala y decide con las demás."}
                </p>
            </CardHeader>

            <CardContent className="grid gap-3 lg:grid-cols-2">
                <div className="space-y-2 rounded-lg border-l-2 border-amber-500/60 bg-amber-500/5 px-3 py-2.5">
                    <div className="flex items-center gap-1.5 text-xs font-medium text-amber-800 dark:text-amber-200">
                        <FileText className="size-3.5" />
                        Lo que dice el PDF
                    </div>
                    <blockquote className="text-sm break-words text-amber-900/90 dark:text-amber-100/80">
                        {nota ? `«${nota}»` : "El documento no trae ninguna nota de este tipo."}
                    </blockquote>
                    {marcadores.length > 0 ? (
                        <p className="text-xs text-muted-foreground">
                            El motor marcó {entero(marcadores.length)}{" "}
                            {marcadores.length === 1 ? "frase" : "frases"}:{" "}
                            {marcadores.map((frase, indice) => (
                                <span key={frase}>
                                    {indice > 0 ? ", " : ""}
                                    <span className="font-mono">«{frase}»</span>
                                </span>
                            ))}
                            .
                        </p>
                    ) : (
                        <p className="text-xs text-muted-foreground">
                            La lectura no dejó ninguna frase marcada en este documento: la señal que
                            salta es la regla de la derecha.
                        </p>
                    )}
                </div>

                <div className={cn("space-y-2 rounded-lg border-l-2 px-3 py-2.5", claseMotor.borde)}>
                    <div className={cn("flex items-center gap-1.5 text-xs font-medium", claseMotor.titulo)}>
                        <Ban className="size-3.5" />
                        Lo que hizo el motor
                    </div>
                    <ul className={cn("space-y-1.5 text-sm", claseMotor.texto)}>
                        {anomalia ? (
                            <li>
                                <strong className="font-medium">Señaló</strong>{" "}
                                {instruccion
                                    ? "la instrucción sin obedecerla"
                                    : "la anomalía sin dejarla pasar"}
                                : {anomalia.motivo}.
                            </li>
                        ) : null}
                        {duplicado ? (
                            <li>
                                <strong className="font-medium">Bloqueó el pago</strong> con la regla
                                dura: {duplicado.motivo}
                                {typeof duplicado.datos.asiento === "string" ? (
                                    <>
                                        {" "}
                                        (asiento{" "}
                                        <span className="font-mono">{duplicado.datos.asiento}</span>)
                                    </>
                                ) : null}
                                .
                            </li>
                        ) : null}
                        <li>
                            <strong className="font-medium">Decidió</strong>{" "}
                            <EtiquetaResultado resultado={factura.resultado} /> {desenlace}.
                        </li>
                    </ul>
                </div>
            </CardContent>
        </Card>
    );
}

/* ----------------------------------------------------------------- evidencia */

function Evidencia({ factura, meta }: { factura: FacturaDetalle; meta: Meta | null }) {
    const traza = meta?.configuracion.datos.traza_paths ?? [];

    // Los anclajes (dónde está escrito cada dato dentro del PDF) se piden aquí y no
    // dentro del visor: los necesita también el formulario de correcciones, y
    // `usePeticion` no guarda caché, así que pedirlos en los dos sitios serían dos
    // vueltas de red para el mismo dato. Son **opcionales**: si no llegan, el visor
    // pinta el documento sin resaltado y esta sección sigue completa.
    const anclajes = useAnclajes(factura.file_id);

    return (
        <Seccion
            id="evidencia"
            icono={Fingerprint}
            titulo="Evidencia"
            descripcion="Con qué se puede comprobar la decisión: la huella del documento, los hechos que la sostienen y la lectura cruda."
        >
            <div className="grid gap-3 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <Fingerprint className="size-4 text-muted-foreground" />
                            Huella del documento
                        </CardTitle>
                        <p className="text-xs text-muted-foreground">
                            El <span className="font-mono">sha256</span> del PDF tal cual se leyó. Es
                            lo que ata la decisión al fichero: si el documento cambia, la huella
                            cambia y la decisión deja de valer para él.
                        </p>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <p className="font-mono text-xs break-all">{factura.sha256}</p>

                        <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
                            <DatoLinea etiqueta="Factura" valor={factura.file_id} mono />
                            <DatoLinea etiqueta="Lote" valor={entero(factura.lote)} />
                            <DatoLinea
                                etiqueta="Identificación fiable"
                                valor={factura.identificacion_fiable ? "sí" : "no"}
                            />
                            <DatoLinea
                                etiqueta="Reglas evaluadas"
                                valor={entero(factura.hechos.length)}
                            />
                        </dl>

                        {traza.length > 0 ? (
                            <div className="space-y-1 border-t pt-3">
                                <div className="text-xs font-medium text-muted-foreground">
                                    De estos ficheros salió la decisión
                                </div>
                                <ul className="space-y-0.5">
                                    {traza.map((ruta) => (
                                        <li key={ruta} className="font-mono text-xs break-all">
                                            {ruta}
                                        </li>
                                    ))}
                                </ul>
                                <p className="text-xs text-muted-foreground">
                                    Rutas que declara{" "}
                                    <span className="font-mono">GET /api/meta</span>: son el origen
                                    real del listado, no una copia. Con la huella del PDF y la línea
                                    de traza, la decisión se puede reproducir fuera de este panel.
                                </p>
                            </div>
                        ) : null}
                    </CardContent>
                </Card>

                <PanelDocumento
                    fileId={factura.file_id}
                    sha256={factura.sha256}
                    anclajes={anclajes}
                />
            </div>

            <PanelHechos hechos={factura.hechos} divisa={factura.resumen.divisa} />

            <Sospechosos lectura={factura.lectura} ordenes={[]} />

            <CamposCrudos campos={factura.campos} divisa={factura.resumen.divisa} />
        </Seccion>
    );
}

/* ------------------------------------------------------------------ versiones */

function Versiones({
    factura,
    meta,
    salud,
    snapshots,
}: {
    factura: FacturaDetalle;
    meta: Meta | null;
    salud: Salud | null;
    snapshots: Snapshot[] | null;
}) {
    const versiones = meta ? Object.entries(meta.motor.versiones_norma) : [];
    const vigente = snapshots?.find((snapshot) => snapshot.vigente) ?? null;

    // La comprobación que da sentido a esta sección: si la norma con la que se
    // decidió esta factura no es la que el motor declara para la traza, la decisión
    // que se enseña arriba no se puede comparar con las demás y hay que decirlo.
    const coincide =
        versiones.length > 0 && versiones.every(([norma]) => norma === factura.version_norma);

    return (
        <Seccion
            id="versiones"
            icono={Tag}
            titulo="Versiones"
            descripcion="Con qué se decidió. Sin esto un resultado no se reproduce: la misma factura con otra norma puede dar otra cosa."
        >
            <div className="grid gap-3 lg:grid-cols-3">
                <Card size="sm" className="gap-0">
                    <CardContent className="space-y-1">
                        <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                            Norma de esta decisión
                        </span>
                        <span className="font-heading text-2xl leading-none font-semibold">
                            {factura.version_norma}
                        </span>
                        {meta ? (
                            <p className="text-xs text-muted-foreground">
                                {coincide ? (
                                    <>
                                        Es la misma con la que se decidieron las{" "}
                                        {entero(meta.motor.facturas_en_traza)} facturas de la traza,
                                        así que esta se puede comparar con todas.
                                    </>
                                ) : (
                                    <span className="text-amber-700 dark:text-amber-300">
                                        La traza declara otras versiones (
                                        {versiones
                                            .map(([norma, cuantas]) => `${norma}: ${entero(cuantas)}`)
                                            .join(", ")}
                                        ). Esta factura no se puede comparar con el resto.
                                    </span>
                                )}
                            </p>
                        ) : (
                            <p className="text-xs text-muted-foreground">
                                Sin <span className="font-mono">/api/meta</span> no se puede
                                comprobar contra qué se decidieron las demás.
                            </p>
                        )}
                    </CardContent>
                </Card>

                <Card size="sm" className="gap-0 lg:col-span-2">
                    <CardContent className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
                        <DatoLinea etiqueta="API" valor={meta?.api_version ?? SIN_DATO} mono />
                        <DatoLinea etiqueta="Servicio" valor={meta?.servicio ?? SIN_DATO} mono />
                        <DatoLinea
                            etiqueta="Normas en la traza"
                            valor={
                                versiones.length > 0
                                    ? versiones
                                          .map(([norma, cuantas]) => `${norma} (${entero(cuantas)})`)
                                          .join(" · ")
                                    : SIN_DATO
                            }
                        />
                        <DatoLinea
                            etiqueta="Esquema de la descarga del ERP"
                            valor={vigente ? vigente.esquema_version : SIN_DATO}
                            mono
                        />
                        <DatoLinea
                            etiqueta="Facturas en la traza"
                            valor={meta ? entero(meta.motor.facturas_en_traza) : SIN_DATO}
                        />
                        <DatoLinea
                            etiqueta="Líneas inválidas"
                            valor={meta ? entero(meta.motor.lineas_invalidas) : SIN_DATO}
                        />
                    </CardContent>
                </Card>
            </div>

            <VersionesOcr salud={salud} />
        </Seccion>
    );
}

/**
 * Los motores de lectura con su modelo.
 *
 * Van aparte del resto de versiones porque son **modelos**, no números de release:
 * el día que se cambie `PP-OCRv5_mobile` por otro, las facturas que se leyeron con
 * OCR pueden leerse distinto y eso hay que poder fechar. Se enseña el motor
 * configurado y no el que contestó porque la API no publica cuál contestó:
 * inventarse esa distinción sería peor que no enseñarla.
 */
function VersionesOcr({ salud }: { salud: Salud | null }) {
    const ocr = salud?.dependencias.ocr;
    const local = ocr?.motores?.local ?? null;
    const nube = ocr?.motores?.cloud ?? null;
    const modelos = Object.entries(local?.models ?? {});

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Server className="size-4 text-muted-foreground" />
                    Motores de lectura
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                    El OCR solo entra cuando el PDF no trae capa de texto. Esto sale de{" "}
                    <span className="font-mono">GET /health</span>, que es el único sitio donde el
                    servicio publica sus modelos.
                </p>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                    <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Local ({texto(local?.runtime)})
                    </div>
                    {modelos.length === 0 ? (
                        <p className="text-sm text-muted-foreground">
                            Sin dato: no se pudo leer el estado del motor de OCR.
                        </p>
                    ) : (
                        <dl className="space-y-0.5">
                            {modelos.map(([tarea, modelo]) => (
                                <div key={tarea} className="flex gap-2 text-xs">
                                    <dt className="w-8 shrink-0 text-muted-foreground">{tarea}</dt>
                                    <dd className="font-mono">{modelo}</dd>
                                </div>
                            ))}
                            <div className="flex gap-2 text-xs">
                                <dt className="w-8 shrink-0 text-muted-foreground">carga</dt>
                                <dd className="font-mono">
                                    {local?.loaded === true
                                        ? "en memoria"
                                        : local?.loaded === false
                                          ? "no cargado"
                                          : SIN_DATO}
                                </dd>
                            </div>
                        </dl>
                    )}
                </div>

                <div className="space-y-1">
                    <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Nube
                    </div>
                    {!nube ? (
                        <p className="text-sm text-muted-foreground">
                            Sin dato: no se pudo leer el estado del motor de OCR.
                        </p>
                    ) : (
                        <dl className="space-y-0.5">
                            <div className="flex gap-2 text-xs">
                                <dt className="w-16 shrink-0 text-muted-foreground">modelo</dt>
                                <dd className="font-mono break-all">{texto(nube.model)}</dd>
                            </div>
                            <div className="flex gap-2 text-xs">
                                <dt className="w-16 shrink-0 text-muted-foreground">token</dt>
                                <dd className="font-mono">{texto(nube.token)}</dd>
                            </div>
                            <div className="flex gap-2 text-xs">
                                <dt className="w-16 shrink-0 text-muted-foreground">servicio</dt>
                                <dd className="font-mono break-all">{texto(ocr?.url)}</dd>
                            </div>
                        </dl>
                    )}
                </div>
            </CardContent>
        </Card>
    );
}

/* ------------------------------------------------------------------- latencia */

function Latencia({
    factura,
    salud,
    snapshots,
    contadores,
}: {
    factura: FacturaDetalle;
    salud: Salud | null;
    snapshots: Snapshot[] | null;
    contadores: Estadisticas | null;
}) {
    const vigente = snapshots?.find((snapshot) => snapshot.vigente) ?? null;
    const dependencias = salud ? ordenarDependencias(salud) : [];
    const maxima = dependencias.reduce(
        (mayor, [, dependencia]) => Math.max(mayor, dependencia.latencia_ms),
        0,
    );

    const porMetodo = contadores ? Object.entries(contadores.por_metodo_lectura) : [];
    const leidas = porMetodo.reduce((suma, [, cuantas]) => suma + cuantas, 0);

    return (
        <Seccion
            id="latencia"
            icono={Timer}
            titulo="Latencia"
            descripcion="Cuánto cuesta cada paso. El tiempo de lectura es el de esta factura; el del ERP y el de las dependencias, el de la última medida que publica el servicio."
        >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <Tarjeta
                    titulo="Leer el documento"
                    valor={latencia(factura.lectura.segundos_lectura)}
                    pie={`${ETIQUETA_METODO[factura.lectura.metodo_lectura] ?? factura.lectura.metodo_lectura}, con una confianza del ${porcentaje(factura.lectura.calidad_lectura)}`}
                    tono="verde"
                />
                <Tarjeta
                    titulo="Descargar el ERP"
                    valor={vigente ? latencia(vigente.duracion_ms / 1000) : SIN_DATO}
                    pie={
                        vigente
                            ? `${entero(vigente.total_asientos)} asientos en ${entero(vigente.paginas)} páginas`
                            : "Sin descarga registrada"
                    }
                />
                <Tarjeta
                    titulo="Comprobar las dependencias"
                    valor={maxima > 0 ? latencia(maxima / 1000) : SIN_DATO}
                    pie="La más lenta de las tres, según la última medida de /health"
                />
            </div>

            {vigente && vigente.total_asientos > 0 ? (
                <p className="text-xs text-muted-foreground">
                    La descarga del ERP son {entero(vigente.total_asientos)} asientos en{" "}
                    {latencia(vigente.duracion_ms / 1000)}: unas{" "}
                    <strong className="font-medium">
                        {decimal(vigente.duracion_ms / vigente.total_asientos, 2)} ms por asiento
                    </strong>
                    . Es la resta que explica por qué la conciliación no es instantánea: el cuello de
                    botella no es decidir, es traer contra qué decidir.
                </p>
            ) : null}

            <Card>
                <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                        <Clock className="size-4 text-muted-foreground" />
                        Lo que tarda cada dependencia
                    </CardTitle>
                    <p className="text-xs text-muted-foreground">
                        {maxima > 0 ? (
                            <>
                                Las barras van escaladas al máximo de esta medida (
                                {latencia(maxima / 1000)}), no a un umbral: son milisegundos, y un
                                umbral pintado aquí sugeriría un límite que nadie ha fijado.
                            </>
                        ) : (
                            "Sin la medida del servicio no hay nada que enseñar aquí."
                        )}
                    </p>
                </CardHeader>
                <CardContent className="space-y-3">
                    {dependencias.length === 0 ? (
                        <p className="text-sm text-muted-foreground">
                            No se pudo leer el estado del servicio, así que tampoco sus tiempos. La
                            decisión de arriba no depende de esto.
                        </p>
                    ) : (
                        dependencias.map(([nombre, dependencia]) => (
                            <div key={nombre} className="space-y-1">
                                <div className="flex items-baseline justify-between gap-3 text-xs">
                                    <span className="font-medium">
                                        {ETIQUETA_DEPENDENCIA[nombre] ?? nombre}
                                    </span>
                                    <span className="tabular-nums text-muted-foreground">
                                        {latencia(dependencia.latencia_ms / 1000)}
                                    </span>
                                </div>
                                <Progress
                                    value={maxima > 0 ? (dependencia.latencia_ms / maxima) * 100 : 0}
                                    className="h-1"
                                />
                            </div>
                        ))
                    )}
                </CardContent>
            </Card>

            {leidas > 0 ? (
                <p className="text-xs text-muted-foreground">
                    De las {entero(leidas)} facturas de la traza,{" "}
                    {porMetodo
                        .map(
                            ([metodo, cuantas]) =>
                                `${entero(cuantas)} se leyeron con ${(ETIQUETA_METODO[metodo] ?? metodo).toLowerCase()}`,
                        )
                        .join(" y ")}
                    . Leer no cuesta lo mismo por los dos caminos, y por eso{" "}
                    <Link
                        to="/escalabilidad"
                        className="text-foreground underline decoration-dotted underline-offset-2"
                    >
                        escalabilidad y coste
                    </Link>{" "}
                    mide el reparto en vez de suponerlo.
                </p>
            ) : null}
        </Seccion>
    );
}

/* --------------------------------------------------------------------- estado */

function Estado({
    salud,
    error,
    reintentar,
    factura,
}: {
    salud: Salud | null;
    error: Error | null;
    reintentar: () => void;
    factura: FacturaDetalle;
}) {
    if (error && !salud) {
        return (
            <Seccion
                id="estado"
                icono={Activity}
                titulo="Estado"
                descripcion="Cómo están las dependencias del servicio."
            >
                <FalloDeCarga
                    error={error}
                    alReintentar={reintentar}
                    queEs="el estado del servicio"
                />
            </Seccion>
        );
    }

    const dependencias = salud ? ordenarDependencias(salud) : [];
    const criticas = salud?.dependencias_criticas ?? [];
    const caidas = salud?.criticas_caidas ?? [];

    return (
        <Seccion
            id="estado"
            icono={Activity}
            titulo="Estado"
            descripcion="Las tres dependencias del servicio, se hayan caído o no. Enseñar solo las que van mal hace que una caída y un sistema sano se parezcan."
        >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Tarjeta
                    titulo="Servicio"
                    valor={salud ? salud.estado : SIN_DATO}
                    pie={salud ? salud.servicio : "Sin dato del servicio"}
                    tono={salud?.estado === "ok" ? "verde" : undefined}
                />
                <Tarjeta
                    titulo="Dependencias críticas"
                    valor={`${entero(caidas.length)} de ${entero(criticas.length)} caídas`}
                    pie={
                        caidas.length === 0
                            ? "Ninguna caída: el servicio puede hacer su trabajo"
                            : `Caídas: ${caidas.join(", ")}`
                    }
                    tono={salud && caidas.length === 0 ? "verde" : undefined}
                />
                <Tarjeta
                    titulo="Traza"
                    valor={salud ? entero(salud.datos.traza.facturas) : SIN_DATO}
                    pie={
                        salud
                            ? `${entero(salud.datos.traza.lineas_invalidas)} líneas inválidas`
                            : "Sin dato de la traza"
                    }
                    tono={salud?.datos.traza.ok ? "verde" : undefined}
                />
                <Tarjeta
                    titulo="Esta factura"
                    valor={salud ? "Decidida" : SIN_DATO}
                    pie={
                        factura.resultado === "PAGAR"
                            ? "El PAGAR no es una avería: no había nada que parar"
                            : factura.resultado === "NO_PAGAR"
                              ? "El NO_PAGAR no es una avería: es una regla dura funcionando"
                              : "El ESCALAR no es una avería: es una persona obligada a mirar"
                    }
                />
            </div>

            {dependencias.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                    No hay medida de las dependencias en esta fuente.
                </p>
            ) : (
                <div className="grid gap-3 lg:grid-cols-3">
                    {dependencias.map(([nombre, dependencia]) => (
                        <DependenciaCard
                            key={nombre}
                            nombre={nombre}
                            dependencia={dependencia}
                            critica={criticas.includes(nombre)}
                        />
                    ))}
                </div>
            )}
        </Seccion>
    );
}

/**
 * Una dependencia: si responde, cuánto tarda y qué se rompe si no.
 *
 * Lo de "qué se rompe" no es adorno: un `ok: false` sin decir qué deja de funcionar
 * obliga a quien mira a saberse el sistema de memoria. Va en un `Tooltip` porque es
 * texto largo y la tarjeta tiene que seguir siendo escaneable.
 */
function DependenciaCard({
    nombre,
    dependencia,
    critica,
}: {
    nombre: string;
    dependencia: Dependencia;
    critica: boolean;
}) {
    const explicacion =
        EXPLICACION_DEPENDENCIA[nombre] ??
        "Esta dependencia no está en el mapa de nombres del panel: se enseña su clave tal cual en vez de esconderla.";
    const detalle = detalleDeDependencia(nombre, dependencia);

    return (
        <Card size="sm" className={cn("gap-2", !dependencia.ok && "border-amber-500/40")}>
            <CardContent className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                    <Tooltip>
                        <TooltipTrigger asChild>
                            <span className="cursor-help font-medium underline decoration-dotted underline-offset-2">
                                {ETIQUETA_DEPENDENCIA[nombre] ?? nombre}
                            </span>
                        </TooltipTrigger>
                        <TooltipContent className="max-w-xs">
                            <p>{explicacion}</p>
                        </TooltipContent>
                    </Tooltip>
                    {critica ? (
                        <Badge variant="outline" className="text-[0.65rem]">
                            crítica
                        </Badge>
                    ) : null}
                </div>

                <p
                    className={cn(
                        "flex items-center gap-1.5 text-sm",
                        dependencia.ok ? "text-muted-foreground" : CLASE_DEPENDENCIA_CAIDA,
                    )}
                >
                    {dependencia.ok ? (
                        <ShieldCheck className="size-4" />
                    ) : (
                        <AlertTriangle className="size-4" />
                    )}
                    {dependencia.ok ? "Responde" : "No responde"}
                    <span className="tabular-nums">
                        · {latencia(dependencia.latencia_ms / 1000)}
                    </span>
                </p>

                {detalle.length > 0 ? (
                    <dl className="space-y-0.5">
                        {detalle.map(([etiqueta, valor]) => (
                            <div key={etiqueta} className="flex gap-2 text-xs">
                                <dt className="shrink-0 text-muted-foreground">{etiqueta}</dt>
                                <dd className="min-w-0 font-mono break-all">{valor}</dd>
                            </div>
                        ))}
                    </dl>
                ) : null}
            </CardContent>
        </Card>
    );
}

/** Lo que cada dependencia trae de más, ya en pares legibles. */
function detalleDeDependencia(nombre: string, dependencia: Dependencia): [string, string][] {
    if (nombre === "mongo") {
        const faltan = Object.entries(dependencia.indices_faltantes ?? {});
        return [
            ["base", texto(dependencia.db)],
            [
                "índices que faltan",
                faltan.length === 0
                    ? "ninguno"
                    : faltan
                          .map(([coleccion, indices]) => `${coleccion}: ${indices.join(", ")}`)
                          .join(" · "),
            ],
        ];
    }

    if (nombre === "ocr") {
        return [
            ["servicio", texto(dependencia.url)],
            ["estado", texto(dependencia.estado_ocr)],
            ["motor", texto(dependencia.motor)],
        ];
    }

    if (nombre === "escritura") {
        return [
            ["bucket", texto(dependencia.bucket)],
            [
                "contenido",
                `${entero(dependencia.expedientes ?? 0)} expedientes · ${entero(dependencia.pdfs ?? 0)} PDF · ${entero(dependencia.eventos ?? 0)} eventos`,
            ],
        ];
    }

    return [];
}

/* -------------------------------------------------------------------- errores */

/**
 * Los errores, uno por uno, incluidos los que no hay.
 *
 * Enseñar la lista entera y no solo los fallos es deliberado: "no hay errores" es
 * una afirmación, y una afirmación sobre un sistema solo vale si se ve sobre qué
 * campos se ha hecho. Un panel que solo pinta lo que falla no distingue "todo bien"
 * de "no lo he mirado".
 */
function Errores({
    salud,
    meta,
    contadores,
    factura,
}: {
    salud: Salud | null;
    meta: Meta | null;
    contadores: Estadisticas | null;
    factura: FacturaDetalle;
}) {
    const mongo = contadores?.mongo ?? null;
    const entrega = contadores?.entrega ?? null;
    const indices = salud?.dependencias.mongo?.indices_faltantes ?? null;
    const nube = salud?.dependencias.ocr?.motores?.cloud ?? null;
    const circuito = nube?.circuit ?? null;

    const loteUno = contadores?.por_lote?.["1"] ?? null;
    const falta = contadores && entrega ? contadores.total - entrega.total : null;

    return (
        <Seccion
            id="errores"
            icono={AlertTriangle}
            titulo="Errores"
            descripcion="Lo que está mal y también lo que se ha mirado y está bien. Un panel que solo enseña los fallos no distingue «todo correcto» de «sin comprobar»."
        >
            <div className="grid gap-3 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <ListChecks className="size-4 text-muted-foreground" />
                            Comprobado y sin incidencias
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        <ul className="space-y-2">
                            <Comprobacion
                                bien={indices !== null && Object.keys(indices).length === 0}
                                etiqueta="Índices de MongoDB"
                                detalle={
                                    indices === null
                                        ? "sin dato: no se pudo leer el estado del servicio."
                                        : Object.keys(indices).length === 0
                                          ? "el esquema y la base coinciden, no falta ningún índice."
                                          : `faltan índices: ${Object.keys(indices).join(", ")}.`
                                }
                            />
                            <Comprobacion
                                bien={mongo?.ok === true}
                                etiqueta="Catálogo del ERP"
                                detalle={
                                    mongo === null
                                        ? "sin dato: no se pudieron leer los contadores."
                                        : mongo.ok
                                          ? "Mongo responde y la API puede comparar los pedidos contra los asientos."
                                          : `Mongo no responde${mongo.error ? `: ${mongo.error}` : ""}. El listado sigue saliendo de la traza, pero no hay contra qué comparar.`
                                }
                            />
                            <Comprobacion
                                bien={meta !== null && meta.motor.lineas_invalidas === 0}
                                etiqueta="Líneas de la traza"
                                detalle={
                                    meta === null
                                        ? "sin dato: no se pudo leer la versión del servicio."
                                        : meta.motor.lineas_invalidas === 0
                                          ? `las ${entero(meta.motor.facturas_en_traza)} líneas se parsean, ninguna rota.`
                                          : `${entero(meta.motor.lineas_invalidas)} líneas no se pudieron parsear y no cuentan en el total.`
                                }
                            />
                            <Comprobacion
                                bien={(contadores?.resultados_desconocidos ?? 0) === 0}
                                etiqueta="Resultados desconocidos"
                                detalle={
                                    contadores === null
                                        ? "sin dato: no se pudieron leer los contadores."
                                        : contadores.resultados_desconocidos === 0
                                          ? "todas las decisiones caen en uno de los tres resultados que el panel sabe pintar."
                                          : `${entero(contadores.resultados_desconocidos)} facturas traen un resultado que el panel no conoce.`
                                }
                            />
                        </ul>
                    </CardContent>
                </Card>

                <div className="space-y-3">
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <AlertTriangle className="size-4 text-muted-foreground" />
                                Lo que no cuadra
                            </CardTitle>
                        </CardHeader>
                        <CardContent>
                            <ul className="space-y-2">
                                <Comprobacion
                                    bien={entrega?.coincide_con_traza !== false}
                                    etiqueta="La entrega y la traza"
                                    detalle={
                                        entrega === null
                                            ? "sin dato: no se pudieron leer los contadores."
                                            : entrega.coincide_con_traza
                                              ? `coinciden: ${entero(entrega.total)} líneas entregadas y las mismas en la traza.`
                                              : `la entrega tiene ${entero(entrega.total)} líneas y la traza ${entero(contadores?.total ?? 0)} facturas${falta !== null ? `: faltan ${entero(falta)}` : ""}. No es un fallo del motor: la entrega es del lote 1${loteUno !== null ? ` (${entero(loteUno)} líneas)` : ""} y el lote 2 no está entregado. Está aquí porque son dos números distintos y fundirlos en uno sería mentir.`
                                    }
                                />
                            </ul>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <Server className="size-4 text-muted-foreground" />
                                Cortacircuitos del OCR en la nube
                            </CardTitle>
                            <p className="text-xs text-muted-foreground">
                                La única señal de error que el servicio publica sobre sí mismo.
                            </p>
                        </CardHeader>
                        <CardContent className="space-y-2">
                            {!nube || !circuito ? (
                                <p className="text-sm text-muted-foreground">
                                    Sin dato: no se pudo leer el estado del motor de OCR.
                                </p>
                            ) : (
                                <>
                                    <div className="flex flex-wrap items-center gap-2">
                                        <Badge
                                            variant="outline"
                                            className={cn(
                                                CLASE_CIRCUITO[circuito.circuit ?? ""] ??
                                                    "text-muted-foreground",
                                            )}
                                        >
                                            {ETIQUETA_CIRCUITO[circuito.circuit ?? ""] ??
                                                texto(circuito.circuit)}
                                        </Badge>
                                        <span className="text-xs text-muted-foreground tabular-nums">
                                            {entero(circuito.failures ?? 0)} fallos de{" "}
                                            {entero(nube.max_failures ?? 0)} ·{" "}
                                            {decimal(circuito.cooldown_remaining ?? 0, 1)} s de
                                            enfriamiento restante
                                        </span>
                                    </div>
                                    <p className="text-xs text-muted-foreground">
                                        {EXPLICACION_CIRCUITO[circuito.circuit ?? ""] ??
                                            "Estado no reconocido: se enseña la clave tal cual."}
                                    </p>
                                    <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                                        <DatoLinea
                                            etiqueta="Último error"
                                            valor={texto(circuito.last_error ?? undefined)}
                                            mono
                                        />
                                        <DatoLinea
                                            etiqueta="Modelo en la nube"
                                            valor={texto(nube.model)}
                                            mono
                                        />
                                    </dl>
                                </>
                            )}
                        </CardContent>
                    </Card>
                </div>
            </div>

            <p className="text-xs text-muted-foreground">
                Y una cosa que <strong className="font-medium">no</strong> es un error: el{" "}
                <span className="font-mono">{factura.file_id}</span> sale{" "}
                <strong className="font-medium">{factura.resultado}</strong>.{" "}
                {factura.resultado === "PAGAR"
                    ? "Eso es el motor funcionando: no encontró nada que lo parase y dejó pasar el pago."
                    : factura.resultado === "NO_PAGAR"
                      ? "Eso es el motor funcionando: vio un motivo duro y paró el pago."
                      : "Eso es el motor funcionando: no tenía con qué decidir y lo mandó a una persona en vez de inventarse una respuesta."}{" "}
                El rojo de esta pantalla significa "aquí hay una persona obligada", no "aquí
                algo se ha roto"; por eso las dependencias caídas van en ámbar y esto no.
            </p>
        </Seccion>
    );
}

/* ------------------------------------------------------------------ reintentos */

function Reintentos({
    snapshots,
    cargando,
    error,
}: {
    snapshots: Snapshot[] | null;
    cargando: boolean;
    error: Error | null;
}) {
    const vigente = snapshots?.find((snapshot) => snapshot.vigente) ?? null;
    const disparados = vigente
        ? Object.values(vigente.reintentos).reduce((suma, cuantos) => suma + cuantos, 0)
        : 0;

    return (
        <Seccion
            id="reintentos"
            icono={RefreshCw}
            titulo="Reintentos"
            descripcion="Son de la descarga de asientos del ERP, no de cada factura: una decisión no se reintenta, se decide. Una decisión dudosa va a la cola de revisión, y eso está en la sección siguiente."
        >
            <PanelErp snapshots={snapshots} cargando={cargando} error={error} />

            <Card>
                <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                        <RefreshCw className="size-4 text-muted-foreground" />
                        Los tres reintentos que sabe hacer el cliente del ERP
                    </CardTitle>
                    <p className="text-xs text-muted-foreground">
                        {vigente ? (
                            <>
                                En la descarga vigente se dispararon{" "}
                                <strong className="font-medium">{entero(disparados)}</strong>{" "}
                                reintentos sobre {entero(vigente.total_asientos)} asientos. Los tres
                                códigos se cuentan por separado y no se funden en una cifra: uno es un
                                fallo transitorio y otro una sesión caducada, y no se arreglan igual.
                            </>
                        ) : (
                            "Sin descarga registrada no hay nada que contar de los reintentos."
                        )}
                    </p>
                </CardHeader>
                <CardContent>
                    <ul className="space-y-2">
                        {REINTENTOS.map((reintento) => {
                            const veces = vigente?.reintentos[reintento.clave] ?? null;

                            return (
                                <li
                                    key={reintento.clave}
                                    className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-lg bg-muted/40 px-3 py-2 text-sm"
                                >
                                    <span className="font-mono text-xs">{reintento.clave}</span>
                                    <span className="font-medium">{reintento.titulo}</span>
                                    <span className="tabular-nums text-muted-foreground">
                                        {veces === null ? SIN_DATO : `${entero(veces)} veces`}
                                    </span>
                                    <span className="w-full text-xs text-muted-foreground">
                                        {reintento.politica} {reintento.porque}
                                    </span>
                                </li>
                            );
                        })}
                    </ul>
                </CardContent>
            </Card>

            {vigente ? (
                <p className="text-xs text-muted-foreground">
                    La descarga vigente es <span className="font-mono">{vigente._id}</span>, del{" "}
                    {fechaHora(vigente.descargado_en)}, en estado{" "}
                    <span className="font-mono">{vigente.estado}</span> y con esquema{" "}
                    <span className="font-mono">{vigente.esquema_version}</span>. Solo una descarga
                    está vigente a la vez: las demás se conservan para poder decir contra cuál se
                    concilió cada factura.
                </p>
            ) : null}
        </Seccion>
    );
}

/* ----------------------------------------------------------- trabajo pendiente */

/**
 * Lo que queda por hacer, y por quién.
 *
 * La distinción que importa aquí es entre **trabajo del sistema** y **trabajo de
 * una persona**: las escaladas no son un fallo, son facturas que el motor no se
 * atrevió a decidir y que esperan a alguien. Se enseña el reparto de la cola porque
 * de las escaladas solo unas pocas se pueden cerrar solas: el resto sigue
 * necesitando ojos.
 */
function Pendiente({
    contadores,
    meta,
    factura,
}: {
    contadores: Estadisticas | null;
    meta: Meta | null;
    factura: FacturaDetalle;
}) {
    if (!contadores) {
        return (
            <Seccion
                id="pendiente"
                icono={ListTodo}
                titulo="Trabajo pendiente"
                descripcion="Lo que sigue esperando a una persona."
            >
                <p className="text-sm text-muted-foreground">
                    Sin los contadores no se puede decir cuánto queda por revisar. No es que no haya
                    nada: es que no se sabe.
                </p>
            </Seccion>
        );
    }

    const cola = contadores.cola_segunda_lectura;
    const pendientes = contadores.pendientes_revision;
    const sinReleer = pendientes === null ? null : Math.max(0, pendientes - cola.anotadas);

    return (
        <Seccion
            id="pendiente"
            icono={ListTodo}
            titulo="Trabajo pendiente"
            descripcion={`La factura que se sigue en esta página ${
                factura.resultado === "ESCALAR"
                    ? "sí está aquí: su decisión fue escalar, o sea que espera a una persona"
                    : `no está aquí: un ${factura.resultado} es una decisión, no una duda`
            }. Lo que espera a una persona son las escaladas, y son las que se cuentan abajo.`}
        >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Tarjeta
                    titulo="Esperando a una persona"
                    valor={pendientes === null ? SIN_DATO : entero(pendientes)}
                    pie={
                        pendientes === null
                            ? "Mongo no responde: no se puede contar"
                            : "Facturas escaladas: el motor no se atrevió a decidir"
                    }
                />
                <Tarjeta
                    titulo="Ya releídas"
                    valor={
                        pendientes === null
                            ? entero(cola.anotadas)
                            : `${entero(cola.anotadas)} de ${entero(pendientes)}`
                    }
                    pie="La segunda lectura del motor sobre una escalada"
                />
                <Tarjeta
                    titulo="Se pueden cerrar solas"
                    valor={entero(cola.confirmables)}
                    pie="La relectura cuadró con el maestro: no hace falta abrir el PDF"
                    tono="verde"
                />
                <Tarjeta
                    titulo="Exigen a alguien"
                    valor={entero(cola.desvios)}
                    pie="La relectura encontró un dato que no es el del maestro"
                />
            </div>

            <Card>
                <CardContent className="space-y-3 pt-4">
                    <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
                        <DatoLinea
                            etiqueta="Sin releer todavía"
                            valor={sinReleer === null ? SIN_DATO : entero(sinReleer)}
                        />
                        <DatoLinea
                            etiqueta="La relectura no concluyó"
                            valor={entero(cola.con_evidencia)}
                        />
                        <DatoLinea
                            etiqueta="Asientos vigentes"
                            valor={
                                contadores.asientos_vigentes === null
                                    ? SIN_DATO
                                    : entero(contadores.asientos_vigentes)
                            }
                        />
                        <DatoLinea
                            etiqueta="Cola de la segunda lectura"
                            valor={
                                meta === null
                                    ? SIN_DATO
                                    : meta.configuracion.datos.cola_existe
                                      ? "el fichero existe"
                                      : "el fichero no existe"
                            }
                        />
                    </dl>

                    <p className="text-xs text-muted-foreground">
                        Las tres cifras de la cola suman lo que se ha releído:{" "}
                        {entero(cola.confirmables)} se pueden cerrar, {entero(cola.desvios)} exigen
                        una persona y {entero(cola.con_evidencia)} quedaron sin conclusión —que no es
                        lo mismo que estar bien: es que la relectura no desató el nudo—.{" "}
                        {sinReleer === null ? (
                            <>
                                Cuántas no tienen segunda lectura no se puede decir sin Mongo, así que
                                no se dice.
                            </>
                        ) : (
                            <>
                                Las {entero(sinReleer)} restantes no tienen segunda lectura, así que
                                hacen la misma falta que antes.
                            </>
                        )}
                    </p>

                    <div className="flex flex-wrap gap-2">
                        <BotonFiltro filtros={{ resultado: "ESCALAR" }}>
                            {pendientes === null
                                ? "Ver las escaladas"
                                : `Ver las ${entero(pendientes)} escaladas`}
                        </BotonFiltro>
                        <BotonFiltro filtros={{ segundaLectura: "desvio" }}>
                            {`Ver los ${entero(cola.desvios)} desvíos de pago`}
                        </BotonFiltro>
                        <BotonFiltro filtros={{ segundaLectura: "confirmable" }}>
                            {`Ver las ${entero(cola.confirmables)} que se pueden cerrar`}
                        </BotonFiltro>
                    </div>
                </CardContent>
            </Card>
        </Seccion>
    );
}

/* --------------------------------------------------------------- piezas sueltas */

/**
 * Un dato con su etiqueta, en una rejilla.
 *
 * Igual que el `Dato` de `ResumenFactura`, pero local: el de allí vive dentro de su
 * fichero y no se exporta. Duplicarlo en cinco líneas es más barato que sacarlo a un
 * módulo compartido para dos usos.
 */
function DatoLinea({
    etiqueta,
    valor,
    mono,
}: {
    etiqueta: string;
    valor: string;
    mono?: boolean;
}) {
    return (
        <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">{etiqueta}</dt>
            <dd className={cn("text-sm break-all", mono && "font-mono text-xs")}>{valor}</dd>
        </div>
    );
}

/** Una comprobación con su resultado. Verde cuando no hay nada que hacer. */
function Comprobacion({
    bien,
    etiqueta,
    detalle,
}: {
    bien: boolean | null;
    etiqueta: string;
    detalle: string;
}) {
    return (
        <li className="flex items-start gap-2 text-sm">
            {bien === null ? (
                <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            ) : bien ? (
                <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
            ) : (
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            )}
            <span className="min-w-0">
                <span className="font-medium">{etiqueta}</span>{" "}
                <span className="text-muted-foreground">{detalle}</span>
            </span>
        </li>
    );
}

/**
 * Un enlace a la tabla con un filtro puesto.
 *
 * El filtro se construye con `escribirFiltros` y no a mano: los nombres de los
 * parámetros viven en un solo sitio (`CLAVES`), y un `?resultado=` escrito de memoria
 * en una página es exactamente el tipo de enlace que se rompe en silencio cuando
 * alguien renombra una clave.
 */
function BotonFiltro({ filtros, children }: { filtros: FiltrosVista; children: ReactNode }) {
    const params = escribirFiltros(filtros, 1).toString();

    return (
        <Link
            to={params ? `/facturas?${params}` : "/facturas"}
            className="inline-flex items-center gap-1.5 rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
        >
            {children}
            <ArrowRight className="size-3.5" />
        </Link>
    );
}

/** Una tarjeta de cabecera: un número grande y su explicación en una línea. */
function Tarjeta({
    titulo,
    valor,
    pie,
    tono,
}: {
    titulo: string;
    valor: string;
    pie: string;
    tono?: "verde";
}) {
    return (
        <Card size="sm" className="gap-0">
            <CardContent className="flex flex-col gap-1">
                <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                    {titulo}
                </span>
                <span
                    className={cn(
                        "font-heading text-2xl leading-none font-semibold tabular-nums",
                        tono === "verde" && "text-emerald-700 dark:text-emerald-300",
                    )}
                >
                    {valor}
                </span>
                <span className="text-xs text-muted-foreground">{pie}</span>
            </CardContent>
        </Card>
    );
}

function Seccion({
    id,
    icono: Icono,
    titulo,
    descripcion,
    children,
}: {
    id: string;
    icono: typeof Activity;
    titulo: string;
    descripcion: string;
    children: ReactNode;
}) {
    return (
        <section id={id} className="scroll-mt-20 space-y-3">
            <div className="space-y-1">
                <h2 className="flex items-center gap-2 font-heading text-lg font-semibold tracking-tight">
                    <Icono className="size-4 text-muted-foreground" />
                    {titulo}
                </h2>
                <p className="max-w-3xl text-sm text-muted-foreground">{descripcion}</p>
            </div>
            {children}
        </section>
    );
}

function Esqueleto() {
    return (
        <div className="space-y-6" aria-busy="true" aria-live="polite">
            <div className="space-y-2">
                <Skeleton className="h-8 w-80" />
                <Skeleton className="h-4 w-full max-w-2xl" />
                <Skeleton className="h-4 w-full max-w-xl" />
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
                {[0, 1, 2, 3, 4].map((indice) => (
                    <Skeleton key={indice} className="h-28 rounded-xl" />
                ))}
            </div>
            <Skeleton className="h-40 rounded-xl" />
            <Skeleton className="h-72 rounded-xl" />
        </div>
    );
}

/* ---------------------------------------------------------------------- datos */

/**
 * Las dependencias en el orden en el que se leen: primero las críticas.
 *
 * El orden lo manda `dependencias_criticas` y no el alfabeto porque lo que se quiere
 * saber primero es si el servicio puede hacer su trabajo, y eso lo deciden las
 * críticas. Las que no están en esa lista van detrás, por orden alfabético para que
 * la fila no baile entre medidas.
 */
function ordenarDependencias(salud: Salud): [string, Dependencia][] {
    const criticas = salud.dependencias_criticas;

    return Object.entries(salud.dependencias).sort(([uno], [dos]) => {
        const posicionUno = criticas.indexOf(uno);
        const posicionDos = criticas.indexOf(dos);
        if (posicionUno !== -1 && posicionDos !== -1) return posicionUno - posicionDos;
        if (posicionUno !== -1) return -1;
        if (posicionDos !== -1) return 1;
        return uno.localeCompare(dos);
    });
}
