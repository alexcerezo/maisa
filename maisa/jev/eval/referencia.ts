/**
 * Clasifica las mismas paginas con un modelo de chat corriente, para tener
 * contra que comparar a Jev.
 *
 * La pregunta que responde: *¿hace falta Jev, o un modelo normal hace lo
 * mismo?* Jev es un modelo de evaluacion especializado y muy rapido; esto mide
 * si esa especializacion se nota en el corpus de verdad.
 *
 *   npm run referencia -- --modelo google/gemini-2.5-flash --golden mano.jsonl
 *
 * Para que la comparacion sea honesta, al modelo de referencia se le da **mas**
 * que a Jev: la lista plana de los 23 tipos con sus descripciones completas, en
 * una sola pregunta, en vez de la cascada de dos pasos con listas cortas. Si
 * aun asi no gana, la conclusion es solida.
 *
 * Escribe las predicciones como jsonl (`{file, page, tipo}`), el mismo formato
 * que las etiquetas, asi que se pueden difear entre si.
 */
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { generateObject } from 'ai'
import { z } from 'zod'
import { buildState, isBlank, lineasDeDocumento, type PageState } from '../src/estado.js'
import { cargarCriterios, criterioDe, type Criteria } from '../src/criterios.js'

const aqui = import.meta.dirname
const raizMaisa = resolve(aqui, '..', '..')

function arg(nombre: string): string | undefined {
  const i = process.argv.indexOf(`--${nombre}`)
  return i === -1 ? undefined : process.argv[i + 1]
}
const num = (n: string, porDefecto: number) => (arg(n) === undefined ? porDefecto : Number(arg(n)))

const dir = resolve(aqui, arg('dir') ?? join(raizMaisa, 'data', 'facturas'))
const cacheDir = resolve(aqui, arg('cache') ?? join(raizMaisa, 'motor', '.cache', 'ocr'))
const ocrUrl = arg('ocr')
const modelo = arg('modelo') ?? 'google/gemini-2.5-flash'
const limit = num('limit', Infinity)
const maxPaginas = num('paginas', 3)
const concurrencia = num('concurrency', 4)
const filtro = arg('filtro')
const salida = arg('salida')
const golden = arg('golden') ? resolve(aqui, arg('golden')!) : undefined

const criterios: Criteria = cargarCriterios()
const IDS = Object.keys(criterios).sort() as [string, ...string[]]

/** El catalogo completo, en texto, con lo mismo que ve Jev de cada tipo. */
function catalogo(c: Criteria): string {
  return Object.keys(c)
    .sort()
    .map((id) => {
      const crit = criterioDe(c[id])
      const partes = [`- ${id}: ${crit.what}`]
      if (crit.examples?.length) partes.push(`  se ve asi: ${crit.examples.join(' | ')}`)
      if (crit.not_for) partes.push(`  ${crit.not_for}`)
      return partes.join('\n')
    })
    .join('\n')
}

const ESQUEMA = z.object({
  tipo: z.enum(IDS).describe('El id del tipo documental de la pagina'),
  motivo: z.string().describe('Una frase corta justificando la eleccion'),
})

function promptDe(state: PageState): string {
  return [
    'Clasifica esta pagina de un documento en uno de los tipos del catalogo.',
    'Lee el emisor, el numero y el titulo impresos en la cabecera, y los totales del pie.',
    'Una pagina de continuacion o de anexo pertenece al documento del que forma parte.',
    '',
    'CATALOGO',
    catalogo(criterios),
    '',
    'CABECERA',
    state.header || '(vacia)',
    '',
    'CUERPO',
    state.body || '(vacio)',
    '',
    'PIE',
    state.footer || '(vacio)',
  ].join('\n')
}

type Prediccion = { file: string; page: number; tipo: string; ms: number; tokens: number }

async function clasificar(file: string, page: number): Promise<Prediccion | null> {
  const t0 = Date.now()
  const { lines } = await lineasDeDocumento(file, { page, cacheDir, ocrUrl, engine: arg('engine') })
  if (!lines.length) return null
  if (isBlank(lines)) return { file, page, tipo: 'blank', ms: Date.now() - t0, tokens: 0 }

  const { object, usage } = await generateObject({
    model: modelo,
    schema: ESQUEMA,
    prompt: promptDe(buildState(lines)),
  })
  return {
    file,
    page,
    tipo: object.tipo,
    ms: Date.now() - t0,
    tokens: (usage.inputTokens ?? 0) + (usage.outputTokens ?? 0),
  }
}

function cargarEtiquetas(ruta: string | undefined): Map<string, string> {
  const out = new Map<string, string>()
  if (!ruta || !existsSync(ruta)) return out
  for (const linea of readFileSync(ruta, 'utf8').split('\n')) {
    const t = linea.trim()
    if (!t || t.startsWith('#') || t.startsWith('//')) continue
    const e = JSON.parse(t) as { file: string; page?: number; tipo: string }
    out.set(`${e.file}#${e.page ?? 1}`, e.tipo)
  }
  return out
}

async function main(): Promise<void> {
  if (!process.env.AI_GATEWAY_API_KEY && !process.env.OPENAI_API_KEY) {
    console.error('Falta AI_GATEWAY_API_KEY (o el de otro proveedor) para llamar al modelo de referencia.')
    process.exit(1)
  }

  const ficheros = readdirSync(dir)
    .filter((f) => f.toLowerCase().endsWith('.pdf'))
    .filter((f) => !filtro || f.includes(filtro))
    .sort()
    .slice(0, limit === Infinity ? undefined : limit)
    .map((f) => join(dir, f))

  const etiquetas = cargarEtiquetas(golden)

  console.log(`modelo:   ${modelo}`)
  console.log(`corpus:   ${dir}`)
  console.log(`cache:    ${cacheDir}${existsSync(cacheDir) ? '' : ' (no existe)'}`)
  console.log(`ficheros: ${ficheros.length}${filtro ? ` (filtro "${filtro}")` : ''}, concurrencia ${concurrencia}`)
  console.log('')

  const t0 = Date.now()
  const preds: Prediccion[] = []
  let siguiente = 0
  let vacias = 0

  await Promise.all(
    Array.from({ length: Math.min(concurrencia, ficheros.length) }, async () => {
      while (siguiente < ficheros.length) {
        const file = ficheros[siguiente++]
        try {
          for (let page = 1; page <= maxPaginas; page++) {
            const p = await clasificar(file, page)
            if (!p) {
              if (page === 1) vacias++
              break
            }
            preds.push(p)
          }
        } catch (err) {
          console.error(`  fallo ${file.split('/').pop()}: ${err instanceof Error ? err.message : err}`)
        }
        process.stderr.write(`  ${file.split('/').pop()}\n`)
      }
    }),
  )

  const ms = Date.now() - t0
  preds.sort((a, b) => (a.file === b.file ? a.page - b.page : a.file < b.file ? -1 : 1))

  if (salida) {
    writeFileSync(
      resolve(aqui, salida),
      preds.map((p) => JSON.stringify({ file: p.file, page: p.page, tipo: p.tipo })).join('\n') + '\n',
    )
  }

  const evaluables = preds.filter((p) => etiquetas.has(`${p.file}#${p.page}`))
  const aciertos = evaluables.filter((p) => etiquetas.get(`${p.file}#${p.page}`) === p.tipo).length
  const tokens = preds.reduce((s, p) => s + p.tokens, 0)

  console.log('')
  console.log(`paginas:      ${preds.length} (${vacias} sin texto)`)
  console.log(`tiempo:       ${(ms / 1000).toFixed(1)} s (${(ms / Math.max(preds.length, 1)).toFixed(0)} ms por pagina)`)
  console.log(`tokens:       ${tokens} (${(tokens / Math.max(preds.length, 1)).toFixed(0)} por pagina)`)
  if (evaluables.length) {
    console.log('')
    console.log(`etiquetadas:  ${evaluables.length}`)
    console.log(`acierto:      ${aciertos}/${evaluables.length} (${((aciertos / evaluables.length) * 100).toFixed(1)}%)`)
    const fallos = evaluables.filter((p) => etiquetas.get(`${p.file}#${p.page}`) !== p.tipo)
    if (fallos.length) {
      console.log('')
      console.log('fallos:')
      for (const f of fallos) {
        console.log(`  ${f.file.split('/').pop()} p${f.page}: dijo ${f.tipo}, era ${etiquetas.get(`${f.file}#${f.page}`)}`)
      }
    }
  } else if (golden) {
    console.log('')
    console.log('sin etiquetas que casen con las paginas clasificadas')
  }
  if (salida) console.log(`\npredicciones: ${resolve(aqui, salida)}`)
}

main().catch((err) => {
  console.error('Referencia fallida:', err instanceof Error ? err.message : err)
  process.exit(1)
})
