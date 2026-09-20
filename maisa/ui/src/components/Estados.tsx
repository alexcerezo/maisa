/**
 * Los tres estados que no son "hay datos": cargando, vacio y roto.
 *
 * Estan juntos porque son el mismo problema resuelto tres veces — que la pantalla
 * diga algo util cuando no hay tabla. Y ese "algo util" es lo que separa un panel
 * que se usa de uno que se abandona: una tabla vacia sin explicacion se lee como
 * "se ha roto", y con 500 facturas de fondo, "no hay resultados" casi siempre
 * significa "tienes un filtro puesto", no "no hay datos".
 *
 * Por eso el estado vacio **dice que filtros hay puestos** y ofrece quitarlos, en
 * vez de un dibujo con un texto generico.
 */

import { AlertTriangle, FilterX, PackageOpen, RefreshCw, SearchX } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
    Empty,
    EmptyContent,
    EmptyDescription,
    EmptyHeader,
    EmptyMedia,
    EmptyTitle,
} from "@/components/ui/empty";
import { ErrorPeticion } from "@/api/cliente";
import type { FiltrosVista } from "@/api/filtros";

/** La lista de filtros puestos, en texto, para poder ensenarla. */
function describirFiltros(filtros: FiltrosVista): string[] {
    const puestos: string[] = [];
    if (filtros.q) puestos.push(`texto «${filtros.q}»`);
    if (filtros.resultado) puestos.push(`decisión ${filtros.resultado}`);
    if (filtros.lote !== null && filtros.lote !== undefined) puestos.push(`lote ${filtros.lote}`);
    if (filtros.nif) puestos.push(`NIF «${filtros.nif}»`);
    if (filtros.fechaDesde) puestos.push(`desde ${filtros.fechaDesde}`);
    if (filtros.fechaHasta) puestos.push(`hasta ${filtros.fechaHasta}`);
    return puestos;
}

/**
 * No hay ninguna fila que ensenar.
 *
 * Dos casos que se ven igual y no lo son: **sin filtros** es que la traza esta
 * vacia (o no se ha podido leer), y **con filtros** es que la combinacion no casa
 * con nada. El texto y el boton cambian segun cual sea, porque el arreglo es
 * distinto.
 */
export function SinResultados({
    filtros,
    alLimpiar,
}: {
    filtros: FiltrosVista;
    alLimpiar: () => void;
}) {
    const puestos = describirFiltros(filtros);

    if (puestos.length === 0) {
        return (
            <Empty className="rounded-xl bg-card ring-1 ring-foreground/10">
                <EmptyHeader>
                    <EmptyMedia variant="icon">
                        <PackageOpen />
                    </EmptyMedia>
                    <EmptyTitle>No hay ninguna factura</EmptyTitle>
                    <EmptyDescription>
                        La fuente de datos ha respondido, pero no trae ni una factura. No es un
                        filtro: no hay ninguno puesto. Si esperabas ver las 500 de la traza, el
                        fichero que se está leyendo está vacío o incompleto.
                    </EmptyDescription>
                </EmptyHeader>
            </Empty>
        );
    }

    return (
        <Empty className="rounded-xl bg-card ring-1 ring-foreground/10">
            <EmptyHeader>
                <EmptyMedia variant="icon">
                    <SearchX />
                </EmptyMedia>
                <EmptyTitle>Ninguna factura casa con el filtro</EmptyTitle>
                <EmptyDescription>
                    Se está filtrando por {puestos.join(", ")}. Hay 500 facturas en la traza, así que
                    lo más probable es que la combinación no exista.{" "}
                    <span className="text-foreground">
                        Ojo con el texto: busca solo en el nombre del fichero, el proveedor, el
                        pedido y el asiento, y no en el NIF.
                    </span>{" "}
                    Para el NIF hay un campo aparte.
                </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
                <Button variant="outline" onClick={alLimpiar}>
                    <FilterX data-icon="inline-start" />
                    Quitar los {puestos.length} filtros
                </Button>
            </EmptyContent>
        </Empty>
    );
}

/**
 * La peticion ha fallado.
 *
 * El mensaje de `ErrorPeticion` ya viene redactado en castellano desde la capa de
 * datos (`cliente.ts` y `datos.ts`), asi que se ensena **tal cual** y no se
 * reinterpreta aqui. Lo unico que se anade es la pista de que hacer, que depende
 * del codigo: no es lo mismo que falte un expediente en el congelado que que la
 * API no conteste.
 */
export function FalloDeCarga({
    error,
    alReintentar,
    queEs,
}: {
    error: Error;
    alReintentar?: () => void;
    /** Que se estaba pidiendo, en castellano. "el listado", "el expediente"... */
    queEs: string;
}) {
    const peticion = error instanceof ErrorPeticion ? error : null;
    const pista = peticion ? pistaPara(peticion) : null;

    return (
        <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>No he podido cargar {queEs}</AlertTitle>
            <AlertDescription>
                <p>{error.message}</p>
                {pista ? <p className="mt-1 text-muted-foreground">{pista}</p> : null}
                {peticion?.ruta ? (
                    <p className="mt-1 font-mono text-xs break-all text-muted-foreground">
                        {peticion.ruta}
                        {peticion.estado !== null ? ` · HTTP ${peticion.estado}` : ""}
                    </p>
                ) : null}
            </AlertDescription>
            {alReintentar ? (
                <div className="col-start-2 row-span-2 mt-2 justify-self-start has-[>svg]:col-start-2">
                    <Button variant="outline" size="sm" onClick={alReintentar}>
                        <RefreshCw data-icon="inline-start" />
                        Reintentar
                    </Button>
                </div>
            ) : null}
        </Alert>
    );
}

/**
 * La explicacion larga de cada codigo de error.
 *
 * Solo se traducen los codigos que **tienen arreglo o matiz**: para el resto se
 * devuelve `null` y se queda el mensaje de la capa de datos, que ya es bueno.
 * Traducir los quince codigos conocidos seria una lista que se queda vieja el dia
 * que la API anada uno, y el mensaje de la API ya viene en castellano.
 */
function pistaPara(error: ErrorPeticion): string | null {
    switch (error.codigo) {
        case "congelado_sin_detalle":
            return (
                "El congelado solo guarda los expedientes que había el día que se generó. " +
                "Si esta factura se ha añadido después, no está: la tabla puede enseñarla " +
                "porque el listado es otro fichero, pero su expediente no se descargó. " +
                "Vuelve a generarlo con `python3 tools/descargar_fixtures.py`."
            );
        case "congelado_sin_pdf":
            return (
                "El congelado solo guarda unos pocos PDF (los casos que se enseñan). " +
                "Los datos de esta factura están completos; lo que falta es el documento."
            );
        case "factura_no_encontrada":
            return (
                "La API ha contestado que esa factura no existe. Si el enlace venía de otra " +
                "sesión o de la traza de ayer, puede que ya no esté."
            );
        case "no_autorizado":
            return (
                "La API pide una clave y no es válida. Se configura con `?api=` o en " +
                "`public/config.json`."
            );
        case "traza_no_disponible":
            return "La traza del motor no está cargada en la API en este momento.";
        default:
            return error.sinRespuesta
                ? "La API no ha contestado. El panel debería haber caído al congelado; si estás viendo esto, tampoco ha podido."
                : null;
    }
}
