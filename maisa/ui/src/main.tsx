import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { ProveedorFuente } from "./api/hooks";
import "./index.css";

const root = document.getElementById("root");
if (!root) throw new Error("Falta el elemento #root en index.html");

/*
 * `ProveedorFuente` va por fuera del router: la decision de si los datos salen de
 * la API o del congelado no depende de la ruta, y dentro del router se volveria a
 * comprobar en cada navegacion. Aqui se comprueba una vez al arrancar y todas las
 * pantallas comparten la misma respuesta.
 */
createRoot(root).render(
    <StrictMode>
        <ProveedorFuente>
            <BrowserRouter>
                <App />
            </BrowserRouter>
        </ProveedorFuente>
    </StrictMode>,
);
