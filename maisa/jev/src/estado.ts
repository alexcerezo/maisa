/**
 * De un PDF (o de su OCR) al `state` que se le manda al modelo.
 *
 * El state no es el documento entero: son tres trozos recortados por posicion,
 * porque en una factura el emisor, el numero y la fecha viven en las primeras
 * lineas, el NIF del cliente y las condiciones en las ultimas, y los importes
 * en medio. Mandar las 40 lineas completas cuesta mas y no mejora la decision.
 *
 * Este modulo no decide *como* se obtiene el texto. Acepta lineas ya extraidas
 * y ofrece la escalera de maisa para conseguirlas: cache del motor, capa de
 * texto del PDF y servicio de OCR.
 */
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { promisify } from 'node:util'

const run = promisify(execFile)

export type PageState = { header: string; body: string; footer: string }

/** De texto crudo a lineas utiles: sin espacios finales y sin lineas vacias. */
export function linesOf(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.replace(/\s+$/, ''))
    .filter((l) => l.trim().length > 0)
}

/**
 * Recorta las lineas a cabecera / cuerpo / pie.
 *
 * El corte solo se aplica en documentos "largos" (>18 lineas): en una pagina
 * corta, recortar el pie se comeria el total, que es justo el dato que
 * distingue una factura de un presupuesto.
 */
export function buildState(lines: string[], bodyChars = 2500): PageState {
  const long = lines.length > 18
  const header = lines.slice(0, 12)
  const footer = long ? lines.slice(-6) : []
  const body = long ? lines.slice(12, -6) : lines.slice(12)
  return {
    header: header.join('\n'),
    body: body.join('\n').slice(0, bodyChars),
    footer: footer.join('\n'),
  }
}

/**
 * Una pagina en blanco no gasta llamada.
 *
 * Se anaden las formulas en castellano a las del original porque el corpus de
 * maisa esta en castellano: el OCR de una pagina escaneada en blanco no
 * devuelve nada, pero una pagina con el aviso impreso si devuelve texto.
 */
export function isBlank(lines: string[]): boolean {
  const t = lines.join(' ').trim()
  return (
    t.length < 60 ||
    /intentionally left blank|left blank|se ha dejado en blanco|esta en blanco|pagina en blanco|página en blanco/i.test(t)
  )
}

// --------------------------------------------------------------------------- //
// Obtencion del texto
// --------------------------------------------------------------------------- //

/** Texto de cada pagina del payload de `/ocr` o `/ocr/text` de maisa. */
export function paginasDePayloadOcr(payload: unknown): string[] {
  if (typeof payload === 'string') return [payload]
  if (payload === null || typeof payload !== 'object') return []
  const p = payload as Record<string, unknown>
  if (Array.isArray(p.results)) {
    return p.results.map((r) => {
      if (typeof r === 'string') return r
      const pagina = r as Record<string, unknown>
      if (typeof pagina.text === 'string') return pagina.text
      if (Array.isArray(pagina.lines)) {
        return pagina.lines
          .map((l) => (typeof l === 'string' ? l : String((l as Record<string, unknown>).text ?? '')))
          .join('\n')
      }
      return ''
    })
  }
  if (Array.isArray(p.paginas)) return p.paginas.filter((x): x is string => typeof x === 'string')
  if (typeof p.texto === 'string') return [p.texto]
  if (typeof p.text === 'string') return [p.text]
  return []
}

/** Lineas de una pagina concreta (1-based) del payload del OCR. */
export function lineasDePaginaOcr(payload: unknown, page = 1): string[] {
  const paginas = paginasDePayloadOcr(payload)
  return linesOf(paginas[page - 1] ?? '')
}

/**
 * Texto ya cacheado por el motor de maisa (`motor/.cache/ocr/<sha256>.json`).
 *
 * Acepta los dos formatos que hay en el repositorio: el actual
 * (`{version, motor, paginas, texto}`) y el antiguo (`{sha256, texto}`). Que
 * las entradas antiguas se acepten es deliberado: la evaluacion tiene que poder
 * correr sin red. En el formato antiguo el texto es el documento entero, asi
 * que solo se devuelve para la pagina 1: devolverlo en cada pagina seria contar
 * el mismo documento dos veces.
 */
export function lineasDeCacheOcr(cacheDir: string, sha256: string, page = 1): string[] | undefined {
  const ruta = join(cacheDir, `${sha256}.json`)
  if (!existsSync(ruta)) return undefined
  const datos = JSON.parse(readFileSync(ruta, 'utf8')) as Record<string, unknown>
  if (Array.isArray(datos.paginas)) {
    const paginas = datos.paginas.filter((x): x is string => typeof x === 'string')
    const texto = paginas[page - 1]
    return texto === undefined ? undefined : linesOf(texto)
  }
  return typeof datos.texto === 'string' ? (page === 1 ? linesOf(datos.texto) : undefined) : undefined
}

/**
 * De que motor salio el texto de una entrada de cache: `nube`, `local`, o
 * `undefined` si la entrada es antigua y no lo dice.
 *
 * Mira `proveedor` (lo que escriben el motor y `precalentar-ocr.ts` desde el
 * arreglo), luego `escalon` (`vision_nube`) y por ultimo el prefijo `nube:` de
 * `motor`. El orden importa: en las entradas contaminadas el `motor` decia
 * `local:...` aunque el texto viniera de la nube, asi que no se puede confiar
 * en el prefijo por si solo.
 */
export function procedenciaDeCacheOcr(cacheDir: string, sha256: string): 'nube' | 'local' | undefined {
  const ruta = join(cacheDir, `${sha256}.json`)
  if (!existsSync(ruta)) return undefined
  try {
    const d = JSON.parse(readFileSync(ruta, 'utf8')) as Record<string, unknown>
    if (d.proveedor === 'nube') return 'nube'
    if (d.proveedor === 'local') return 'local'
    if (d.escalon === 'vision_nube') return 'nube'
    if (typeof d.motor === 'string' && d.motor.startsWith('nube:')) return 'nube'
    return undefined
  } catch {
    return undefined
  }
}

/** sha256 del fichero: la misma llave que usa el cache del motor. */
export function sha256DeFichero(file: string): string {
  return createHash('sha256').update(readFileSync(file)).digest('hex')
}

/** Capa de texto del PDF (`pdftotext -layout`), gratis y exacta cuando existe. */
export async function lineasDePdf(file: string, page: number): Promise<string[]> {
  const { stdout } = await run('pdftotext', ['-layout', '-f', String(page), '-l', String(page), file, '-'], {
    maxBuffer: 16 * 1024 * 1024,
  })
  return linesOf(stdout)
}

/**
 * Peldano de la escalera del que salio el texto.
 *
 * `cache-nube` distingue el texto cacheado que leyo la nube del que leyo el
 * motor local: son calidades y riesgos distintos (la nube alucina y no da
 * score; el local es trazable) y una evaluacion sobre un corpus mixto no vale
 * para decidir nada si no se puede separar.
 */
export type FuenteTexto = 'cache' | 'cache-nube' | 'pdf' | 'ocr'

export type OpcionesTexto = {
  /** Base del servicio de OCR de maisa, p.ej. `http://127.0.0.1:8866`. */
  ocrUrl?: string
  /** Directorio del cache del motor, p.ej. `maisa/motor/.cache/ocr`. */
  cacheDir?: string
  /** Motor del OCR: `auto`, `cloud` o `local`. */
  engine?: string
  page?: number
  timeoutMs?: number
}

/**
 * La escalera de maisa, en el mismo orden que `motor/src/maisa/lectura.py`:
 * de lo mas barato a lo mas caro, parando en el primer peldano que da texto.
 *
 *   1. **Cache** del motor, por sha256 del PDF (no por nombre).
 *   2. **Capa de texto** del PDF con `pdftotext`.
 *   3. **Servicio de OCR** (`POST /ocr/text`), que es lo que hace falta en las
 *      facturas escaneadas.
 *
 * Devuelve tambien de que peldano salio, porque el coste y la fiabilidad de la
 * clasificacion dependen de ello y eso hay que poder verlo en la traza.
 */
export async function lineasDeDocumento(
  file: string,
  opts: OpcionesTexto = {},
): Promise<{ lines: string[]; source: FuenteTexto }> {
  const page = opts.page ?? 1

  if (opts.cacheDir) {
    const sha = sha256DeFichero(file)
    const cached = lineasDeCacheOcr(opts.cacheDir, sha, page)
    if (cached?.length) {
      return { lines: cached, source: procedenciaDeCacheOcr(opts.cacheDir, sha) === 'nube' ? 'cache-nube' : 'cache' }
    }
  }

  try {
    const delPdf = await lineasDePdf(file, page)
    if (delPdf.length) return { lines: delPdf, source: 'pdf' }
  } catch {
    // Sin `pdftotext` instalado, o PDF sin capa de texto: se sigue bajando.
  }

  if (opts.ocrUrl) {
    const lineas = await lineasDeOcr(file, { ...opts, page })
    if (lineas.length) return { lines: lineas, source: 'ocr' }
  }

  return { lines: [], source: 'pdf' }
}

/** OCR de una pagina contra el servicio de maisa. */
export async function lineasDeOcr(file: string, opts: OpcionesTexto = {}): Promise<string[]> {
  const base = (opts.ocrUrl ?? 'http://127.0.0.1:8866').replace(/\/$/, '')
  const url = new URL(`${base}/ocr/text`)
  if (opts.engine) url.searchParams.set('engine', opts.engine)

  const form = new FormData()
  form.append('file', new Blob([readFileSync(file)]), file.split('/').pop() ?? 'documento.pdf')

  const res = await fetch(url, {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(opts.timeoutMs ?? 180_000),
  })
  if (!res.ok) throw new Error(`OCR HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`)
  return lineasDePaginaOcr(await res.json(), opts.page ?? 1)
}
