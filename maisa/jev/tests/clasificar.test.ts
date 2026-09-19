import { describe, expect, it } from 'vitest'
import { fakeBackend } from '../src/backend.js'
import { clasificarPagina } from '../src/clasificar.js'
import { FAMILIAS, cargarCriterios, listaDeFamilia } from '../src/criterios.js'
import type { Backend, ChoiceAnswer, EstadoModelo } from '../src/tipos.js'

const criteria = cargarCriterios()

const factura = [
  'GESTORIA EJEMPLO SL',
  'C/ Mayor 1, 28001 Madrid',
  'NIF: B12345678',
  'FACTURA Nº 2026/22608',
  'Fecha: 12/03/2026',
  'Cliente: ACME SA',
  'Base imponible 100,00',
  'IVA 21% 21,00',
  'Total factura 121,00',
]

/** Un doble que responde segun la pregunta y deja ver que se le ha preguntado. */
function doble(respuestas: Record<string, Partial<ChoiceAnswer>>) {
  const vistas: { id: string; opciones: string[]; state: EstadoModelo }[] = []
  return {
    vistas,
    backend: fakeBackend((id, question, state) => {
      vistas.push({ id, opciones: Object.keys(question.criteria), state })
      return respuestas[id] ?? { choice: 'carta', confidence: 0.5 }
    }, { inputTokens: 600 }),
  }
}

describe('clasificarPagina', () => {
  it('no llama al modelo para una pagina en blanco', async () => {
    const { backend, vistas } = doble({})
    const r = await clasificarPagina(['Pagina 2'], { backend, criteria })
    expect(vistas).toHaveLength(0)
    expect(r).toMatchObject({ tipo: 'blank', kind: 'blank', calls: 0, inputTokens: 0, gated: true })
  })

  it('resuelve un documento suelto en una sola llamada', async () => {
    const { backend, vistas } = doble({
      kind: { choice: 'documento', confidence: 0.99 },
      tipo: { choice: 'carta', confidence: 0.9, probabilities: { carta: 0.9, contrato: 0.05, not_in_this_list: 0.05 } },
    })
    const r = await clasificarPagina(factura, { backend, criteria })
    expect(r.tipo).toBe('carta')
    expect(r.kind).toBe('documento')
    expect(r.calls).toBe(1)
    expect(r.inputTokens).toBe(600)
    expect(r.stepConfidences).toEqual([0.9])
    expect(r.tipoConfidence).toBe(0.9)
    expect(vistas.map((v) => v.id)).toEqual(['kind', 'tipo'])
    expect(vistas[0].opciones).toContain('documento')
    expect(vistas[1].opciones).toContain('factura')
    expect(vistas[1].opciones).toContain('not_in_this_list')
  })

  it('manda el documento como state, no como prompt', async () => {
    const { backend, vistas } = doble({
      kind: { choice: 'documento', confidence: 1 },
      tipo: { choice: 'carta', confidence: 1, probabilities: { carta: 1 } },
    })
    await clasificarPagina(factura, { backend, criteria })
    const state = vistas[0].state
    expect(state.header).toContain('FACTURA Nº 2026/22608')
    expect(state.body).not.toContain('FACTURA Nº 2026/22608')
  })

  it('baja a la variante cuando el tipo elegido es una familia', async () => {
    const { backend, vistas } = doble({
      kind: { choice: 'documento', confidence: 0.98 },
      tipo: { choice: 'factura', confidence: 0.8, probabilities: { factura: 0.8, abono: 0.2 } },
      sub: { choice: 'factura-rectificativa', confidence: 0.7, probabilities: { 'factura-rectificativa': 0.7, factura: 0.3 } },
    })
    const r = await clasificarPagina(factura, { backend, criteria })
    expect(vistas.map((v) => v.id)).toEqual(['kind', 'tipo', 'sub'])
    expect(vistas[2].opciones).toEqual(Object.keys(listaDeFamilia('factura', criteria)))
    expect(r.tipo).toBe('factura-rectificativa')
    expect(r.calls).toBe(2)
    expect(r.inputTokens).toBe(1200)
    expect(r.stepConfidences).toEqual([0.8, 0.7])
  })

  it('la confianza del resultado es la del paso mas debil', async () => {
    const { backend } = doble({
      kind: { choice: 'documento', confidence: 0.99 },
      tipo: { choice: 'factura', confidence: 0.9, probabilities: { factura: 0.9, abono: 0.1 } },
      sub: { choice: 'factura', confidence: 0.55, probabilities: { factura: 0.55, proforma: 0.45 } },
    })
    const r = await clasificarPagina(factura, { backend, criteria })
    expect(r.tipoConfidence).toBe(0.55)
    expect(r.gated).toBe(false)
  })

  it('no baja a la variante si el tipo no es una familia', async () => {
    const { backend, vistas } = doble({
      kind: { choice: 'documento', confidence: 0.9 },
      tipo: { choice: 'pedido', confidence: 0.9, probabilities: { pedido: 0.9, not_in_this_list: 0.1 } },
    })
    const r = await clasificarPagina(factura, { backend, criteria })
    expect(vistas).toHaveLength(2)
    expect(r.tipo).toBe('pedido')
  })

  it('la opcion de escape nunca es la respuesta: se elige la mejor opcion real', async () => {
    const { backend } = doble({
      kind: { choice: 'documento', confidence: 0.9 },
      tipo: { choice: 'not_in_this_list', confidence: 0.97, probabilities: { not_in_this_list: 0.97, carta: 0.02, contrato: 0.01 } },
    })
    const r = await clasificarPagina(factura, { backend, criteria })
    expect(r.tipo).toBe('carta')
    expect(r.tipoConfidence).toBe(0.02)
    expect(r.gated).toBe(false)
  })

  it('la puerta se abre por defecto a 0.95', async () => {
    const { backend } = doble({
      kind: { choice: 'documento', confidence: 0.95 },
      tipo: { choice: 'carta', confidence: 0.95, probabilities: { carta: 0.95, not_in_this_list: 0.05 } },
    })
    expect((await clasificarPagina(factura, { backend, criteria })).gated).toBe(true)
  })

  it('permite mover la puerta', async () => {
    const { backend } = doble({
      kind: { choice: 'documento', confidence: 0.9 },
      tipo: { choice: 'carta', confidence: 0.9, probabilities: { carta: 0.9, not_in_this_list: 0.1 } },
    })
    expect((await clasificarPagina(factura, { backend, criteria, gate: 0.85 })).gated).toBe(true)
  })

  it('falla claro si el modelo no contesta a una pregunta', async () => {
    const backend: Backend = { ask: async () => ({ answers: {}, inputTokens: 0 }) }
    await expect(clasificarPagina(factura, { backend, criteria })).rejects.toThrow(/no respondio/)
  })

  it('la primera pregunta cabe en el limite de 255 opciones del modelo', async () => {
    const { backend, vistas } = doble({
      kind: { choice: 'documento', confidence: 1 },
      tipo: { choice: 'carta', confidence: 1, probabilities: { carta: 1 } },
    })
    await clasificarPagina(factura, { backend, criteria })
    for (const vista of vistas) expect(vista.opciones.length).toBeLessThanOrEqual(255)
  })

  it('todas las familias tienen su segunda pregunta', () => {
    for (const familia of FAMILIAS) {
      expect(Object.keys(listaDeFamilia(familia, criteria)).length).toBeGreaterThan(2)
    }
  })
})
