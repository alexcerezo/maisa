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
import type { Resultado } from "@/api/types";
import {
    CLASE_RESULTADO,
    CLASE_SEVERIDAD,
    ETIQUETA_RESULTADO,
    ETIQUETA_SEVERIDAD,
    ICONO_RESULTADO,
    ICONO_SEVERIDAD,
} from "@/theme";
import { cn } from "@/lib/utils";

/**
 * La decision del motor.
 *
 * No se usa `variant` del badge sino una clase propia: los colores de aqui son
 * del dominio (verde paga, ambar duda, rojo no paga) y no los semanticos del
 * sistema de diseño (`destructive` es "algo se ha roto", que es justo lo que
 * `NO_PAGAR` **no** significa).
 */
export function EtiquetaResultado({
    resultado,
    className,
}: {
    resultado: Resultado;
    className?: string;
}) {
    const Icono = ICONO_RESULTADO[resultado];
    return (
        <Badge variant="outline" className={cn(CLASE_RESULTADO[resultado], className)}>
            <Icono />
            {ETIQUETA_RESULTADO[resultado]}
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
