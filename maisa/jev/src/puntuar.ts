/**
 * La puntuacion, en la definicion estricta del clasificador original.
 *
 * Una pagina cuenta como error si la respuesta es incorrecta **o** si su
 * confianza no llega a la puerta. Es la definicion que importa en un lote: una
 * respuesta acertada con confianza 0.6 obliga a que alguien la revise, y eso
 * cuesta lo mismo que una respuesta equivocada.
 */
import type { PageResult } from './clasificar.js'

export type Scored = { truth: string; result: PageResult; wrong: boolean; strict: boolean }

export function puntuar(truth: string, result: PageResult, gate = 0.95): Scored {
  const wrong = truth !== result.tipo
  return { truth, result, wrong, strict: wrong || result.tipoConfidence < gate }
}

export type Summary = {
  pages: number
  wrong: number
  strict: number
  strictRate: number
  calls: number
  inputTokens: number
  estCostUsd: number
}

/**
 * `usdPorMillonTokens` es el precio publicado de Jev: **$0.042 por millon de
 * tokens de entrada, salida gratis**. No hay tarifa plana: el coste es
 * proporcional a lo que se lee, y aun asi una pagina sale por una milesima de
 * centimo porque el `state` son ~600 tokens, no un PDF.
 *
 * Ojo: el precio es un parametro y no una constante a proposito. El AI Gateway
 * de Vercel tiene a Jev en promocion a 0 hasta el 25 de septiembre de 2026, y
 * ese dia vuelve al precio de lista sin que cambie nada en el codigo.
 */
export function resumir(rows: Scored[], usdPorMillonTokens = 0.042): Summary {
  const pages = rows.length
  const wrong = rows.filter((r) => r.wrong).length
  const strict = rows.filter((r) => r.strict).length
  const calls = rows.reduce((n, r) => n + r.result.calls, 0)
  const inputTokens = rows.reduce((n, r) => n + r.result.inputTokens, 0)
  return {
    pages,
    wrong,
    strict,
    strictRate: pages ? strict / pages : 0,
    calls,
    inputTokens,
    estCostUsd: (inputTokens * usdPorMillonTokens) / 1e6,
  }
}
