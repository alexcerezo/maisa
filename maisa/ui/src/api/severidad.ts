/**
 * El significado de un hecho y de un resultado, en un solo sitio.
 *
 * Este fichero existe porque la tabla y el detalle lo necesitan **los dos** y
 * tienen que estar de acuerdo. Si cada carril se inventa su propia escala de
 * colores, acabas con una fila en ambar que al abrirla sale roja, y eso en un
 * panel que decide pagos es peor que un bug de pintado: es una contradiccion.
 *
 * Aqui se decide **que significa** cada cosa. **Como se pinta es de los
 * componentes** (las clases de Tailwind viven en `src/theme.ts` cuando toque,
 * no aqui): asi se puede cambiar la paleta sin tocar la logica, y se puede
 * probar la logica sin navegador.
 */

import type { Hecho, Resultado } from "./types";

/**
 * Las cuatro situaciones en que puede estar un hecho.
 *
 * `ok` y `duro` **no son lo mismo**, y confundirlos es el error facil de este
 * proyecto: un hecho puede incumplirse (`ok: false`) sin bloquear nada. Solo
 * `duro` significa "no se paga".
 */
export type Severidad =
    /** Se cumple. Lo normal: no deberia llamar la atencion. */
    | "ok"
    /** Incumplimiento duro: bloquea el pago. En el corpus, solo R5. */
    | "bloquea"
    /** Aviso explicito (`informativo: true`): no decide, pero se ensena. */
    | "aviso"
    /** Anomalia reportada sin concluir (`ok: false` y nada mas). */
    | "anomalia";

/** El orden de gravedad, de mas a menos. Para ordenar hechos sin inventarse otro. */
export const ORDEN_SEVERIDAD: Record<Severidad, number> = {
    bloquea: 0,
    aviso: 1,
    anomalia: 2,
    ok: 3,
};

/**
 * Traduce un hecho a su severidad, que es la tabla de `Hecho` en `types.ts`
 * leida en codigo. Se escribe una vez y no se repite en la tabla y en el
 * detalle.
 */
export function severidadHecho(hecho: Hecho): Severidad {
    if (hecho.ok) return "ok";
    if (hecho.duro) return "bloquea";
    if (hecho.informativo) return "aviso";
    return "anomalia";
}

/** Ordena hechos dejando delante lo que hay que mirar. No muta la entrada. */
export function ordenarHechos(hechos: readonly Hecho[]): Hecho[] {
    return [...hechos].sort((a, b) => ORDEN_SEVERIDAD[severidadHecho(a)] - ORDEN_SEVERIDAD[severidadHecho(b)]);
}

/**
 * La gravedad de la decision. Ojo: `NO_PAGAR` es la mas "grave" para el
 * proveedor pero es el sistema **funcionando** — detecto un pago duplicado y lo
 * paro. `ESCALAR` es el que pide un humano. La pantalla no deberia tratar el
 * rojo como un error del sistema, y por eso la escala se llama por lo que
 * significa y no por lo que se siente.
 */
export type Gravedad = "verde" | "ambar" | "rojo";

export function gravedadResultado(resultado: Resultado): Gravedad {
    switch (resultado) {
        case "PAGAR":
            return "verde";
        case "ESCALAR":
            return "ambar";
        case "NO_PAGAR":
            return "rojo";
    }
}
