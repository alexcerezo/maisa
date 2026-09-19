/**
 * Tipos del clasificador.
 *
 * El contrato es el mismo que el del clasificador original sobre el que se
 * apoya este modulo: el modelo no devuelve texto, devuelve una opcion elegida
 * dentro de una lista cerrada mas una distribucion de probabilidad sobre esa
 * lista. Nada que parsear y nada que pueda salirse del conjunto de opciones.
 */

/**
 * Una opcion del registro tal y como se le describe al modelo.
 *
 * `what` es la descripcion; `examples` son cadenas que aparecen impresas en ese
 * tipo de pagina (son el ancla mas fuerte: el modelo ve el texto del documento,
 * no una descripcion abstracta); `not_for` desambigua frente a la opcion
 * vecina, que es donde el modelo falla.
 */
export type Criterion = {
  what: string
  examples?: string[]
  not_for?: string
}

/**
 * El estado que se le manda al modelo.
 *
 * Es JSON plano y no texto: el modelo ve un objeto con campos, no un prompt.
 * Por eso no hay plantilla de prompt en este paquete y no hay nada que
 * inyectar: el documento va como dato y las preguntas van aparte.
 */
export type EstadoModelo = Record<string, string | number | boolean | null>

/** Una pregunta de eleccion: una lista cerrada de opciones y nada mas. */
export type ChoiceQuestion = {
  type: 'choice'
  instructions: string
  criteria: Record<string, Criterion | null>
}

/** La respuesta a una pregunta de eleccion. */
export type ChoiceAnswer = {
  choice: string
  confidence: number
  probabilities: Record<string, number>
}

/** Resultado de una llamada: una respuesta por pregunta, evaluadas en paralelo. */
export type AskResult = {
  answers: Record<string, ChoiceAnswer>
  inputTokens: number
}

/**
 * El unico punto por el que el clasificador habla con un modelo.
 *
 * Existe para que las pruebas y la evaluacion corran **sin red y sin clave**:
 * el clasificador no sabe si detras hay Jev, el AI SDK o un doble de prueba.
 */
export interface Backend {
  ask(state: EstadoModelo, questions: Record<string, ChoiceQuestion>): Promise<AskResult>
}
