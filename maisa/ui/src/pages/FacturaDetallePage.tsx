/**
 * El expediente de una factura: todo lo que hace falta para decidir si se paga.
 *
 * Esta pantalla no calcula nada. La severidad de un hecho, los colores, los
 * nombres de las reglas y los textos de método y escalón viven en `theme.ts` y en
 * los componentes que ya existen; aquí solo se pone cada pieza en su sitio. Si la
 * página decidiera algo por su cuenta, la tabla y el detalle podrían acabar
 * diciendo cosas distintas de la misma factura, y en un panel que decide pagos eso
 * es peor que un fallo de pintado.
 *
 * Tres cosas del comportamiento que no son de gusto:
 *
 * 1. **El orden de lectura es el orden en que se decide.** Primero la decisión con
 *    sus motivos, después las reglas que la sostienen, después la lectura cruda y
 *    al final lo que el motor detectó como intento de manipular a quien lee. Al
 *    revés — empezando por los datos — hay que bajar hasta el final para saber de
 *    qué se está hablando.
 *
 * 2. **El documento va al lado y se queda quieto.** Un PDF de tres páginas junto a
 *    veinte hechos: si el documento se va con el scroll, se pierde la comparación,
 *    que es justo para lo que está ahí. Por eso la columna derecha es `sticky`.
 *
 * 3. **El `fileId` sale de la URL y no se toca.** `useParams` ya lo devuelve
 *    decodificado (el listado enlaza con `encodeURIComponent`), así que se pasa tal
 *    cual a `useDetalle` y a `PanelDocumento`. Volver a codificarlo pediría un
 *    fichero con un `%` dentro del nombre, que no existe en el corpus.
 *
 * 4. **El documento y el formulario se miran.** El visor resalta dónde está escrito
 *    cada dato y el formulario deja anotar lo que el motor no supo leer, así que los
 *    dos hablan de los mismos siete campos. El estado que los une —cuál está
 *    señalado— vive aquí, en la pantalla que compone los dos, y no dentro de
 *    ninguno: si cada uno tuviera el suyo, señalar un dato en el PDF no haría nada
 *    en el formulario, que es exactamente lo que se espera que haga.
 */

import { ArrowLeft, FileQuestion } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { CompletarDatos } from "@/components/Correcciones";
import { FalloDeCarga } from "@/components/Estados";
import { CamposCrudos, Sospechosos } from "@/components/Evidencia";
import { AvisoFuente } from "@/components/Fuente";
import { PanelDocumento } from "@/components/PanelDocumento";
import { PanelHechos } from "@/components/PanelHechos";
import { ResumenFactura } from "@/components/ResumenFactura";
import { Button } from "@/components/ui/button";
import {
    Empty,
    EmptyContent,
    EmptyDescription,
    EmptyHeader,
    EmptyMedia,
    EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnclajes, useCorrecciones, useDetalle } from "@/api/hooks";

/**
 * El reparto de columnas, en una constante.
 *
 * La usan la pantalla y su esqueleto de carga, y tiene que ser la misma: si el
 * esqueleto se pintara de otra forma, al llegar los datos la página daría un
 * salto de sitio justo cuando el usuario ya está leyendo.
 *
 * `minmax(0, 1fr)` y no `1fr` a secas porque el contenido de la izquierda lleva
 * `file_id` largos y valores de `campos` sin espacios: con `1fr` el mínimo
 * automático de la pista crece con el contenido y una de las dos columnas se come
 * a la otra.
 */
const COLUMNAS = "grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]";

/**
 * La vuelta al listado, en dos enlaces que no hacen lo mismo.
 *
 * El primero conserva el sitio: `location.search` **es** el estado del listado
 * (sus filtros y su página), porque el listado enlaza aquí pegándolo al `fileId`.
 * El `state` es lo que le permite marcar la fila de la que se viene. Sin él,
 * volver de un expediente deja al usuario en la tabla pero sin saber por dónde
 * iba, que es justo lo que se quiere conservar cuando se revisan 40 escaladas una
 * a una. Y el texto dice lo que hace: con filtros se vuelve a los resultados, sin
 * ellos al listado entero.
 *
 * El segundo existe para el enlace directo (pegado en un chat, abierto en otra
 * pestaña): ahí no hay listado del que volver y el botón "atrás" del navegador
 * saldría del panel entero. "Ver todas" es la entrada al listado que no depende
 * del historial, y cuando sí hay filtros además los quita, que es lo que se quiere
 * cuando la lista filtrada era un callejón sin salida.
 */
function Cabecera({ fileId, busqueda }: { fileId: string; busqueda: string }) {
    return (
        <div className="space-y-1">
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                <Link
                    to={`/facturas${busqueda}`}
                    state={{ desde: fileId }}
                    className="inline-flex items-center gap-1.5 rounded-sm text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                    <ArrowLeft className="size-3.5" />
                    {busqueda ? "Volver a los resultados" : "Volver al listado"}
                </Link>

                <Link
                    to="/facturas"
                    className="rounded-sm text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                    Ver todas las facturas
                </Link>
            </div>

            {/*
             * El nombre del fichero es el `<h1>` de la pantalla, en mono y con
             * `break-all` porque es largo (`2026-01-08_P001.pdf`) y sin partirse
             * empujaría el ancho de la página en el móvil. Se lee del `fileId` de
             * la URL y no de `detalle.file_id`: es el mismo, pero así el
             * encabezado existe desde el primer pintado, antes de que llegue el
             * expediente.
             */}
            <h1 className="font-mono text-xl font-semibold break-all sm:text-2xl">{fileId}</h1>
        </div>
    );
}

/**
 * Un bloque del expediente con su encabezado.
 *
 * Las tarjetas ya traen su título visible, así que el `<h2>` no lo repite: nombra
 * el bloque ("Por qué se decidió esto", "Lectura del documento") para que el orden
 * de lectura se pueda seguir —y anunciar— sin depender de los títulos de dentro de
 * cada tarjeta. `aria-labelledby` apunta al propio `<h2>` para no escribir el
 * nombre del bloque dos veces.
 */
function Seccion({ id, titulo, children }: { id: string; titulo: string; children: ReactNode }) {
    return (
        <section aria-labelledby={id} className="space-y-2">
            <h2 id={id} className="text-sm font-medium text-muted-foreground">
                {titulo}
            </h2>
            {children}
        </section>
    );
}

/**
 * La ruta no trae `fileId`.
 *
 * Con `/facturas/:fileId` no debería pasar, pero el tipo es `string | undefined` y
 * un estado vacío decente cuesta menos que una pantalla en blanco: quien ha pegado
 * una URL a medias tiene que entender qué falta, y no ver un error de carga que no
 * lo es.
 */
function SinFactura() {
    return (
        <Empty className="rounded-xl bg-card ring-1 ring-foreground/10">
            <EmptyHeader>
                <EmptyMedia variant="icon">
                    <FileQuestion />
                </EmptyMedia>
                <EmptyTitle>Esta dirección no lleva a ningún expediente</EmptyTitle>
                <EmptyDescription>
                    El expediente se pide por el nombre del PDF, tal cual
                    (`/facturas/2026-01-08_P001.pdf`), y en esta dirección no hay ninguno. No es
                    un fallo de carga: es que falta la factura que habría que enseñar.
                </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
                <Button asChild variant="outline">
                    <Link to="/facturas">
                        <ArrowLeft data-icon="inline-start" />
                        Ir al listado de facturas
                    </Link>
                </Button>
            </EmptyContent>
        </Empty>
    );
}

/**
 * La pantalla mientras no hay expediente: esqueletos con el mismo reparto que la
 * real, y ni un texto de "Cargando…". Mientras `fuente.ts` está decidiendo si se
 * lee de la API o del congelado no hay nada que contar, y un texto que no cambia
 * en dos segundos se lee como que la pantalla se ha quedado colgada.
 */
function Esqueleto() {
    return (
        <div className={COLUMNAS} aria-busy="true">
            <div className="space-y-6">
                <Skeleton className="h-64 rounded-xl" />
                <Skeleton className="h-80 rounded-xl" />
                <Skeleton className="h-72 rounded-xl" />
            </div>
            <Skeleton className="h-[60vh] min-h-80 rounded-xl" />
        </div>
    );
}

export default function FacturaDetallePage() {
    const { fileId } = useParams<{ fileId: string }>();
    const { search } = useLocation();

    // El hook se llama siempre, aunque falte el `fileId`: no puede ir después de un
    // `return`, porque las reglas de los hooks no lo permiten. Con `fileId` a
    // `undefined` la petición ni se lanza —`usePeticion` recibe clave `null`—, así
    // que el `throw` que lleva dentro no llega a ocurrir.
    const detalle = useDetalle(fileId);

    // Los anclajes se piden una sola vez aquí y se reparten: los necesita el visor
    // para resaltar y el formulario para contrastar. Se lanzan a la vez que el
    // detalle, en paralelo, y no cuando llegue: encadenarlos añadiría una vuelta de
    // red a una pantalla que ya hace tres.
    const anclajes = useAnclajes(fileId);

    // Las correcciones salen del detalle, que ya las trae. Mientras el detalle no
    // ha llegado son `undefined` —"no lo sé todavía"—, y el hook lo trata como
    // "ninguna": pintar un formulario vacío y luego llenarlo se lee como un fallo.
    const edicion = useCorrecciones(fileId ?? "", detalle.datos?.correcciones);

    // El campo señalado, compartido por el visor y el formulario. Es un `string` y
    // no un `CampoAnclable` porque llega de la URL y de un clic: quien lo escribe
    // puede ser cualquiera de los dos, y cada uno valida lo suyo.
    const [campoActivo, setCampoActivo] = useState<string | null>(null);

    if (!fileId) {
        return (
            <div className="space-y-6">
                <AvisoFuente />
                <SinFactura />
            </div>
        );
    }

    const cabecera = <Cabecera fileId={fileId} busqueda={search} />;
    const volverA = `/facturas${search}`;

    if (detalle.error) {
        return (
            <div className="space-y-6">
                <AvisoFuente />
                {cabecera}

                <FalloDeCarga
                    error={detalle.error}
                    alReintentar={detalle.reintentar}
                    queEs="el expediente"
                />

                {/*
                 * El enlace de arriba ya vuelve al listado, pero se repite aquí a
                 * propósito: cuando algo falla, el ojo se va al aviso rojo y no a
                 * la esquina de arriba. Y una pantalla de error sin salida es una
                 * ratonera: lo único que se puede hacer con esta factura es dejarla
                 * e ir a por otra.
                 */}
                <p className="text-sm text-muted-foreground">
                    Puedes{" "}
                    <Link
                        to={volverA}
                        state={{ desde: fileId }}
                        className="rounded-sm underline underline-offset-4 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                    >
                        volver a la lista
                    </Link>{" "}
                    y abrir otra factura. El enlace de arriba hace lo mismo.
                </p>
            </div>
        );
    }

    const factura = detalle.datos;

    if (!factura) {
        return (
            <div className="space-y-6">
                <AvisoFuente />
                {cabecera}
                <Esqueleto />
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <AvisoFuente />

            {cabecera}

            <div className={COLUMNAS}>
                <div className="space-y-6">
                    <Seccion id="seccion-decision" titulo="Decisión">
                        <ResumenFactura detalle={factura} />
                    </Seccion>

                    <Seccion id="seccion-porque" titulo="Por qué se decidió esto">
                        <PanelHechos
                            hechos={factura.hechos}
                            divisa={factura.resumen.divisa}
                        />
                    </Seccion>

                    <Seccion id="seccion-lectura" titulo="Lectura del documento">
                        <CamposCrudos
                            campos={factura.campos}
                            divisa={factura.resumen.divisa}
                        />
                    </Seccion>

                    {/*
                     * Va pegado a la lectura cruda porque es su continuación: arriba
                     * está lo que sacó el motor y aquí lo que dice quien tiene la
                     * factura delante. Separarlos obligaría a subir y bajar para
                     * comparar los mismos siete campos.
                     */}
                    <Seccion id="seccion-completar" titulo="Completar a mano">
                        <CompletarDatos
                            edicion={edicion}
                            anclajes={anclajes.datos}
                            cargandoAnclajes={anclajes.cargando}
                            campoActivo={campoActivo}
                            onCampoActivo={setCampoActivo}
                        />
                    </Seccion>

                    {/*
                     * Las órdenes salen de `campos.ordenes_resultado`, que es donde
                     * el motor deja lo que encontró escrito dentro del PDF. No se
                     * pasan desde `lectura.sospechosos`: son dos listas distintas y
                     * en 3 de los 4 casos del corpus la de `lectura` viene vacía
                     * aunque el documento sí traiga órdenes.
                     */}
                    <Seccion id="seccion-ordenes" titulo="Órdenes dentro del documento">
                        <Sospechosos
                            lectura={factura.lectura}
                            ordenes={factura.campos.ordenes_resultado ?? []}
                        />
                    </Seccion>
                </div>

                {/*
                 * `top-20` (5 rem) es lo que ocupa la cabecera del marco —`h-14`,
                 * 3,5 rem— más el padding de arriba del `main`, 1,5 rem: con un
                 * `top` menor, el documento se metería por debajo de la cabecera
                 * pegajosa y se perdería el borde de arriba del PDF. `self-start`
                 * es lo que hace falta para que la columna no se estire a lo alto
                 * de la de la izquierda: una caja estirada no tiene recorrido que
                 * pegar.
                 *
                 * El `<h2>` va oculto porque la tarjeta ya imprime el suyo
                 * ("Documento original") justo debajo, y repetirlo sería la misma
                 * frase dos veces seguidas; existe para que el bloque tenga nombre
                 * en el árbol de accesibilidad.
                 */}
                <section
                    aria-labelledby="seccion-documento"
                    className="lg:sticky lg:top-20 lg:self-start"
                >
                    <h2 id="seccion-documento" className="sr-only">
                        Documento original
                    </h2>

                    <PanelDocumento
                        fileId={fileId}
                        sha256={factura.sha256}
                        anclajes={anclajes}
                        campoActivo={campoActivo}
                        onCampo={setCampoActivo}
                    />
                </section>
            </div>
        </div>
    );
}
