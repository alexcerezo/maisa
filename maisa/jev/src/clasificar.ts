/**
 * La clasificacion de una pagina.
 *
 * Una pagina, una llamada (dos si cae en una familia). Se preguntan a la vez el
 * papel de la pagina y su tipo documental, porque Jev evalua las preguntas en
 * paralelo sobre el mismo `state`: anadir la segunda pregunta casi no cuesta
 * tiempo y evita mandar el documento dos veces.
 */
import type { Backend, ChoiceAnswer, Criterion } from './tipos.js'
import { buildState, isBlank } from './estado.js'
import {
  FAMILIAS,
  KIND_CRITERIA,
  NOT_IN_LIST,
  listaDeFamilia,
  listaPrimera,
  type Criteria,
  type Kind,
} from './criterios.js'

const TIPO_INSTRUCTIONS =
  '¿A qué tipo de documento pertenece esta página? Lee el emisor, el número y el título impresos en la cabecera, y los totales del pie (por ejemplo "FACTURA Nº", "ALBARÁN", "PRESUPUESTO", "EXTRACTO DE CUENTA"). Una página de continuación o de anexo pertenece al documento del que forma parte. Si el documento no está entre las opciones, elige not_in_this_list.'

export type ClassifyOptions = {
  backend: Backend
  criteria: Criteria
  /** Umbral de la puerta. Por defecto 0.95, el mismo que el clasificador original. */
  gate?: number
  /** Primera lista ya construida, para no rehacerla en cada pagina de un lote. */
  firstList?: Record<string, Criterion | null>
}

export type PageResult = {
  /** El id del tipo documental del registro, o `not_in_this_list`. */
  tipo: string
  /** El papel de la pagina: `documento`, `anexo`, `blank`... */
  kind: Kind
  /** El minimo de la confianza de cada paso: es la que se compara con la puerta. */
  tipoConfidence: number
  kindConfidence: number
  stepConfidences: number[]
  /** `true` si `tipoConfidence >= gate`: por encima, se puede actuar sin mirar. */
  gated: boolean
  calls: number
  inputTokens: number
  probabilities: {
    tipo: Record<string, number>
    kind: Record<string, number>
    sub?: Record<string, number>
  }
}

/**
 * El mejor candidato de una distribucion, ignorando la opcion de escape.
 *
 * Esto es lo que hace que `not_in_this_list` funcione: se le ofrece al modelo
 * como destino legitimo para la masa de probabilidad que no corresponde a
 * ninguna opcion, pero **nunca se devuelve como respuesta**. El modelo puede
 * decir "esto no es ninguna de estas" sin que el clasificador tenga que
 * inventarse un tipo de documento para ese caso.
 */
function best(a: ChoiceAnswer, exclude: string): [string, number] {
  let top: [string, number] = ['', -1]
  for (const [k, v] of Object.entries(a.probabilities)) {
    if (k !== exclude && v > top[1]) top = [k, v]
  }
  if (top[1] < 0 && a.choice !== exclude) return [a.choice, a.confidence]
  return top
}

function exigir(respuestas: Record<string, ChoiceAnswer>, id: string, tipo: string): ChoiceAnswer {
  const a = respuestas[id]
  if (!a) throw new Error(`El modelo no respondio a la pregunta "${id}" (${tipo})`)
  return a
}

export async function clasificarPagina(lines: string[], opts: ClassifyOptions): Promise<PageResult> {
  const gate = opts.gate ?? 0.95

  // Una pagina en blanco se responde sin llamar al modelo: no hay nada que leer.
  if (isBlank(lines)) {
    return {
      tipo: 'blank',
      kind: 'blank',
      tipoConfidence: 1,
      kindConfidence: 1,
      stepConfidences: [1],
      gated: true,
      calls: 0,
      inputTokens: 0,
      probabilities: { tipo: { blank: 1 }, kind: { blank: 1 } },
    }
  }

  const state = buildState(lines)
  const firstList = opts.firstList ?? listaPrimera(opts.criteria)
  const r1 = await opts.backend.ask(state, {
    kind: { type: 'choice', instructions: '¿Qué clase de página es esta?', criteria: KIND_CRITERIA },
    tipo: { type: 'choice', instructions: TIPO_INSTRUCTIONS, criteria: firstList },
  })

  const kindA = exigir(r1.answers, 'kind', 'clase de pagina')
  const tipoA = exigir(r1.answers, 'tipo', 'tipo de documento')
  let [tipo, tipoP] = best(tipoA, NOT_IN_LIST)
  const steps = [tipoP]
  let calls = 1
  let inputTokens = r1.inputTokens
  let sub: Record<string, number> | undefined

  // Solo las familias pagan una segunda llamada, y solo si el modelo eligio la
  // familia: es la misma cascada del clasificador original.
  if ((FAMILIAS as readonly string[]).includes(tipo)) {
    const r2 = await opts.backend.ask(state, {
      sub: {
        type: 'choice',
        instructions: `¿Qué documento concreto es esta página? Todas las opciones pertenecen a: ${opts.criteria[tipo]?.title ?? tipo}. Lee el número y el título impresos en la cabecera y en el pie; una página de continuación pertenece al documento que continúa.`,
        criteria: listaDeFamilia(tipo, opts.criteria),
      },
    })
    calls++
    inputTokens += r2.inputTokens
    const subA = exigir(r2.answers, 'sub', 'variante')
    sub = subA.probabilities
    const [variante, varianteP] = best(subA, NOT_IN_LIST)
    tipo = variante
    steps.push(varianteP)
  }

  const tipoConfidence = Math.min(...steps)
  return {
    tipo,
    kind: kindA.choice as Kind,
    tipoConfidence,
    kindConfidence: kindA.confidence,
    stepConfidences: steps,
    gated: tipoConfidence >= gate,
    calls,
    inputTokens,
    probabilities: { tipo: tipoA.probabilities, kind: kindA.probabilities, ...(sub ? { sub } : {}) },
  }
}
