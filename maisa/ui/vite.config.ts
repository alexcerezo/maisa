import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
    plugins: [react(), tailwindcss()],
    server: {
        // El 5173 no es capricho: `DEFAULT_CORS_ORIGINS` en `maisa/api/app/config.py`
        // ya incluye `http://localhost:5173` y `http://127.0.0.1:5173`. Moviendo el
        // puerto, el navegador bloquea las peticiones a la API en local y el fallo
        // parece de la UI.
        port: 5173,
        strictPort: true,
    },
});
