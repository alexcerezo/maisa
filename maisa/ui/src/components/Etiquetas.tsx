/**
 * Las dos etiquetas de estado que se repiten en toda la pantalla.
 *
 * Van juntas en un fichero porque son el mismo problema resuelto dos veces: un
 * valor del dominio (`Resultado`, `Severidad`) que se convierte en un badge con
 * su color y su icono. Separarlas en dos ficheros de veinte lineas solo haria
 * que hubiera que abrir dos para cambiar un color.
 *
 * Las dos leen su pintado de `theme.ts` y **no** deciden nada por su cuenta: si
 * decidieran aqui, la tabla y el detalle podrian acabar pintando el mismo estado
 * de distinta forma.
 */

import { Badge } from "@/components/ui/badge";
import type { Severidad } from "@/api/severidad";
import type { Resultado, SegundaLecturaResumen } from "@/api/types";
import {
    CLASE_COLA,
    CLASE_RESULTADO,
    CLASE_RESULTADO_DESCONOCIDO,
    CLASE_SEVERIDAD,
    ETIQUETA_COLA,
    ETIQUETA_RESULTADO,
    ETIQUETA_RESULTADO_DESCONOCIDO,
    ETIQUETA_SEVERIDAD,
    ICONO_COLA,
    ICONO_RESULTADO,
    ICONO_RESULTADO_DESCONOCIDO,
    ICONO_SEVERIDAD,
    estadoCola,
} from "@/theme";
import { cn } from "@/lib/utils";

/**
 * La decision del motor.
 *
 * No se usa `variant` del badge sino una clase propia: los colores de aqui son
 * del dominio (verde paga, ambar duda, rojo no paga) y no los semanticos del
 * sistema de diseño (`destructive` es "algo se ha roto", que es justo lo que
 * `NO_PAGAR` **no** significa).
 *
 * Los tres `??` no sobran aunque el tipo diga que la clave existe: `resultado`
 * viene por HTTP del motor y puede llegar a `null` cuando la traza no trae
 * `result`. Sin ellos, `ICONO_RESULTADO[null]` es `undefined` y `<Icono />`
 * tumba la pantalla entera (error #130). El mismo seguro que ya lleva el icono
 * de escalon en la tabla.
 */
export function EtiquetaResultado({
    resultado,
    className,
}: {
    resultado: Resultado;
    className?: string;
}) {
    const Icono = ICONO_RESULTADO[resultado] ?? ICONO_RESULTADO_DESCONOCIDO;
    return (
        <Badge
            variant="outline"
            className={cn(CLASE_RESULTADO[resultado] ?? CLASE_RESULTADO_DESCONOCIDO, className)}
        >
            <Icono />
            {ETIQUETA_RESULTADO[resultado] ?? ETIQUETA_RESULTADO_DESCONOCIDO}
        </Badge>
    );
}

/** La gravedad de un hecho evaluado. `ok` no llama la atencion, a proposito. */
export function EtiquetaSeveridad({
    severidad,
    className,
}: {
    severidad: Severidad;
    className?: string;
}) {
    const Icono = ICONO_SEVERIDAD[severidad];
    return (
        <Badge variant="outline" className={cn(CLASE_SEVERIDAD[severidad], className)}>
            <Icono />
            {ETIQUETA_SEVERIDAD[severidad]}
        </Badge>
    );
}

/**
 * En que ha quedado la segunda lectura de una escalada.
 *
 * **Devuelve `null` cuando no hay segunda lectura, y eso es deliberado**: 54 de
 * las 63 escaladas no la tienen, y pintarles un badge de "sin conclusión" seria
 * decir que el motor las releyó y no supo, cuando lo que pasa es que no las ha
 * releido nadie. Un hueco es la verdad; un badge falso, no.
 */
export function EtiquetaCola({
    segunda,
    className,
}: {
    segunda: SegundaLecturaResumen | null | undefined;
    className?: string;
}) {
    const estado = estadoCola(segunda);
    if (!estado) return null;
    const Icono = ICONO_COLA[estado];
    return (
        <Badge variant="outline" className={cn(CLASE_COLA[estado], className)}>
            <Icono />
            {ETIQUETA_COLA[estado]}
        </Badge>
    );
}
