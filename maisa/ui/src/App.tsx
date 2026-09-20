import { Navigate, Route, Routes } from "react-router-dom";

import Disposicion from "./components/Disposicion";
import EscalabilidadPage from "./pages/EscalabilidadPage";
import FacturaDetallePage from "./pages/FacturaDetallePage";
import FacturasPage from "./pages/FacturasPage";
import TrazabilidadPage from "./pages/TrazabilidadPage";

/**
 * Cuatro rutas reales, y una de ellas con parámetro.
 *
 * El detalle vive en la URL (`/facturas/:fileId`) y no en un estado interno de
 * la pantalla: así se puede recargar sin perderlo y el enlace se comparte. Es
 * lo único que hay que fijar antes de repartir los carriles, porque el carril
 * del detalle y el de la tabla tienen que estar de acuerdo en esto.
 *
 * `/escalabilidad` es hermana de `/facturas`, no hija: es un documento sobre el
 * motor (capacidad, coste y plan), no una vista de los datos. Por eso no cuelga
 * de `/facturas` ni comparte estado con la tabla. Y por eso mismo es la única
 * ruta que no habla con la API: lee un JSON estático generado en el build, así
 * que se puede abrir aunque la API esté caída, que es justo cuando se quiere
 * leer por qué algo no escala.
 *
 * `/trazabilidad` es la otra hermana, y la que sí habla con la API. Enseña
 * **una** factura seguida de punta a punta en vez de un agregado, así que pide
 * el detalle de un expediente concreto además de `/health` y `/api/meta`. Es la
 * pantalla que contesta "¿y esto de dónde sale?" cuando alguien mira un
 * `NO_PAGAR` en la tabla y no se lo cree.
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
                <Route path="/escalabilidad" element={<EscalabilidadPage />} />
                <Route path="/trazabilidad" element={<TrazabilidadPage />} />
                <Route path="*" element={<Navigate to="/facturas" replace />} />
            </Routes>
        </Disposicion>
    );
}
