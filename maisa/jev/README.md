# jev · clasificación de páginas de documento

Clasifica **una página** de un documento en un tipo del registro (factura,
factura rectificativa, abono, albarán valorado, extracto bancario, nómina…)
usando **Jev**, el modelo System One de TypeSafe, a través del **AI SDK de
Vercel**.

Es un port a TypeScript del clasificador de
[`kyotofin/tax-doc-classifier`](https://github.com/kyotofin/tax-doc-classifier)
(261 formularios del IRS), adaptado al dominio de Maisa y montado como paquete
aparte. Del original se traen las ideas que funcionan —backend inyectable,
registro de criterios como datos, cascada por familias, puerta de confianza,
puntuación estricta y evaluación con coste— y se deja fuera lo que era
específico del IRS.

## Lo primero: el precio, y por qué hay dos vías

Jev es de pago, pero **ahora mismo se puede usar gratis** por una promoción:

- **Vía AI Gateway (recomendada hoy): gratis hasta el 25 de septiembre de
  2026.** Vercel tiene a Jev en promoción a coste cero en su
  [AI Gateway](https://vercel.com/ai-gateway/models/jev); la ficha del modelo lo
  pinta como `Free` y dice literalmente *"Promotional pricing ends on September
  25, 2026"*. Es una **fecha, no una condición**: el 26 de septiembre el precio
  vuelve solo, sin que cambie nada en el código. Se usa con
  `AI_GATEWAY_API_KEY`.
- **Vía TypeSafe directa: $0.042 por millón de tokens de entrada**, salida
  gratis (blog de TypeSafe, *Introducing System One Models & Jev*). No hay capa
  gratuita ni tarifa plana: se paga por lo que se lee. El acceso está en **early
  access, con lista de espera**, y la clave se saca de `console.typesafe.ai/keys`.
  Se usa con `TYPESAFE_AI_API_KEY`.

**Comprobado con una cuenta real (2026-09-19).** La promoción es de verdad y Jev
entra en el tier gratuito, pero hay dos pegas que no se leen en la ficha:

1. **La cuenta necesita una tarjeta en fichero.** Sin ella, el Gateway
   autentica la clave y contesta **HTTP 403** con *"AI Gateway requires a valid
   credit card on file to service requests"*. No cobra mientras dure la promo,
   pero no sirve nada sin tarjeta. Se añade en
   [la configuración del AI Gateway](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card).
2. **El tier gratuito solo abre un subconjunto de modelos.** Jev **sí** está
   (`typesafe-ai/jev` respondió y facturó $0), pero modelos grandes no: pedir
   `openai/gpt-5.5`, `openai/gpt-5.5-fast`, `anthropic/claude-haiku-4.5` o
   `deepseek/deepseek-v3.2` devuelve **HTTP 403** *"Free tier users do not have
   access to this model"*. Los que sí respondieron en la prueba: `openai/gpt-5-mini`,
   `openai/gpt-4.1-mini`, `openai/gpt-4o-mini`, `google/gemini-2.5-flash`,
   `google/gemini-2.5-flash-lite`, `meta/llama-3.3-70b`, `mistral/mistral-small`.

Por eso el paquete trae `npm run smoke` (`examples/gateway-smoke.ts`): verifica
clave, tarjeta y acceso a modelo en un segundo, sin tocar a Jev.

El *"gratis con el AI SDK"* que se suele citar es impreciso por dos motivos: el
AI SDK es un cliente y no fija tarifas, y lo que es gratis es la **promoción del
Gateway**, no el AI SDK. Lo que sí es cierto es que el AI SDK no cobra nada por
ser el cliente, y que **el Gateway no aplica margen** sobre las tarifas del
provider: tras la promoción, las dos vías cuestan lo mismo.

Consecuencias prácticas:

- El código no asume el precio en ningún sitio: `gatewayBackend()` y
  `jevBackend()` son intercambiables, y la evaluación recibe el precio por
  `--precio` (por defecto 0 con `--backend gateway` y $0.042 con `--backend jev`).
- **Si el paquete se usa para estimar coste, revisa la fecha.** El 25 de
  septiembre de 2026 el coste real pasa de 0 al precio de lista sin que cambie
  una línea.
- Sin saldo, la API directa contesta **HTTP 402** (el backend lo traduce a un
  mensaje que dice el precio y recuerda la alternativa gratis del Gateway).

Lo que no cambia con la promoción es que es rápido y barato: Jev resuelve una
página en 70–500 ms y una página de factura cuesta del orden de **$0.0001** al
precio de lista (ver `npm run eval`, que imprime el coste medido).

## Cómo decide

Una página se convierte en tres trozos de texto (`header` / `body` / `footer`)
y se le hacen **dos preguntas a la vez**, en la misma llamada:

| pregunta | opciones | para qué |
| --- | --- | --- |
| `kind` | 7 papeles (documento, continuación, anexo, blank, extracto bancario, nómina, carta u otros) | saber qué *es* la página antes de decidir qué hacer con ella |
| `tipo` | 10 opciones + `not_in_this_list` | qué documento es |

Si el tipo elegido es una **familia** (factura, abono, albarán, presupuesto,
extracto bancario) se hace una **segunda llamada** con las variantes de esa
familia. La cascada existe solo donde la página anuncia el padre más claro que
la variante: una rectificativa suele imprimir «FACTURA» bien grande y
«rectificativa» en un cuerpo de letra que el OCR se come.

El modelo **no devuelve texto**: devuelve una opción de una lista cerrada más
una distribución de probabilidad sobre esa lista. No hay nada que parsear y
nada que pueda salirse del registro.

Dos detalles que conviene conocer porque son deliberados:

- **`not_in_this_list` nunca es la respuesta.** Se le ofrece al modelo como
  destino legítimo para la masa de probabilidad que no corresponde a ninguna
  opción, pero el clasificador elige siempre la mejor opción *real*. Así el
  modelo puede decir «esto no es ninguna de estas» sin que el clasificador tenga
  que inventarse un tipo para ese caso. El precio es que una página que no está
  en el registro acaba pegada a la opción más parecida, con una confianza baja
  (y la confianza baja es justo la señal de «mírala a mano»).
- **La confianza va aparte de las probabilidades.** TypeSafe la manda en
  `providerMetadata.typesafe.confidence`; cuando no la manda, se usa la
  probabilidad de la opción elegida. Sin ese respaldo, un `undefined` se colaría
  como 0 y todo el lote saldría por debajo de la puerta.

Y la **puerta**: `gated = confianza >= 0.95`. Por encima se puede actuar sin
mirar; por debajo, la página entra en la cola de revisión. La confianza de un
resultado con cascada es la del **paso más débil**, no la del último.

## De dónde sale el texto

El paquete no decide cómo se obtiene el texto: acepta líneas ya extraídas y
ofrece la misma escalera que el motor de Maisa
(`motor/src/maisa/lectura.py`), de lo más barato a lo más caro:

1. **Caché del motor** (`motor/.cache/ocr/<sha256>.json`), por sha256 del PDF y
   no por nombre.
2. **Capa de texto del PDF** con `pdftotext -layout`.
3. **Servicio de OCR** de Maisa (`POST /ocr/text`, puerto 8866).

Se para en el primer peldaño que da texto y se devuelve también de qué peldaño
salió (`source`), porque el coste y la fiabilidad de la clasificación dependen
de ello.

El `state` no es el documento entero: son 12 líneas de cabecera, el cuerpo
recortado a 2.500 caracteres y 6 líneas de pie. En una factura el emisor, el
número y la fecha viven arriba, el NIF del cliente y las condiciones abajo, y
los importes en medio. En documentos de menos de 18 líneas **no se recorta el
pie**, porque ahí el total es justo el dato que distingue una factura de un
presupuesto.

Una página en blanco no gasta llamada: se responde sin consultar al modelo.

## Uso

```bash
cd maisa/jev
npm install

# 1. El registro de tipos documentales es válido y cabe en el modelo
npm run build-criterios

# 2. Tipos y tests (62 tests, sin red y sin clave)
npm run typecheck
npm test

# 3. Compilar
npm run build

# 4. Evaluar sobre el corpus real
npm run eval -- --filtro scan_                    # doble heurístico, offline, sin coste
npm run eval -- --backend gateway --limit 500     # Jev por el Gateway (gratis hasta el 25-sep-2026)
npm run eval -- --backend jev --limit 500         # Jev por TypeSafe directo ($0.042/MTok)
```

El comando de evaluación acepta `--dir`, `--filtro`, `--limit`, `--paginas`,
`--gate`, `--concurrency`, `--cache`, `--ocr`, `--engine`, `--golden`, `--json`,
`--precio`, `--backend` (`gateway`, `jev` o `fake`) y `--modelo` (por defecto
`typesafe-ai/jev` con el Gateway y `jev-latest` con TypeSafe).
Sin `--backend`, la vía se autodetecta por el entorno: `AI_GATEWAY_API_KEY`
primero, luego `TYPESAFE_AI_API_KEY`, y si no hay ninguna, el **doble
heurístico** que clasifica por palabras clave: sirve para comprobar la tubería,
medir tokens, latencia y cuántas páginas pasan la puerta, pero **no mide la
precisión de Jev** (el informe lo dice en su primera línea).

Uso desde código:

```ts
import { cargarCriterios, clasificarPagina, gatewayBackend } from 'maisa-jev'

const backend = gatewayBackend()                   // AI_GATEWAY_API_KEY, gratis hasta el 25-sep-2026
const criteria = cargarCriterios()
const r = await clasificarPagina(lines, { backend, criteria })

r.tipo              // 'factura-rectificativa'
r.tipoConfidence    // 0.55
r.gated             // false -> va a la cola de revisión
r.calls             // 2 (cascada)
```

`backend` es una interfaz de un solo método. Hay cuatro implementaciones:

| backend | clave | cuándo |
| --- | --- | --- |
| `gatewayBackend()` | `AI_GATEWAY_API_KEY` | la recomendada hoy: gratis hasta el 25-sep-2026, con trazas y presupuesto por equipo |
| `jevBackend()` | `TYPESAFE_AI_API_KEY` | TypeSafe directo, sin depender de Vercel; precio de lista |
| `httpBackend()` | `TYPESAFE_API_KEY` / `TYPESAFE_AI_API_KEY` | HTTP directo, el contrato de TypeSafe tal cual lo llamaba el clasificador original |
| `fakeBackend()` | ninguna | pruebas y evaluación sin red |

Por eso todo el paquete se verifica en CI sin clave y sin gastar dinero.

## Añadir un tipo documental

No se toca el código: se añade una entrada a `data/criterios.json`.

```json
"factura-recargo-equivalencia": {
  "id": "factura-recargo-equivalencia",
  "label": "Factura con recargo de equivalencia",
  "title": "Factura a minorista en recargo de equivalencia...",
  "parent": "factura",
  "boxes": ["RECARGO DE EQUIVALENCIA", "Recargo", "5,2 %"],
  "notFor": ["factura ordinaria"]
}
```

- `boxes` son cadenas que **aparecen impresas** en ese tipo de página. Son el
  ancla más fuerte: el modelo ve el texto del documento, no una descripción
  abstracta.
- `notFor` desambigua frente a la opción vecina, que es donde el modelo falla.
- `parent` mete la entrada en una cascada. Solo vale una familia de `FAMILIAS`;
  una familia con menos de dos miembros se rechaza al cargar, porque no merece
  una segunda llamada.

`npm run build-criterios` valida ids, etiquetas duplicadas, padres inexistentes,
familias sin variantes y el límite de 255 opciones del modelo. El registro se
valida **al cargar**, no a mitad de un lote de 500 facturas.

## Archivos

| archivo | qué es |
| --- | --- |
| `src/tipos.ts` | el contrato: `Backend`, `ChoiceQuestion`, `ChoiceAnswer`, `EstadoModelo` |
| `src/backend.ts` | `gatewayBackend` (AI Gateway), `jevBackend` (TypeSafe por el AI SDK), `httpBackend` (HTTP directo), `fakeBackend` (doble) |
| `src/criterios.ts` | el registro: `KINDS`, `FAMILIAS`, la primera lista y la lista de cada familia |
| `src/estado.ts` | la escalera de texto y el recorte cabecera/cuerpo/pie |
| `src/clasificar.ts` | la decisión de una página: dos preguntas, cascada, puerta |
| `src/puntuar.ts` | puntuación estricta y resumen de coste |
| `data/criterios.json` | 23 tipos documentales de Maisa |
| `eval/run.ts` | evaluación sobre `maisa/data/facturas/` |
| `tests/` | 62 tests, todos sin red |

## Qué está verificado y qué no

- **Verificado**: los tipos compilan; los 62 tests pasan; los caminos completos
  del AI SDK + provider de TypeSafe **y del AI Gateway** se recorren con un
  `fetch` inyectado que habla el contrato real de cada API
  (`tests/backend.test.ts`) — la URL `/evaluation-model`, las cabeceras
  `ai-model-id` y `ai-evaluation-model-specification-version`, la propagación de
  `providerMetadata.typesafe.confidence` por el Gateway, y los errores 401, 402
  y 429; el registro se valida; la escalera de texto funciona contra el OCR real
  de Maisa y contra la caché del motor; y la evaluación corre sobre las **26
  facturas ya escaneadas y cacheadas** del corpus.
- **No verificado**: no hay `AI_GATEWAY_API_KEY` ni `TYPESAFE_AI_API_KEY` en
  este entorno, así que **nunca se ha llamado a Jev de verdad desde este
  paquete**. La latencia, la precisión y el coste reales solo se pueden medir
  con clave: `npm run eval -- --backend gateway`.
- **Sin etiquetas**: `maisa/data/golden/` está vacío, así que la evaluación
  imprime la distribución de tipos, la cola de revisión y el coste, pero **no
  el acierto**. Para medirlo hace falta un jsonl con
  `{"file": "...", "page": 1, "tipo": "factura"}` pasado con `--golden`.

## Qué se trae del original y qué no

Se trae: la arquitectura (backend inyectable, criterios como datos, cascada,
puerta, scorer estricto, evaluación con coste), la idea de que el modelo elige
de una lista cerrada en vez de escribir texto, y el manejo del 402.

No se trae: los 261 formularios del IRS, ni el `pdftotext` como única fuente de
texto (aquí hay una escalera con la caché y el OCR de Maisa), ni el backend por
`fetch` como camino principal (aquí el camino principal es el AI SDK —por el
Gateway o directo— y el `fetch` se conserva como alternativa).
