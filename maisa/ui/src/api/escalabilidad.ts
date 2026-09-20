/**
 * El contrato de `public/data/escalabilidad.json`: capacidad, coste y plan.
 *
 * Este fichero **no sale de la API**. La API sirve la conciliación (facturas,
 * traza, PDFs); esto es el banco de medidas del motor, que es un artefacto
 * estático y no cambia de una petición a otra. Por eso se lee del mismo sitio
 * en modo vivo y en modo congelado, y por eso no pasa por `Acceso`: no hay dos
 * orígenes entre los que elegir.
 *
 * Lo genera `tools/generar_escalabilidad.py` a partir de
 * `maisa/motor/docs/bench.json`, que a su vez lo escribió
 * `maisa/motor/tools/bench.py` cronometrando el lote de 500 facturas. La regla
 * de la casa vale aquí igual que allí: **lo que dice `medido: true` se midió en
 * esta máquina; lo que dice `medido: false` es extrapolación con el modelo
 * declarado**, y la pantalla tiene que distinguirlo o miente.
 *
 * Los tipos son deliberadamente cerrados y no `Record<string, unknown>`: si el
 * generador cambia una clave, esto deja de compilar y el fallo aparece en el
 * `build`, no como un `undefined` pintado en la página.
 */

import { esJson } from "./config";
import { ErrorPeticion } from "./cliente";

export const RUTA_ESCALABILIDAD = "/data/escalabilidad.json";

/** De dónde salen las cifras y cuándo se tomaron. */
export interface Procedencia {
    generado_por: string;
    fuente: string;
    medido_por: string;
    /** ISO 8601 con huso. Es un instante, no un día suelto. */
    medido_en: string;
    nota: string;
}

/** La máquina en la que se midió. Sin esto, "135 facturas/s" no significa nada. */
export interface Hardware {
    cpu_logicos: number;
    memoria_total_gb: number;
    plataforma: string;
    python: string;
    /** Carga media de 1, 5 y 15 minutos al arrancar la medida. */
    carga_media_al_inicio: number[];
    nota: string;
}

/** Una pasada del lote completo a un número de trabajadores. */
export interface FilaTrabajadores {
    trabajadores: number;
    mediana_s: number;
    min_s: number;
    max_s: number;
    /** Dispersión entre pasadas, en porcentaje. Mide si la máquina es estable. */
    rango_relativo_pct: number;
    facturas_por_s: number;
}

/** En qué se va el tiempo, por fase. */
export interface Fase {
    fase: string;
    segundos: number;
    reparto_pct: number;
}

export interface Capacidad {
    medido: true;
    lote: number;
    mejor_trabajadores: number;
    mejor_mediana_s: number;
    mejor_facturas_por_s: number;
    mejor_rango_relativo_pct: number;
    con_traza: { trabajadores: number; segundos: number; eventos: number };
    trabajadores: FilaTrabajadores[];
    fases: Fase[];
    total_fases_s: number;
    decision_ms_por_factura: number;
    reparto_lectura: {
        capa_texto: number;
        ocr: number;
        pct_capa_texto: number;
        pct_ocr: number;
    };
    lectura_texto: { ms_por_factura: number; facturas_por_s: number };
    lectura_cache: { ms_por_factura: number; facturas_por_s: number };
    ocr: {
        servicio_s_por_factura: number;
        facturas_por_hora_por_ranura: number;
        frio_serial_s_por_factura: number;
        frio_serial_facturas_por_s: number;
        /**
         * Solapar y ganar caudal son dos cosas distintas, y el generador las
         * mide por separado a proposito: `solapa_peticiones` sale del `idle` de
         * `/health` (0 = los dos motores ocupados a la vez) y dice si las
         * inferencias se pisan de verdad; `speedup_caudal_1_a_2` dice si eso se
         * traduce en mas facturas por segundo, que solo pasa si sobran nucleos.
         * Sustituyen a `paraleliza_el_contenedor`/`speedup_1_a_4_hilos`, que
         * mezclaban las dos en un solo numero.
         */
        motores: number;
        solapa_peticiones: boolean;
        speedup_caudal_1_a_2: number;
        en_vuelo: {
            peticiones: number;
            pared_mediana_s: number;
            latencia_mediana_s: number;
            facturas_por_s: number;
            idle_minimo: number;
        }[];
        nota_concurrencia: string;
    };
}

/** Un límite reconocido: qué limita, cuánto, y de dónde sale la cifra. */
export interface Limite {
    titulo: string;
    magnitud: string;
    detalle: string;
    origen: string;
}

/** Un escenario de extrapolación. `medido: false` por definición. */
export interface Escenario {
    clave: string;
    nombre: string;
    cuando: string;
    descripcion: string;
    /** Lo que cuesta cada escaneada en este régimen, en segundos de OCR. */
    coste_ocr_s: number;
    /** Clave = número de facturas como texto (`"50000"`), valor = segundos. */
    tiempos_s: Record<string, number>;
    facturas_por_s: Record<string, number>;
}

export interface Coste {
    medido: true;
    /** Hoy, en euros. Es 0 y el texto de al lado explica por qué. */
    hoy_eur: number;
    nota_hoy: string;
    unidad: string;
    por_factura_texto_vcpu_s: number;
    por_factura_cache_vcpu_s: number;
    por_factura_escaneada_vcpu_s: number;
    formula_tiempo: string;
    formula_euros: string;
    regimenes: Record<string, string>;
    constantes: Record<string, number>;
    contraste: {
        regimen: string;
        medido_s: number;
        modelo_s: number;
        error_relativo_pct: number;
    };
    escenarios: Escenario[];
    objetivos: number[];
    vcpu: { para_10000_escaneadas: number; para_1000000_escaneadas: number };
}

export interface PasoVolumen {
    titulo: string;
    detalle: string;
    coste: string;
    cuando: string;
}

export interface TipoArchivo {
    tipo: string;
    lector: string;
    detalle: string;
    ocr: string;
    estado: string;
}

export interface Plan {
    volumen: PasoVolumen[];
    tipos_archivo: TipoArchivo[];
    /** Lo que hay que tocar en el código para que entre un tipo nuevo. */
    pasos_tipo_nuevo: string[];
}

export interface Escalabilidad {
    procedencia: Procedencia;
    hardware: Hardware;
    capacidad: Capacidad;
    limites: Limite[];
    coste: Coste;
    plan: Plan;
    supuestos: string[];
    no_medido: string[];
}

/**
 * Lee el JSON del banco.
 *
 * El cuidado con el tipo de la respuesta es el mismo que en `datos.ts` y por el
 * mismo motivo: el `rewrites` de Vercel manda cualquier ruta desconocida a
 * `/index.html` **con 200**, así que un fichero que falta no da 404, da HTML. Sin
 * mirar el `content-type` antes, el fallo sería un `Unexpected token '<'` que no
 * explica nada.
 */
export async function cargarEscalabilidad(): Promise<Escalabilidad> {
    let respuesta: Response;
    try {
        respuesta = await fetch(RUTA_ESCALABILIDAD, { cache: "no-store" });
    } catch (exc) {
        throw new ErrorPeticion(`No he podido leer el banco de medidas (${RUTA_ESCALABILIDAD}).`, {
            ruta: RUTA_ESCALABILIDAD,
            sinRespuesta: true,
            causa: exc,
        });
    }
    if (!respuesta.ok) {
        throw new ErrorPeticion(`El banco de medidas responde ${respuesta.status}.`, {
            ruta: RUTA_ESCALABILIDAD,
            estado: respuesta.status,
        });
    }
    if (!esJson(respuesta)) {
        throw new ErrorPeticion(
            `Falta el banco de medidas: ${RUTA_ESCALABILIDAD} no devuelve JSON. ` +
                "Vuelve a generarlo con `python3 tools/generar_escalabilidad.py`.",
            { ruta: RUTA_ESCALABILIDAD, estado: respuesta.status },
        );
    }
    try {
        return (await respuesta.json()) as Escalabilidad;
    } catch (exc) {
        throw new ErrorPeticion(`El banco de medidas no es JSON válido (${RUTA_ESCALABILIDAD}).`, {
            ruta: RUTA_ESCALABILIDAD,
            estado: respuesta.status,
            causa: exc,
        });
    }
}
