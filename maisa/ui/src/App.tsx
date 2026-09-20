import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AvisoFuente } from "./components/AvisoFuente";
import FacturaDetallePage from "./pages/FacturaDetallePage";
import FacturasPage from "./pages/FacturasPage";

/**
 * El armazon: cabecera, banda de fuente y las rutas.
 *
 * La banda de fuente (`AvisoFuente`) vive **aqui** y no dentro de la tabla, y es
 * lo unico de esta pantalla que se decidio con cuidado. Saber si lo que se esta
 * leyendo es la API de ahora o el ultimo volcado importa igual en la tabla que en
 * el detalle: una decision tomada sobre datos de hace un mes tiene que verse
 * igual de marcada en los dos sitios. Dentro de `FacturasPage` solo saldria en la
 * tabla, y quien llegase a una factura por un enlace directo no lo veria nunca.
 *
 * El enlace de la cabecera conserva los filtros cuando ya se esta en la tabla:
 * pulsar el nombre del producto no deberia borrar una busqueda que ha costado
 * escribir. Desde el detalle si lleva a la lista limpia, porque alli el que
 * manda es el boton "volver", que si trae los filtros.
 */
function Estructura() {
    const { pathname, search } = useLocation();
    const destino = pathname === "/facturas" ? `/facturas${search}` : "/facturas";

    return (
        <div className="min-h-screen bg-slate-50 text-slate-900">
            <header className="border-b border-slate-200 bg-white">
                <div className="mx-auto flex max-w-[110rem] items-center gap-3 px-4 py-3 sm:px-6 lg:px-8">
                    <Link
                        to={destino}
                        className="text-sm font-semibold tracking-tight text-slate-900 hover:text-slate-600"
                    >
                        Maisa
                    </Link>
                </div>
            </header>

            <div className="mx-auto max-w-[110rem] px-4 pt-4 sm:px-6 lg:px-8">
                <AvisoFuente />
            </div>

            <Routes>
                <Route path="/" element={<Navigate to="/facturas" replace />} />
                <Route path="/facturas" element={<FacturasPage />} />
                <Route path="/facturas/:fileId" element={<FacturaDetallePage />} />
                {/*
                 * El comodin es obligatorio: sin el, una direccion desconocida deja
                 * la pantalla en blanco, sin cabecera ni banda de fuente, y parece
                 * que la aplicacion se ha roto.
                 */}
                <Route path="*" element={<Navigate to="/facturas" replace />} />
            </Routes>
        </div>
    );
}

export default function App() {
    return <Estructura />;
}
