import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { ProveedorFuente } from "./api/hooks";
import { ProveedorTema } from "./components/tema";
import { TooltipProvider } from "./components/ui/tooltip";
import "./index.css";

const root = document.getElementById("root");
if (!root) throw new Error("Falta el elemento #root en index.html");

/*
 * `ProveedorFuente` va por fuera del router: la decision de si los datos salen de
 * la API o del congelado no depende de la ruta, y dentro del router se volveria a
 * comprobar en cada navegacion. Aqui se comprueba una vez al arrancar y todas las
 * pantallas comparten la misma respuesta.
 *
 * `ProveedorTema` va por fuera de todo, incluido `ProveedorFuente`: la eleccion
 * de tema es del navegador y no de los datos, y si estuviera dentro, un fallo al
 * decidir la fuente dejaria el conmutador sin montar.
 *
 * `TooltipProvider` hace falta una sola vez para toda la aplicacion. Los
 * `Tooltip` de shadcn no funcionan sin el, y el fallo que dan es un error de
 * contexto en tiempo de ejecucion, no un aviso de tipos: por eso conviene que
 * este aqui arriba y no repetido en cada pantalla.
 */
createRoot(root).render(
    <StrictMode>
        <ProveedorTema>
            <TooltipProvider>
                <ProveedorFuente>
                    <BrowserRouter>
                        <App />
                    </BrowserRouter>
                </ProveedorFuente>
            </TooltipProvider>
        </ProveedorTema>
    </StrictMode>,
);
