import { createServer, type Server } from 'node:http'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { lineasDeDocumento, lineasDeOcr } from '../src/estado.js'

/**
 * El servicio de OCR de maisa es una dependencia real del paquete, asi que se
 * prueba contra un doble que habla su contrato: multipart con el fichero, el
 * motor como parametro de la URL y el payload `{results: [{page, lines}]}`.
 */
let server: Server
let base: string
let visto: { url: string; contentType: string; cuerpo: string } | undefined

const payload = {
  file: 'factura.pdf',
  engine: 'auto',
  pages: 1,
  elapsed: 1.2,
  stats: {},
  results: [{ page: 1, lines: [{ text: 'FACTURA Nº 1', score: 0.99 }], text: 'FACTURA Nº 1' }],
}

beforeAll(async () => {
  server = createServer((req, res) => {
    const trozos: Buffer[] = []
    req.on('data', (c) => trozos.push(c as Buffer))
    req.on('end', () => {
      visto = {
        url: req.url ?? '',
        contentType: req.headers['content-type'] ?? '',
        cuerpo: Buffer.concat(trozos).toString('latin1'),
      }
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify(payload))
    })
  })
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
  const addr = server.address()
  base = `http://127.0.0.1:${typeof addr === 'object' && addr ? addr.port : 0}`
})

afterAll(() => new Promise<void>((r) => server.close(() => r())))

describe('lineasDeOcr', () => {
  it('manda el fichero como multipart y el motor en la query', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'jev-ocr-'))
    const file = join(dir, 'factura.pdf')
    writeFileSync(file, '%PDF-1.4 de mentira')

    const lines = await lineasDeOcr(file, { ocrUrl: `${base}/`, engine: 'cloud' })
    expect(lines).toEqual(['FACTURA Nº 1'])
    expect(visto?.url).toBe('/ocr/text?engine=cloud')
    expect(visto?.contentType).toContain('multipart/form-data')
    expect(visto?.cuerpo).toContain('filename="factura.pdf"')
    expect(visto?.cuerpo).toContain('%PDF-1.4 de mentira')
  })

  it('falla claro si el OCR contesta con error', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'jev-ocr-'))
    const file = join(dir, 'x.pdf')
    writeFileSync(file, 'x')
    await expect(lineasDeOcr(file, { ocrUrl: 'http://127.0.0.1:1' })).rejects.toThrow()
  })
})

describe('lineasDeDocumento con el OCR', () => {
  it('baja al OCR cuando no hay cache ni capa de texto', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'jev-doc-ocr-'))
    const file = join(dir, 'escaneada.pdf')
    writeFileSync(file, 'sin capa de texto')

    const { lines, source } = await lineasDeDocumento(file, { cacheDir: dir, ocrUrl: base, engine: 'auto' })
    expect(source).toBe('ocr')
    expect(lines).toEqual(['FACTURA Nº 1'])
  })
})
