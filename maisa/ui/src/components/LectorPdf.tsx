/**
 * El documento original, dentro del panel.
 *
 * El PDF se pide con `fetch` y se convierte a `blob:` (ver `usePdf`), en vez de
 * poner en el `<iframe>` la direccion de la API. El motivo esta en `hooks.tsx` y
 * no es cosmético: un `<iframe>` no puede mandar la cabecera `X-API-Key`, asi que
 * el dia que la API cierre el paso, el visor dejaria de ensenar nada sin que nadie
 * hubiera tocado esta pantalla.
 *
 * Aqui no se decide nada, solo se ensena. Si el PDF no esta, se dice **cual** de
 * los dos casos es, porque se arreglan distinto: en el plan B congelado solo hay 9
 * de los 500 documentos, y que falte el PDF no invalida la ficha (el detalle se
 * puede leer entero igual); en vivo, que falte es un problema de verdad.
 */

import { useFuente, usePdf } from "../api/hooks";
import { Fallo } from "./Estados";
import { APUNTE, TARJETA } from "../theme";

export function LectorPdf({ fileId }: { fileId: string }) {
    const { url, error, cargando } = usePdf(fileId);
    /*
     * El mismo fallo significa dos cosas distintas segun de donde se lea, y por eso
     * el texto se elige aqui y no se deja fijo: en el congelado es lo normal (solo
     * se trajeron 9 documentos, el resto se sabia que no iba a estar); en vivo es un
     * aviso de que la API no esta sirviendo ese fichero. Decir "es lo normal" cuando
     * ocurre en vivo es exactamente el tipo de frase que hace desconfiar del panel.
     */
    const { estado } = useFuente();
    const congelado = estado?.fuente === "congelado";

    return (
        <section className={`${TARJETA} overflow-hidden`}>
            <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
                <h2 className="text-sm font-semibold text-slate-900">Documento</h2>
                {url ? (
                    <a
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs font-medium text-blue-700 hover:underline"
                    >
                        Abrir en otra pestana ↗
                    </a>
                ) : null}
            </header>

            {error ? (
                <div className="p-4">
                    <Fallo error={error}>
                        {congelado ? (
                            <>
                                En el plan B congelado solo se guardaron 9 de los 500 documentos, y
                                este no es uno de ellos. Sin el PDF, la ficha sigue siendo completa:
                                lo que decide es la decision y las reglas, y eso esta en la traza.
                            </>
                        ) : (
                            <>
                                La API esta en vivo y aun asi no ha devuelto este documento. La
                                ficha de al lado no depende de el, asi que se puede seguir leyendo,
                                pero conviene mirar por que falta el fichero.
                            </>
                        )}
                    </Fallo>
                </div>
            ) : cargando ? (
                <div className="flex h-[70vh] items-center justify-center gap-3 text-slate-400">
                    <span
                        aria-hidden="true"
                        className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600"
                    />
                    <span className="text-sm">Cargando el documento…</span>
                </div>
            ) : url ? (
                <iframe src={url} title={`Documento de ${fileId}`} className="h-[70vh] w-full bg-slate-100" />
            ) : (
                <p className={`${APUNTE} p-4`}>Sin documento que ensenar.</p>
            )}
        </section>
    );
}
