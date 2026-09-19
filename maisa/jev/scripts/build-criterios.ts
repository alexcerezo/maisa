/**
 * Valida el registro de criterios y lo describe.
 *
 * No genera el fichero: `data/criterios.json` se escribe a mano porque es
 * conocimiento del dominio, no un artefacto de compilacion. Este script existe
 * para que ese fichero a mano no pueda romper el clasificador en silencio.
 *
 *   npm run build-criterios          # valida y describe
 *   npm run build-criterios -- --json  # volcado de las listas tal como las ve el modelo
 */
import { FAMILIAS, cargarCriterios, listaDeFamilia, listaPrimera, rutaCriteriosPorDefecto } from '../src/criterios.js'

const ruta = process.argv.includes('--ruta')
  ? process.argv[process.argv.indexOf('--ruta') + 1]
  : rutaCriteriosPorDefecto()

const criteria = cargarCriterios(ruta)
const primera = listaPrimera(criteria)

if (process.argv.includes('--json')) {
  const listas: Record<string, unknown> = { primera }
  for (const familia of FAMILIAS) listas[familia] = listaDeFamilia(familia, criteria)
  console.log(JSON.stringify(listas, null, 2))
} else {
  console.log(`criterios: ${ruta}`)
  console.log(`tipos documentales: ${Object.keys(criteria).length}`)
  console.log(`opciones en la primera pregunta: ${Object.keys(primera).length}`)
  console.log('')
  console.log('cascadas:')
  for (const familia of FAMILIAS) {
    const miembros = Object.keys(listaDeFamilia(familia, criteria)).filter((id) => id !== 'not_in_this_list')
    console.log(`  ${familia.padEnd(20)} ${miembros.length} opciones: ${miembros.join(', ')}`)
  }
  const sueltos = Object.keys(primera).filter(
    (id) => id !== 'not_in_this_list' && !(FAMILIAS as readonly string[]).includes(id),
  )
  console.log('')
  console.log(`sueltos (se preguntan de una vez): ${sueltos.join(', ')}`)
  console.log('')
  console.log('OK: el registro es valido')
}
