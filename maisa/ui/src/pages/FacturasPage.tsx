import { Link } from "react-router-dom";

/**
 * Ruta `/facturas` — el explorador. Hoy es un andamio del Paso 0.
 *
 * Aquí van la cabecera, los filtros y la tabla. El enlace de abajo existe para
 * que se pueda comprobar que el router y la ruta de detalle funcionan sin
 * recargar la página; se borra cuando la tabla sea real.
 */
export default function FacturasPage() {
    return (
        <main className="mx-auto max-w-5xl p-8">
            <h1 className="text-2xl font-semibold text-slate-900">Facturas</h1>
            <p className="mt-2 text-slate-600">
                Esqueleto del Paso 0. Sin datos todavía: la capa de API y las fixtures
                son el Paso 1.
            </p>
            <Link
                to="/facturas/2026-01-08_P001.pdf"
                className="mt-4 inline-block text-blue-700 underline"
            >
                Abrir una factura de ejemplo →
            </Link>
        </main>
    );
}
