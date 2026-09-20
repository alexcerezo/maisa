/**
 * El tema claro/oscuro, con conmutador.
 *
 * El preset de shadcn ya trae las dos paletas (`:root` y `.dark` en
 * `index.css`), pero sin esto la oscura era inalcanzable: nadie le pone la clase
 * `dark` al `<html>`. Este fichero es solo eso, mas la memoria de la eleccion.
 *
 * Tres detalles que se resuelven aqui:
 *
 * 1. **La primera pintada no parpadea.** La clase la pone un script en linea en
 *    `index.html`, antes de que exista React. Si esperasemos a este componente,
 *    la pagina se pintaria en claro y saltaria a oscuro al montar. Este fichero
 *    solo mantiene el estado a partir de ahi.
 *
 * 2. **`color-scheme` se sincroniza.** No es decorativo: es lo que hace que la
 *    barra de scroll, los desplegables nativos y el selector de fecha de los
 *    filtros salgan oscuros. Sin esto, un `<input type="date">` se queda con
 *    fondo blanco dentro de un panel oscuro.
 *
 * 3. **Se sigue al sistema mientras el usuario no elija.** Si nadie ha tocado el
 *    conmutador, el panel cambia solo cuando el sistema cambia de tema. En cuanto
 *    se elige a mano, manda la eleccion y se deja de escuchar.
 */

import {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useState,
    type ReactNode,
} from "react";
import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

export type Tema = "claro" | "oscuro";

/**
 * La misma clave que lee el script de `index.html`. Si se cambia aqui, hay que
 * cambiarla alli: son dos trozos del mismo acuerdo y no pueden discrepar, o el
 * parpadeo vuelve sin que nadie sepa por que.
 */
export const CLAVE_TEMA = "albertitos.tema";

interface ValorTema {
    tema: Tema;
    alternar: () => void;
}

const ContextoTema = createContext<ValorTema | null>(null);

/** El almacenamiento puede estar prohibido (modo privado, cookies bloqueadas). */
function leerGuardado(): Tema | null {
    try {
        const valor = window.localStorage.getItem(CLAVE_TEMA);
        return valor === "claro" || valor === "oscuro" ? valor : null;
    } catch {
        return null;
    }
}

function guardar(tema: Tema): void {
    try {
        window.localStorage.setItem(CLAVE_TEMA, tema);
    } catch {
        // Sin sitio donde guardarlo el panel funciona igual: se pierde la
        // eleccion al recargar, y eso no es motivo para romper la pantalla.
    }
}

function temaDelSistema(): Tema {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "oscuro" : "claro";
}

export function ProveedorTema({ children }: { children: ReactNode }) {
    const [tema, setTema] = useState<Tema>(() => leerGuardado() ?? temaDelSistema());
    const [elegidoAMano, setElegidoAMano] = useState<boolean>(() => leerGuardado() !== null);

    useEffect(() => {
        const raiz = document.documentElement;
        raiz.classList.toggle("dark", tema === "oscuro");
        raiz.style.colorScheme = tema === "oscuro" ? "dark" : "light";
    }, [tema]);

    useEffect(() => {
        if (elegidoAMano) return;
        const consulta = window.matchMedia("(prefers-color-scheme: dark)");
        const alCambiar = (evento: MediaQueryListEvent) => {
            setTema(evento.matches ? "oscuro" : "claro");
        };
        consulta.addEventListener("change", alCambiar);
        return () => consulta.removeEventListener("change", alCambiar);
    }, [elegidoAMano]);

    const alternar = useCallback(() => {
        const siguiente: Tema = tema === "oscuro" ? "claro" : "oscuro";
        setTema(siguiente);
        setElegidoAMano(true);
        guardar(siguiente);
    }, [tema]);

    const valor = useMemo<ValorTema>(() => ({ tema, alternar }), [tema, alternar]);

    return <ContextoTema.Provider value={valor}>{children}</ContextoTema.Provider>;
}

export function useTema(): ValorTema {
    const valor = useContext(ContextoTema);
    if (!valor) throw new Error("Falta <ProveedorTema> envolviendo la aplicacion.");
    return valor;
}

/**
 * El boton del conmutador.
 *
 * Enseña el icono de **a donde vas** y no de donde estas (con el panel en claro
 * sale la luna), y el `aria-label` lo dice con palabras para que no dependa del
 * icono. El `<title>` es el que da la pista al pasar el raton.
 */
export function BotonTema() {
    const { tema, alternar } = useTema();
    const aOscuro = tema === "claro";

    return (
        <Button
            variant="ghost"
            size="icon"
            onClick={alternar}
            aria-label={aOscuro ? "Cambiar a tema oscuro" : "Cambiar a tema claro"}
            title={aOscuro ? "Tema oscuro" : "Tema claro"}
        >
            {aOscuro ? <Moon /> : <Sun />}
        </Button>
    );
}
