/**
 * El visor de PDF: el documento de verdad, con los datos que el motor leyó
 * resaltados encima.
 *
 * Antes esto era un `<iframe>` al PDF. Un `<iframe>` pinta el documento con el
 * visor del navegador y **no deja tocar nada de dentro**: no hay forma de
 * preguntarle dónde ha puesto el texto ni de dibujar encima. Para poder señalar
 * un dato hay que interpretar el PDF aquí, y eso es lo que hace `pdf.js`.
 *
 * Las tres piezas de cada página, de abajo arriba:
 *
 * 1. El **canvas**, con el dibujo de la página.
 * 2. La **capa de texto**: un `<span>` invisible por cada trozo de texto del
 *    PDF, colocado justo encima de donde se ve. No se ve, pero está: es lo que
 *    permite seleccionar y copiar el texto de un PDF escaneado… digo de uno con
 *    capa de texto. Y es también de donde se sacan las coordenadas para
 *    resaltar, midiendo sus cajas.
 * 3. Las **cajas del resaltado**, que van arriba del todo y no reciben clics
 *    (salvo su etiqueta), para no robarle el texto a la capa de abajo.
 *
 * De dónde salen las coordenadas depende del origen, y es la misma decisión que
 * toma la API:
 *
 * - **`capa_texto`** (471 de las 500): el PDF trae el texto de verdad, así que
 *   se busca el token que manda la API dentro del texto de la página y se rodea
 *   el trozo donde cae. Es más exacto que fiarse de cajas.
 * - **`ocr`** (29, las escaneadas): aquí no hay texto que buscar —el PDF es una
 *   foto—, así que se pintan las cajas que el OCR guardó al leer la imagen. Esas
 *   cajas vienen en puntos del PDF y rodean la **línea** entera, no el dato
 *   suelto: el `NIF` de una escaneada sale con el `IBAN` de la misma línea
 *   dentro de la misma caja. Se acepta a cambio de poder señalar algo, y la
 *   etiqueta del campo al lado es lo que explica qué se está señalando.
 */

import { ScanSearch } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { Anclajes, CampoAnclable, CampoAnclado } from "@/api/types";
import {
    abrirPdf,
    buscaEnPagina,
    indexaTexto,
    pdfjs,
    type CapaDeTexto,
    type DocumentoPdf,
    type IndiceTexto,
    type TareaDeCarga,
    type TareaDePintado,
} from "@/lib/pdf";

/**
 * El color de cada campo.
 *
 * En `rgb` suelto y no en clases de Tailwind porque el color entra en un
 * `style` calculado, y una clase construida a trozos (`bg-${color}-500`) no la
 * ve el empaquetador y no llega al CSS. Y en dos tonos —relleno y borde— porque
 * el relleno tiene que dejar leer el dato que hay debajo y el borde tiene que
 * verse sobre un papel blanco.
 *
 * Los colores no significan nada por sí mismos: el mismo dato lleva el mismo
 * color en la leyenda de arriba y en la caja de la página. Eso es todo lo que
 * tienen que hacer.
 */
const TINTA: Record<CampoAnclable, string> = {
    pedido: "37 99 235",
    nif: "124 58 237",
    iban: "13 148 136",
    fecha: "217 119 6",
    base: "5 150 105",
    iva: "219 39 119",
    total: "220 38 38",
};

/**
 * El nombre corto que cabe encima de una caja.
 *
 * La API manda la etiqueta larga (`Base imponible`) y esa se usa en la leyenda,
 * donde hay sitio. Encima de una caja de 50 px de ancho no cabe, y un rótulo
 * cortado es peor que uno corto.
 */
const ETIQUETA_CORTA: Record<CampoAnclable, string> = {
    pedido: "Pedido",
    nif: "NIF",
    iban: "IBAN",
    fecha: "Fecha",
    base: "Base",
    iva: "IVA",
    total: "Total",
};

/**
 * El límite de densidad de píxeles al que se pinta.
 *
 * Un canvas se pinta a `escala * devicePixelRatio` o se ve borroso en las
 * pantallas de retina. Pero un móvil moderno declara 3, y una A4 a 3x son casi
 * 25 millones de píxeles por página: memoria y tiempo de pintado para una
 * nitidez que a este tamaño de pantalla no se aprecia. A partir de 2 no se
 * gana nada que se vea.
 */
const DPR_MAXIMO = 2;

/**
 * Cuánto tiene que cambiar el ancho para volver a pintar.
 *
 * El `ResizeObserver` avisa con fracciones de píxel y a veces en ráfaga. Volver
 * a pintar dos canvas por una décima de píxel es tirar trabajo, y además da un
 * parpadeo que se nota.
 */
const UMBRAL_ANCHO = 1;

/** A partir de aquí una caja es lo bastante ancha para llevar rótulo. */
const ANCHO_MINIMO_ROTULO = 44;

/** Una caja ya colocada, lista para pintar. */
interface Caja {
    campo: CampoAnclable;
    x: number;
    y: number;
    ancho: number;
    alto: number;
    /** `true` en la primera caja del campo: es la que lleva el rótulo. */
    primera: boolean;
    /** `true` cuando el dato no casaba exacto (errata del OCR) o va sin pista. */
    dudosa: boolean;
}

export interface VisorPdfProps {
    /** El PDF ya descargado, tal cual lo devuelve `usePdf`. */
    blob: Blob;
    /** Dónde está cada dato, o `null` mientras se pide o si no se pudo pedir. */
    anclajes: Anclajes | null;
    /**
     * El campo que el operador tiene delante en el formulario. Los demás se
     * atenúan: con siete datos resaltados a la vez, todos igual de fuertes, no
     * se está señalando nada.
     */
    campoActivo?: string | null;
    /** Se avisa al pulsar el rótulo de una caja, para enlazar con el formulario. */
    onCampo?: (campo: CampoAnclable) => void;
}

export function VisorPdf({ blob, anclajes, campoActivo = null, onCampo }: VisorPdfProps) {
    const marco = useRef<HTMLDivElement>(null);
    const [ancho, setAncho] = useState(0);
    const [documento, setDocumento] = useState<DocumentoPdf | null>(null);
    const [fallo, setFallo] = useState<string | null>(null);

    /*
     * El ancho disponible manda la escala. Se mide con `ResizeObserver` y no con
     * `window.innerWidth` porque lo que importa no es la ventana sino la
     * columna: el panel pone el documento al lado de los hechos, y al plegarse
     * en una pantalla estrecha la columna cambia de ancho sin que la ventana
     * cambie de tamaño.
     *
     * Se mide el `contentRect`, que es la caja de contenido: ya viene sin el
     * relleno y sin la barra de desplazamiento. Si se midiera el ancho del
     * elemento, la página saldría unos píxeles más ancha que el hueco y
     * aparecería una barra horizontal.
     */
    useEffect(() => {
        const el = marco.current;
        if (!el) return;

        const observador = new ResizeObserver((entradas) => {
            const medida = entradas[0].contentRect.width;
            setAncho((previo) => (Math.abs(medida - previo) < UMBRAL_ANCHO ? previo : medida));
        });
        observador.observe(el);
        return () => observador.disconnect();
    }, []);

    useEffect(() => {
        let vivo = true;
        let carga: TareaDeCarga | null = null;

        setDocumento(null);
        setFallo(null);

        blob.arrayBuffer()
            .then((datos) => {
                if (!vivo) return null;
                carga = abrirPdf(datos);
                return carga.promise;
            })
            .then((doc) => {
                if (!doc) return;
                if (!vivo) return;
                setDocumento(doc);
            })
            .catch((exc: unknown) => {
                if (!vivo) return;
                setFallo(exc instanceof Error ? exc.message : String(exc));
            });

        return () => {
            vivo = false;
            // Soltar la tarea cierra el worker. Sin esto, cada factura que se ha
            // mirado deja un hilo vivo hasta que se recarga la pagina.
            if (carga) void carga.destroy();
        };
    }, [blob]);

    const paginas = documento ? Array.from({ length: documento.numPages }, (_, i) => i + 1) : [];

    return (
        <div className="flex flex-col gap-2">
            <Leyenda
                anclajes={anclajes}
                campoActivo={campoActivo}
                onCampo={onCampo}
            />

            <div
                ref={marco}
                /*
                 * `scrollbar-gutter: stable` reserva el sitio de la barra aunque
                 * no haga falta. Sin él, la página ancha hace aparecer la barra,
                 * la barra estrecha el hueco, y el hueco vuelve a pintar la
                 * página: un bucle de reescalado que se ve como un temblor.
                 */
                className="relative h-[60vh] min-h-80 overflow-auto rounded-lg bg-muted/40 p-2 ring-1 ring-foreground/10 [scrollbar-gutter:stable]"
                aria-busy={!documento && !fallo}
            >
                {fallo ? (
                    <p className="p-4 text-center text-xs text-muted-foreground">
                        No he podido interpretar el PDF para resaltarlo: {fallo}
                    </p>
                ) : !documento || ancho <= 0 ? (
                    <p className="p-4 text-center text-xs text-muted-foreground">
                        Abriendo el documento…
                    </p>
                ) : (
                    <div className="space-y-2">
                        {paginas.map((numero) => (
                            <Pagina
                                key={numero}
                                documento={documento}
                                numero={numero}
                                ancho={ancho}
                                anclajes={anclajes}
                                campoActivo={campoActivo}
                                onCampo={onCampo}
                            />
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

/**
 * La leyenda: qué color es cada campo, con el valor que leyó el motor.
 *
 * Hace dos trabajos. El primero es decir qué significa cada color, que sin esto
 * sería un adorno. El segundo es **llevar el ojo al dato**: pulsar un campo
 * resalta sus cajas y atenúa las demás, que es lo que hace falta cuando hay
 * siete datos marcados en una factura densa.
 */
function Leyenda({
    anclajes,
    campoActivo,
    onCampo,
}: {
    anclajes: Anclajes | null;
    campoActivo: string | null;
    onCampo?: (campo: CampoAnclable) => void;
}) {
    if (!anclajes || anclajes.campos.length === 0) {
        return (
            <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                <ScanSearch className="mt-px size-3.5 shrink-0" />
                {anclajes
                    ? anclajes.aviso ??
                      "El motor no dejó anotado dónde está escrito ningún dato de esta factura."
                    : "Buscando dónde está escrito cada dato…"}
            </p>
        );
    }

    return (
        <div className="flex flex-wrap items-center gap-1.5">
            {anclajes.campos.map((campo) => {
                const tinta = TINTA[campo.campo];
                const activo = campoActivo === campo.campo;

                return (
                    <button
                        key={campo.campo}
                        type="button"
                        data-leyenda={campo.campo}
                        onClick={() => onCampo?.(campo.campo)}
                        title={`${campo.etiqueta}: ${campo.valor}`}
                        style={{
                            backgroundColor: `rgb(${tinta} / ${activo ? 0.22 : 0.1})`,
                            boxShadow: `inset 0 0 0 1px rgb(${tinta} / ${activo ? 0.9 : 0.45})`,
                        }}
                        className="rounded-md px-1.5 py-0.5 text-xs transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                    >
                        <span className="font-medium">{ETIQUETA_CORTA[campo.campo]}</span>
                        <span className="ml-1 font-mono text-[11px] text-muted-foreground">
                            {campo.valor}
                        </span>
                    </button>
                );
            })}
        </div>
    );
}

/**
 * Una página: el dibujo, el texto invisible y las cajas.
 *
 * Se pintan todas las páginas seguidas y se desplaza, en vez de paginar. Las
 * facturas del corpus tienen una o dos páginas, y con dos un botón de "siguiente"
 * es una vuelta de más para ver la segunda mitad de un documento que cabe en dos
 * pantallas.
 */
function Pagina({
    documento,
    numero,
    ancho,
    anclajes,
    campoActivo,
    onCampo,
}: {
    documento: DocumentoPdf;
    numero: number;
    ancho: number;
    anclajes: Anclajes | null;
    campoActivo: string | null;
    onCampo?: (campo: CampoAnclable) => void;
}) {
    const lienzo = useRef<HTMLCanvasElement>(null);
    const contenedorTexto = useRef<HTMLDivElement>(null);
    const caja = useRef<HTMLDivElement>(null);
    const divs = useRef<HTMLElement[]>([]);
    const indice = useRef<IndiceTexto | null>(null);

    const [medida, setMedida] = useState<{ ancho: number; alto: number } | null>(null);
    const [escala, setEscala] = useState(0);
    const [cajas, setCajas] = useState<Caja[]>([]);

    /*
     * Pintar la página. Este efecto hace tres cosas y las hace juntas porque
     * dependen las unas de las otras: si se pintara el canvas sin la capa de
     * texto, no habría coordenadas que medir; si se midiera antes de que el
     * canvas esté, la página daría un salto de tamaño al aparecer el dibujo.
     */
    useEffect(() => {
        let vivo = true;
        let tarea: TareaDePintado | null = null;

        const pintar = async () => {
            const pagina = await documento.getPage(numero);
            if (!vivo) return;

            const base = pagina.getViewport({ scale: 1 });
            const escala = ancho / base.width;
            const vista = pagina.getViewport({ scale: escala });
            const dpr = Math.min(window.devicePixelRatio || 1, DPR_MAXIMO);
            const nitida = pagina.getViewport({ scale: escala * dpr });

            const elLienzo = lienzo.current;
            const elTexto = contenedorTexto.current;
            if (!elLienzo || !elTexto) return;

            /*
             * El canvas se dimensiona a mano: `pdf.js` **no** redimensiona el
             * canvas que le pasas, pinta en el que haya. Con `width`/`height`
             * puestos a la medida nítida y el CSS al tamaño de la vista, el
             * dibujo sale a la resolución de la pantalla sin cambiar de sitio.
             */
            elLienzo.width = Math.floor(nitida.width);
            elLienzo.height = Math.floor(nitida.height);

            tarea = pagina.render({ canvas: elLienzo, viewport: nitida });
            await tarea.promise;
            if (!vivo) return;

            /*
             * `--total-scale-factor` es lo que convierte la altura de fuente del
             * PDF a píxeles dentro de la capa de texto. Y el `width`/`height`
             * se fijan a mano porque el constructor de `TextLayer` los deja en
             * un `round(down, ...)` que depende de `--scale-round-x`, una
             * variable que pone el visor de `pdf.js` y aquí no existe.
             */
            elTexto.replaceChildren();
            elTexto.style.setProperty("--total-scale-factor", String(escala));
            elTexto.style.width = `${vista.width}px`;
            elTexto.style.height = `${vista.height}px`;

            const contenido = await pagina.getTextContent();
            const capa: CapaDeTexto = new pdfjs.TextLayer({
                textContentSource: contenido,
                container: elTexto,
                viewport: vista,
            });
            await capa.render();
            if (!vivo) return;

            /*
             * `textDivs` viene alineado uno a uno con los trozos de
             * `getTextContent()`: es la misma lista que `pdf.js` usa para
             * rellenar la capa. De ahí que se puedan buscar los trozos por
             * índice.
             */
            divs.current = capa.textDivs;
            indice.current = indexaTexto(
                contenido.items.map((item) => ("str" in item ? item.str : "")),
            );

            setMedida({ ancho: vista.width, alto: vista.height });
            setEscala(escala);
        };

        pintar().catch((exc: unknown) => {
            if (!vivo) return;
            /*
             * Al cambiar de factura o de ancho se cancela el pintado que
             * estuviera en marcha, y eso **rechaza** la promesa. No es un fallo:
             * es el aviso de que se ha dejado de pintar lo que ya no hace falta.
             * Sin filtrarlo, la consola se llena de errores rojos cada vez que se
             * redimensiona el panel.
             */
            if (exc instanceof pdfjs.RenderingCancelledException) return;
            if (exc instanceof pdfjs.AbortException) return;
            console.error("No he podido pintar la página", numero, exc);
        });

        return () => {
            vivo = false;
            tarea?.cancel();
        };
    }, [documento, numero, ancho]);

    /*
     * Dónde va cada caja. Va en un efecto aparte del pintado porque necesita el
     * resultado del anterior: las coordenadas del texto no existen hasta que la
     * capa está montada. `escala` es lo que las conecta —cambia solo cuando la
     * capa ya está lista—, y por eso está en las dependencias.
     */
    useEffect(() => {
        const elCaja = caja.current;
        if (!elCaja || !escala || !anclajes) {
            setCajas([]);
            return;
        }

        const pagina = numero - 1;
        const origen = elCaja.getBoundingClientRect();
        const nuevas: Caja[] = [];
        const vistas = new Set<CampoAnclable>();

        for (const campo of anclajes.campos) {
            /*
             * Las dos ramas devuelven **coordenadas de la pagina**, no de la
             * ventana: el OCR porque multiplica los `bbox` —que ya vienen en
             * puntos del PDF— por la escala, y la capa de texto porque le resta
             * el origen que se le pasa. Por eso aqui no se resta otra vez: la
             * caja se coloca con `position: absolute` dentro del contenedor de
             * la pagina, que es justo ese sistema de coordenadas.
             */
            const rectangulos =
                anclajes.origen === "ocr"
                    ? cajasDelOcr(campo, pagina, escala)
                    : cajasDelTexto(campo, divs.current, indice.current, origen);

            for (const rectangulo of rectangulos) {
                nuevas.push({
                    campo: campo.campo,
                    x: rectangulo.x,
                    y: rectangulo.y,
                    ancho: rectangulo.ancho,
                    alto: rectangulo.alto,
                    // El rótulo va en la primera caja del campo: repetirlo en las
                    // cuatro apariciones de un NIF es la misma palabra cuatro
                    // veces sobre el documento.
                    primera: !vistas.has(campo.campo),
                    dudosa: rectangulo.dudosa,
                });
                vistas.add(campo.campo);
            }
        }

        setCajas(nuevas);
    }, [anclajes, escala, numero]);

    return (
        <div
            ref={caja}
            className="relative mx-auto overflow-hidden rounded bg-white shadow-sm"
            style={medida ? { width: medida.ancho, height: medida.alto } : undefined}
        >
            <canvas ref={lienzo} className="absolute inset-0 size-full" />

            <div ref={contenedorTexto} className="textLayer" />

            {cajas.length > 0 ? (
                <div className="pointer-events-none absolute inset-0 z-10">
                    {cajas.map((una, i) => (
                        <Resalte
                            key={`${una.campo}-${i}`}
                            caja={una}
                            atenuada={campoActivo !== null && campoActivo !== una.campo}
                            onCampo={onCampo}
                        />
                    ))}
                </div>
            ) : null}
        </div>
    );
}

/** Un rectángulo en coordenadas del navegador. */
interface Rectangulo {
    x: number;
    y: number;
    ancho: number;
    alto: number;
    dudosa: boolean;
}

/**
 * Las cajas de un campo en una escaneada: las del OCR, tal cual vienen.
 *
 * Los `bbox` ya vienen en puntos del PDF y con el origen arriba a la izquierda,
 * que es la misma convención que el canvas: solo hay que multiplicar por la
 * escala a la que se pinta. Las líneas de `pdf.js` no sirven aquí porque un PDF
 * escaneado no tiene capa de texto: no hay nada que medir.
 *
 * **La caja es la de la línea, no la del dato.** El OCR guarda dónde está escrita
 * cada línea, no cada palabra, así que dos datos leídos en la misma línea
 * —`NIF:B98120774.IBAN:ES44…`, `Base 1.025,49 IVA21%215,35`— comparten caja. Se
 * podría recortar a ojo repartiendo el ancho de la línea entre sus caracteres,
 * pero eso sería geometría inventada: es más honesto enseñar la línea entera,
 * que es lo que de verdad se sabe, que una caja estrecha que acierta por
 * casualidad. El `texto` de cada ancla es la línea, y el color del campo dice
 * cuál de los dos datos se está mirando.
 */
function cajasDelOcr(campo: CampoAnclado, pagina: number, escala: number): Rectangulo[] {
    return campo.anclas
        .filter((ancla) => ancla.pagina === pagina)
        .map((ancla) => {
            const [x0, y0, x1, y1] = ancla.bbox;
            return {
                x: x0 * escala,
                y: y0 * escala,
                ancho: Math.max((x1 - x0) * escala, 1),
                alto: Math.max((y1 - y0) * escala, 1),
                dudosa: ancla.aproximado || ancla.confianza < 0.95,
            };
        });
}

/**
 * Las cajas de un campo en un PDF con capa de texto: se busca y se mide.
 *
 * El rectángulo se saca de las cajas reales de los `<span>` de la capa de texto,
 * no de su posición calculada. La diferencia importa: `pdf.js` estira en
 * horizontal los trozos de más de un carácter (`--scale-x`) para que el texto
 * seleccionable mida lo mismo que el impreso, y eso no se ve en `offsetWidth`.
 * Midiendo el resultado ya estirado, la caja cae donde se ve el dato.
 */
function cajasDelTexto(
    campo: CampoAnclado,
    divs: readonly HTMLElement[],
    indice: IndiceTexto | null,
    origen: DOMRect,
): Rectangulo[] {
    if (!indice || divs.length === 0) return [];

    const cajas: Rectangulo[] = [];

    for (const coincidencia of buscaEnPagina(indice, campo.tokens, campo.pistas)) {
        let x0 = Infinity;
        let y0 = Infinity;
        let x1 = -Infinity;
        let y1 = -Infinity;

        for (const i of coincidencia.items) {
            const div = divs[i];
            if (!div) continue;
            const rect = div.getBoundingClientRect();
            x0 = Math.min(x0, rect.left);
            y0 = Math.min(y0, rect.top);
            x1 = Math.max(x1, rect.right);
            y1 = Math.max(y1, rect.bottom);
        }

        if (!Number.isFinite(x0)) continue;

        // `getBoundingClientRect` da coordenadas de la ventana y el resaltado se
        // coloca dentro de la pagina, asi que se pasa a su origen.
        cajas.push({
            x: x0 - origen.left,
            y: y0 - origen.top,
            ancho: Math.max(x1 - x0, 1),
            alto: Math.max(y1 - y0, 1),
            // Sin pista al lado el dato puede ser cualquier cifra que se le
            // parezca, y eso se dice con el borde discontinuo.
            dudosa: !coincidencia.conPista,
        });
    }

    return cajas;
}

/**
 * Una caja del resaltado, con su rótulo.
 *
 * La caja no recibe clics (`pointer-events-none` en el contenedor): si los
 * recibiera, no se podría seleccionar el texto que hay debajo, y poder copiar el
 * dato del PDF es media razón de tener el documento al lado. El rótulo sí los
 * recibe, porque pulsarlo lleva al campo del formulario y eso no le quita nada a
 * nadie.
 */
function Resalte({
    caja,
    atenuada,
    onCampo,
}: {
    caja: Caja;
    atenuada: boolean;
    onCampo?: (campo: CampoAnclable) => void;
}) {
    const tinta = TINTA[caja.campo];
    const conRotulo = caja.primera && caja.ancho >= ANCHO_MINIMO_ROTULO;

    return (
        <div
            data-ancla={caja.campo}
            data-dudosa={caja.dudosa ? "si" : "no"}
            className="absolute rounded-[3px] transition-opacity"
            style={{
                left: caja.x,
                top: caja.y,
                width: caja.ancho,
                height: caja.alto,
                backgroundColor: `rgb(${tinta} / 0.16)`,
                // El borde se hace con `box-shadow` y no con `border` porque un
                // borde de verdad cambiaría la caja de sitio: el ancho incluye
                // el borde, y la caja dejaría de caer sobre el dato.
                boxShadow: `inset 0 0 0 ${caja.dudosa ? 1 : 2}px rgb(${tinta} / ${
                    caja.dudosa ? 0.55 : 0.85
                })`,
                opacity: atenuada ? 0.18 : 1,
            }}
        >
            {conRotulo ? (
                <button
                    type="button"
                    onClick={() => onCampo?.(caja.campo)}
                    title={`${ETIQUETA_CORTA[caja.campo]} — buscar en el formulario`}
                    style={{ backgroundColor: `rgb(${tinta} / 0.92)` }}
                    className="pointer-events-auto absolute -top-4 left-0 rounded-sm px-1 text-[10px] leading-4 font-medium whitespace-nowrap text-white focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                    {ETIQUETA_CORTA[caja.campo]}
                </button>
            ) : null}
        </div>
    );
}
