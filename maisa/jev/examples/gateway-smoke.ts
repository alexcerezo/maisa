/**
 * Prueba de humo del AI Gateway de Vercel.
 *
 * Comprueba dos cosas: que `AI_GATEWAY_API_KEY` esta puesta y que el Gateway
 * devuelve texto de verdad. Es a proposito un modelo de chat corriente y no
 * Jev: si esto falla, el problema es la clave, la cuenta o la red, no el
 * modelo de evaluacion, y se sabe en un segundo.
 *
 *   npm run smoke
 *   SMOKE_MODEL=google/gemini-2.5-flash npm run smoke
 *
 * Sobre el modelo por defecto: `openai/gpt-5.5` es el que se pidio, pero el
 * tier gratuito del Gateway no da acceso a el. Si el Gateway contesta con ese
 * aviso, el ejemplo reintenta una vez con `RESPALDO` para que la prueba siga
 * sirviendo para lo que sirve (verificar el cableado). Lo dice en voz alta,
 * no lo esconde.
 *
 * La clave se lee del entorno; este fichero no la toca, no la imprime y no la
 * escribe en ningun sitio.
 */
import { generateText } from 'ai'

const modelo = process.env.SMOKE_MODEL ?? 'openai/gpt-5.5'
const RESPALDO = process.env.SMOKE_FALLBACK ?? 'openai/gpt-4o-mini'

const PROMPT =
  'Inventa una festividad nueva y describela en dos frases: cuando se celebra, que se come y que tradicion rara tiene.'

/** El Gateway cobra el acceso al modelo, no la peticion: no vale reintentar igual. */
function esModeloFueraDelTierGratuito(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err)
  return /free tier users do not have access/i.test(msg)
}

async function pedir(m: string) {
  const { text, usage, response } = await generateText({ model: m, prompt: PROMPT })
  return { text, usage, modelId: response.modelId ?? m }
}

async function main(): Promise<void> {
  if (!process.env.AI_GATEWAY_API_KEY) {
    console.error(
      'Falta AI_GATEWAY_API_KEY. Ponla en maisa/.env o en un .env.local (los dos estan ignorados por git).',
    )
    process.exit(1)
  }

  let usado = modelo
  let salida
  try {
    salida = await pedir(modelo)
  } catch (err) {
    if (!esModeloFueraDelTierGratuito(err)) throw err
    console.log(`${modelo} no esta en el tier gratuito del Gateway; reintento con ${RESPALDO}.\n`)
    usado = RESPALDO
    salida = await pedir(RESPALDO)
  }

  console.log(`modelo:  ${salida.modelId}${usado !== modelo ? ` (pedido: ${modelo})` : ''}`)
  console.log(`tokens:  ${salida.usage.inputTokens} entrada / ${salida.usage.outputTokens} salida`)
  console.log('')
  console.log(salida.text)

  if (!salida.text.trim()) {
    console.error('')
    console.error('El Gateway respondio pero sin texto: la prueba NO es valida.')
    process.exit(1)
  }
}

main().catch((err) => {
  console.error('La prueba de humo fallo:', err instanceof Error ? err.message : err)
  process.exit(1)
})
