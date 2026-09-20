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
 *
 * Los enlaces viven aqui y no en las paginas por el mismo motivo: son el
 * mismo marco. Se pintan como icono solo en pantallas estrechas y con etiqueta a
 * partir de `sm`, porque en un movil la cabecera ya lleva el distintivo de fuente
 * y el conmutador de tema, y meter dos palabras mas la parte por la mitad.
 */

import { Receipt, Ruler, Route, TableProperties } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { BadgeFuente, PieFuente } from "@/components/Fuente";
import { BotonTema } from "@/components/tema";
import { cn } from "@/lib/utils";

/**
 * Las secciones del panel.
 *
 * `/facturas` va sin `end` a proposito: el detalle de una factura sigue siendo
 * la seccion de facturas, asi que el enlace tiene que quedarse marcado al abrir
 * un expediente. Si se pusiera `end`, el menu se apagaria justo al entrar en el
 * detalle y pareceria que se ha salido del panel.
 *
 * El orden es el del recorrido de una factura: la tabla (los datos), la traza
 * (de donde sale un dato concreto) y el motor (si aguanta y cuanto cuesta). No
 * es alfabetico a proposito: el menu es la unica pista de por donde empezar.
 */
const SECCIONES = [
    { a: "/facturas", etiqueta: "Facturas", Icono: TableProperties },
    { a: "/trazabilidad", etiqueta: "Trazabilidad", Icono: Route },
    { a: "/escalabilidad", etiqueta: "Escalabilidad y coste", Icono: Ruler },
] as const;

function Navegacion() {
    return (
        <nav aria-label="Secciones" className="flex shrink-0 items-center gap-0.5">
            {SECCIONES.map(({ a, etiqueta, Icono }) => (
                <NavLink
                    key={a}
                    to={a}
                    title={etiqueta}
                    className={({ isActive }) =>
                        cn(
                            "inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground",
                            isActive && "bg-muted font-medium text-foreground",
                        )
                    }
                >
                    <Icono className="size-4 shrink-0" />
                    <span className="hidden sm:inline">{etiqueta}</span>
                </NavLink>
            ))}
        </nav>
    );
}

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

                    <Navegacion />

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
