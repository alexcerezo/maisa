/**
 * La barra de filtros.
 *
 * El componente **no guarda los filtros**: los recibe y avisa de los cambios. El
 * estado vive en la URL (ver `lib/urlFiltros.ts`), y esta pieza solo lo pinta y
 * lo edita. Asi el conmutador de resultado de las tarjetas de contador y el de
 * aqui escriben en el mismo sitio y no pueden discrepar.
 *
 * Tres decisiones de esta pantalla:
 *
 * 1. **El buscador va retrasado.** Esta atado a la URL y la URL dispara la
 *    peticion, asi que sin retardo cada letra seria una peticion y una entrada en
 *    el historial. Ver `lib/useTextoRetrasado.ts`.
 *
 * 2. **La decision es un grupo de botones y no un desplegable.** Son tres valores
 *    y se cambian todo el rato: con un desplegable hay que abrir, leer y elegir
 *    para pasar de "Escalar" a "No pagar", y aqui es un clic. Ademas enseña cual
 *    esta puesto sin abrir nada.
 *
 * 3. **El aviso de las fechas.** Ocho de las 500 facturas no tienen fecha, y al
 *    poner un filtro de fechas **desaparecen** de la tabla. Es correcto, pero
 *    parece perdida de datos, asi que cuando hay un filtro de fechas puesto se
 *    dice cuantas facturas se quedan fuera por no tener fecha.
 *
 * 4. **La segunda lectura es un grupo de botones y no un desplegable**, por lo
 *    mismo que la decision: son tres valores, se cambian todo el rato y se quiere
 *    ver cual esta puesto sin abrir nada. Cada boton lleva su `title` con la
 *    explicacion larga, porque "Se puede cerrar" a secas no dice que se cierra.
 */

import { CalendarDays, Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { LIMITE_TEXTO, type FiltrosVista } from "@/api/filtros";
import { ESTADOS_COLA, RESULTADOS, type EstadoCola, type Resultado } from "@/api/types";
import { entero } from "@/lib/formato";
import {
    ETIQUETA_COLA,
    ETIQUETA_RESULTADO,
    EXPLICACION_COLA,
    ICONO_COLA,
    ICONO_RESULTADO,
} from "@/theme";
import { cuantosFiltros } from "@/lib/urlFiltros";
import { useTextoRetrasado } from "@/lib/useTextoRetrasado";

/** El valor que representa "sin filtro" en el desplegable de lote. */
const TODOS = "todos";

export function BarraFiltros({
    filtros,
    lotes,
    sinFecha,
    alCambiar,
    alLimpiar,
}: {
    filtros: FiltrosVista;
    /** Los lotes que existen, de los contadores. Ordenados por numero. */
    lotes: number[];
    /** Cuantas facturas del conjunto actual no tienen fecha. */
    sinFecha: number;
    alCambiar: (parcial: Partial<FiltrosVista>) => void;
    alLimpiar: () => void;
}) {
    const [busqueda, setBusqueda] = useTextoRetrasado(filtros.q ?? "", (valor) => {
        alCambiar({ q: valor });
    });

    const [nif, setNif] = useTextoRetrasado(filtros.nif ?? "", (valor) => {
        alCambiar({ nif: valor });
    });

    const puestos = cuantosFiltros(filtros);
    const hayFechas = Boolean(filtros.fechaDesde || filtros.fechaHasta);

    return (
        <section
            aria-label="Filtros"
            className="space-y-4 rounded-xl bg-card p-4 ring-1 ring-foreground/10"
        >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                <div className="relative flex-1">
                    <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                    <Input
                        value={busqueda}
                        onChange={(evento) => setBusqueda(evento.target.value)}
                        maxLength={LIMITE_TEXTO}
                        placeholder="Buscar por factura, proveedor, pedido o asiento…"
                        aria-label="Buscar facturas"
                        className="pl-8"
                    />
                </div>

                <Button
                    variant="outline"
                    onClick={alLimpiar}
                    disabled={puestos === 0}
                    title={puestos === 0 ? "No hay ningún filtro puesto" : "Quitar todos los filtros"}
                >
                    <X data-icon="inline-start" />
                    Limpiar{puestos > 0 ? ` (${puestos})` : ""}
                </Button>
            </div>

            <div className="flex flex-wrap items-center gap-x-6 gap-y-4">
                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Decisión</Label>
                    <ToggleGroup
                        type="single"
                        variant="outline"
                        size="sm"
                        value={filtros.resultado ?? ""}
                        onValueChange={(valor) => {
                            // Radix manda cadena vacia al volver a pulsar el que ya
                            // estaba: es justo el "quitar el filtro" que se quiere.
                            alCambiar({ resultado: (valor || null) as Resultado | null });
                        }}
                    >
                        {RESULTADOS.map((resultado) => {
                            const Icono = ICONO_RESULTADO[resultado];
                            return (
                                <ToggleGroupItem key={resultado} value={resultado}>
                                    <Icono />
                                    {ETIQUETA_RESULTADO[resultado]}
                                </ToggleGroupItem>
                            );
                        })}
                    </ToggleGroup>
                </div>

                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Segunda lectura</Label>
                    <ToggleGroup
                        type="single"
                        variant="outline"
                        size="sm"
                        value={filtros.segundaLectura ?? ""}
                        onValueChange={(valor) => {
                            alCambiar({ segundaLectura: (valor || null) as EstadoCola | null });
                        }}
                    >
                        {ESTADOS_COLA.map((estado) => {
                            const Icono = ICONO_COLA[estado];
                            return (
                                <ToggleGroupItem
                                    key={estado}
                                    value={estado}
                                    title={EXPLICACION_COLA[estado]}
                                >
                                    <Icono />
                                    {ETIQUETA_COLA[estado]}
                                </ToggleGroupItem>
                            );
                        })}
                    </ToggleGroup>
                </div>

                <div className="space-y-1.5">
                    <Label htmlFor="filtro-lote" className="text-xs text-muted-foreground">
                        Lote
                    </Label>
                    <Select
                        value={filtros.lote === null || filtros.lote === undefined ? TODOS : String(filtros.lote)}
                        onValueChange={(valor) => {
                            alCambiar({ lote: valor === TODOS ? null : Number(valor) });
                        }}
                    >
                        <SelectTrigger id="filtro-lote" size="sm" className="w-32">
                            <SelectValue placeholder="Todos" />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value={TODOS}>Todos</SelectItem>
                            {lotes.map((lote) => (
                                <SelectItem key={lote} value={String(lote)}>
                                    Lote {lote}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                </div>

                <div className="space-y-1.5">
                    <Label htmlFor="filtro-nif" className="text-xs text-muted-foreground">
                        NIF
                    </Label>
                    <Input
                        id="filtro-nif"
                        value={nif}
                        onChange={(evento) => setNif(evento.target.value)}
                        maxLength={LIMITE_TEXTO}
                        placeholder="B12345678"
                        className="w-36"
                    />
                </div>

                <div className="space-y-1.5">
                    <Label htmlFor="filtro-desde" className="text-xs text-muted-foreground">
                        Desde
                    </Label>
                    <Input
                        id="filtro-desde"
                        type="date"
                        value={filtros.fechaDesde ?? ""}
                        max={filtros.fechaHasta ?? undefined}
                        onChange={(evento) => alCambiar({ fechaDesde: evento.target.value || null })}
                        className="w-40"
                    />
                </div>

                <div className="space-y-1.5">
                    <Label htmlFor="filtro-hasta" className="text-xs text-muted-foreground">
                        Hasta
                    </Label>
                    <Input
                        id="filtro-hasta"
                        type="date"
                        value={filtros.fechaHasta ?? ""}
                        min={filtros.fechaDesde ?? undefined}
                        onChange={(evento) => alCambiar({ fechaHasta: evento.target.value || null })}
                        className="w-40"
                    />
                </div>
            </div>

            {hayFechas && sinFecha > 0 ? (
                <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <CalendarDays className="size-3.5" />
                    {entero(sinFecha)} de estas facturas no tienen fecha y quedan fuera de la tabla
                    mientras el filtro de fechas esté puesto. No se han perdido.
                </p>
            ) : null}
        </section>
    );
}
