/**
 * Ruta `/escalabilidad` — capacidad, coste y plan de crecimiento.
 *
 * Esta pantalla no calcula nada y no mide nada: pinta el banco de medidas que
 * genera `tools/generar_escalabilidad.py` desde `motor/docs/bench.json`. Eso es
 * deliberado y es la razón de que exista. Las cifras de capacidad y coste de un
 * sistema son justo donde más fácil es escribir un folleto, y aquí cada número
 * que se enseña viene de una medición con su fecha, su máquina y su origen.
 *
 * Las tres decisiones de esta pantalla que no son de pintado:
 *
 * 1. **Lo medido y lo extrapolado van separados y etiquetados.** La tabla de
 *    trabajadores son tres pasadas cronometradas; la tabla de 50 000 facturas es
 *    un modelo. Pintarlas igual haría pasar una extrapolación por una medición, y
 *    la primera pregunta que hará quien mire la segunda tabla es si se probó. No
 *    se probó: se midió a 500 y el modelo reproduce esa medida con un −2,6 % de
 *    error, y eso es lo que se enseña.
 *
 * 2. **El coste se da en vCPU·s y no en euros.** No tenemos la tarifa de esta
 *    máquina, así que dar un precio sería inventarlo. Se da la unidad que sí se
 *    midió y la fórmula para convertirla, y quien tenga la tarifa multiplica. Hoy
 *    el coste en euros es 0 y seguirá siéndolo, pero eso es una afirmación sobre
 *    el despliegue, no sobre la factura de una nube.
 *
 * 3. **Los límites se enseñan con la misma jerarquía que las capacidades.** Un
 *    panel que solo cuenta lo que escala bien es un folleto. Los diez límites de
 *    abajo son cosas que hoy NO se pueden hacer, cada una con la cifra que la
 *    sostiene y el documento del que sale.
 */

import {
    AlertTriangle,
    Cpu,
    Gauge,
    GitBranch,
    HardDrive,
    Layers,
    LineChart,
    Sigma,
    Sparkles,
    Target,
} from "lucide-react";
import type { ReactNode } from "react";
import { Fragment } from "react";

import { useEscalabilidad } from "@/api/hooks";
import type { Escalabilidad } from "@/api/escalabilidad";
import { FalloDeCarga } from "@/components/Estados";
import { Badge } from "@/components/ui/badge";
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { decimal, duracion, entero, fechaHora } from "@/lib/formato";
import { cn } from "@/lib/utils";

/** El reparto de columnas de una tabla que puede desbordar en un móvil. */
const CELDA_NUMERO = "text-right tabular-nums";

/** Los anclajes de la página. Se enseñan arriba para poder saltar sin bajar. */
const ANCLAS = [
    { id: "capacidad", etiqueta: "Capacidad" },
    { id: "hardware", etiqueta: "Hardware" },
    { id: "limites", etiqueta: "Límites" },
    { id: "coste", etiqueta: "Fórmula de coste" },
    { id: "plan", etiqueta: "Plan de crecimiento" },
    { id: "honestidad", etiqueta: "Lo que no sabemos" },
];

/**
 * El texto del banco, con sus nombres propios en monoespaciada.
 *
 * Las frases que se pintan aquí se escriben en `tools/generar_escalabilidad.py`
 * y rodean con acentos graves lo que es un fichero, una clave o un símbolo
 * (`.cache/ocr`, `continuar=True`, `escalon`). En el JSON viajan con los acentos
 * dentro, así que sin partirlos saldrían literales en pantalla: peor que no
 * haberlos puesto, porque el acento grave se lee como un error de tecleo.
 *
 * Se parten por el delimitador y los tramos impares van en `<code>`. Un texto
 * con un número impar de acentos deja el último tramo sin cerrar en texto
 * normal, que es la degradación correcta: se ve el contenido, no se pierde.
 */
function Prosa({ children }: { children: string }) {
    const tramos = children.split("`");

    return (
        <>
            {tramos.map((tramo, indice) =>
                indice % 2 === 1 ? (
                    <code
                        key={indice}
                        className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em] break-all"
                    >
                        {tramo}
                    </code>
                ) : (
                    <Fragment key={indice}>{tramo}</Fragment>
                ),
            )}
        </>
    );
}

export default function EscalabilidadPage() {
    const { datos, error, cargando, reintentar } = useEscalabilidad();

    if (error) {
        return (
            <FalloDeCarga
                error={error}
                alReintentar={reintentar}
                queEs="el banco de medidas de capacidad y coste"
            />
        );
    }

    if (cargando || !datos) return <Esqueleto />;

    return (
        <div className="space-y-8">
            <Cabecera datos={datos} />
            <Resumen datos={datos} />
            <Capacidad datos={datos} />
            <Hardware datos={datos} />
            <Limites datos={datos} />
            <Coste datos={datos} />
            <Plan datos={datos} />
            <Honestidad datos={datos} />
        </div>
    );
}

/**
 * El esqueleto de carga.
 *
 * Tiene la misma altura aproximada que el contenido real para que la página no dé
 * un salto de sitio cuando lleguen los datos. El fichero es un JSON pequeño que se
 * sirve del propio build, así que esto casi nunca se ve; existe para el caso de
 * red lenta, que es cuando más molesta un salto.
 */
function Esqueleto() {
    return (
        <div className="space-y-6" aria-busy="true" aria-live="polite">
            <div className="space-y-2">
                <Skeleton className="h-8 w-72" />
                <Skeleton className="h-4 w-full max-w-2xl" />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {[0, 1, 2, 3].map((indice) => (
                    <Skeleton key={indice} className="h-28 rounded-xl" />
                ))}
            </div>
            <Skeleton className="h-72 rounded-xl" />
            <Skeleton className="h-72 rounded-xl" />
        </div>
    );
}

function Cabecera({ datos }: { datos: Escalabilidad }) {
    const { procedencia, hardware } = datos;

    return (
        <header className="space-y-4">
            <div className="space-y-2">
                <h1 className="font-heading text-2xl font-semibold tracking-tight sm:text-3xl">
                    Escalabilidad y coste
                </h1>
                <p className="max-w-3xl text-sm text-muted-foreground">
                    Cuánto aguanta el sistema, cuánto cuesta una factura, dónde está el cuello de
                    botella y qué haríamos para multiplicar el volumen por diez. Todo lo que dice
                    «medido» se cronometró sobre el lote de {entero(datos.capacidad.lote)} facturas
                    en esta máquina; lo que dice «extrapolado» es un modelo con sus constantes
                    medidas a la vista.
                </p>
            </div>

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

            <p className="text-xs text-muted-foreground">
                Medido el <strong className="font-medium">{fechaHora(procedencia.medido_en)}</strong>{" "}
                por <span className="font-mono">{procedencia.medido_por}</span> sobre{" "}
                <strong className="font-medium">
                    {hardware.cpu_logicos} vCPU / {decimal(hardware.memoria_total_gb, 2)} GB
                </strong>
                . Fuente: <span className="font-mono">{procedencia.fuente}</span>.{" "}
                <Prosa>{procedencia.nota}</Prosa>
            </p>
        </header>
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
    tono?: "verde" | "neutro";
}) {
    return (
        <Card size="sm" className="gap-0">
            <CardContent className="flex flex-col gap-1">
                <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                    {titulo}
                </span>
                <span
                    className={cn(
                        "font-heading text-3xl leading-none font-semibold tabular-nums",
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

function Resumen({ datos }: { datos: Escalabilidad }) {
    const { capacidad, coste, hardware } = datos;

    return (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tarjeta
                titulo="Coste hoy"
                valor={`${entero(coste.hoy_eur)} €`}
                pie="Y seguirá en 0 €: todo corre en local"
                tono="verde"
            />
            <Tarjeta
                titulo={`Lote de ${entero(capacidad.lote)}`}
                valor={duracion(capacidad.mejor_mediana_s)}
                pie={`Con ${capacidad.mejor_trabajadores} trabajadores, caché caliente`}
            />
            <Tarjeta
                titulo="Rendimiento"
                valor={`${decimal(capacidad.mejor_facturas_por_s, 1)}/s`}
                pie={`Dispersión del ${decimal(capacidad.mejor_rango_relativo_pct, 1)} % entre pasadas`}
            />
            <Tarjeta
                titulo="Sin pagar OCR"
                valor={`${decimal(capacidad.reparto_lectura.pct_capa_texto, 1)} %`}
                pie={`${entero(capacidad.reparto_lectura.capa_texto)} de ${entero(capacidad.lote)} facturas por capa de texto`}
            />
            <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-4">
                El motor de decisión cuesta{" "}
                <strong className="font-medium">
                    {decimal(capacidad.decision_ms_por_factura, 3)} ms por factura
                </strong>{" "}
                (regex + aritmética + precedencia, sin modelo de lenguaje). Todo lo demás es leer
                los documentos: por eso la palanca de coste no es decidir más rápido, es no llamar
                al OCR cuando la capa de texto ya es exacta. Medido en{" "}
                {hardware.cpu_logicos} vCPU / {decimal(hardware.memoria_total_gb, 2)} GB.
            </p>
        </div>
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
    icono: typeof Gauge;
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

function Capacidad({ datos }: { datos: Escalabilidad }) {
    const { capacidad } = datos;

    return (
        <Seccion
            id="capacidad"
            icono={Gauge}
            titulo="Capacidad medida"
            descripcion={`Tres pasadas del lote completo de ${entero(capacidad.lote)} facturas por cada número de trabajadores, end-to-end y escribiendo el JSONL. Todos los tiempos son de pared, cronometrados con perf_counter en una máquina compartida (la carga media de cada pasada está en el JSON).`}
        >
            <div className="grid gap-4 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle>Trabajadores frente a tiempo</CardTitle>
                        <CardDescription>
                            La mejor configuración medida es{" "}
                            <strong className="font-medium text-foreground">
                                {capacidad.mejor_trabajadores} trabajadores
                            </strong>
                            . A partir de ahí no se compra nada: los hilos de Python comparten GIL y
                            el trabajo no se reparte entre núcleos.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>Hilos</TableHead>
                                    <TableHead className={CELDA_NUMERO}>Mediana</TableHead>
                                    <TableHead className={CELDA_NUMERO}>Mín – máx</TableHead>
                                    <TableHead className={CELDA_NUMERO}>Dispersión</TableHead>
                                    <TableHead className={CELDA_NUMERO}>Facturas/s</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {capacidad.trabajadores.map((fila) => (
                                    <TableRow
                                        key={fila.trabajadores}
                                        className={cn(
                                            fila.trabajadores === capacidad.mejor_trabajadores &&
                                                "bg-muted/40 font-medium",
                                        )}
                                    >
                                        <TableCell className="tabular-nums">
                                            {fila.trabajadores}
                                            {fila.trabajadores === capacidad.mejor_trabajadores ? (
                                                <Badge variant="secondary" className="ml-2">
                                                    mejor
                                                </Badge>
                                            ) : null}
                                        </TableCell>
                                        <TableCell className={CELDA_NUMERO}>
                                            {duracion(fila.mediana_s)}
                                        </TableCell>
                                        <TableCell className={cn(CELDA_NUMERO, "text-muted-foreground")}>
                                            {duracion(fila.min_s)} – {duracion(fila.max_s)}
                                        </TableCell>
                                        <TableCell className={CELDA_NUMERO}>
                                            {decimal(fila.rango_relativo_pct, 1)} %
                                        </TableCell>
                                        <TableCell className={CELDA_NUMERO}>
                                            {decimal(fila.facturas_por_s, 1)}
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                        <p className="mt-3 text-xs text-muted-foreground">
                            Con la traza encadenada activada (<span className="font-mono">
                                --traza-hash
                            </span>
                            ), el lote tarda {duracion(capacidad.con_traza.segundos)} a{" "}
                            {capacidad.con_traza.trabajadores} trabajadores y emite{" "}
                            {entero(capacidad.con_traza.eventos)} eventos de auditoría.
                        </p>
                    </CardContent>
                </Card>

                <div className="space-y-4">
                    <Card>
                        <CardHeader>
                            <CardTitle>Dónde se va el tiempo</CardTitle>
                            <CardDescription>
                                Desglose por fase a {capacidad.mejor_trabajadores} trabajadores. Leer
                                los documentos es el 95 % y el motor de reglas es ruido:{" "}
                                {decimal(capacidad.decision_ms_por_factura, 3)} ms por factura.
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            {capacidad.fases.map((fase) => (
                                <div key={fase.fase} className="space-y-1">
                                    <div className="flex items-baseline justify-between gap-3 text-sm">
                                        <span>{fase.fase}</span>
                                        <span className="tabular-nums text-muted-foreground">
                                            {duracion(fase.segundos)} ·{" "}
                                            {decimal(fase.reparto_pct, 1)} %
                                        </span>
                                    </div>
                                    <Progress value={fase.reparto_pct} className="h-1" />
                                </div>
                            ))}
                            <p className="text-xs text-muted-foreground">
                                Total de las fases: {duracion(capacidad.total_fases_s)}. La suma no
                                incluye el arranque del intérprete, que es por eso por lo que el
                                lote completo tarda algo más.
                            </p>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Cómo se lee cada factura</CardTitle>
                            <CardDescription>
                                El reparto decide el coste entero del sistema: la capa de texto es
                                exacta y gratis, la visión es lo único que se paga.
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            <RepartoLectura
                                etiqueta="Capa de texto del PDF"
                                cuantas={capacidad.reparto_lectura.capa_texto}
                                total={capacidad.lote}
                                coste="0 vCPU·s"
                                tiempo={`${decimal(capacidad.lectura_texto.ms_por_factura, 2)} ms`}
                            />
                            <RepartoLectura
                                etiqueta="Visión OCR (escaneadas)"
                                cuantas={capacidad.reparto_lectura.ocr}
                                total={capacidad.lote}
                                coste={`${decimal(capacidad.ocr.servicio_s_por_factura, 2)} vCPU·s`}
                                tiempo={`${decimal(capacidad.ocr.frio_serial_s_por_factura, 2)} s`}
                                tono="aviso"
                            />
                            <p className="text-xs text-muted-foreground">
                                Una escaneada <strong className="font-medium">ya vista</strong> cuesta{" "}
                                {decimal(capacidad.lectura_cache.ms_por_factura, 2)} ms y 0 vCPU·s:
                                la caché es por <span className="font-mono">sha256</span> del PDF, no
                                por nombre, así que reejecutar el lote no vuelve a pagar visión.
                            </p>
                        </CardContent>
                    </Card>
                </div>
            </div>

            <Card>
                <CardHeader>
                    <CardTitle>El OCR, medido aparte</CardTitle>
                    <CardDescription>
                        Es el único componente que no escala con la CPU que le demos. Por eso tiene
                        su propia medida, con el contenedor en frío y la caché en un directorio
                        temporal.
                    </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                    <Metrica
                        icono={Sparkles}
                        etiqueta="Servicio OCR"
                        valor={`${decimal(capacidad.ocr.servicio_s_por_factura, 2)} s`}
                        pie="por factura escaneada, mediana"
                    />
                    <Metrica
                        icono={Target}
                        etiqueta="Techo por ranura"
                        valor={`${entero(capacidad.ocr.facturas_por_hora_por_ranura)}/h`}
                        pie="facturas escaneadas por hora y ranura"
                    />
                    <Metrica
                        icono={Cpu}
                        etiqueta="¿Paraleliza?"
                        valor={capacidad.ocr.paraleliza_el_contenedor ? "Sí" : "No"}
                        pie={`×${decimal(capacidad.ocr.speedup_1_a_4_hilos, 2)} al pasar de 1 a 4 hilos`}
                    />
                    <Metrica
                        icono={HardDrive}
                        etiqueta="Escaneada en frío"
                        valor={`${decimal(capacidad.ocr.frio_serial_s_por_factura, 2)} s`}
                        pie={`${decimal(capacidad.ocr.frio_serial_facturas_por_s, 3)} facturas/s en serie`}
                    />
                    <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-4">
                        Consecuencia práctica: subir <span className="font-mono">--trabajadores</span>{" "}
                        no compra OCR. Se compra con más ranuras de visión, con varias réplicas del
                        contenedor, o —mucho más barato— con la caché.
                    </p>
                </CardContent>
            </Card>
        </Seccion>
    );
}

function RepartoLectura({
    etiqueta,
    cuantas,
    total,
    coste,
    tiempo,
    tono,
}: {
    etiqueta: string;
    cuantas: number;
    total: number;
    coste: string;
    tiempo: string;
    tono?: "aviso";
}) {
    const pct = total ? (100 * cuantas) / total : 0;

    return (
        <div className="space-y-1">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-sm">
                <span className="flex items-center gap-2">
                    {etiqueta}
                    <Badge variant={tono === "aviso" ? "destructive" : "secondary"}>{coste}</Badge>
                </span>
                <span className="tabular-nums text-muted-foreground">
                    {entero(cuantas)} · {decimal(pct, 1)} % · {tiempo}
                </span>
            </div>
            <Progress
                value={pct}
                className={cn("h-1", tono === "aviso" && "[&>[data-slot=progress-indicator]]:bg-amber-500")}
            />
        </div>
    );
}

function Metrica({
    icono: Icono,
    etiqueta,
    valor,
    pie,
}: {
    icono: typeof Gauge;
    etiqueta: string;
    valor: string;
    pie: string;
}) {
    return (
        <div className="space-y-1">
            <span className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                <Icono className="size-3.5" />
                {etiqueta}
            </span>
            <p className="font-heading text-2xl leading-none font-semibold tabular-nums">{valor}</p>
            <p className="text-xs text-muted-foreground">{pie}</p>
        </div>
    );
}

function Hardware({ datos }: { datos: Escalabilidad }) {
    const { hardware } = datos;

    return (
        <Seccion
            id="hardware"
            icono={Cpu}
            titulo="Hardware de la medida"
            descripcion="Sin esto, «135 facturas/s» no significa nada: el mismo motor sobre otra máquina da otra cifra. Es la máquina del hackathon, y estaba compartida mientras se medía."
        >
            <Card>
                <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
                    <Metrica
                        icono={Cpu}
                        etiqueta="vCPU"
                        valor={entero(hardware.cpu_logicos)}
                        pie="núcleos lógicos"
                    />
                    <Metrica
                        icono={HardDrive}
                        etiqueta="Memoria"
                        valor={`${decimal(hardware.memoria_total_gb, 2)} GB`}
                        pie="RAM total"
                    />
                    <Metrica
                        icono={Layers}
                        etiqueta="Python"
                        valor={hardware.python}
                        pie="el motor es Python puro"
                    />
                    <Metrica
                        icono={LineChart}
                        etiqueta="Carga al medir"
                        valor={hardware.carga_media_al_inicio.map((valor) => decimal(valor, 2)).join(" · ")}
                        pie="media de 1, 5 y 15 minutos"
                    />
                    <Metrica
                        icono={HardDrive}
                        etiqueta="Plataforma"
                        valor={hardware.plataforma.split("-with-")[0].replace("Linux-", "Linux ")}
                        pie={hardware.plataforma.split("-with-")[1] ?? ""}
                    />
                    <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-5">
                        {hardware.nota}. Las cifras de tiempo se ven afectadas por esa carga: por eso
                        cada fila de la tabla de trabajadores trae su dispersión y no solo su mediana.
                    </p>
                </CardContent>
            </Card>
        </Seccion>
    );
}

function Limites({ datos }: { datos: Escalabilidad }) {
    return (
        <Seccion
            id="limites"
            icono={AlertTriangle}
            titulo="Límites reconocidos"
            descripcion="Lo que hoy no se puede hacer, con la cifra que lo sostiene y el documento del que sale. Enseñarlos tiene el mismo peso que enseñar las capacidades: un panel que solo cuenta lo que va bien es un folleto."
        >
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {datos.limites.map((limite) => (
                    <Card key={limite.titulo} size="sm">
                        <CardHeader>
                            <div className="flex items-start justify-between gap-2">
                                <CardTitle>{limite.titulo}</CardTitle>
                                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                            </div>
                            <CardDescription className="font-mono text-xs text-amber-700 dark:text-amber-300">
                                {limite.magnitud}
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-2">
                            <p className="text-sm">
                                <Prosa>{limite.detalle}</Prosa>
                            </p>
                            <p className="font-mono text-xs break-all text-muted-foreground">
                                {limite.origen}
                            </p>
                        </CardContent>
                    </Card>
                ))}
            </div>
        </Seccion>
    );
}

function Coste({ datos }: { datos: Escalabilidad }) {
    const { coste } = datos;

    return (
        <Seccion
            id="coste"
            icono={Sigma}
            titulo="Fórmula de coste"
            descripcion="El modelo que convierte el reparto de lectura en tiempo y en vCPU·s. Está contrastado contra la medida real a 500 facturas: si el modelo no reproduce lo medido, la extrapolación no vale nada."
        >
            <div className="grid gap-4 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle>El modelo</CardTitle>
                        <CardDescription>
                            Dos fórmulas: cuánto tarda y cuánto cuesta. La primera se contrasta
                            contra la realidad; la segunda necesita un precio que aquí no tenemos.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-3">
                        <Formula titulo="Tiempo" texto={coste.formula_tiempo} />
                        <Formula titulo="Euros" texto={coste.formula_euros} />
                        <dl className="grid gap-1 text-xs text-muted-foreground">
                            {Object.entries(coste.regimenes).map(([clave, texto]) => (
                                <div key={clave} className="flex flex-wrap gap-x-2">
                                    <dt className="font-mono text-foreground">{clave}</dt>
                                    <dd>{texto}</dd>
                                </div>
                            ))}
                        </dl>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle>Constantes medidas</CardTitle>
                        <CardDescription>
                            Cada constante de la fórmula y de dónde sale. Ninguna está estimada: las
                            que no se midieron no están en la tabla.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>Constante</TableHead>
                                    <TableHead className={CELDA_NUMERO}>Valor</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {Object.entries(coste.constantes).map(([clave, valor]) => (
                                    <TableRow key={clave}>
                                        <TableCell className="font-mono text-xs">{clave}</TableCell>
                                        <TableCell className={CELDA_NUMERO}>{valor}</TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                        <p className="mt-3 text-xs text-muted-foreground">
                            Contraste del modelo con la realidad a {entero(datos.capacidad.lote)}{" "}
                            facturas: medido {duracion(coste.contraste.medido_s)}, modelo{" "}
                            {duracion(coste.contraste.modelo_s)}, error{" "}
                            <strong className="font-medium">
                                {decimal(coste.contraste.error_relativo_pct, 1)} %
                            </strong>
                            . El modelo reproduce la realidad dentro del ruido de la máquina, y por
                            eso se puede extrapolar con él.
                        </p>
                    </CardContent>
                </Card>
            </div>

            <Card>
                <CardHeader>
                    <CardTitle>Coste unitario, escalón a escalón</CardTitle>
                    <CardDescription>
                        Lo que cuesta una sola factura según por dónde entre. Es la tabla que
                        convierte el modelo en una decisión de negocio.
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead>Escalón de lectura</TableHead>
                                <TableHead className={CELDA_NUMERO}>Coste OCR</TableHead>
                                <TableHead className={CELDA_NUMERO}>Tiempo de pared</TableHead>
                                <TableHead className="hidden sm:table-cell">Recurso externo</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            <TableRow>
                                <TableCell>
                                    Capa de texto{" "}
                                    <span className="text-muted-foreground">
                                        ({decimal(datos.capacidad.reparto_lectura.pct_capa_texto, 1)} % del lote)
                                    </span>
                                </TableCell>
                                <TableCell className={cn(CELDA_NUMERO, "text-emerald-700 dark:text-emerald-300")}>
                                    {coste.por_factura_texto_vcpu_s} vCPU·s
                                </TableCell>
                                <TableCell className={CELDA_NUMERO}>
                                    {decimal(datos.capacidad.lectura_texto.ms_por_factura, 2)} ms
                                </TableCell>
                                <TableCell className="hidden text-muted-foreground sm:table-cell">
                                    ninguna
                                </TableCell>
                            </TableRow>
                            <TableRow>
                                <TableCell>Escaneada ya vista (caché sha256)</TableCell>
                                <TableCell className={cn(CELDA_NUMERO, "text-emerald-700 dark:text-emerald-300")}>
                                    {coste.por_factura_cache_vcpu_s} vCPU·s
                                </TableCell>
                                <TableCell className={CELDA_NUMERO}>
                                    {decimal(datos.capacidad.lectura_cache.ms_por_factura, 2)} ms
                                </TableCell>
                                <TableCell className="hidden text-muted-foreground sm:table-cell">
                                    disco local
                                </TableCell>
                            </TableRow>
                            <TableRow>
                                <TableCell>Escaneada nueva (OCR en frío)</TableCell>
                                <TableCell className={cn(CELDA_NUMERO, "text-amber-700 dark:text-amber-300")}>
                                    {decimal(coste.por_factura_escaneada_vcpu_s, 2)} vCPU·s
                                </TableCell>
                                <TableCell className={CELDA_NUMERO}>
                                    {decimal(datos.capacidad.ocr.frio_serial_s_por_factura, 2)} s
                                </TableCell>
                                <TableCell className="hidden text-muted-foreground sm:table-cell">
                                    contenedor OCR
                                </TableCell>
                            </TableRow>
                        </TableBody>
                    </Table>
                    <p className="mt-3 text-xs text-muted-foreground">
                        Una escaneada nueva cuesta{" "}
                        {entero(
                            Math.round(
                                datos.capacidad.ocr.frio_serial_s_por_factura /
                                    datos.capacidad.lectura_texto.ms_por_factura /
                                    1000,
                            ),
                        )}{" "}
                        veces lo que una de texto. Ahí está todo el gasto:{" "}
                        {entero(coste.vcpu.para_10000_escaneadas)} vCPU·s para 10 000 escaneadas,{" "}
                        {entero(coste.vcpu.para_1000000_escaneadas)} para un millón. En euros:{" "}
                        {coste.formula_euros}.
                    </p>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>
                        Extrapolación
                        <Badge variant="outline" className="ml-2">
                            no medido
                        </Badge>
                    </CardTitle>
                    <CardDescription>
                        Lo que tardaría el lote a 5 000, 50 000 y 1 000 000 de facturas con el
                        modelo de arriba. A 500 el modelo falla un{" "}
                        {decimal(coste.contraste.error_relativo_pct, 1)} %, que es lo único que
                        respalda estas cifras.
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead>Facturas</TableHead>
                                {coste.escenarios.map((escenario) => (
                                    <TableHead key={escenario.clave} className={CELDA_NUMERO}>
                                        <Tooltip>
                                            <TooltipTrigger asChild>
                                                <span className="cursor-help underline decoration-dotted underline-offset-2">
                                                    {escenario.nombre}
                                                </span>
                                            </TooltipTrigger>
                                            <TooltipContent className="max-w-xs">
                                                {escenario.descripcion}
                                            </TooltipContent>
                                        </Tooltip>
                                    </TableHead>
                                ))}
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {coste.objetivos.map((objetivo) => (
                                <TableRow key={objetivo}>
                                    <TableCell className="tabular-nums">{entero(objetivo)}</TableCell>
                                    {coste.escenarios.map((escenario) => (
                                        <TableCell key={escenario.clave} className={CELDA_NUMERO}>
                                            <CeldaEscenario
                                                segundos={escenario.tiempos_s[String(objetivo)]}
                                                porSegundo={escenario.facturas_por_s[String(objetivo)]}
                                            />
                                        </TableCell>
                                    ))}
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                    <div className="grid gap-3 sm:grid-cols-3">
                        {coste.escenarios.map((escenario) => (
                            <div key={escenario.clave} className="space-y-1 text-xs">
                                <p className="font-medium text-foreground">{escenario.nombre}</p>
                                <p className="text-muted-foreground">{escenario.descripcion}</p>
                                <p className="text-muted-foreground">
                                    Coste de OCR aplicado:{" "}
                                    <span className="tabular-nums text-foreground">
                                        {decimal(escenario.coste_ocr_s, 3)} s
                                    </span>{" "}
                                    por escaneada.
                                </p>
                            </div>
                        ))}
                    </div>
                </CardContent>
            </Card>
        </Seccion>
    );
}

/** Una celda de la tabla de extrapolación: el tiempo y, debajo, el ritmo. */
function CeldaEscenario({
    segundos,
    porSegundo,
}: {
    segundos: number | undefined;
    porSegundo: number | undefined;
}) {
    return (
        <span className="inline-flex flex-col items-end">
            <span>{duracion(segundos)}</span>
            <span className="text-xs text-muted-foreground">
                {decimal(porSegundo, 2)}/s
            </span>
        </span>
    );
}

function Formula({ titulo, texto }: { titulo: string; texto: string }) {
    return (
        <div className="space-y-1">
            <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                {titulo}
            </p>
            <pre className="overflow-x-auto rounded-lg bg-muted p-3 text-xs leading-relaxed">
                <code className="font-mono whitespace-pre-wrap break-words">{texto}</code>
            </pre>
        </div>
    );
}

function Plan({ datos }: { datos: Escalabilidad }) {
    const { plan } = datos;

    return (
        <Seccion
            id="plan"
            icono={GitBranch}
            titulo="Plan de crecimiento"
            descripcion="Qué haríamos, por orden de rentabilidad, para multiplicar el volumen por diez y para abrir la puerta a tipos de archivo que hoy no se leen. Todo lo de esta sección cuesta 0 € menos el OCR, y el OCR es justo lo que la caché evita pagar dos veces."
        >
            <div className="grid gap-4 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle>Más volumen</CardTitle>
                        <CardDescription>
                            Siete pasos en orden. Los tres primeros son los que de verdad mueven la
                            aguja; los demás evitan que un cuello se mueva a otro sitio al crecer.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        {plan.volumen.map((paso, indice) => (
                            <div key={paso.titulo} className="flex gap-3">
                                <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold tabular-nums">
                                    {indice + 1}
                                </span>
                                <div className="space-y-1">
                                    <p className="text-sm font-medium">
                                        <Prosa>{paso.titulo}</Prosa>
                                    </p>
                                    <p className="text-sm text-muted-foreground">
                                        <Prosa>{paso.detalle}</Prosa>
                                    </p>
                                    <p className="flex flex-wrap gap-2 pt-0.5">
                                        <Badge variant="outline">Coste: {paso.coste}</Badge>
                                        <Badge variant="secondary">{paso.cuando}</Badge>
                                    </p>
                                </div>
                            </div>
                        ))}
                    </CardContent>
                </Card>

                <div className="space-y-4">
                    <Card>
                        <CardHeader>
                            <CardTitle>Nuevos tipos de archivo</CardTitle>
                            <CardDescription>
                                El punto de extensión es el <strong className="font-medium">contrato
                                de lectura</strong>, no el motor: un lector nuevo produce una{" "}
                                <span className="font-mono">Lectura</span> y la norma no se toca. El
                                motor consume lecturas, no PDFs.
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            {plan.tipos_archivo.map((tipo) => (
                                <div
                                    key={tipo.tipo}
                                    className="space-y-1.5 rounded-lg bg-muted/40 p-3 ring-1 ring-foreground/5"
                                >
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                        <p className="text-sm font-medium">{tipo.tipo}</p>
                                        <Badge variant="outline">{tipo.estado}</Badge>
                                    </div>
                                    <p className="text-sm text-muted-foreground">
                                        <Prosa>{tipo.detalle}</Prosa>
                                    </p>
                                    <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                                        <span>
                                            Lector:{" "}
                                            <span className="text-foreground">{tipo.lector}</span>
                                        </span>
                                        <span>
                                            OCR: <span className="text-foreground">{tipo.ocr}</span>
                                        </span>
                                    </p>
                                </div>
                            ))}
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Lo que hay que tocar para que entre uno</CardTitle>
                            <CardDescription>
                                La parte que se olvida y rompe la trazabilidad. Sin los tres primeros
                                pasos, el documento entra pero no se puede auditar.
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <ol className="list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
                                {plan.pasos_tipo_nuevo.map((paso) => (
                                    <li key={paso}>
                                        <Prosa>{paso}</Prosa>
                                    </li>
                                ))}
                            </ol>
                        </CardContent>
                    </Card>
                </div>
            </div>
        </Seccion>
    );
}

function Honestidad({ datos }: { datos: Escalabilidad }) {
    return (
        <Seccion
            id="honestidad"
            icono={AlertTriangle}
            titulo="Lo que no sabemos"
            descripcion="Los supuestos con los que se extrapola y lo que no se ha medido en absoluto. Es la parte que hace que el resto de la página se pueda creer."
        >
            <div className="grid gap-4 lg:grid-cols-2">
                <Card>
                    <CardHeader>
                        <CardTitle>Supuestos de la extrapolación</CardTitle>
                        <CardDescription>
                            Si alguno de estos deja de cumplirse, las cifras de la tabla de
                            extrapolación cambian con él.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <ul className="space-y-2 text-sm text-muted-foreground">
                            {datos.supuestos.map((supuesto) => (
                                <li key={supuesto} className="flex gap-2">
                                    <span aria-hidden className="text-foreground">
                                        ·
                                    </span>
                                    <span>
                                        <Prosa>{supuesto}</Prosa>
                                    </span>
                                </li>
                            ))}
                        </ul>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle>No medido</CardTitle>
                        <CardDescription>
                            No es una lista de pendientes: es la frontera de lo que esta página
                            puede afirmar. Lo que no está aquí, se midió.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <ul className="space-y-2 text-sm text-muted-foreground">
                            {datos.no_medido.map((pendiente) => (
                                <li key={pendiente} className="flex gap-2">
                                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                                    <span>
                                        <Prosa>{pendiente}</Prosa>
                                    </span>
                                </li>
                            ))}
                        </ul>
                    </CardContent>
                </Card>
            </div>
        </Seccion>
    );
}
