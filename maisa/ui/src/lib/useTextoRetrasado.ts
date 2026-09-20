/**
 * Un texto que se escribe letra a letra, pero que solo se manda cuando el que
 * escribe para.
 *
 * El buscador esta atado a la URL y la URL manda la peticion. Sin esto, escribir
 * "catering" serian ocho filtros, ocho `history.pushState` y ocho peticiones a
 * la API: la tabla parpadearia ocho veces y la barra de direcciones tendria ocho
 * entradas en el historial, con lo que el boton de atras del navegador dejaria
 * de servir para salir de la pantalla.
 *
 * Dos detalles que no se ven y son los que rompen estas cosas:
 *
 * 1. **El valor de fuera manda.** Si el filtro cambia por algo que no es este
 *    campo (el boton de limpiar, o un enlace pegado en la barra), el campo tiene
 *    que ponerse al dia. Se distingue "cambio de fuera" de "lo que acabo de
 *    escribir yo" comparando con lo ultimo que se envio, porque si no, cada
 *    pulsacion de tecla se veria a si misma como un cambio externo y el cursor
 *    saltaria.
 *
 * 2. **La funcion que envia va en un `ref`.** Se construye en cada render (cierra
 *    sobre los filtros), asi que usarla como dependencia del efecto lo volveria a
 *    lanzar en cada render y el temporizador no llegaria a cumplirse nunca. Es el
 *    mismo motivo por el que `hooks.tsx` guarda la peticion en un `ref`.
 */

import { useEffect, useRef, useState } from "react";

export const RETARDO_POR_DEFECTO = 300;

export function useTextoRetrasado(
    valor: string,
    alConfirmar: (valor: string) => void,
    retardo: number = RETARDO_POR_DEFECTO,
): [string, (siguiente: string) => void] {
    const [local, setLocal] = useState(valor);
    const ultimoEnviado = useRef(valor);

    const enviar = useRef(alConfirmar);
    enviar.current = alConfirmar;

    useEffect(() => {
        if (valor === ultimoEnviado.current) return;
        ultimoEnviado.current = valor;
        setLocal(valor);
    }, [valor]);

    useEffect(() => {
        if (local === ultimoEnviado.current) return;
        const temporizador = window.setTimeout(() => {
            ultimoEnviado.current = local;
            enviar.current(local);
        }, retardo);
        return () => window.clearTimeout(temporizador);
    }, [local, retardo]);

    return [local, setLocal];
}
