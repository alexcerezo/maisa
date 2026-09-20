/**
 * Ruta `/facturas` — el explorador.
 *
 * Esta pantalla es el **pegamento** y nada mas: los datos los trae `useFacturas`
 * y todo lo que se ve vive en componentes sueltos (`BarraFiltros`,
 * `TablaFacturas`, `Paginacion`…). Lo unico que se decide aqui es el estado de la
 * vista, y ese estado esta entero en la barra de direcciones.
 *
 * Por que en la URL y no en un `useState` (el argumento largo esta en
 * `lib/urlFiltros.ts`): una tabla filtrada se comparte, se recarga y se manda por
 * chat. Con los filtros en memoria, el enlace que se manda abre las 500 facturas
 * y quien lo recibe no ve el caso del que se estaba hablando.
 *
 * Aqui no hay ni un `<main>`: el marco lo pone `Disposicion`, que ya trae el ancho
 * maximo y el relleno. Anidar otro duplicaria el margen.
 */

import { useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import type { FiltrosVista } from "@/api/filtros";
import { useFacturas, useFuente } from "@/api/hooks";
import type { Resultado } from "@/api/types";
import { BarraFiltros } from "@/components/BarraFiltros";
import { FalloDeCarga, SinResultados } from "@/components/Estados";
import { AvisoFuente } from "@/components/Fuente";
import { Paginacion } from "@/components/Paginacion";
import { TablaFacturas } from "@/components/TablaFacturas";
import { TarjetasEstadisticas } from "@/components/TarjetasEstadisticas";
import { entero } from "@/lib/formato";
import { TAMANO_TABLA, totalDePaginas } from "@/lib/paginacion";
import { cuantosFiltros, escribirFiltros, filtrosDeUrl, paginaDeUrl } from "@/lib/urlFiltros";

/**
 * Los lotes que se pueden elegir, sacados de los contadores.
 *
 * `por_lote` trae las claves en texto (`{"1": 500}`) y puede traer
 * `"desconocido"`, que no es un lote: es el motor diciendo que no lo supo. El
 * filtro por forma de entero es a proposito y no `Number.isInteger(Number(x))`,
 * porque ese camino convertiria cualquier clave rara en un lote (la cadena vacia
 * da 0, y `"1.0"` da 1) en vez de descartarla.
 */
function lotesDeEstadisticas(porLote: Record<string, number> | null | undefined): number[] {
    if (!porLote) return [];
    return Object.keys(porLote)
        .filter((clave) => /^\d+$/.test(clave))
        .map(Number)
        .sort((uno, otro) => uno - otro);
}

/**
 * La frase de debajo del titulo.
 *
 * Existe por una razon concreta: **un cero no siempre es un cero**. Mientras no
 * se sabe de donde leer no hay ninguna peticion hecha, y mientras la respuesta
 * esta en camino no hay filas que contar; en los dos casos hay cero filas en
 * pantalla y decir "0 facturas" seria contar algo que nadie ha mirado todavia.
 * Por eso el texto se decide antes de mirar el numero.
 */
function resumenDelListado(
    situacion: "sin-fuente" | "cargando" | "error" | "listo",
    filas: number,
    totalSinFiltros: number,
    puestos: number,
): string {
    if (situacion === "sin-fuente") return "Comprobando de dónde se leen los datos…";
    if (situacion === "cargando") return "Cargando el listado…";
    if (situacion === "error") return "No he podido leer el listado.";
    if (puestos === 0) return `${entero(filas)} facturas, sin ningún filtro puesto.`;
    const etiqueta = puestos === 1 ? "filtro" : "filtros";
    return `${entero(filas)} de ${entero(totalSinFiltros)} facturas, con ${puestos} ${etiqueta} puesto${
        puestos === 1 ? "" : "s"
    }.`;
}

export default function FacturasPage() {
    const [params, setParams] = useSearchParams();
    const ubicacion = useLocation();
    const { estado } = useFuente();

    // `filtrosDeUrl` construye un objeto nuevo en cada llamada. `useFacturas` no se
    // rompe con eso (su clave de peticion se arma campo a campo), pero memorizarlo
    // deja claro que la identidad de los filtros solo cambia cuando cambia la
    // direccion, y ahorra sanear la consulta en cada render.
    const filtros = useMemo(() => filtrosDeUrl(params), [params]);
    const pagina = paginaDeUrl(params);
    const puestos = cuantosFiltros(filtros);

    const listado = useFacturas(filtros);

    // Cuantas facturas del conjunto actual se quedan fuera por no tener fecha.
    // Hace falta preguntar lo mismo **sin** las fechas, y eso es una segunda
    // peticion que solo tiene sentido si hay un filtro de fechas puesto: con
    // `activo` en `false` no se lanza nada y se cuenta sobre el listado normal.
    const hayFechas = Boolean(filtros.fechaDesde || filtros.fechaHasta);
    const sinFechas = useFacturas({ ...filtros, fechaDesde: null, fechaHasta: null }, hayFechas);
    const conjuntoSinFechas = hayFechas ? sinFechas.datos : listado.datos;
    const sinFecha = conjuntoSinFechas?.items.filter((factura) => !factura.fecha).length ?? 0;

    const lotes = useMemo(() => lotesDeEstadisticas(estado?.estadisticas?.por_lote), [estado]);

    const items = listado.datos?.items ?? [];
    // La pagina se acota antes de recortar: `?pagina=40` escrito a mano, o filtrar
    // hasta dejar menos filas, dejarian la tabla vacia sin decir por que. El
    // recorte y el pie tienen que salir del mismo numero, o el pie diria "filas
    // 976 a 1000" con cuatro filas pintadas.
    const ultimaPagina = totalDePaginas(items.length);
    const paginaEfectiva = Math.min(pagina, ultimaPagina);
    const desde = (paginaEfectiva - 1) * TAMANO_TABLA;
    const visibles = items.slice(desde, desde + TAMANO_TABLA);

    // El total de la traza es solo el "de N" del pie. Nunca el recuento de filas:
    // el `total` de la API no sabe de fechas ni de NIF y puede discrepar de lo que
    // se pinta, que es exactamente el fallo que ya se cometio una vez.
    const totalSinFiltros = estado?.estadisticas?.total ?? 0;

    // La fila de la que se vuelve, puesta por el detalle al navegar. Sin esto, tras
    // revisar 40 escaladas no se sabe cual se acaba de mirar.
    const volviendoDe = (ubicacion.state as { desde?: string } | null)?.desde ?? null;

    // Sin fuente no hay peticion hecha ni filtros que aplicar, y `items` esta vacio
    // porque no ha llegado nada, no porque no haya nada. La tabla se queda en
    // esqueleto y el aviso de arriba explica que no se sabe de donde leer.
    const cargandoListado = listado.cargando || !estado;

    const situacion = !estado
        ? "sin-fuente"
        : listado.error
          ? "error"
          : listado.datos
            ? "listo"
            : "cargando";

    const alCambiar = (parcial: Partial<FiltrosVista>) => {
        // Cualquier cambio de filtro vuelve a la primera pagina: la pagina 5 de un
        // conjunto mas pequeño no existe, y quedarse en ella daria una tabla vacia
        // justo despues de filtrar.
        setParams(escribirFiltros({ ...filtros, ...parcial }, 1));
    };

    const alLimpiar = () =>
        setParams(
            escribirFiltros(
                {
                    q: null,
                    resultado: null,
                    lote: null,
                    nif: null,
                    fechaDesde: null,
                    fechaHasta: null,
                },
                1,
            ),
        );

    // La tarjeta pulsada es un conmutador: volver a pulsar la que ya filtra quita
    // el filtro, para poder deshacer sin ir a buscar el boton de limpiar.
    const alElegirResultado = (resultado: Resultado) =>
        alCambiar({ resultado: filtros.resultado === resultado ? null : resultado });

    // La primera pagina no se escribe (`escribirFiltros` la omite), asi que la
    // vista sin filtros queda en un `/facturas` limpio y no en `/facturas?pagina=1`.
    const rutaDePagina = (numero: number) => {
        const consulta = escribirFiltros(filtros, numero).toString();
        return consulta ? `/facturas?${consulta}` : "/facturas";
    };

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <h1 className="font-heading text-2xl font-semibold tracking-tight">Facturas</h1>
                <p className="text-sm text-muted-foreground">
                    {resumenDelListado(situacion, items.length, totalSinFiltros, puestos)}
                </p>
            </div>

            {/*
             * El aviso de la fuente se monta aqui, en la pantalla que enseña los
             * datos, y no en la cabecera del marco: es este listado el que puede
             * estar enseñando el congelado de ayer como si fuera de ahora.
             */}
            <AvisoFuente />

            <TarjetasEstadisticas
                estadisticas={estado?.estadisticas ?? null}
                cargando={cargandoListado}
                resultadoActivo={filtros.resultado ?? null}
                alElegirResultado={alElegirResultado}
            />

            <BarraFiltros
                filtros={filtros}
                lotes={lotes}
                sinFecha={sinFecha}
                alCambiar={alCambiar}
                alLimpiar={alLimpiar}
            />

            {listado.error ? (
                // Con la peticion rota no se pinta la tabla: una tabla vacia al lado
                // de un error se lee como "no hay facturas", que es otra cosa.
                <FalloDeCarga
                    error={listado.error}
                    alReintentar={listado.reintentar}
                    queEs="el listado"
                />
            ) : items.length === 0 && !cargandoListado ? (
                <SinResultados filtros={filtros} alLimpiar={alLimpiar} />
            ) : (
                <TablaFacturas
                    items={visibles}
                    // Con filas ya en pantalla se atenua en vez de vaciarse: filtrar
                    // no puede parpadear a blanco en cada pulsacion de tecla.
                    cargando={cargandoListado && items.length === 0}
                    atenuada={listado.cargando && items.length > 0}
                    fileIdActivo={volviendoDe}
                    parametrosLista={ubicacion.search}
                />
            )}

            {items.length > 0 ? (
                // Sin filas el pie solo repetiria "Ninguna factura", que ya lo dice
                // `SinResultados` justo encima.
                <Paginacion
                    pagina={paginaEfectiva}
                    totalFilas={items.length}
                    totalSinFiltros={totalSinFiltros}
                    rutaDePagina={rutaDePagina}
                />
            ) : null}
        </div>
    );
}
