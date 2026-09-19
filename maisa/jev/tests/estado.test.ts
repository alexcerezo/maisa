import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  buildState,
  isBlank,
  lineasDeCacheOcr,
  lineasDeDocumento,
  lineasDePaginaOcr,
  linesOf,
  paginasDePayloadOcr,
} from '../src/estado.js'

describe('linesOf', () => {
  it('quita lineas vacias y espacios finales', () => {
    expect(linesOf('FACTURA  \n\n  \nTotal 100\n')).toEqual(['FACTURA', 'Total 100'])
  })
})

describe('buildState', () => {
  const corta = ['FACTURA Nº 1', 'Cliente: ACME', 'Total: 100 EUR']

  it('en una pagina corta no recorta el pie: el total es el dato que decide', () => {
    const s = buildState(corta)
    expect(s.header).toBe('FACTURA Nº 1\nCliente: ACME\nTotal: 100 EUR')
    expect(s.body).toBe('')
    expect(s.footer).toBe('')
  })

  it('en una pagina larga separa cabecera, cuerpo y pie', () => {
    const larga = Array.from({ length: 30 }, (_, i) => `linea ${i + 1}`)
    const s = buildState(larga)
    expect(s.header.split('\n')).toHaveLength(12)
    expect(s.header).toContain('linea 1')
    expect(s.body).toContain('linea 13')
    expect(s.body).not.toContain('linea 25')
    expect(s.footer.split('\n')).toHaveLength(6)
    expect(s.footer).toContain('linea 30')
  })

  it('limita el cuerpo: el state se paga por token', () => {
    const larga = Array.from({ length: 60 }, () => 'x'.repeat(200))
    expect(buildState(larga, 100).body.length).toBe(100)
  })
})

describe('isBlank', () => {
  it('una pagina sin texto esta en blanco', () => {
    expect(isBlank([])).toBe(true)
    expect(isBlank(['', '   '])).toBe(true)
  })

  it('una pagina con poco texto esta en blanco', () => {
    expect(isBlank(['Pagina 2'])).toBe(true)
  })

  it('reconoce el aviso impreso, tambien en castellano', () => {
    expect(isBlank(['This page intentionally left blank and has no content at all'])).toBe(true)
    expect(isBlank(['Esta pagina se ha dejado en blanco de forma intencionada para el archivo'])).toBe(true)
  })

  it('una factura con texto no esta en blanco', () => {
    expect(isBlank(['FACTURA Nº 2026/22608', 'Cliente: ACME SL', 'Base imponible 100,00', 'Total 121,00'])).toBe(false)
  })
})

describe('payload del OCR de maisa', () => {
  it('lee el formato de /ocr/text con results[].lines', () => {
    const payload = {
      file: 'a.pdf',
      pages: 2,
      results: [
        { page: 1, lines: [{ text: 'FACTURA', score: 0.99 }, { text: 'Total 100', score: 0.9 }] },
        { page: 2, lines: [{ text: 'Anexo', score: 0.8 }] },
      ],
    }
    expect(paginasDePayloadOcr(payload)).toHaveLength(2)
    expect(lineasDePaginaOcr(payload, 1)).toEqual(['FACTURA', 'Total 100'])
    expect(lineasDePaginaOcr(payload, 2)).toEqual(['Anexo'])
  })

  it('lee el formato con text por pagina', () => {
    const payload = { results: [{ page: 1, text: 'FACTURA\nTotal 100' }] }
    expect(lineasDePaginaOcr(payload)).toEqual(['FACTURA', 'Total 100'])
  })

  it('lee el formato cache del motor con paginas[]', () => {
    expect(paginasDePayloadOcr({ version: 2, motor: 'paddle', paginas: ['uno', 'dos'] })).toEqual(['uno', 'dos'])
  })

  it('devuelve vacio si no hay texto reconocible', () => {
    expect(paginasDePayloadOcr({ nada: true })).toEqual([])
    expect(paginasDePayloadOcr(null)).toEqual([])
  })
})

describe('cache del motor', () => {
  const dir = mkdtempSync(join(tmpdir(), 'jev-cache-'))

  it('lee el formato nuevo {version, motor, paginas, texto}', () => {
    writeFileSync(
      join(dir, 'aaa.json'),
      JSON.stringify({ version: 2, motor: 'paddle', paginas: ['FACTURA\nTotal 100'], texto: 'FACTURA\nTotal 100' }),
    )
    expect(lineasDeCacheOcr(dir, 'aaa')).toEqual(['FACTURA', 'Total 100'])
  })

  it('lee el formato antiguo {sha256, texto}', () => {
    writeFileSync(join(dir, 'bbb.json'), JSON.stringify({ sha256: 'bbb', texto: 'ALBARAN\nCantidad 3' }))
    expect(lineasDeCacheOcr(dir, 'bbb')).toEqual(['ALBARAN', 'Cantidad 3'])
  })

  it('no inventa texto si no hay entrada', () => {
    expect(lineasDeCacheOcr(dir, 'ccc')).toBeUndefined()
  })
})

describe('lineasDeDocumento', () => {
  const dir = mkdtempSync(join(tmpdir(), 'jev-doc-'))
  const pdf = join(dir, 'factura.pdf')
  writeFileSync(pdf, 'no soy un pdf de verdad')

  it('empieza por el cache del motor, por sha256 del fichero', async () => {
    const { sha256DeFichero } = await import('../src/estado.js')
    const sha = sha256DeFichero(pdf)
    writeFileSync(join(dir, `${sha}.json`), JSON.stringify({ paginas: ['FACTURA DESDE CACHE'] }))
    const { lines, source } = await lineasDeDocumento(pdf, { cacheDir: dir })
    expect(source).toBe('cache')
    expect(lines).toEqual(['FACTURA DESDE CACHE'])
  })

  it('si no hay cache ni capa de texto, devuelve vacio en vez de romper', async () => {
    const vacio = join(dir, 'otro.pdf')
    writeFileSync(vacio, 'tampoco')
    const { lines } = await lineasDeDocumento(vacio, { cacheDir: dir })
    expect(lines).toEqual([])
  })
})
