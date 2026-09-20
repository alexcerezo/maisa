/**
 * El marco de la aplicacion: cabecera, contenido y pie.
 *
 * Vive en `App.tsx` y no dentro de cada pagina a proposito. Si cada pantalla
 * pintase su propia cabecera, al navegar entre la tabla y el detalle la barra
 * se remontaria, el distintivo de fuente volveria a su esqueleto y el conmutador
 * de tema parpadearia: el marco no cambia de una ruta a otra, asi que no tiene
 * por que volver a montarse.
 *
 * La cabecera es `sticky` y con `backdrop-blur` porque la tabla es larga (500
 * filas) y el estado de la fuente tiene que seguir visible mientras se baja. Un
 * aviso de "estas viendo el congelado" que se pierde al hacer scroll es un aviso
 * que no cumple su funcion.
 */

import { Receipt } from "lucide-react";
import type { ReactNode } from "react";

import { BadgeFuente, PieFuente } from "@/components/Fuente";
import { BotonTema } from "@/components/tema";

export default function Disposicion({ children }: { children: ReactNode }) {
    return (
        <div className="flex min-h-dvh flex-col bg-background">
            <header className="sticky top-0 z-20 border-b bg-background/80 backdrop-blur-sm">
                <div className="mx-auto flex h-14 max-w-7xl items-center gap-3 px-4 sm:px-6 lg:px-8">
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                        <Receipt className="size-4" />
                    </div>

                    <div className="mr-auto flex min-w-0 items-baseline gap-2">
                        <span className="font-heading font-semibold tracking-tight">Maisa</span>
                        <span className="hidden truncate text-sm text-muted-foreground sm:inline">
                            Conciliación de facturas
                        </span>
                    </div>

                    <BadgeFuente />
                    <BotonTema />
                </div>
            </header>

            <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">
                {children}
            </main>

            <footer className="mt-8 border-t">
                <div className="mx-auto max-w-7xl px-4 py-4 sm:px-6 lg:px-8">
                    <PieFuente />
                </div>
            </footer>
        </div>
    );
}
