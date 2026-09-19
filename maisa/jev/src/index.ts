export type {
  AskResult,
  Backend,
  ChoiceAnswer,
  ChoiceQuestion,
  Criterion,
  EstadoModelo,
} from './tipos.js'

export {
  buildState,
  isBlank,
  lineasDeCacheOcr,
  lineasDeDocumento,
  lineasDeOcr,
  lineasDePaginaOcr,
  lineasDePdf,
  linesOf,
  paginasDePayloadOcr,
  sha256DeFichero,
} from './estado.js'
export type { FuenteTexto, OpcionesTexto, PageState } from './estado.js'

export {
  FAMILIAS,
  KINDS,
  KIND_CRITERIA,
  NOT_IN_LIST,
  cargarCriterios,
  criterioDe,
  familiaDe,
  listaDeFamilia,
  listaPrimera,
  miembrosDe,
  rutaCriteriosPorDefecto,
  validarCriterios,
} from './criterios.js'
export type { Criteria, CriteriaEntry, Kind } from './criterios.js'

export { fakeBackend, gatewayBackend, httpBackend, jevBackend } from './backend.js'
export type { GatewayOptions, JevOptions, RespuestaFake } from './backend.js'

export { clasificarPagina } from './clasificar.js'
export type { ClassifyOptions, PageResult } from './clasificar.js'

export { puntuar, resumir } from './puntuar.js'
export type { Scored, Summary } from './puntuar.js'
