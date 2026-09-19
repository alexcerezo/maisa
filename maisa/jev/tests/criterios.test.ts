import { describe, expect, it } from 'vitest'
import {
  FAMILIAS,
  NOT_IN_LIST,
  cargarCriterios,
  criterioDe,
  familiaDe,
  listaDeFamilia,
  listaPrimera,
  miembrosDe,
  validarCriterios,
  type Criteria,
} from '../src/criterios.js'

const criteria = cargarCriterios()

describe('registro real de criterios', () => {
  it('carga y valida el fichero de datos', () => {
    expect(Object.keys(criteria).length).toBe(23)
  })

  it('la primera pregunta no lleva las variantes de una familia', () => {
    const primera = listaPrimera(criteria)
    expect(Object.keys(primera)).not.toContain('factura-rectificativa')
    expect(Object.keys(primera)).not.toContain('abono-parcial')
    expect(Object.keys(primera)).toContain('factura')
    expect(Object.keys(primera)).toContain('carta')
  })

  it('la primera pregunta siempre ofrece la opcion de escape', () => {
    expect(listaPrimera(criteria)[NOT_IN_LIST]).toBeDefined()
    expect(listaDeFamilia('factura', criteria)[NOT_IN_LIST]).toBeDefined()
  })

  it('cada familia cabe en una pregunta de eleccion (maximo 255 opciones)', () => {
    for (const familia of FAMILIAS) {
      expect(Object.keys(listaDeFamilia(familia, criteria)).length).toBeLessThanOrEqual(255)
    }
  })

  it('toda variante se resuelve por su familia', () => {
    for (const id of Object.keys(criteria)) {
      const familia = familiaDe(id, criteria)
      expect(miembrosDe(familia, criteria)).toContain(id)
    }
  })

  it('la primera lista es corta: es lo que hace barata la llamada', () => {
    expect(Object.keys(listaPrimera(criteria)).length).toBeLessThanOrEqual(20)
  })
})

describe('criterioDe', () => {
  it('junta etiqueta, descripcion, anclas y desambiguacion', () => {
    const c = criterioDe(criteria['factura-rectificativa'])
    expect(c.what).toContain('Factura rectificativa')
    expect(c.examples).toContain('FACTURA RECTIFICATIVA')
    expect(c.not_for).toContain('factura ordinaria')
  })

  it('no deja campos vacios cuando la entrada no los trae', () => {
    const c = criterioDe({ id: 'x', label: 'X', title: 'Cosa' })
    expect(c).toEqual({ what: 'X — Cosa' })
  })
})

describe('validarCriterios', () => {
  const base = (): Criteria => ({
    factura: { id: 'factura', label: 'Factura', title: 'Factura' },
    'factura-x': { id: 'factura-x', label: 'Factura X', title: 'Variante', parent: 'factura' },
    abono: { id: 'abono', label: 'Abono', title: 'Abono' },
    'abono-x': { id: 'abono-x', label: 'Abono X', title: 'Variante', parent: 'abono' },
    albaran: { id: 'albaran', label: 'Albaran', title: 'Albaran' },
    'albaran-x': { id: 'albaran-x', label: 'Albaran X', title: 'Variante', parent: 'albaran' },
    presupuesto: { id: 'presupuesto', label: 'Presupuesto', title: 'Presupuesto' },
    'presupuesto-x': { id: 'presupuesto-x', label: 'Presupuesto X', title: 'Variante', parent: 'presupuesto' },
    'extracto-bancario': { id: 'extracto-bancario', label: 'Extracto', title: 'Extracto' },
    'extracto-x': { id: 'extracto-x', label: 'Extracto X', title: 'Variante', parent: 'extracto-bancario' },
  })

  it('acepta un registro coherente', () => {
    expect(() => validarCriterios(base())).not.toThrow()
  })

  it('rechaza una etiqueta duplicada', () => {
    const c = base()
    c['factura-x'].label = 'Factura'
    expect(() => validarCriterios(c)).toThrow(/label duplicada/)
  })

  it('rechaza un padre inexistente', () => {
    const c = base()
    c['factura-x'].parent = 'fantasma'
    expect(() => validarCriterios(c)).toThrow(/padre inexistente/)
  })

  it('rechaza un padre que no esta en FAMILIAS', () => {
    const c = base()
    c['factura-x'].parent = 'factura-suelta'
    c['factura-suelta'] = { id: 'factura-suelta', label: 'Factura suelta', title: 'No es familia' }
    expect(() => validarCriterios(c)).toThrow(/no esta en FAMILIAS/)
  })

  it('rechaza una familia sin variantes: no merece cascada', () => {
    const c = base()
    delete c['abono-x']
    expect(() => validarCriterios(c)).toThrow(/no tiene variantes/)
  })

  it('rechaza un registro vacio', () => {
    expect(() => validarCriterios({})).toThrow(/vacio/)
  })
})
