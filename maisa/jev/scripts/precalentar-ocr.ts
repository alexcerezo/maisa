/**
 * Precalienta la cache de OCR del corpus.
 *
 * La evaluacion necesita el texto de cada pagina, y `src/estado.ts` solo *lee*
 * la cache del motor: no la escribe. Sin esto, cada pasada de `npm run eval`
 * vuelve a mandar los 500 PDFs al servicio de OCR (~7 s por pagina, ~15 min
 * para el corpus entero). Con esto se paga una vez.
 *
 *   npm run precalentar
 *   npm run precalentar -- --cache ../_scratch/jev-eval/ocr --concurrency 6
 *
 * Escribe en el formato moderno del motor (`{version, sha256, motor, escalon,
 * paginas, texto}`), el mismo que lee `lineasDeCacheOcr`, asi que la cache vale
 * tanto para este paquete como para el motor. Por defecto NO toca
 * `motor/.cache/ocr`: ese directorio esta versionado en git y meter ahi 500
 * ficheros ensuciaria el repo. Se escribe en `_scratch/`, que si esta ignorado.
 */
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { paginasDePayloadOcr } from '../src/estado.js'

const aqui = import.meta.dirname
const raizMaisa = resolve(aqui, '..', '..')

function arg(nombre: string): string | undefined {
  const i = process.argv.indexOf(`--${nombre}`)
  return i === -1 ? undefined : process.argv[i + 1]
}
const num = (n: string, porDefecto: number) => (arg(n) === undefined ? porDefecto : Number(arg(n)))

const dir = resolve(aqui, arg('dir') ?? join(raizMaisa, 'data', 'facturas'))
const cacheDir = resolve(aqui, arg('cache') ?? join(raizMaisa, '_scratch', 'jev-eval', 'ocr'))
const ocrUrl = (arg('ocr') ?? 'http://127.0.0.1:8866').replace(/\/$/, '')
const engine = arg('engine') ?? 'auto'
const concurrencia = num('concurrency', 4)
const forzar = process.argv.includes('--forzar')

/** Version del formato de cache que acepta el motor (`VERSION_CACHE`). */
const VERSION_CACHE = 2

const sha256DeFichero = (file: string) => createHash('sha256').update(readFileSync(file)).digest('hex')

/** Firma de los modelos del motor de vision, igual que `firma_motor()` del motor. */
async function firmaMotor(): Promise<string> {
  try {
    const r = await fetch(`${ocrUrl}/health`, { signal: AbortSignal.timeout(2000) })
    const d = (await r.json()) as { engines?: { local?: { models?: Record<string, string> } } }
    const m = d.engines?.local?.models ?? {}
    const piezas = ['det', 'rec', 'cls'].map((k) => m[k] || 'sin_cls')
    return m.det || m.rec ? `local:${piezas.join('/')}` : ''
  } catch {
    return ''
  }
}

/** Una entrada ya vale si trae `paginas` y todas las paginas del PDF. */
function yaCacheada(file: string, sha: string, paginas: number): boolean {
  const ruta = join(cacheDir, `${sha}.json`)
  if (!existsSync(ruta)) return false
  try {
    const d = JSON.parse(readFileSync(ruta, 'utf8')) as Record<string, unknown>
    if (!Array.isArray(d.paginas)) return false
    return paginas === 0 || d.paginas.length >= paginas
  } catch {
    return false
  }
}

/** Paginas del PDF leyendo `/Type /Page` en crudo (no hay `pdfinfo` en el sistema). */
function cuentaPaginas(file: string): number {
  const crudo = readFileSync(file, 'latin1')
  const coincidencias = crudo.match(/\/Type\s*\/Page[^s]/g)
  return coincidencias?.length ?? 0
}

async function ocr(file: string, firma: string): Promise<'ok' | 'vacio'> {
  const sha = sha256DeFichero(file)
  const paginasPdf = cuentaPaginas(file)
  if (!forzar && yaCacheada(file, sha, paginasPdf)) return 'ok'

  const form = new FormData()
  form.append('file', new Blob([readFileSync(file)]), file.split('/').pop() ?? 'documento.pdf')

  const url = new URL(`${ocrUrl}/ocr`)
  if (engine) url.searchParams.set('engine', engine)

  const res = await fetch(url, { method: 'POST', body: form, signal: AbortSignal.timeout(300_000) })
  if (!res.ok) throw new Error(`OCR HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`)

  const paginas = paginasDePayloadOcr(await res.json())
  if (!paginas.length) return 'vacio'

  mkdirSync(cacheDir, { recursive: true })
  writeFileSync(
    join(cacheDir, `${sha}.json`),
    JSON.stringify({
      version: VERSION_CACHE,
      sha256: sha,
      motor: firma,
      escalon: 'vision_ocr',
      paginas,
      texto: paginas.join('\n'),
    }),
  )
  return 'ok'
}

async function main(): Promise<void> {
  const salud = await fetch(`${ocrUrl}/health`, { signal: AbortSignal.timeout(3000) }).catch(() => null)
  if (!salud?.ok) {
    console.error(`El servicio de OCR no responde en ${ocrUrl}/health. Levantalo antes de precalentar.`)
    process.exit(1)
  }
  const firma = await firmaMotor()

  const ficheros = readdirSync(dir)
    .filter((f) => f.toLowerCase().endsWith('.pdf'))
    .sort()

  console.log(`corpus:   ${dir}`)
  console.log(`cache:    ${cacheDir}`)
  console.log(`motor:    ${firma || '(sin firma)'}`)
  console.log(`ficheros: ${ficheros.length}, concurrencia ${concurrencia}`)
  console.log('')

  const t0 = Date.now()
  let hechos = 0
  let vacios = 0
  let fallos = 0
  let siguiente = 0

  async function trabajador(): Promise<void> {
    while (siguiente < ficheros.length) {
      const file = join(dir, ficheros[siguiente++])
      try {
        if ((await ocr(file, firma)) === 'vacio') vacios++
      } catch (err) {
        fallos++
        console.error(`  fallo ${ficheros[siguiente - 1]}: ${err instanceof Error ? err.message : err}`)
      }
      hechos++
      if (hechos % 25 === 0 || hechos === ficheros.length) {
        const s = (Date.now() - t0) / 1000
        console.log(`  ${hechos}/${ficheros.length}  ${s.toFixed(0)} s  (${(s / hechos).toFixed(1)} s/fichero)`)
      }
    }
  }

  await Promise.all(Array.from({ length: Math.max(1, concurrencia) }, trabajador))

  const s = (Date.now() - t0) / 1000
  console.log('')
  console.log(`listo: ${hechos} ficheros en ${s.toFixed(0)} s`)
  if (vacios) console.log(`sin texto: ${vacios}`)
  if (fallos) console.log(`fallos:    ${fallos}`)
  console.log('')
  console.log(`Ahora:  npm run eval -- --backend gateway --cache ${cacheDir}`)
}

main().catch((err) => {
  console.error('Precalentado fallido:', err instanceof Error ? err.message : err)
  process.exit(1)
})
