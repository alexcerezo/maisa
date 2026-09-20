/**
 * Un cuadro de texto que avisa a quien lo usa con retardo.
 *
 * Hace falta por el buscador. Cada tecla escrita cambiaria el parametro de la
 * direccion y, en modo vivo, lanzaria una peticion a la API: escribir "catering"
 * serian ocho consultas, siete de ellas tiradas. Peor todavia, la tabla
 * parpadearia con los resultados de las busquedas a medias.
 *
 * El retardo es corto (350 ms) a proposito: lo justo para agrupar las teclas de
 * una palabra escrita seguida. Con medio segundo, el buscador empieza a notarse
 * lento, y eso molesta mas que las peticiones que ahorra.
 *
 * El `useRef` de `alConfirmar` no es un adorno: la funcion se reconstruye en cada
 * render, y si entrara en las dependencias del efecto, el temporizador se
 * reiniciaria en cada render y **el aviso no llegaria nunca**.
 */

import { useEffect, useRef, useState } from "react";

export function useTextoConRetardo(
    valor: string,
    alConfirmar: (nuevo: string) => void,
    retardoMs = 350,
): readonly [string, (nuevo: string) => void] {
    const [borrador, setBorrador] = useState(valor);
    const confirmar = useRef(alConfirmar);
    confirmar.current = alConfirmar;

    // Si el valor de fuera cambia y no coincide con lo que hay escrito (por
    // ejemplo porque se ha pulsado "limpiar"), el borrador se pone al dia. La
    // comparacion dentro del `setBorrador` es la que evita el bucle: devolver el
    // mismo valor hace que React no vuelva a renderizar.
    useEffect(() => {
        setBorrador((actual) => (actual === valor ? actual : valor));
    }, [valor]);

    useEffect(() => {
        if (borrador === valor) return;
        const reloj = window.setTimeout(() => confirmar.current(borrador), retardoMs);
        return () => window.clearTimeout(reloj);
    }, [borrador, valor, retardoMs]);

    return [borrador, setBorrador];
}
