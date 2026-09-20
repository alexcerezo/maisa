/**
 * Completar a mano los datos que el motor no supo leer.
 *
 * Las 29 facturas escaneadas del corpus no son las unicas que llegan a mano, pero
 * si las que peor se leen: el OCR confunde una `O` con un cero y un `5` con una
 * `S`, y hay campos que directamente no encuentra. Antes de esto, el panel decia
 * "el motor leyo esto" y ahi se acababa: quien miraba la factura veia el dato
 * bueno en el papel y no tenia donde dejarlo escrito.
 *
 * Tres decisiones que no son de gusto:
 *
 * 1. **Esto no cambia la decision.** El `PAGAR` / `NO_PAGAR` / `ESCALAR` de
 *    arriba sigue siendo el que calcularon las reglas, y no se vuelve a calcular
 *    al corregir. La API no decide nada y el panel tampoco: corregir es
 *    **anotar**, no resolver. Si esto moviera la decision, cualquiera podria
 *    convertir un `NO_PAGAR` en un `PAGAR` escribiendo en un formulario, que es
 *    justo lo que el motor existe para evitar. La tarjeta lo dice con todas las
 *    letras porque es facil suponer lo contrario.
 *
 * 2. **Se guarda campo a campo, no el formulario entero.** El `PUT` funde lo que
 *    le mandes con lo que ya hay. Guardar los siete de golpe obligaria a mandar
 *    los seis que no se han tocado, y el dia que dos personas rellenen campos
 *    distintos la segunda pisaria a la primera. Ademas el servidor devuelve el
 *    estado **completo** despues de cada guardado, asi que no hay que recomponer
 *    nada en el cliente ni adivinar que ha quedado guardado.
 *
 * 3. **El valor del motor se ensena al lado, no debajo.** Lo que hay que poder
 *    comparar es "el motor leyo esto y yo digo aquello". Un formulario que
 *    empieza con el valor del motor precargado y deja escribirlo encima pierde
 *    esa comparacion para siempre: al guardar ya no se sabe de donde venia.
 *    Aqui el valor del motor no se toca y el del operador va aparte.
 */

import { Check, CircleSlash, Eraser, Loader2, PencilLine, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { EdicionCorrecciones } from "@/api/hooks";
import { CAMPOS_ANCLABLES, type Anclajes, type CampoAnclable, type Correcciones } from "@/api/types";
import { cn } from "@/lib/utils";
import { ETIQUETA_CAMPO } from "@/theme";

/**
 * Donde se recuerda quien esta corrigiendo.
 *
 * La API guarda un `autor` de texto libre y no un usuario autenticado, asi que
 * hay que preguntarlo. Preguntarlo **en cada fila** seria siete veces lo mismo, y
 * en cada factura otra vez. Se guarda en el navegador porque no es un dato del
 * servidor sino de quien esta sentado delante.
 *
 * Es `localStorage` a pelo y con `try`: en una ventana privada o con las cookies
 * bloqueadas, `localStorage` existe pero lanza al escribir. Que se pierda el
 * nombre recordado es una molestia; que el formulario no se monte, no.
 */
const CLAVE_AUTOR = "maisa.correcciones.autor";

function autorRecordado(): string {
    try {
        return window.localStorage.getItem(CLAVE_AUTOR) ?? "";
    } catch {
        return "";
    }
}

function recordarAutor(autor: string): void {
    try {
        if (autor) window.localStorage.setItem(CLAVE_AUTOR, autor);
        else window.localStorage.removeItem(CLAVE_AUTOR);
    } catch {
        // Sin almacenamiento se sigue pudiendo corregir: solo no se recuerda.
    }
}

export function CompletarDatos({
    edicion,
    anclajes,
    cargandoAnclajes,
    campoActivo,
    onCampoActivo,
}: {
    edicion: EdicionCorrecciones;
    /** Lo que leyo el motor, para el contraste. `null` si no se ha podido pedir. */
    anclajes: Anclajes | null;
    cargandoAnclajes: boolean;
    campoActivo?: string | null;
    onCampoActivo?: (campo: string | null) => void;
}) {
    const [autor, setAutor] = useState(autorRecordado);

    const guardadas = new Map(edicion.correcciones.campos.map((c) => [c.campo, c]));
    const leidas = new Map((anclajes?.campos ?? []).map((c) => [c.campo, c.valor]));

    const cuantas = edicion.correcciones.campos.length;

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <PencilLine className="size-4 text-muted-foreground" />
                    Completar los datos a mano
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                    Escribe aquí lo que lees en el documento, sobre todo lo que el motor no supo
                    sacar. Se guarda por campo y queda anotado quién lo escribió.
                </p>
            </CardHeader>

            <CardContent className="space-y-4">
                {/*
                 * El aviso de que la decision no se mueve. Va arriba y no en un
                 * pie de pagina: es lo primero que hay que saber antes de escribir
                 * en un formulario que esta al lado de un `NO_PAGAR`.
                 */}
                <Alert>
                    <CircleSlash />
                    <AlertTitle>Esto no cambia la decisión</AlertTitle>
                    <AlertDescription>
                        Corregir un dato <strong>no recalcula</strong> el resultado de arriba. El
                        motor sigue diciendo lo que dijo: esto es una anotación para quien revise
                        el expediente, no una forma de cambiar el pago.
                    </AlertDescription>
                </Alert>

                {!edicion.sePuedeEscribir ? (
                    <Alert className="border-amber-600/40 bg-amber-500/5 dark:border-amber-400/40 dark:bg-amber-400/5">
                        <AlertTitle className="text-amber-800 dark:text-amber-200">
                            Sin conexión con la API: no se puede guardar
                        </AlertTitle>
                        <AlertDescription className="text-amber-800/80 dark:text-amber-200/70">
                            Los datos de la factura que estás viendo vienen del congelado, y las
                            correcciones van a la base de datos. Los campos están deshabilitados para
                            no dejar escribir algo que se va a perder.
                        </AlertDescription>
                    </Alert>
                ) : null}

                {edicion.error ? (
                    <Alert variant="destructive">
                        <AlertTitle>No se ha podido guardar</AlertTitle>
                        <AlertDescription>{edicion.error.message}</AlertDescription>
                    </Alert>
                ) : null}

                <div className="max-w-xs space-y-1">
                    <Label htmlFor="autor-correccion" className="text-xs">
                        ¿Quién completa los datos?
                    </Label>
                    <Input
                        id="autor-correccion"
                        value={autor}
                        disabled={!edicion.sePuedeEscribir}
                        maxLength={120}
                        placeholder="Tu nombre"
                        onChange={(evento) => {
                            setAutor(evento.target.value);
                            recordarAutor(evento.target.value.trim());
                        }}
                    />
                    <p className="text-xs text-muted-foreground">
                        Se guarda con cada campo que completes. No hace falta que sea un usuario del
                        sistema: es para saber a quién preguntar.
                    </p>
                </div>

                <ul className="divide-y divide-dashed">
                    {CAMPOS_ANCLABLES.map((campo) => (
                        <Fila
                            key={campo}
                            campo={campo}
                            valorMotor={leidas.get(campo) ?? null}
                            guardada={guardadas.get(campo) ?? null}
                            hayAnclajes={anclajes !== null}
                            cargandoAnclajes={cargandoAnclajes}
                            activo={campoActivo === campo}
                            alActivar={onCampoActivo}
                            edicion={edicion}
                            autor={autor.trim() || undefined}
                        />
                    ))}
                </ul>

                {/*
                 * El boton de borrar todo solo aparece si hay algo que borrar. Un
                 * boton permanentemente deshabilitado ocupa sitio y ensena una
                 * accion que no existe.
                 */}
                {cuantas > 0 ? (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <Button
                            variant="ghost"
                            size="sm"
                            disabled={!edicion.sePuedeEscribir || edicion.guardando}
                            onClick={() => void edicion.deshacer()}
                        >
                            <Eraser data-icon="inline-start" />
                            Deshacer las {cuantas} correcciones
                        </Button>
                        <p className="text-xs text-muted-foreground">
                            {cuantas} de {CAMPOS_ANCLABLES.length} campos completados a mano
                        </p>
                    </div>
                ) : null}
            </CardContent>
        </Card>
    );
}

/**
 * Una fila del formulario: lo que leyo el motor, lo que dice el operador y el
 * boton de guardar.
 *
 * El estado del texto vive **aqui** y no en el padre a proposito: mientras se
 * escribe no hay nada que guardar ni que compartir, y subirlo obligaria a
 * rehacer la tarjeta entera —las siete filas— con cada tecla.
 */
function Fila({
    campo,
    valorMotor,
    guardada,
    hayAnclajes,
    cargandoAnclajes,
    activo,
    alActivar,
    edicion,
    autor,
}: {
    campo: CampoAnclable;
    valorMotor: string | null;
    guardada: Correcciones["campos"][number] | null;
    hayAnclajes: boolean;
    cargandoAnclajes: boolean;
    activo: boolean;
    alActivar?: (campo: string | null) => void;
    edicion: EdicionCorrecciones;
    autor?: string;
}) {
    const [valor, setValor] = useState(guardada?.valor ?? "");
    const [nota, setNota] = useState(guardada?.nota ?? "");
    const [verNota, setVerNota] = useState(Boolean(guardada?.nota));
    const fila = useRef<HTMLLIElement>(null);

    /*
     * Pulsar el rotulo de un dato en el PDF marca su fila aqui. En una columna
     * estrecha la fila puede estar fuera de la pantalla, y entonces el resaltado
     * del documento no llevaria a ninguna parte: `nearest` solo mueve la pagina si
     * de verdad hace falta, para no dar un salto cuando la fila ya se ve.
     */
    useEffect(() => {
        if (activo) fila.current?.scrollIntoView({ block: "nearest" });
    }, [activo]);

    /*
     * Cuando el servidor devuelve el estado (despues de guardar, o al pedir el
     * detalle otra vez), lo que hay guardado manda sobre lo que se estaba
     * escribiendo. Sin esto, guardar un campo dejaria el texto viejo en la caja
     * hasta que el componente se desmontara.
     */
    useEffect(() => {
        setValor(guardada?.valor ?? "");
        setNota(guardada?.nota ?? "");
        setVerNota(Boolean(guardada?.nota));
    }, [guardada?.valor, guardada?.nota]);

    const limpio = valor.trim();
    const cambiado =
        limpio !== (guardada?.valor ?? "") || nota.trim() !== (guardada?.nota ?? "");

    // Vaciar un campo ya guardado no es guardar vacio —la API lo rechaza con un
    // 400—, es quitar la correccion. Es la unica forma de dejar el campo como
    // estaba, y por eso el boton cambia de texto en vez de deshabilitarse.
    const quiereQuitar = Boolean(guardada) && limpio === "";
    const puedeGuardar =
        edicion.sePuedeEscribir && cambiado && !edicion.guardando && (quiereQuitar || limpio !== "");

    const guardar = async () => {
        if (quiereQuitar) {
            await edicion.deshacer(campo);
            return;
        }
        await edicion.guardar({ [campo]: { valor: limpio, nota: nota.trim() || undefined } }, autor);
    };

    return (
        <li
            ref={fila}
            className={cn(
                "space-y-2 py-3 transition-colors",
                // La fila activa se marca porque es la que esta resaltada en el
                // documento: sin esta pista, pulsar un dato del PDF y ver que "no
                // pasa nada" es lo que parece.
                activo && "-mx-2 rounded-lg bg-muted/50 px-2",
            )}
        >
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <button
                    type="button"
                    onClick={() => alActivar?.(activo ? null : campo)}
                    className="rounded-sm text-xs font-medium underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                    title="Ver dónde está este dato en el documento"
                >
                    {ETIQUETA_CAMPO[campo]}
                </button>

                <span className="font-mono text-xs break-all text-muted-foreground">
                    {valorMotor !== null ? (
                        <>
                            motor: <span className="text-foreground">{valorMotor}</span>
                        </>
                    ) : cargandoAnclajes ? (
                        "mirando lo que leyó el motor…"
                    ) : hayAnclajes ? (
                        "el motor no lo leyó"
                    ) : (
                        "sin comparación con el motor"
                    )}
                </span>

                {guardada ? (
                    <span className="ml-auto inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-300">
                        <Check className="size-3" />
                        {guardada.autor ? `corregido por ${guardada.autor}` : "corregido"}
                    </span>
                ) : null}
            </div>

            <div className="flex flex-wrap items-center gap-2">
                <Input
                    value={valor}
                    disabled={!edicion.sePuedeEscribir || edicion.guardando}
                    aria-label={`Valor de ${ETIQUETA_CAMPO[campo]}`}
                    placeholder={valorMotor ?? "Escribe lo que ves en el documento"}
                    className="max-w-xs font-mono"
                    onChange={(evento) => setValor(evento.target.value)}
                />

                {/*
                 * La nota esta plegada por defecto. Siete cajas de nota abiertas
                 * convierten la tarjeta en un formulario de veinte campos cuando
                 * la mayoria de las veces no hace falta explicar nada; y cuando
                 * hace falta, se despliega sola porque ya hay una guardada.
                 */}
                {verNota ? (
                    <div className="flex min-w-0 flex-1 items-center gap-1">
                        <Input
                            value={nota}
                            disabled={!edicion.sePuedeEscribir || edicion.guardando}
                            aria-label={`Nota de ${ETIQUETA_CAMPO[campo]}`}
                            placeholder="Por qué lo cambias (opcional)"
                            className="min-w-0 flex-1"
                            onChange={(evento) => setNota(evento.target.value)}
                        />
                        {!guardada?.nota ? (
                            <Button
                                variant="ghost"
                                size="icon-sm"
                                aria-label="Quitar la nota"
                                onClick={() => {
                                    setNota("");
                                    setVerNota(false);
                                }}
                            >
                                <X />
                            </Button>
                        ) : null}
                    </div>
                ) : (
                    <Button variant="ghost" size="sm" onClick={() => setVerNota(true)}>
                        Añadir nota
                    </Button>
                )}

                <Button size="sm" disabled={!puedeGuardar} onClick={() => void guardar()}>
                    {edicion.guardando ? (
                        <Loader2 data-icon="inline-start" className="animate-spin" />
                    ) : quiereQuitar ? (
                        <Eraser data-icon="inline-start" />
                    ) : (
                        <Save data-icon="inline-start" />
                    )}
                    {quiereQuitar ? "Quitar" : "Guardar"}
                </Button>
            </div>

            {/*
             * El contraste: cuando ya hay algo guardado y no es lo mismo que leyo
             * el motor, se ensenan los dos. Es lo que convierte la correccion en
             * evidencia en vez de en un dato suelto.
             */}
            {guardada && guardada.valor_motor !== null && guardada.valor_motor !== guardada.valor ? (
                <p className="text-xs text-muted-foreground">
                    El motor leyó{" "}
                    <span className="font-mono line-through">{guardada.valor_motor}</span> y se ha
                    completado como <span className="font-mono">{guardada.valor}</span>.
                </p>
            ) : null}
        </li>
    );
}
