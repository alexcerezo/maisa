/**
 * El registro de tipos documentales y su traduccion a opciones del modelo.
 *
 * El clasificador no sabe nada de facturas: sabe elegir de una lista. Todo el
 * conocimiento del dominio vive en `data/criterios.json`, que es un fichero de
 * datos y no de codigo. Anadir un tipo documental nuevo no toca este modulo.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Criterion } from './tipos.js'

/** Una entrada del registro: como se llama y con que se reconoce. */
export type CriteriaEntry = {
  id: string
  /** Nombre corto que se le muestra al modelo como clave de la opcion. */
  label: string
  /** Descripcion larga: que es este documento. */
  title: string
  /** Familia a la que pertenece, si es una variante. Ver `FAMILIAS`. */
  parent?: string
  /** Cadenas que aparecen impresas en este tipo de pagina. El ancla del modelo. */
  boxes?: string[]
  /** Opciones vecinas con las que se confunde. La desambiguacion. */
  notFor?: string[]
}

export type Criteria = Record<string, CriteriaEntry>

/**
 * Que papel juega la pagina dentro del documento.
 *
 * Va separado del tipo documental porque son dos preguntas independientes: una
 * pagina de "condiciones generales" pertenece a una factura, y una pagina de
 * "continuacion" pertenece a un albaran. Mezclarlas en una sola pregunta obliga
 * al modelo a elegir entre dos ejes que no compiten.
 */
export const KINDS = [
  'documento',
  'continuacion',
  'anexo',
  'blank',
  'extracto_bancario',
  'nomina',
  'carta_o_otros',
] as const
export type Kind = (typeof KINDS)[number]

export const KIND_CRITERIA: Record<Kind, Criterion> = {
  documento: {
    what: 'Pagina con datos de facturacion: emisor y/o cliente identificados, numero de documento, lineas con importes y totales',
    examples: [
      'FACTURA Nº 2026/22608',
      'Base imponible',
      'Total factura',
      'NIF: B90233410',
      'Forma de pago',
      'Fecha de expedicion',
    ],
  },
  continuacion: {
    what: 'Pagina de continuacion del mismo documento: solo lineas de detalle, subtotales o firmas, sin cabecera con emisor y numero',
    examples: ['(continuacion)', 'Suma y sigue', 'Total paginas', 'Viene de la pagina anterior'],
  },
  anexo: {
    what: 'Pagina sin datos de facturacion: condiciones generales, letra pequena, datos bancarios o informacion legal',
    examples: ['Condiciones generales', 'Datos bancarios', 'IBAN', 'Proteccion de datos', 'Clausulas'],
  },
  blank: { what: 'Pagina en blanco o que dice estarlo; sin texto util' },
  extracto_bancario: {
    what: 'Extracto, apunte o justificante de una entidad financiera: saldos, movimientos, concepto y fecha valor',
    examples: ['Extracto de cuenta', 'Saldo anterior', 'Fecha valor', 'Movimientos', 'Oficina'],
  },
  nomina: {
    what: 'Nomina o recibo de salario: devengos, deducciones y bases de cotizacion',
    examples: ['Nomina', 'Devengos', 'Deducciones', 'Base de contingencias', 'Liquido a percibir'],
  },
  carta_o_otros: {
    what: 'Cualquier otra pagina: carta, comunicacion, burofax, nota manuscrita, presupuesto de puno y letra',
  },
}

/**
 * Las familias que se preguntan en dos pasos.
 *
 * La regla es la del clasificador original: la cascada existe solo donde la
 * pagina **anuncia el padre mas claro que la variante**. Una factura rectificativa
 * suele imprimir "FACTURA" bien grande y "rectificativa" en un cuerpo de letra
 * que el OCR se come; preguntar primero la familia y despues la variante, con
 * una lista corta y descripciones que se excluyen entre si, sale mejor que una
 * lista unica de 23 opciones donde las seis facturas compiten entre ellas.
 *
 * Todo lo que no esta aqui se pregunta de una vez.
 */
export const FAMILIAS = ['factura', 'abono', 'albaran', 'presupuesto', 'extracto-bancario'] as const

/** La opcion de escape: el documento existe pero no es ninguno del registro. */
export const NOT_IN_LIST = 'not_in_this_list'

export function criterioDe(e: CriteriaEntry): Criterion {
  const c: Criterion = { what: e.title ? `${e.label} — ${e.title}` : e.label }
  if (e.boxes?.length) c.examples = e.boxes
  if (e.notFor?.length) c.not_for = `No es ${e.notFor.join(', ')}`
  return c
}

/** El id de la familia por la que se pregunta a este documento, o el suyo. */
export function familiaDe(id: string, criteria: Criteria): string {
  const parent = criteria[id]?.parent
  if (parent && (FAMILIAS as readonly string[]).includes(parent)) return parent
  return id
}

/**
 * La primera pregunta: un representante por familia mas todos los sueltos.
 *
 * Los hijos de una familia no aparecen aqui; se resuelven en la segunda
 * pregunta, y solo si el modelo eligio esa familia.
 */
export function listaPrimera(criteria: Criteria): Record<string, Criterion | null> {
  const out: Record<string, Criterion | null> = {}
  for (const id of Object.keys(criteria).sort()) {
    if (familiaDe(id, criteria) !== id) continue
    out[id] = criterioDe(criteria[id])
  }
  out[NOT_IN_LIST] = {
    what: 'La pagina pertenece a un documento que NO es ninguna de las opciones anteriores',
  }
  return out
}

/** Los miembros de una familia, incluida ella misma si es un tipo en si. */
export function miembrosDe(parent: string, criteria: Criteria): string[] {
  return Object.keys(criteria)
    .filter((id) => id === parent || criteria[id].parent === parent)
    .sort()
}

/** Base de las opciones del segundo paso: los miembros de la familia. */
export function listaDeFamilia(parent: string, criteria: Criteria): Record<string, Criterion | null> {
  const out: Record<string, Criterion | null> = {}
  for (const id of miembrosDe(parent, criteria)) out[id] = criterioDe(criteria[id])
  out[NOT_IN_LIST] = { what: 'La pagina pertenece a un documento que NO es ninguna de las opciones anteriores' }
  return out
}

export function rutaCriteriosPorDefecto(): string {
  return join(dirname(fileURLToPath(import.meta.url)), '..', 'data', 'criterios.json')
}

/**
 * Comprueba el registro antes de usarlo.
 *
 * Se llama al cargar y en el generador: un registro mal formado no debe
 * descubrirse a mitad de un lote de 500 facturas, sino al arrancar.
 */
export function validarCriterios(criteria: Criteria): void {
  const ids = Object.keys(criteria)
  if (ids.length === 0) throw new Error('criterios.json esta vacio')

  const etiquetas = new Set<string>()
  for (const id of ids) {
    const e = criteria[id]
    if (e.id !== id) throw new Error(`criterios.json: la entrada "${id}" declara id "${e.id}"`)
    if (!e.label) throw new Error(`criterios.json: la entrada "${id}" no tiene label`)
    if (!e.title) throw new Error(`criterios.json: la entrada "${id}" no tiene title`)
    if (etiquetas.has(e.label)) throw new Error(`criterios.json: label duplicada "${e.label}"`)
    etiquetas.add(e.label)
    if (e.parent !== undefined) {
      if (!criteria[e.parent]) throw new Error(`criterios.json: "${id}" apunta a un padre inexistente "${e.parent}"`)
      if (!(FAMILIAS as readonly string[]).includes(e.parent)) {
        throw new Error(`criterios.json: "${id}" apunta a "${e.parent}", que no esta en FAMILIAS`)
      }
    }
  }

  for (const familia of FAMILIAS) {
    if (!criteria[familia]) throw new Error(`criterios.json: falta la entrada de la familia "${familia}"`)
    if (miembrosDe(familia, criteria).length < 2) {
      throw new Error(`criterios.json: la familia "${familia}" no tiene variantes; no merece cascada`)
    }
  }

  const opciones = Object.keys(listaPrimera(criteria)).length
  if (opciones > 255) throw new Error(`criterios.json: la primera lista tiene ${opciones} opciones (maximo 255)`)
}

export function cargarCriterios(ruta: string = rutaCriteriosPorDefecto()): Criteria {
  const criteria = JSON.parse(readFileSync(ruta, 'utf8')) as Criteria
  validarCriterios(criteria)
  return criteria
}
