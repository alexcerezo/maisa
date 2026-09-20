# Rúbrica del tribunal — 100 + 10 puntos

Documento de trabajo. La primera parte es **la rúbrica tal cual la dio el tribunal** (no se
reescribe ni se resume: es el criterio). La segunda es nuestro **estado real** frente a cada
criterio, con la evidencia y lo que falta. Se actualiza según se avanza; si algo no está
hecho, aquí dice que no está hecho.

---

## 0. Validación funcional (APTO / NO APTO) — decide la elegibilidad

**Un registro único por archivo · ambos lotes · resultado aceptado.**

- Solo se validan `file_id` y `result` del entregable.
- No suma puntos, no genera ranking ni desempata.
- Si no somos aptos, se puede defender el proyecto y recibir feedback, pero **no se opta al premio**.
- El PDF se evalúa en *Producto, arquitectura y ADRs*; no forma parte de esta validación.

| Requisito | Estado | Evidencia |
|---|---|---|
| Un registro por archivo | ✅ | 540 registros, uno por `file_id` |
| Ambos lotes | ✅ | 500 (lote 1) + 40 (lote 2) |
| Resultado aceptado | ✅ | `outcomes.jsonl` + `outcomes_lote2.jsonl` |

---

## 1. Producto, arquitectura y ADRs — 35 pts

> Defended el problema, el formato, la arquitectura y las decisiones, alternativas y
> trade-offs recogidos en `albertitos_plan.pdf`.

| Evidencia | Dónde |
|---|---|
| Plan con problema, formato, arquitectura y trade-offs | `albertitos_plan.pdf`, `maisa/albertitos_plan.md`, `maisa/motor/docs/albertitos_plan.md` |
| Diseño conceptual y lógico | `maisa/diseño_conceptual.md`, `maisa/diseño_logico.md` |
| ADR | `maisa/docs/ADR-0001-middleware-bff.md` |
| Spec y plan | `maisa/spec_y_plan.md` |
| Entrega | `maisa/docs/ENTREGA.md` |

**Pendiente:** ADRs numerados para las decisiones que hoy viven dentro del plan (formato de
salida, elección de motor, segunda lectura, multi-lote) y una tabla de alternativas
descartadas con su trade-off. Un ADR por decisión, corto.

---

## 2. Trazabilidad y observabilidad — 20 pts

> Seguid una decisión real y mostrad estado, evidencia, versiones, latencia, errores,
> reintentos y trabajo pendiente.

| Verbo de la rúbrica | Dónde se responde | Estado |
|---|---|---|
| Estado | Columna de resultado y de cola en el panel; `result` en la entrega | ✅ |
| Evidencia | Panel de hechos + PDF + `anclajes` (dónde está cada dato en el PDF) + `campos` de la segunda lectura | ✅ |
| Versiones | `version_norma` y `sha256` por factura en el panel; `hash`/`hash_prev` encadenados y `sello_previo` del evento `fin` | ✅ |
| Latencia | Por factura en el panel (`latencia()`: `10 ms`, `1,2 s`) y percentiles por escalón en el evento `fin` | ✅ |
| Errores | `motivos` y severidades; fallos del ERP documentados | ✅ |
| **Reintentos** | `erp_retry_transient`, `erp_rate_limit_backoff`, `SES-401 → login` | ⚠️ solo en doc, no en el panel |
| Trabajo pendiente | `pendientes_revision`, `cola_segunda_lectura`, cola de segunda lectura filtrable | ✅ |

| Fuente | Dónde |
|---|---|
| Traza encadenada por hash | `maisa/outputs/outcomes_lote2_traza_hash.jsonl` |
| Recorrido narrado de una decisión | `maisa/traces/trazabilidad.md` |
| Panel | `maisa/ui` (`/facturas`, `/facturas/:fileId`) |
| Arnés que mata el lote y comprueba la cadena | `maisa/motor/tools/evidencia_resiliencia.py` |

**Pendiente:** llevar los **reintentos** al panel. Es lo único de la rúbrica que solo existe en un
markdown. Pero no se puede pintar desde los datos: el corpus no trae ni un evento de reintento (el
lote 2 corrió sin ERP y el lote 1 no tuvo incidencias transitorias), así que un contador de
reintentos en el panel habría que **inventarlo**. Lo honesto es defenderlo con el arnés, que sí lo
provoca de verdad: `evidencia_resiliencia.py` mata el pipeline a mitad, comprueba que la cadena de
hash no se rompe y que se puede continuar encima sin reescribir nada.

**Cerrado en este tramo** (lo que la rúbrica pedía y no se veía):

- La **segunda lectura** existía en los datos y no se veía en ninguna parte: 9 de las 63 escaladas
  la traen (4 confirmables, 4 desvíos, 1 sin conclusión). Ahora es una columna, un panel en el
  expediente y un contador de trabajo pendiente, con filtro propio.
- La **latencia** se pintaba como `0,0 s` en 302 de las 540 facturas —la mediana está en 43 ms—,
  o sea que el dato que la rúbrica pide expresamente era ilegible. Ahora `4 ms` contra `299 ms`
  distingue una lectura de capa de texto de una de OCR.

---

## 3. Escalabilidad y coste — 25 pts

> Aportad capacidad, hardware, límites, fórmula de coste y un plan para incorporar más
> volumen y nuevos tipos de archivo.

| Evidencia | Dónde |
|---|---|
| Capacidad medida (500 por nº de trabajadores, desglose por fase) | `maisa/motor/docs/capacidad.md` §1–§4 |
| Coste unitario por factura, escalón a escalón | §5 |
| Extrapolación a 5.000 / 50.000 / 1.000.000 | §6, escenarios A/B/C |
| Plan a 10x y 100x | §7 |
| Supuestos y lo que **no** se ha medido | §9, §10 |
| Simulador | `maisa/motor/docs/simulador.md` |

**Pendiente:** el "plan para nuevos tipos de archivo" está implícito en el diseño pero no
escrito como plan. Falta cerrarlo explícitamente.

---

## 4. Resiliencia y recuperación — 10 pts

> Explicad cómo conserváis el estado, evitáis duplicados, degradáis el servicio y recuperáis
> el trabajo ante fallos del proveedor de LLM.

| Escenario | Dónde |
|---|---|
| Mapa de escenarios | `maisa/motor/docs/resiliencia.md` §0 |
| Muerte a mitad del lote (SIGKILL) y reanudación | §1 |
| Traza manipulada | §2 |
| ERP: `ORA-00600`, `ERP-429`, caducidad de sesión, caída total | §3.1–§3.4 |
| PDF corrupto | §4 |
| Caché de OCR: corrupción, caída, pérdida | §5.1–§5.3 |
| Límites honestos (lo que **no** está cubierto) | §6 |
| Guion de 90 s | §7 |
| Procedencia de las medidas | Anexo |

**Pendiente:** el escenario "fallo del proveedor de LLM" en el sentido literal (proveedor
externo de modelo) no es el que cubre el doc: aquí el "proveedor" es el ERP y el OCR. Hay
que decirlo así en la defensa y no dejar que el tribunal lo descubra.

---

## 5. Calidad de ejecución — 10 pts

> Una solución clara, proporcionada y agradable de operar, con decisiones útiles para Alberto.

| Evidencia | Dónde |
|---|---|
| Panel de conciliación sobre shadcn | `maisa/ui` |
| Listado con filtros, paginación y aviso de fuente | `/facturas` |
| Expediente: hechos, documento, segunda lectura | `/facturas/:fileId` |
| Dos fuentes (API viva o congelado) | `public/config.json`, `?fuente=congelado` |

---

## 6. Bonus: mejora adicional para Alberto — hasta +10

> Hasta 10 puntos por resolver una necesidad concreta con una mejora **original, implementada
> y mostrada** en la defensa. No cuentan propuestas, maquetas, cambios cosméticos ni partes
> necesarias del flujo principal. Es opcional y distinto del lote adicional del sábado.

**Candidata:** el hilo de la **segunda lectura** — decir, para cada escalada, si se puede
cerrar sin abrir el PDF, si hay un desvío que exige persona, o si la relectura no concluyó.
Es una decisión de trabajo, no cosmética, y ataca la necesidad concreta de Alberto: 63
escaladas y no todas cuestan lo mismo.

**Riesgo a declarar:** hay que argumentar por qué no es "parte del flujo principal". El
argumento es que **no cambia ninguna decisión del motor** (una escalada confirmable sigue
siendo `ESCALAR` en la entrega): cambia cuánto trabajo humano queda, que es información que
hoy no está en ningún sitio.

---

## Desempates (en este orden)

1. **Escalabilidad y coste**
2. **Resiliencia**
3. **Bonus de mejora adicional**
4. **Decisión motivada del tribunal**

Consecuencia práctica: si hay que elegir dónde invertir el último tramo, el orden es
*Escalabilidad* → *Resiliencia* → *Bonus*. Producto (35) no desempata porque no es un
desempate: es el criterio que más pesa en la nota base y se defiende con el PDF.
