import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeBackend, gatewayBackend, httpBackend, jevBackend } from '../src/backend.js'
import type { ChoiceQuestion } from '../src/tipos.js'

const pregunta: Record<string, ChoiceQuestion> = {
  tipo: {
    type: 'choice',
    instructions: '¿Que documento es?',
    criteria: { factura: { what: 'Factura' }, carta: { what: 'Carta' }, not_in_this_list: { what: 'Otra cosa' } },
  },
}
const state = { header: 'FACTURA Nº 1', body: '', footer: '' }

const respuestaApi = {
  model: 'jev-latest',
  answers: {
    tipo: { type: 'choice', choice: 'factura', probabilities: { factura: 0.88, carta: 0.07, not_in_this_list: 0.05 }, confidence: 0.91 },
  },
  usage: { input_tokens: 612, output_tokens: 12 },
}

function fetchFalso(impl: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const llamadas: { url: string; init: RequestInit; body: any }[] = []
  const fn = (async (input: any, init: any = {}) => {
    const url = String(input)
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    llamadas.push({ url, init, body })
    return impl(url, init)
  }) as unknown as typeof globalThis.fetch
  return { fn, llamadas }
}

afterEach(() => vi.unstubAllGlobals())

describe('httpBackend', () => {
  it('habla el contrato de TypeSafe: POST {base}/systemone con model, state y questions', async () => {
    const { fn, llamadas } = fetchFalso(
      () => new Response(JSON.stringify(respuestaApi), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    vi.stubGlobal('fetch', fn)
    const backend = httpBackend({ apiKey: 'k' })
    const r = await backend.ask(state, pregunta)

    expect(llamadas[0].url).toBe('https://api.typesafe.ai/v1/systemone')
    expect((llamadas[0].init.headers as Record<string, string>).Authorization).toBe('Bearer k')
    expect(llamadas[0].body.model).toBe('jev-latest')
    expect(llamadas[0].body.state).toEqual(state)
    expect(Object.keys(llamadas[0].body.questions)).toEqual(['tipo'])
    expect(r.answers.tipo.choice).toBe('factura')
    expect(r.inputTokens).toBe(612)
  })

  it('el 402 no se reintenta: es la respuesta de que Jev se cobra', async () => {
    const { fn, llamadas } = fetchFalso(() => new Response('{"message":"payment required"}', { status: 402 }))
    vi.stubGlobal('fetch', fn)
    await expect(httpBackend({ apiKey: 'k', backoffMs: 1 }).ask(state, pregunta)).rejects.toThrow(/pago \(402\)/)
    expect(llamadas).toHaveLength(1)
  })

  it('reintenta un fallo transitorio', async () => {
    let n = 0
    const { fn, llamadas } = fetchFalso(() => {
      n++
      return n === 1
        ? new Response('boom', { status: 503 })
        : new Response(JSON.stringify(respuestaApi), { status: 200, headers: { 'content-type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fn)
    const r = await httpBackend({ apiKey: 'k', backoffMs: 1 }).ask(state, pregunta)
    expect(llamadas).toHaveLength(2)
    expect(r.answers.tipo.choice).toBe('factura')
  })

  it('exige la clave: sin ella no hay backend', () => {
    const antes = process.env.TYPESAFE_API_KEY
    const antesAi = process.env.TYPESAFE_AI_API_KEY
    delete process.env.TYPESAFE_API_KEY
    delete process.env.TYPESAFE_AI_API_KEY
    try {
      expect(() => httpBackend()).toThrow(/TYPESAFE_AI_API_KEY/)
    } finally {
      if (antes !== undefined) process.env.TYPESAFE_API_KEY = antes
      if (antesAi !== undefined) process.env.TYPESAFE_AI_API_KEY = antesAi
    }
  })
})

describe('jevBackend (AI SDK + provider de TypeSafe)', () => {
  it('recorre el AI SDK entero y traduce la respuesta', async () => {
    const { fn, llamadas } = fetchFalso(
      () => new Response(JSON.stringify(respuestaApi), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const backend = jevBackend({ apiKey: 'k', fetch: fn })
    const r = await backend.ask(state, pregunta)

    expect(llamadas[0].url).toBe('https://api.typesafe.ai/v1/systemone')
    expect(llamadas[0].body.model).toBe('jev-latest')
    expect(llamadas[0].body.state).toEqual(state)
    expect(r.answers.tipo.choice).toBe('factura')
    expect(r.answers.tipo.probabilities.factura).toBe(0.88)
    expect(r.inputTokens).toBe(612)
  })

  it('toma la confianza de providerMetadata, no de la probabilidad', async () => {
    const { fn } = fetchFalso(
      () => new Response(JSON.stringify(respuestaApi), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const r = await jevBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)
    expect(r.answers.tipo.confidence).toBe(0.91)
  })

  it('si el modelo no manda confianza, usa la probabilidad de la opcion elegida', async () => {
    const sinConfianza = {
      answers: { tipo: { type: 'choice', choice: 'carta', probabilities: { factura: 0.2, carta: 0.75, not_in_this_list: 0.05 } } },
      usage: { input_tokens: 10 },
    }
    const { fn } = fetchFalso(
      () => new Response(JSON.stringify(sinConfianza), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const r = await jevBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)
    expect(r.answers.tipo.confidence).toBe(0.75)
    expect(r.inputTokens).toBe(10)
  })

  it('un 402 sale con un mensaje que dice el precio', async () => {
    const { fn } = fetchFalso(() => new Response('{"message":"payment required"}', { status: 402 }))
    await expect(jevBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)).rejects.toThrow(/0\.042/)
  })

  it('una clave invalida sale con un mensaje que dice que falta la clave', async () => {
    const { fn } = fetchFalso(() => new Response('{"message":"unauthorized"}', { status: 401 }))
    await expect(jevBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)).rejects.toThrow(/TYPESAFE_AI_API_KEY/)
  })
})

describe('gatewayBackend (AI Gateway de Vercel)', () => {
  const respuestaGateway = {
    answers: {
      tipo: { type: 'choice', choice: 'factura', probabilities: { factura: 0.88, carta: 0.07, not_in_this_list: 0.05 } },
    },
    usage: { inputTokens: 612, outputTokens: 12 },
    providerMetadata: { typesafe: { confidence: { tipo: 0.93 } } },
  }

  it('habla el contrato del Gateway: POST {base}/evaluation-model con state y questions', async () => {
    const { fn, llamadas } = fetchFalso(
      () =>
        new Response(JSON.stringify(respuestaGateway), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const r = await gatewayBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)

    expect(llamadas[0].url).toBe('https://ai-gateway.vercel.sh/v4/ai/evaluation-model')
    expect(llamadas[0].init.method).toBe('POST')
    expect((llamadas[0].init.headers as Record<string, string>)['ai-model-id']).toBe('typesafe-ai/jev')
    expect((llamadas[0].init.headers as Record<string, string>)['ai-evaluation-model-specification-version']).toBe('4')
    expect(llamadas[0].body.state).toEqual(state)
    expect(llamadas[0].body.questions).toEqual(pregunta)
    expect(r.answers.tipo.choice).toBe('factura')
    expect(r.inputTokens).toBe(612)
  })

  it('la confianza sigue viniendo de providerMetadata.typesafe, igual que por TypeSafe directo', async () => {
    const { fn } = fetchFalso(
      () =>
        new Response(JSON.stringify(respuestaGateway), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const r = await gatewayBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)
    expect(r.answers.tipo.confidence).toBe(0.93)
  })

  it('admite otro modelo del Gateway', async () => {
    const { fn, llamadas } = fetchFalso(
      () =>
        new Response(JSON.stringify(respuestaGateway), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    await gatewayBackend({ apiKey: 'k', model: 'otro/modelo', fetch: fn }).ask(state, pregunta)
    expect((llamadas[0].init.headers as Record<string, string>)['ai-model-id']).toBe('otro/modelo')
  })

  it('un 401 dice que falta AI_GATEWAY_API_KEY, no TYPESAFE_AI_API_KEY', async () => {
    const { fn } = fetchFalso(
      () =>
        new Response(JSON.stringify({ error: { type: 'authentication_error', message: 'unauthorized' } }), {
          status: 401,
          headers: { 'content-type': 'application/json' },
        }),
    )
    await expect(gatewayBackend({ apiKey: 'k', fetch: fn }).ask(state, pregunta)).rejects.toThrow(/AI_GATEWAY_API_KEY/)
  })

  it('un 429 del free tier sale como rate limit, no como fallo de red', async () => {
    const { fn } = fetchFalso(
      () =>
        new Response(JSON.stringify({ error: { type: 'rate_limit_exceeded', message: 'too many requests' } }), {
          status: 429,
          headers: { 'content-type': 'application/json' },
        }),
    )
    // `retries: 0` porque un 429 si es reintentable: el AI SDK lo reintentaria
    // con backoff y el test se pasaria del timeout esperando.
    await expect(gatewayBackend({ apiKey: 'k', fetch: fn, retries: 0 }).ask(state, pregunta)).rejects.toThrow(/429/)
  })
})

describe('fakeBackend', () => {
  it('rellena la confianza con la probabilidad de la opcion elegida', async () => {
    const backend = fakeBackend(() => ({ choice: 'carta', probabilities: { carta: 0.4, factura: 0.3 } }))
    const r = await backend.ask(state, pregunta)
    expect(r.answers.tipo).toEqual({ choice: 'carta', confidence: 0.4, probabilities: { carta: 0.4, factura: 0.3 } })
  })

  it('puede simular un fallo de red', async () => {
    const backend = fakeBackend(() => ({}), { falla: new Error('sin red') })
    await expect(backend.ask(state, pregunta)).rejects.toThrow('sin red')
  })
})
