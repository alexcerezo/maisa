import { Navigate, Route, Routes } from "react-router-dom";

import Disposicion from "./components/Disposicion";
import FacturaDetallePage from "./pages/FacturaDetallePage";
import FacturasPage from "./pages/FacturasPage";

/**
 * Dos rutas reales, y una de ellas con parámetro.
 *
 * El detalle vive en la URL (`/facturas/:fileId`) y no en un estado interno de
 * la pantalla: así se puede recargar sin perderlo y el enlace se comparte. Es
 * lo único que hay que fijar antes de repartir los carriles, porque el carril
 * del detalle y el de la tabla tienen que estar de acuerdo en esto.
 *
 * Ojo con el `*`: sin él, cualquier ruta desconocida deja la pantalla en blanco.
 *
 * `Disposicion` envuelve a las `Routes` y no al reves: el marco (cabecera, pie,
 * distintivo de fuente) no cambia de una ruta a otra, así que se monta una sola
 * vez. Al revés, cada navegación lo remontaría y el distintivo de fuente
 * parpadearía en cada clic de la tabla.
 */
export default function App() {
    return (
        <Disposicion>
            <Routes>
                <Route path="/" element={<Navigate to="/facturas" replace />} />
                <Route path="/facturas" element={<FacturasPage />} />
                <Route path="/facturas/:fileId" element={<FacturaDetallePage />} />
                <Route path="*" element={<Navigate to="/facturas" replace />} />
            </Routes>
        </Disposicion>
    );
}
