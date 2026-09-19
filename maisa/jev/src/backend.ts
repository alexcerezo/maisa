/**
 * Los backends: quien responde de verdad a las preguntas.
 *
 * Hay cuatro y los cuatro implementan el mismo `Backend`:
 *
 * 1. `gatewayBackend()` — **el recomendado hoy**: el mismo Jev, pero a traves
 *    del AI Gateway de Vercel, que lo tiene en precio promocional 0 hasta el
 *    25 de septiembre de 2026.
 * 2. `jevBackend()` — el camino directo a TypeSafe con el AI SDK de Vercel y
 *    su provider (`@ai-sdk/typesafe-ai`). Precio de lista, sin Gateway.
 * 3. `httpBackend()` — el mismo modelo por HTTP directo, sin el AI SDK. Existe
 *    porque el clasificador original hablaba asi con TypeSafe, y porque tener
 *    los dos deja claro que la diferencia es de ergonomia y no de resultado.
 * 4. `fakeBackend()` — un doble sin red, para pruebas y para la evaluacion
 *    offline. No es un adorno: es lo que permite que este paquete se pueda
 *    verificar en CI sin clave y sin gastar dinero.
 */
// La API de evaluacion todavia viaja con el prefijo `experimental_` en el AI
// SDK: el prefijo es del nombre exportado, no de la estabilidad del modelo.
import { APICallError, experimental_evaluate as evaluate } from 'ai'
import { GatewayError, createGateway, type GatewayEvaluationModelId } from '@ai-sdk/gateway'
import { createTypeSafeAi, typeSafeAi } from '@ai-sdk/typesafe-ai'
import type { AskResult, Backend, ChoiceAnswer, ChoiceQuestion, EstadoModelo } from './tipos.js'

/** Lo que devuelve el AI SDK, reducido a lo que este paquete usa. */
type Evaluacion = {
  answers: Record<string, { type: string; choice?: string; probabilities?: Record<string, number> }>
  usage?: { inputTokens?: number | undefined } | undefined
  providerMetadata?: Record<string, unknown> | undefined
}

/**
 * Traduce una respuesta del AI SDK a `AskResult`.
 *
 * La confianza no viene en la respuesta de la pregunta, viene aparte en
 * `providerMetadata.typesafe.confidence` — y viaja igual por los dos caminos,
 * porque el Gateway reenvia la metadata del provider tal cual. Cuando el modelo
 * no la manda, se cae a la probabilidad de la opcion elegida, que es la misma
 * magnitud medida de otra forma: sin ese respaldo, un `undefined` se colaria
 * como 0 y todo el lote saldria por debajo de la puerta.
 */
function aRespuestas(result: Evaluacion): AskResult {
  const confianza = (result.providerMetadata?.typesafe as { confidence?: Record<string, number> } | undefined)
    ?.confidence
  const answers: Record<string, ChoiceAnswer> = {}
  for (const [id, respuesta] of Object.entries(result.answers)) {
    if (respuesta.type !== 'choice') continue
    const probabilities = respuesta.probabilities ?? {}
    const choice = respuesta.choice ?? ''
    answers[id] = {
      choice,
      confidence: confianza?.[id] ?? probabilities[choice] ?? 0,
      probabilities,
    }
  }
  return { answers, inputTokens: result.usage?.inputTokens ?? 0 }
}

export type JevOptions = {
  /** Por defecto, `TYPESAFE_AI_API_KEY` del entorno. */
  apiKey?: string
  /** Alias o version del modelo. Por defecto `jev-latest`. */
  model?: string
  /** Base de la API. Por defecto la del provider. */
  baseUrl?: string
  /** Reintentos ante fallos transitorios. Por defecto 2, el del AI SDK. */
  retries?: number
  /** `fetch` propio: es el punto por el que entran las pruebas sin red. */
  fetch?: typeof globalThis.fetch
}

/**
 * El camino directo a TypeSafe: AI SDK + provider de TypeSafe.
 *
 * Se paga el precio de lista ($0.042/MTok de entrada), asi que hoy es el
 * backend caro de los dos. Se mantiene porque es el unico que no depende de una
 * cuenta de Vercel y porque es el que no puede cambiar de precio por una
 * promocion.
 */
export function jevBackend(opts: JevOptions = {}): Backend {
  const model = opts.model ?? 'jev-latest'
  const provider =
    opts.apiKey !== undefined || opts.baseUrl !== undefined || opts.fetch !== undefined
      ? createTypeSafeAi({
          apiKey: opts.apiKey ?? process.env.TYPESAFE_AI_API_KEY,
          baseURL: opts.baseUrl,
          fetch: opts.fetch,
        })
      : typeSafeAi

  return {
    async ask(state, questions): Promise<AskResult> {
      try {
        const result = await evaluate({
          model: provider.evaluationModel(model),
          state,
          questions,
          maxRetries: opts.retries ?? 2,
        })
        return aRespuestas(result)
      } catch (err) {
        throw traducirError(err)
      }
    },
  }
}

export type GatewayOptions = {
  /** Por defecto, `AI_GATEWAY_API_KEY` del entorno (o el token OIDC de Vercel). */
  apiKey?: string
  /** Por defecto `typesafe-ai/jev`, el unico modelo de evaluacion del Gateway. */
  model?: GatewayEvaluationModelId
  /** Base del Gateway. Por defecto la del provider. */
  baseUrl?: string
  /** Reintentos ante fallos transitorios. Por defecto 2, el del AI SDK. */
  retries?: number
  /** `fetch` propio: es el punto por el que entran las pruebas sin red. */
  fetch?: typeof globalThis.fetch
}

/**
 * El backend recomendado hoy: el mismo Jev a traves del AI Gateway de Vercel.
 *
 * Merece la pena por dos razones. La primera es el precio: el Gateway tiene a
 * Jev en **promocion a 0 hasta el 25 de septiembre de 2026**; despues vuelve al
 * precio de lista, que es el mismo $0.042/MTok de entrada que cobra TypeSafe
 * directo (el Gateway no aplica margen sobre las tarifas del provider). La
 * segunda es operativa: el Gateway da trazabilidad por peticion, presupuesto por
 * equipo y BYOK, cosas que la cuenta de TypeSafe por si sola no da.
 *
 * `typesafe-ai/jev` es el unico modelo de evaluacion del Gateway y esta tipado
 * como literal en `@ai-sdk/gateway`, asi que un nombre mal escrito lo caza tsc y
 * no una llamada de red.
 *
 * Ojo con la promocion: es una fecha, no una condicion. Si el paquete se usa
 * para estimar coste, el 25 de septiembre de 2026 el coste real pasa de 0 al
 * precio de lista sin que cambie nada en el codigo.
 */
export function gatewayBackend(opts: GatewayOptions = {}): Backend {
  const model = opts.model ?? 'typesafe-ai/jev'
  const gw = createGateway({ apiKey: opts.apiKey, baseURL: opts.baseUrl, fetch: opts.fetch })

  return {
    async ask(state, questions): Promise<AskResult> {
      try {
        const result = await evaluate({
          model: gw.evaluationModel(model),
          state,
          questions,
          maxRetries: opts.retries ?? 2,
        })
        return aRespuestas(result)
      } catch (err) {
        throw traducirError(err)
      }
    },
  }
}

/**
 * El mismo modelo por HTTP directo.
 *
 * Es la traduccion del backend del clasificador original: `POST {base}/systemone`
 * con `{model, state, questions}` y `{answers, usage}` de vuelta. Se mantiene
 * porque es el contrato de la API, y tenerlo escrito aqui hace que el dia que
 * el AI SDK cambie de version no haya que releer la documentacion de TypeSafe.
 */
export function httpBackend(opts: JevOptions & { timeoutMs?: number; backoffMs?: number } = {}): Backend {
  const apiKey = opts.apiKey ?? process.env.TYPESAFE_API_KEY ?? process.env.TYPESAFE_AI_API_KEY
  if (!apiKey) throw new Error('Falta TYPESAFE_AI_API_KEY (o TYPESAFE_API_KEY) en el entorno')
  const model = opts.model ?? 'jev-latest'
  const url = `${opts.baseUrl ?? 'https://api.typesafe.ai/v1'}/systemone`
  const timeoutMs = opts.timeoutMs ?? 90_000
  const retries = opts.retries ?? 4
  const backoffMs = opts.backoffMs ?? 1500

  return {
    async ask(state, questions): Promise<AskResult> {
      const body = JSON.stringify({ model, state, questions })
      let lastErr: unknown
      for (let attempt = 0; attempt < retries; attempt++) {
        try {
          const res = await fetch(url, {
            method: 'POST',
            headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
            body,
            signal: AbortSignal.timeout(timeoutMs),
          })
          if (res.status === 402) throw new Error('TypeSafe: hace falta pago (402)')
          if (!res.ok) throw new Error(`TypeSafe HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`)
          const json = (await res.json()) as {
            answers: Record<string, ChoiceAnswer>
            usage?: { input_tokens?: number }
          }
          return { answers: json.answers, inputTokens: json.usage?.input_tokens ?? 0 }
        } catch (err) {
          lastErr = err
          if (err instanceof Error && /402/.test(err.message)) throw err
          await new Promise((r) => setTimeout(r, backoffMs * (attempt + 1)))
        }
      }
      throw lastErr
    },
  }
}

export type RespuestaFake = (
  questionId: string,
  question: ChoiceQuestion,
  state: EstadoModelo,
) => Partial<ChoiceAnswer>

/**
 * Un doble de backend para pruebas y evaluacion sin red.
 *
 * `responder` recibe la pregunta ya construida, asi que la prueba puede
 * comprobar *que* se le esta preguntando al modelo y no solo que devuelve.
 */
export function fakeBackend(
  responder: RespuestaFake,
  opts: { inputTokens?: number; falla?: Error } = {},
): Backend {
  return {
    async ask(state, questions): Promise<AskResult> {
      if (opts.falla) throw opts.falla
      const answers: Record<string, ChoiceAnswer> = {}
      for (const [id, question] of Object.entries(questions)) {
        const r = responder(id, question, state)
        const probabilities = r.probabilities ?? (r.choice ? { [r.choice]: r.confidence ?? 1 } : {})
        const choice = r.choice ?? Object.keys(probabilities)[0] ?? ''
        answers[id] = {
          choice,
          confidence: r.confidence ?? probabilities[choice] ?? 0,
          probabilities,
        }
      }
      return { answers, inputTokens: opts.inputTokens ?? 0 }
    },
  }
}

/** Un 402 no es un fallo de red: es la respuesta de que Jev se cobra. */
function traducirError(err: unknown): Error {
  if (GatewayError.isInstance(err)) {
    if (err.statusCode === 401 || err.statusCode === 403) {
      // El caso mas comun no es una clave mala: es una cuenta sin tarjeta. El
      // Gateway deja autenticar pero no sirve nada hasta que haya una tarjeta
      // en fichero, aunque el modelo este en promocion a 0.
      if (/credit card/i.test(err.message)) {
        return new Error(
          'AI Gateway 403: la clave vale, pero la cuenta no tiene ninguna tarjeta en fichero y el Gateway no sirve peticiones sin una, aunque el modelo este en promocion a 0.\n' +
            'Anade una tarjeta (no se cobra mientras dure la promo) en https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card',
        )
      }
      return new Error(
        `AI Gateway ${err.statusCode}: falta la clave, no vale, o la cuenta no tiene creditos. Define AI_GATEWAY_API_KEY (https://vercel.com/d?to=%2F%5Bteam%5D%2F~%2Fai%2Fapi-keys).\n${err.message}`,
      )
    }
    if (err.statusCode === 429) {
      return new Error(
        `AI Gateway 429: demasiadas peticiones a la vez (el tier gratuito tiene limites mas bajos). Baja --concurrency y reintenta.\n${err.message}`,
      )
    }
    return new Error(`AI Gateway HTTP ${err.statusCode}: ${err.message}`)
  }
  if (APICallError.isInstance(err)) {
    if (err.statusCode === 402) {
      return new Error(
        'TypeSafe 402: la cuenta no tiene saldo. Jev cuesta $0.042 por millon de tokens de entrada (la salida es gratis). Alternativa: gatewayBackend(), que hasta el 25 de septiembre de 2026 tiene a Jev en promocion a 0 en el AI Gateway de Vercel.',
      )
    }
    if (err.statusCode === 401 || err.statusCode === 403) {
      return new Error('TypeSafe 401/403: falta la clave o no vale. Define TYPESAFE_AI_API_KEY.')
    }
    return new Error(`TypeSafe HTTP ${err.statusCode ?? '?'}: ${err.message}`)
  }
  return err instanceof Error ? err : new Error(String(err))
}
