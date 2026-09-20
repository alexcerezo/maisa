import { Link, useParams } from "react-router-dom";

/**
 * Ruta `/facturas/:fileId` — el detalle. Andamio del Paso 0.
 *
 * El `fileId` viene de la URL y es el nombre del PDF, tal cual
 * (`2026-01-08_P001.pdf`): es la clave primaria de todo el sistema, la misma
 * que usan `/api/facturas/{file_id}` y el `/pdf`.
 */
export default function FacturaDetallePage() {
    const { fileId } = useParams<{ fileId: string }>();

    return (
        <main className="mx-auto max-w-5xl p-8">
            <Link to="/facturas" className="text-blue-700 underline">
                ← Volver a la lista
            </Link>

            <h1 className="mt-4 text-2xl font-semibold break-all text-slate-900">
                {fileId}
            </h1>

            <div className="mt-6 grid gap-6 lg:grid-cols-2">
                <section className="rounded border border-slate-200 p-4">
                    <h2 className="font-medium text-slate-700">Datos y evidencia</h2>
                    <p className="mt-2 text-slate-500">
                        Pendiente: motivo, hechos, campos y sospechosos.
                    </p>
                </section>
                <section className="rounded border border-slate-200 p-4">
                    <h2 className="font-medium text-slate-700">Documento original</h2>
                    <p className="mt-2 text-slate-500">
                        Pendiente: el PDF embebido. Va en un <code>&lt;iframe&gt;</code>, así
                        que el navegador no le aplica CORS.
                    </p>
                </section>
            </div>
        </main>
    );
}
