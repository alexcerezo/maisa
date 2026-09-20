/**
 * La banda que dice de donde salen los datos. Es la pieza que hace honesto al
 * plan B.
 *
 * El panel puede leer de la API viva o del congelado de `public/data/`, y las dos
 * cosas se ven igual en la tabla. Eso es comodo y es peligroso: un panel que
 * ensena datos de ayer con el mismo aspecto que los de ahora no esta degradando,
 * esta enganando. Por eso, en cuanto no se lee en vivo, esta banda aparece arriba
 * con el motivo exacto, la fecha de la foto y un boton para volver a probar.
 *
 * Los enlaces de aqui son `<a href>` de verdad y **no** enlaces del router, y eso
 * es a proposito: `fuente.ts` lee `?fuente=congelado` una sola vez, al cargar el
 * modulo. Cambiar el parametro con el router no volveria a evaluarlo, asi que el
 * cambio de fuente tiene que ser una recarga de pagina entera.
 */

import { useLocation, useSearchParams } from "react-router-dom";

import type { OrigenConfig } from "../api/config";
import { useFuente } from "../api/hooks";
import { fechaHora } from "../formato";
import { APUNTE, ENLACE_BOTON } from "../theme";
import { DistintivoNeutro } from "./Distintivos";

/** De donde salio la URL de la API. Se ensena porque explica por que cambia de un sitio a otro. */
const ETIQUETA_ORIGEN: Record<OrigenConfig, string> = {
    consulta: "la direccion (?api=)",
    localStorage: "lo guardado en este navegador",
    "config.json": "public/config.json",
    ninguno: "ningun sitio",
};

export function AvisoFuente() {
    const { estado, cargando, error, reintentar } = useFuente();
    const [parametros] = useSearchParams();
    const ubicacion = useLocation();

    /**
     * La misma pantalla con `fuente` puesto o quitado, conservando lo demas (los
     * filtros, y sobre todo `api`, que es lo que permite volver a la API).
     */
    function direccionAlterna(poner: "congelado" | null): string {
        const siguientes = new URLSearchParams(parametros);
        if (poner) siguientes.set("fuente", poner);
        else siguientes.delete("fuente");
        const cadena = siguientes.toString();
        return `${ubicacion.pathname}${cadena ? `?${cadena}` : ""}`;
    }

    if (cargando) {
        return (
            <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2">
                <span
                    aria-hidden="true"
                    className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600"
                />
                <span className={APUNTE}>Comprobando de donde leer los datos…</span>
            </div>
        );
    }

    // Este fallo es el de la comprobacion en si, no el de una peticion: el
    // congelado nunca es un error, es un estado. Si llegamos aqui es que ni
    // siquiera se ha podido averiguar con quien hablar.
    if (error) {
        return (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3">
                <p className="text-sm text-rose-900">{error}</p>
                <button type="button" onClick={reintentar} className={`${ENLACE_BOTON} mt-2`}>
                    Volver a intentarlo
                </button>
            </div>
        );
    }

    if (!estado) return null;

    if (estado.fuente === "vivo") {
        return (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
                <span aria-hidden="true" className="h-2 w-2 rounded-full bg-emerald-500" />
                <span className="font-semibold">Datos en vivo</span>
                <span className="font-mono">{estado.config.api}</span>
                {estado.config.apiKey ? <DistintivoNeutro>con clave</DistintivoNeutro> : null}
                <span className="text-emerald-800/80">·</span>
                <span className="text-emerald-800/80">
                    direccion tomada de {ETIQUETA_ORIGEN[estado.config.origen]}
                </span>
                <a
                    href={direccionAlterna("congelado")}
                    className="ml-auto font-medium text-emerald-900 underline decoration-emerald-400 underline-offset-2 hover:decoration-emerald-700"
                >
                    Ver el plan B congelado →
                </a>
            </div>
        );
    }

    // Congelado. Aqui esta el texto que justifica todo el fichero.
    return (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span aria-hidden="true" className="h-2 w-2 rounded-full bg-amber-500" />
                <span className="text-sm font-semibold text-amber-900">
                    Datos congelados: se lee del ultimo volcado, no de la API
                </span>
                <span className="text-xs text-amber-800/80">·</span>
                <span className="text-xs text-amber-800/80">direccion tomada de {ETIQUETA_ORIGEN[estado.config.origen]}</span>
            </div>

            <p className="mt-1 text-sm text-amber-900">{estado.motivo}</p>

            {estado.manifiesto ? (
                <p className="mt-1 text-xs text-amber-800">
                    La foto es del <strong>{fechaHora(estado.manifiesto.congelado_en)}</strong> y
                    viene de <span className="font-mono">{estado.manifiesto.origen}</span>. Trae{" "}
                    {estado.manifiesto.facturas_en_el_listado} facturas en el listado y{" "}
                    {estado.manifiesto.detalles_guardados} expedientes completos, pero solo los{" "}
                    {estado.manifiesto.pdfs.length} PDF de esta lista: el resto de los documentos
                    no se trajeron, y al abrirlos lo dice.
                </p>
            ) : (
                // Sin manifiesto el panel sigue, pero no puede decir de cuando son
                // los datos, y eso hay que decirlo en voz alta.
                <p className="mt-1 text-xs text-amber-800">
                    No encuentro el manifiesto del congelado, asi que no puedo decirte de cuando
                    son estos datos. Se regenera con{" "}
                    <span className="font-mono">python3 tools/descargar_fixtures.py</span>.
                </p>
            )}

            <div className="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" onClick={reintentar} className={ENLACE_BOTON}>
                    Volver a probar la API
                </button>
                <a href={direccionAlterna(null)} className={ENLACE_BOTON}>
                    Intentar en vivo sin el parametro fuente
                </a>
            </div>
        </div>
    );
}
