/**
 * Evaluacion sobre el corpus real de maisa.
 *
 *   npm run eval -- --limit 30
 *   npm run eval -- --backend gateway --limit 500
 *   npm run eval -- --golden ../data/golden/etiquetas.jsonl
 *
 * Por defecto corre con un **doble heuristico**: clasifica por palabras clave
 * del propio texto y no llama a ningun modelo. Sirve para lo que se puede
 * comprobar sin gastar dinero ni tener clave: que la escalera de texto
 * (cache -> capa del PDF -> OCR) funciona sobre los PDF de verdad, cuanto
 * cuesta cada pagina en tokens, cuantas se quedan por debajo de la puerta y
 * cuanto se tarda. **No mide la precision de Jev**: eso hace falta la clave y
 * las etiquetas de `maisa/data/golden/`.
 *
 * `--backend` elige la via: `gateway` (AI Gateway de Vercel, gratis hasta el
 * 25 de septiembre de 2026), `jev` (TypeSafe directo, $0.042/MTok) o `fake`.
 * Sin `--backend`, se autodetecta por el entorno: primero `AI_GATEWAY_API_KEY`,
 * luego `TYPESAFE_AI_API_KEY`, y si no hay ninguna, el doble.
 */
import { execFile } from 'node:child_process'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { fakeBackend, gatewayBackend, jevBackend } from '../src/backend.js'
import { clasificarPagina, type PageResult } from '../src/clasificar.js'
import { cargarCriterios } from '../src/criterios.js'
import { lineasDeDocumento, type FuenteTexto } from '../src/estado.js'
import { puntuar, resumir, type Scored } from '../src/puntuar.js'
import type { Backend, ChoiceAnswer, ChoiceQuestion, EstadoModelo } from '../src/tipos.js'

const run = promisify(execFile)
const aqui = dirname(fileURLToPath(import.meta.url))
const raizMaisa = resolve(aqui, '..', '..')

// --------------------------------------------------------------------------- //
// Argumentos
// --------------------------------------------------------------------------- //

function arg(nombre: string): string | undefined {
  const i = process.argv.indexOf(`--${nombre}`)
  return i === -1 ? undefined : process.argv[i + 1]
}
const num = (n: string, porDefecto: number) => (arg(n) === undefined ? porDefecto : Number(arg(n)))

const dir = resolve(aqui, arg('dir') ?? join(raizMaisa, 'data', 'facturas'))
const cacheDir = resolve(aqui, arg('cache') ?? join(raizMaisa, 'motor', '.cache', 'ocr'))
const ocrUrl = arg('ocr')
const gate = num('gate', 0.95)
const limit = num('limit', Infinity)
const maxPaginas = num('paginas', 3)
const concurrencia = num('concurrency', 4)
const golden = arg('golden') ? resolve(aqui, arg('golden')!) : undefined
const filtro = arg('filtro')
const comoJson = process.argv.includes('--json')
const backendPedido =
  arg('backend') ??
  (process.env.AI_GATEWAY_API_KEY ? 'gateway' : process.env.TYPESAFE_AI_API_KEY ? 'jev' : 'fake')

// El tier gratuito del Gateway solo admite 30 peticiones por ventana y contesta
// 429 con un `retry-after` de casi un minuto. Los 2 reintentos por defecto del
// AI SDK se agotan antes de que la ventana se vuelva a abrir, asi que una
// evaluacion de 500 paginas se caia a mitad. Se suben, y `--rpm` ademas espacia
// las peticiones para no llegar al 429.
const reintentos = num('retries', backendPedido === 'gateway' ? 6 : 2)
const rpm = num('rpm', 0)

/**
 * Freno de ritmo: no deja pasar mas de `rpm` peticiones por minuto.
 *
 * Ir despacio sale mas barato que reintentar. Un 429 del Gateway no se resuelve
 * en milisegundos: se resuelve cuando se abre la ventana, y mientras tanto la
 * peticion esta gastada.
 */
class Ritmo {
  private marcas: number[] = []

  constructor(private readonly porMinuto: number) {}

  async esperar(): Promise<void> {
    if (!this.porMinuto) return
    for (;;) {
      const ahora = Date.now()
      while (this.marcas.length && ahora - this.marcas[0] >= 60_000) this.marcas.shift()
      if (this.marcas.length < this.porMinuto) {
        this.marcas.push(ahora)
        return
      }
      await new Promise((r) => setTimeout(r, 60_000 - (ahora - this.marcas[0]) + 50))
    }
  }
}

const ritmo = new Ritmo(rpm)

// --------------------------------------------------------------------------- //
// Doble heuristico
// --------------------------------------------------------------------------- //

/**
 * El doble: reparte la probabilidad por palabras clave impresas.
 *
 * No pretende acertar. Pretende que la tuberia tenga algo que decidir para que
 * el informe sea comparable con el de Jev cuando haya clave.
 */
function heuristica(): Backend {
  const doble = fakeBackend((_id, question: ChoiceQuestion, state: EstadoModelo) => {
    const texto = `${state.header ?? ''}\n${state.body ?? ''}\n${state.footer ?? ''}`.toLowerCase()
    const puntos: Record<string, number> = {}
    for (const [opcion, criterio] of Object.entries(question.criteria)) {
      let n = 0
      for (const pista of criterio?.examples ?? []) {
        const limpia = pista.toLowerCase().replace(/[%]/g, '')
        if (limpia.length >= 4 && texto.includes(limpia)) n += limpia.length
      }
      puntos[opcion] = n
    }
    const total = Object.values(puntos).reduce((a, b) => a + b, 0)
    const probabilities: Record<string, number> = {}
    for (const [opcion, n] of Object.entries(puntos)) {
      probabilities[opcion] = total > 0 ? Number((n / total).toFixed(2)) : Number((1 / Object.keys(puntos).length).toFixed(2))
    }
    let mejor = ''
    for (const [opcion, p] of Object.entries(probabilities)) if (p > (probabilities[mejor] ?? -1)) mejor = opcion
    return { choice: mejor, probabilities } satisfies Partial<ChoiceAnswer>
  })
  // El doble no gasta tokens, pero la peticion si ocupa: se estima a 4
  // caracteres por token sobre lo mismo que se manda de verdad (state +
  // preguntas), para que el informe de coste sea comparable con el de Jev.
  return {
    ask: async (state, questions) => {
      const r = await doble.ask(state, questions)
      return { ...r, inputTokens: Math.ceil(JSON.stringify({ state, questions }).length / 4) }
    },
  }
}

const backend: Backend =
  backendPedido === 'gateway'
    ? gatewayBackend({ model: arg('modelo'), retries: reintentos })
    : backendPedido === 'jev'
      ? jevBackend({ model: arg('modelo'), retries: reintentos })
      : backendPedido === 'fake'
        ? heuristica()
        : (() => {
            throw new Error(`Backend desconocido: ${backendPedido} (usa gateway, jev o fake)`)
          })()

const backendDescripcion =
  backendPedido === 'gateway'
    ? 'Jev (typesafe-ai/jev) a traves del AI Gateway de Vercel'
    : backendPedido === 'jev'
      ? 'Jev (TypeSafe) directo, a traves del AI SDK'
      : 'doble heuristico por palabras clave (NO es Jev)'

// El AI Gateway tiene a Jev en promocion a 0 hasta el 25 de septiembre de 2026;
// despues, y por el camino directo, el precio de lista es $0.042/MTok de
// entrada. `--precio` permite estimar el otro escenario sin tocar el codigo.
const precioPorMillon = num('precio', backendPedido === 'gateway' ? 0 : 0.042)

// --------------------------------------------------------------------------- //
// Etiquetas
// --------------------------------------------------------------------------- //

type Etiquetas = Map<string, string>
function cargarEtiquetas(ruta: string | undefined): Etiquetas {
  const out: Etiquetas = new Map()
  if (!ruta) return out
  if (!existsSync(ruta)) {
    console.error(`aviso: no existe el fichero de etiquetas ${ruta}; se evalua sin verdad`)
    return out
  }
  for (const linea of readFileSync(ruta, 'utf8').split('\n')) {
    const t = linea.trim()
    if (!t || t.startsWith('#') || t.startsWith('//')) continue
    const e = JSON.parse(t) as { file: string; page?: number; tipo: string }
    out.set(`${e.file}#${e.page ?? 1}`, e.tipo)
  }
  return out
}
const etiquetas = cargarEtiquetas(golden)

// --------------------------------------------------------------------------- //
// Paginas del PDF
// --------------------------------------------------------------------------- //

async function paginasDePdf(file: string): Promise<number> {
  try {
    const { stdout } = await run('pdfinfo', [file])
    const m = /^Pages:\s+(\d+)/m.exec(stdout)
    if (m) return Math.min(Number(m[1]), maxPaginas)
  } catch {
    // Sin poppler: se prueban paginas hasta que una salga vacia.
  }
  return maxPaginas
}

type Fila = {
  file: string
  page: number
  fuente: FuenteTexto | 'vacio'
  resultado: PageResult | null
  ms: number
}

async function clasificarFichero(file: string, criterios: ReturnType<typeof cargarCriterios>): Promise<Fila[]> {
  const filas: Fila[] = []
  const total = await paginasDePdf(file)
  for (let page = 1; page <= total; page++) {
    const t0 = Date.now()
    const { lines, source } = await lineasDeDocumento(file, { page, cacheDir, ocrUrl, engine: arg('engine') })
    if (!lines.length) {
      // La pagina 1 sin texto es una pagina que no se puede clasificar; a partir
      // de la segunda significa que el documento ya se acabo.
      if (page === 1) filas.push({ file, page, fuente: 'vacio', resultado: null, ms: Date.now() - t0 })
      break
    }
    await ritmo.esperar()
    const resultado = await clasificarPagina(lines, { backend, criteria: criterios, gate })
    filas.push({ file, page, fuente: source, resultado, ms: Date.now() - t0 })
  }
  return filas
}

// --------------------------------------------------------------------------- //
// Recorrido
// --------------------------------------------------------------------------- //

const criterios = cargarCriterios()
const ficheros = readdirSync(dir)
  .filter((f) => f.toLowerCase().endsWith('.pdf'))
  .filter((f) => !filtro || f.includes(filtro))
  .sort()
  .slice(0, limit === Infinity ? undefined : limit)
  .map((f) => join(dir, f))

if (!ficheros.length) {
  console.error(`no hay PDF en ${dir}`)
  process.exit(1)
}

const t0 = Date.now()
const filas: Fila[] = []
let siguiente = 0
await Promise.all(
  Array.from({ length: Math.min(concurrencia, ficheros.length) }, async () => {
    while (siguiente < ficheros.length) {
      const f = ficheros[siguiente++]
      const propias = await clasificarFichero(f, criterios)
      filas.push(...propias)
      process.stderr.write(`  ${f.split('/').pop()} -> ${propias.length} pagina(s)\n`)
    }
  }),
)
const ms = Date.now() - t0
filas.sort((a, b) => (a.file === b.file ? a.page - b.page : a.file < b.file ? -1 : 1))

// --------------------------------------------------------------------------- //
// Informe
// --------------------------------------------------------------------------- //

const conResultado = filas.filter((f) => f.resultado) as (Fila & { resultado: PageResult })[]
const paginas: Scored[] = conResultado.map((f) => puntuar('', f.resultado, gate))
const resumen = resumir(paginas, precioPorMillon)

// El acierto solo se puede medir sobre las paginas etiquetadas a mano. Sin
// etiquetas, el informe mide lo que si es medible: coste, latencia, cobertura
// del texto y cuantas paginas pasan la puerta.
const etiquetadas = conResultado
  .map((f) => ({ f, truth: etiquetas.get(`${f.file}#${f.page}`) }))
  .filter((x): x is { f: Fila & { resultado: PageResult }; truth: string } => x.truth !== undefined)
  .map((x) => puntuar(x.truth, x.f.resultado, gate))
const resumenEtiquetas = resumir(etiquetadas, precioPorMillon)
const gated = conResultado.filter((f) => f.resultado.gated).length

const porFuente: Record<string, number> = {}
for (const f of filas) porFuente[f.fuente] = (porFuente[f.fuente] ?? 0) + 1

const porTipo: Record<string, number> = {}
for (const f of conResultado) porTipo[f.resultado.tipo] = (porTipo[f.resultado.tipo] ?? 0) + 1

const cola = conResultado
  .filter((f) => !f.resultado.gated)
  .sort((a, b) => a.resultado.tipoConfidence - b.resultado.tipoConfidence)

if (comoJson) {
  console.log(
    JSON.stringify(
      {
        backend: backendDescripcion,
        precioPorMillon,
        dir,
        gate,
        resumen,
        etiquetadas: resumenEtiquetas,
        porFuente,
        porTipo,
        ms,
        filas: conResultado.map((f) => ({
          file: f.file,
          page: f.page,
          fuente: f.fuente,
          tipo: f.resultado.tipo,
          kind: f.resultado.kind,
          confianza: f.resultado.tipoConfidence,
          gated: f.resultado.gated,
          calls: f.resultado.calls,
          ms: f.ms,
        })),
      },
      null,
      2,
    ),
  )
} else {
  const pct = (n: number, d: number) => `${((n / Math.max(d, 1)) * 100).toFixed(1)}%`
  console.log('')
  console.log(`backend:        ${backendDescripcion}`)
  console.log(`corpus:         ${dir}`)
  console.log(`cache OCR:      ${cacheDir}${existsSync(cacheDir) ? '' : ' (no existe)'}`)
  console.log(`puerta:         ${gate}`)
  if (rpm) console.log(`ritmo:          ${rpm} peticiones/minuto (${reintentos} reintentos)`)
  console.log('')
  console.log(`documentos:     ${ficheros.length}`)
  console.log(`paginas:        ${filas.length} (${conResultado.length} clasificadas, ${filas.length - conResultado.length} sin texto)`)
  console.log(`texto de:       ${Object.entries(porFuente).map(([k, v]) => `${k} ${v}`).join(', ')}`)
  console.log(`tiempo:         ${(ms / 1000).toFixed(1)} s (${(ms / Math.max(filas.length, 1)).toFixed(0)} ms por pagina)`)
  console.log('')
  console.log(`llamadas:       ${resumen.calls} (${(resumen.calls / Math.max(conResultado.length, 1)).toFixed(2)} por pagina)`)
  console.log(`tokens entrada: ${resumen.inputTokens}`)
  console.log(
    `precio:         $${precioPorMillon}/MTok de entrada${
      backendPedido === 'gateway' ? ' (promocion del Gateway, hasta el 25 de septiembre de 2026)' : ''
    }`,
  )
  console.log(`coste estimado: $${resumen.estCostUsd.toFixed(6)} (${(resumen.estCostUsd / Math.max(conResultado.length, 1) * 1000).toFixed(4)} $ por 1000 paginas)`)
  console.log('')
  console.log(`pasarian la puerta: ${gated} de ${conResultado.length} (${pct(gated, conResultado.length)})`)
  console.log('')
  console.log('tipos:')
  for (const [tipo, n] of Object.entries(porTipo).sort((a, b) => b[1] - a[1])) {
    console.log(`  ${tipo.padEnd(28)} ${String(n).padStart(4)}  ${pct(n, conResultado.length)}`)
  }
  console.log('')
  if (resumenEtiquetas.pages) {
    console.log(`paginas etiquetadas: ${resumenEtiquetas.pages}`)
    console.log(`aciertos:            ${resumenEtiquetas.pages - resumenEtiquetas.wrong} de ${resumenEtiquetas.pages} (${pct(resumenEtiquetas.pages - resumenEtiquetas.wrong, resumenEtiquetas.pages)})`)
    console.log(`estricto:            ${resumenEtiquetas.pages - resumenEtiquetas.strict} de ${resumenEtiquetas.pages} (${pct(resumenEtiquetas.pages - resumenEtiquetas.strict, resumenEtiquetas.pages)})`)
  } else {
    console.log('sin etiquetas: no se mide acierto. Para medirlo, crea un jsonl con')
    console.log('  {"file": "/ruta/factura.pdf", "page": 1, "tipo": "factura"}')
    console.log('y pasalo con --golden. El corpus de maisa/data/golden/ esta vacio.')
  }
  console.log('')
  console.log(`cola de revision (${cola.length} paginas por debajo de la puerta, las 20 peores):`)
  for (const f of cola.slice(0, 20)) {
    console.log(`  ${f.resultado.tipoConfidence.toFixed(2)}  ${f.resultado.tipo.padEnd(24)} ${f.file.split('/').pop()}#${f.page} [${f.fuente}]`)
  }
}
