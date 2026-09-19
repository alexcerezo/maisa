# Albertitos · Plan de Entrega

**Equipo (teamId):** YEM9Q8TP
**Repositorio GitHub:** https://github.com/alexcerezo/maisa
**Fecha límite:** Domingo 20, 10:30 (hora de Madrid)

---

## 0. Entregables y contrato

Tres ficheros sueltos en la raíz del repositorio público, junto a la carpeta
`maisa/` (motor, datos y docs) y `.github/` (workflows):

```
outcomes.jsonl          # 500 facturas del lote 1
outcomes_lote2.jsonl    # lote adicional del sábado
albertitos_plan.pdf     # este documento
maisa/                  # motor, datos, docs y ui (no es entregable)
```

La spec pide un repositorio separado con *"exactamente estos tres archivos"* en la
raíz. Mantenemos los tres ficheros sueltos en la raíz, que es la lectura literal,
y convivimos con `maisa/` y `.github/` en el mismo repositorio por decisión
explícita del equipo: la CI tiene que estar en `.github/workflows/` para correr.
Es una desviación consciente y la asumimos en la defensa.

**Contrato JSONL** (una línea por factura, UTF-8 sin BOM, saltos LF):

```json
{"file_id":"factura_5518.pdf","result":"PAGAR"}
```

Estados permitidos: `PAGAR`, `NO_PAGAR`, `ESCALAR`. `file_id` es el nombre
exacto del PDF, con extensión y mayúsculas incluidas. Los campos de traza son
opcionales; **los omitimos en la entrega** para minimizar el riesgo de que un
parser estricto se atragante. La traza completa se genera aparte, para el pitch.

**Resultado del lote 1:** 500/500 líneas, un outcome por factura, sin
duplicados. Distribución: **448 PAGAR · 43 ESCALAR · 9 NO_PAGAR**.

---

## 1. Semántica de los estados (definición canónica)

- **PAGAR** → la factura casa con un asiento del ERP en estado `PENDIENTE`:
  pedido existente, del proveedor correcto, importes conciliados, IVA coherente
  y fecha válida. **Procede pagarla.**
- **NO_PAGAR** → **hecho duro**: el asiento ya está `PAGADA` en el ERP, o hay
  duplicidad explícita. **No volver a pagar.** Es la única etiqueta que no
  admite matices: exige una prueba documental, no una sospecha.
- **ESCALAR** → duda razonable. Intervención humana obligatoria: la factura no
  consta en el ERP, los importes no cuadran, el IBAN de abono no es el del
  proveedor, falta un identificador crítico o la lectura es de baja calidad.

**Política ante la duda: conservadora.** En empate, `ESCALAR`, nunca `PAGAR`.
El coste de una revisión manual es asumible; el de un pago erróneo, no.

> **Precedencia formal:** `NO_PAGAR > ESCALAR > PAGAR`. Los hechos duros ganan
> siempre; ante duda razonable, se escala antes que pagar. La precedencia está
> declarada en `config/reglas.toml`, no dispersa en el código.

---

## 2. Arquitectura

### 2.1 Flujo de datos

Tres fuentes que se contradicen: **PDFs** de facturas, un **Excel caótico** y
un **ERP de 2009** con un bridge HTTP que habla XML en ISO-8859-1.

```
   PDF ──▶ 1. Lectura en escalera ──▶ 2. Extracción ──▶ 3. Normalización
             (capa de texto | OCR)        de campos        determinista
                    │                          │                │
                    │                          ▼                ▼
                    │                    4. Concilia con ERP (fuente de verdad)
                    │                       y Excel (contexto)
                    │                          │
                    ▼                          ▼
               caché sha256            5. Motor de reglas (norma v3)
              (no se paga OCR            ├── hecho duro ──▶ NO_PAGAR
               dos veces)                ├── todo casa  ──▶ PAGAR
                                         └── duda       ──▶ ESCALAR + motivo
                                              │
                                              ▼
                                    6. Emisión + validación del JSONL
```

**El paso 1 es la decisión de coste del sistema.** No mandamos las 500 facturas
a OCR: intentamos primero la **capa de texto** del PDF (gratis, instantánea,
exacta) y **solo escalamos a OCR las que no la traen o la traen degradada**.
Resultado medido: **471 facturas (94,2%) se resuelven sin OCR** y solo **29
(5,8%)** necesitan visión. El OCR se cachea por `sha256` del PDF, así que
reejecutar es gratis.

### 2.2 Reparto entre modelos, agentes y personas

| Actor | Responsabilidad | Qué **NO** hace |
|---|---|---|
| **OCR (modelo)** | Traducir píxeles a texto. Solo se invoca para el 5,8% de la Caja. | No extrae campos, no normaliza, no decide. |
| **Extractor + normalizadores (deterministas)** | Sacar NIF, pedido, fecha, base, IVA, total y convertirlos a tipos canónicos. | No adivina: si no puede convertir, devuelve `None` y eso es una señal. |
| **Motor de reglas (determinista)** | Aplicar la norma v3 y asignar `PAGAR`/`NO_PAGAR`/`ESCALAR`. | No inventa datos que faltan. Nunca usa un LLM para la etiqueta final. |
| **Persona (operador)** | Revisar los 43 `ESCALAR` con el PDF y la traza a la vista. | No revisa lo que el motor ya cerró con hecho duro. |

**Cómo trabajó el equipo.** El desarrollo se repartió entre personas y agentes
de IA en paralelo: agentes de construcción para trazabilidad, pruebas y
validación de entrega, y revisión adversarial del propio motor con una batería
de casos límite. El **motor de decisión**, en cambio, es determinista y
auditable línea a línea: ninguna etiqueta de la entrega sale de un modelo de
lenguaje. Es una decisión deliberada, y es la que permite defender un descuadre
contable ante Finanzas.

### 2.3 Reglas de decisión (la norma v3)

El motor recibe la factura leída, el asiento del ERP y el proveedor del maestro,
y evalúa en orden:

| # | Condición | Resultado |
|---|---|---|
| R1 | NIF del emisor en el maestro **y** IBAN de abono coincidente | — (prerequisito) |
| R2 | El pedido existe en el ERP, es del proveedor e importes conciliados (tol. 0,01 €) | PAGAR |
| R3 | IVA bien calculado: `total = base + IVA` (tol. 0,01 €) | PAGAR |
| R4 | Fecha **existente en el calendario** y no futura respecto a `hoy` | PAGAR |
| R5 | Estado del asiento en el ERP es `PENDIENTE`; **nunca pagar dos veces** | PAGAR / NO_PAGAR |
| R6 | Cualquier anomalía que un humano deba ver | ESCALAR **con motivo** |

Cada regla deja un `Hecho` con su nombre, su resultado y los datos que lo
sustentan. La decisión final es la del hecho más grave según la precedencia.

**Cobertura medida de cada regla sobre el lote 1** (`tools/oro.py --cobertura`,
sobre los 500 hechos registrados en la traza):

| Regla | Facturas que la pasan | Facturas que la suspenden | De ellas, hechos duros | Informativas |
|---|---:|---:|---:|---:|
| `R1_identidad` | 480 | 15 | 0 | 2 |
| `R2_pedido` | 484 | 16 | 0 | 0 |
| `R3_iva` | 486 | 2 | 0 | 9 |
| `R4_fecha` | 489 | 5 | 0 | 3 |
| `R5_estado` | 488 | 9 | **9** | 0 |
| `R6_anomalia` | 0 | 14 | 0 | 20 |

(Los conteos son **por factura**: `R1_identidad` emite dos hechos por factura
—NIF e IBAN— y contarlos sueltos inflaría la tabla. Las 20 informativas de
`R6` son las notas dirigidas al sistema, que se registran sin decidir.)

Solo `R5_estado` produce hechos duros, y produce exactamente **9**: los mismos 9
asientos que el ERP marca `PAGADA`. Es la comprobación de que la única regla que
puede emitir `NO_PAGAR` es la que exige prueba documental.

**La regla 6 tiene nombre y apellidos.** "Cualquier anomalía que un humano deba
ver" era la puerta por la que cabía todo, así que la norma v3.1 la descompone en
cuatro anomalías **nombradas**, cada una con su clave en `config/reglas.toml` y
su motivo en la traza:

| Anomalía | Qué la dispara | Resultado |
|---|---|---|
| `si_pedido_repetido` | El mismo pedido aparece en dos facturas del lote | ESCALAR **las dos** |
| `si_instruccion` | El documento dicta el resultado del flujo de decisión | ESCALAR |
| `si_pendiente_revision` | El pedido figura en la hoja `pendiente_revisar` del maestro | ESCALAR |
| `si_documento_no_legible` | Escaneo del que no se lee el IBAN de abono | ESCALAR |

Ninguna baja a `PAGAR`: son anomalías, no defectos formales. Y ninguna es una
regla nueva de negocio; son formas concretas del punto 6 de la norma que antes
se trataban en bloque y ahora se pueden discutir **una a una** con Finanzas sin
tocar el motor. El caso del pedido repetido merece mención: pagar las dos
facturas es pagar dos veces, y elegir una es decidir por el humano, así que
escalan las dos y la decisión queda escrita en las dos trazas.

**Un detalle que casi nos cuesta una factura.** Una fecha como `31/02/2026` no
es "una fecha ilegible": es una fecha **impresa y imposible**. Nuestro
normalizador las descartaba a propósito, así que la traza decía "fecha no
legible" y el motivo se confundía con el de un OCR que no ha leído nada. Ahora
se distinguen los dos casos y la traza dice `fecha invalida: 31/02/2026`. No
cambia ninguna etiqueta —ambas son `ESCALAR`—, pero cambia lo que el revisor
humano lee, y eso es exactamente lo que se le pide a una traza. Son 3 facturas
(`2026-03-19_P008.pdf`, `FA-1123_construcciones.pdf`, `FA-2967_seguridad.pdf`).
La primera no lleva ninguna nota: **prueba que el defecto es la fecha y no el
texto que la acompaña**.

**La norma vive en datos, no en código.** Umbrales, precedencia y hechos duros
están en `config/reglas.toml`:

```toml
hoy = "2026-09-19"                 # fecha de corte, inyectable

[precedencia]
PAGAR = 0
ESCALAR = 1
NO_PAGAR = 2

[hechos_duros]                     # lo único que produce NO_PAGAR
pago_duplicado    = "NO_PAGAR"
factura_duplicada = "NO_PAGAR"

tolerancia_importe = 0.01
```

Esto es lo que hace que **la regla nueva del sábado sea un cambio de datos, no
de software**: se añade una entrada al TOML, se reejecuta y ya está. El motor
no se recompila ni se redespliega.

### 2.4 Trazabilidad y observabilidad

Por cada factura emitimos **dos eventos** en la traza: uno de *lectura* (cómo se
leyó) y uno de *decisión* (qué se decidió y con qué hechos). Entre los dos
llevan: `sha256` del PDF, escalón de lectura usado (`capa_texto` / `ocr`), si
vino de caché, calidad de la lectura, segundos de lectura, pedido y asiento
conciliados, importes antes y después de reparar, candidatos de NIF, IBAN y
fecha, identidad heredada, los **hechos de cada regla** (`R1..R6` con su
resultado y sus datos), sospechosos de inyección en el cuerpo y en los
metadatos, la nota del documento y el motivo textual de la decisión.

El lote 1 produce **1002 eventos** (500 lecturas + 500 decisiones + apertura de
lote + cierre) y pesa ~1,5 MB.

**La traza es una cadena, no un log.** Cada evento lleva `hash_prev` (el hash
del evento anterior) y su propio `hash`, calculado sobre la forma canónica del
evento **incluido** `hash_prev`. Cambiar un solo campo de una sola decisión
rompe el eslabón y todo lo que cuelga de él. El verificador `trace.verifica()`
relee el fichero y reporta `cadena_rota`, `seq_discontinuo`, `json_invalido`,
`linea_vacia` o `sello_distinto` **con el número de línea exacto**, y **no
revienta** si una línea está corrupta: la salta, la reporta y sigue. La cabeza
de la cadena se publica como **sello** (`trace.sello()`), de modo que se puede
citar un hash corto en el pitch y demostrar en vivo que la traza no se ha tocado
después.

```
traza hash : 1002 eventos, OK
sello      : b77131772ad17b38c459d0ceafefa3740bbebe9b424c66b64d608e3e89ae21b7
```

**Seguir una decisión de principio a fin** es una consulta, no una arqueología:
se filtra la traza por `file_id` y aparecen la lectura y la decisión con sus
hechos. Es el ejercicio que pide el tribunal ("seguid una decisión real") y está
resuelto con `grep` sobre el JSONL, sin herramienta propietaria.

**Señales operativas** que emitimos por lote: reparto de resultados, reparto de
escalones de lectura (cuántas por capa de texto y cuántas por OCR), tiempo
total, sello de la traza y resultado de la validación del árbol de entrega. Si
el reparto de escalones se desvía del histórico (por ejemplo, el OCR empieza a
resolver menos facturas), eso es la señal temprana de que algo ha cambiado en la
entrada, **antes** de que se note en las etiquetas.

**Un log truncado, una línea malformada o un PDF ilegible producen un informe, no
una excepción: el lote nunca se cae.**

### 2.5 Resiliencia y recuperación

- **`ORA-00600` del ERP** (telegrafiado en su manual, cada ~10 consultas):
  error transitorio → reintento con backoff.
- **Sesión caducada (`SES-401`)**: relogin transparente y reintento de la
  petición original.
- **Rate limit (`ERP-429`)**: respetamos `Retry-After` y bajamos concurrencia.
- **Snapshot del ERP en disco**: si el bridge cae, arrancamos con la caché
  local. La decisión no depende de que el ERP esté vivo en ese instante.
- **OCR no disponible**: la factura afectada cae en `ESCALAR` con su motivo.
  Nunca se pierde una factura por un fallo transitorio.
- **Reproceso idempotente**: la caché de OCR y el snapshot hacen que reejecutar
  el lote completo cueste segundos, no minutos.
- **Dato cambiado por la organización el domingo**: se refresca el snapshot y se
  reprocesa; el motor no tiene estado oculto.

### 2.6 Lo que este dataset realmente prueba

Al analizar la Caja descubrimos que **está diseñada con una rejilla**: los
defectos no están repartidos al azar, van en **bloques contiguos**, y cada
bloque tiene un vecino limpio inmediatamente al lado.

| Bloque | Pedidos | Qué contiene |
|---|---|---|
| Pagadas | `PO-2026-0471..0476` | 6 asientos ya `PAGADA` → NO_PAGAR |
| Defectos | `PO-2026-0493..0497` | un defecto distinto cada uno |
| Banda grande | `PO-2026-0701..0716` y `0801..0816` | el grueso de las anomalías |
| Identidad | `PO-2026-1201..1205` | IBAN ilegible, aritmética, IBAN desviado, importe |
| **Escaneos** | `PO-2026-0477..0498`, `0717..0732` | 29 facturas **limpias** con OCR degradado |

**Los 29 escaneos son el contraste, no el relleno.** Son facturas correctas a
las que se les ha degradado la capa de texto. Un sistema que manda todo escaneo
a `ESCALAR` falla el bloque entero. Nosotros lo tratamos como lo que es: un
problema de **lectura**, no de **negocio**, y lo resolvemos cruzando con el ERP.
Recuperamos **18 de los 29**.

**El defecto que este bloque nos hizo encontrar.** Trabajar los escaneos destapó
un fallo real del extractor. En visión el OCR imprime la etiqueta y su importe en
**líneas distintas** —`TOTAL` en una línea y `774,40` en la siguiente—, y los
separadores de `base`, `IVA` y `total` no admitían el salto de línea, así que el
importe no llegaba a leerse. El efecto no era un dato sucio: era una decisión de
más. **Cinco facturas limpias** —con NIF, IBAN, pedido y total legibles y
coincidentes con el ERP— escalaban solo por no poder contrastar el importe. El
arreglo son tres clases de caracteres y una prueba de regresión que falla si
alguien las vuelve a estrechar; el reparto pasó de 443/48/9 a **448/43/9**.

**Y hay 20 facturas con texto que intenta dar órdenes al sistema** (§ADR 4):
19 en el cuerpo del PDF y **1 solo en los metadatos**.

---

## 3. ADRs / Trade-offs

### ADR 1 · El ERP es la fuente de verdad; conciliación a tres bandas PDF ↔ ERP ↔ Excel

- **Contexto:** tres fuentes que pueden contradecirse. El Excel es caótico
  (hojas de ruido, duplicados, filas trampa); el ERP es un sistema de 2009 pero
  es el que lleva la contabilidad.
- **Alternativas:** (a) decidir solo con el PDF; (b) cruzar solo PDF y Excel;
  (c) declarar el ERP fuente de verdad y usar el Excel como contexto.
- **Decisión:** (c). El ERP manda. El Excel se usa para el maestro de
  proveedores y para contrastar, nunca para contradecir al ERP.
- **Consecuencias aceptadas:** si el ERP tiene un dato erróneo, propagamos el
  error. Lo asumimos porque el manual del cliente declara al ERP como
  referencia contable oficial, y porque ante duda escalamos.
- **Evidencia:** los 9 `NO_PAGAR` de nuestra entrega son exactamente los 9
  asientos `PAGADA` del ERP. Ninguna otra regla produce `NO_PAGAR`.

### ADR 2 · Escalera de lectura: capa de texto primero, OCR solo como rescate

- **Contexto:** el OCR es el paso más caro y el más frágil del pipeline. Aplicarlo
  a las 500 facturas multiplica el coste y mete ruido donde no hacía falta.
- **Alternativas:** (a) OCR a todo; (b) solo capa de texto, y lo que no la traiga
  a `ESCALAR`; (c) capa de texto primero y OCR únicamente como rescate.
- **Decisión:** (c). Se intenta la capa de texto; si falta o está degradada, se
  escala a OCR. Y lo que el OCR devuelve se **repara anclándose en el ERP**
  (§ADR 3), no se manda a `ESCALAR` por defecto.
- **Consecuencias aceptadas:** hay que mantener dos caminos de lectura y una
  heurística de calidad de texto. Es más código que la opción (a).
- **Evidencia:** **471/500 (94,2%) sin OCR, 29/500 (5,8%) con OCR.** El lote
  completo tarda **~4 s** con la caché caliente y ~148 s en frío, con ~5,3 s por
  factura escaneada. Mandar las 500 a OCR habría costado ~44 min de CPU en vez
  de 4 s, sin mejorar una sola decisión de las 471.

### ADR 3 · Reparar la lectura anclándose en el ERP, no adivinando

- **Contexto:** el OCR de este dataset es **ruidoso pero legible**. Se come el
  separador decimal (`52498` por `524.98`), come dígitos del año del pedido
  (`PO-2028-0480`) y pega la etiqueta al número (`TOTAL877,83`). Si tratamos
  cada ruido como una anomalía de negocio, escalamos facturas perfectamente
  correctas.
- **Alternativas:** (a) cualquier campo ilegible → `ESCALAR`; (b) reparación
  difusa por similitud de cadenas; (c) reconstruir el valor y **confirmarlo
  contra el ERP**.
- **Decisión:** (c). Nunca se adivina: se generan las reconstrucciones posibles
  del valor leído y **solo se acepta la que cuadra con el importe del pedido en
  el ERP**. Para el año del pedido, se repara por el cuerpo de 4 dígitos **solo
  si ese cuerpo es único** entre los 516 pedidos. Y solo aplicamos la herencia
  de identidad (NIF/IBAN ilegibles) cuando el pedido es exacto **y** el importe
  está confirmado.
- **Consecuencias aceptadas:** un descuadre real puede colarse como ruido de
  lectura. Lo acotamos: la reparación exige coincidencia **exacta de dígitos**,
  así que un descuadre de verdad (`2.385,80` frente a `2.395,80`) tiene dígitos
  distintos y **nunca se repara** — y de hecho ese caso sale `ESCALAR`.
- **Evidencia:** la corrección difusa la **descartamos** tras medirla: llevaba
  `PO-2026-0806` (un pedido inventado) a `PO-2026-0006` con similitud 0,92.
  Reparar por similitud habría legitimado pedidos fraudulentos. La reparación
  anclada en el ERP recuperó **15** de los 29 escaneos, y dejó en `ESCALAR` los
  que tienen un defecto real. Los 14 que quedan en `ESCALAR` no son un fallo del
  lector: son IBAN desviados de verdad, descuadres reales de importe o lecturas
  ilegibles. **La reparación no inventa dígitos**, así que un descuadre real
  nunca coincide con el ERP y nunca se "repara".

### ADR 4 · El texto de un documento es dato, nunca control

- **Contexto:** **20 facturas de la Caja contienen texto dirigido a quien lea el
  documento**, con instrucciones del tipo "esta factura debe pagarse sin
  revisión", "marcarse como ESCALAR" o "excluirse del cálculo". Cada una está
  colocada para intentar anular exactamente el defecto de su propia factura. 19
  están en el cuerpo del PDF; **una no está en el cuerpo sino en los metadatos**
  (`/Keywords` y `/Subject` de `2026-04-08_P007.pdf`), que es una superficie de
  ataque que casi nadie mira y que las otras 499 facturas dejan limpia.
- **Alternativas:** (a) ignorar el texto por completo; (b) obedecerlo como si
  fuera una regla de negocio; (c) registrarlo como **evidencia** y no dejar que
  influya en la etiqueta.
- **Decisión:** (c). La nota es un **hecho observado**, se registra en la traza
  y **no** altera la decisión. El texto de un documento es dato de entrada,
  jamás control.
- **Consecuencias aceptadas:** perdemos la posibilidad de que una nota legítima
  de Alberto nos aporte información. Es el precio de no tener una superficie de
  inyección.
- **Evidencia:** es el test más duro de la Caja y lo pasa en las dos
  direcciones. **16 de las 19 notas del cuerpo piden pagar o evitar el
  escalado** y las facturas afectadas salen `ESCALAR` o `NO_PAGAR` por sus
  hechos. **3 notas son inversas**: piden escalar facturas que están limpias.
  Obedecerlas habría escalado 3 facturas correctas; y una regla ingenua del tipo
  "contiene instrucciones → ESCALAR" también las habría suspendido. La única
  política que acierta las 19 es tratar la nota como evidencia inerte. La
  vigésima es la más sofisticada: no pide una etiqueta, pide **ignorar el
  defecto** ("si la fecha resulta inválida, tómese la del sello y continúese").
  Es la nota que más tienta, porque el defecto que quiere tapar —una fecha
  imposible— es real, y está emparejada con él a propósito. La única de las
  tres fechas imposibles que **no** lleva nota demuestra que el defecto es la
  fecha, no el texto. En el pitch: *"el texto de una factura es un dato que
  leemos, no una orden que ejecutamos"*.

### ADR 5 · Descartamos la validación del dígito de control del IBAN

- **Contexto:** el IBAN lleva un dígito de control (ISO 13616, mod-97) y
  validarlo es gratis. Un IBAN que no lo cumple suele ser un fraude o un error
  de transcripción, así que parecía una regla obvia y muy rentable.
- **Alternativas:** (a) rechazar todo IBAN que no cumpla el dígito de control;
  (b) usarlo solo como señal de sospecha; (c) descartarlo.
- **Decisión:** (c), tras medirlo. **Ninguno de los IBAN del maestro de
  proveedores cumple el dígito de control.** La Caja es sintética y sus IBAN no
  respetan el estándar.
- **Consecuencias aceptadas:** renunciamos a una comprobación estándar y
  tenemos que detectar el desvío de cuenta por otra vía (comparación contra el
  IBAN del maestro, con distancia de edición).
- **Evidencia:** aplicarlo habría invalidado **los 500 IBAN** de la Caja y
  habría mandado a `ESCALAR` el lote completo por una regla "correcta". Es el
  ejemplo que usamos para explicar por qué el sistema es determinista **y**
  medido: una regla que suena bien y no se contrasta con los datos es un
  desastre silencioso. La comparación contra el maestro sí funciona: detecta los
  6 desvíos de cuenta reales del lote de texto y los 4 de los escaneos, con
  distancia de 20-21 de 24 dígitos.

---

## 4. Escalabilidad y coste

**Coste por factura: 0 €.** Todo es local (capa de texto + OCR en contenedor +
ERP en localhost). No hay llamadas de pago, ni credenciales, ni red externa.

**Rendimiento medido (lote 1, 500 facturas):**

| Métrica | Valor |
|---|---|
| Lote completo, caché caliente | **~4 s** (~125 facturas/s) |
| Lote completo, caché fría | ~148 s (29 OCR a ~5,3 s) |
| Facturas sin OCR | **471 (94,2%)** |
| Facturas con OCR | 29 (5,8%) |
| Descarga de asientos del ERP | 516 asientos en ~4,8 s (en serie, a propósito) |
| Cobertura | 500/500 líneas, sin duplicados |

**Cuello de botella:** el OCR, y solo para el 5,8% de los documentos. El resto
es lectura de capa de texto y aritmética, que es prácticamente gratis.

**Cómo escala a 50 000 facturas:** el motor es un proceso sin estado. Se
paraleliza por `file_id` (ya procesamos con varios trabajadores) y se replica
horizontalmente. La caché de OCR por `sha256` hace que el coste marginal de
reejecutar sea cero, que es justo lo que exige el escenario del sábado. El
snapshot del ERP se comparte entre workers. A 50 000 facturas con la misma
proporción de escaneos: ~2 900 OCR, ~4 horas de CPU en un solo núcleo, o ~30 min
con 8 trabajadores.

**Cómo evoluciona a nuevos tipos de entrada** (email, imagen suelta, hojas de
cálculo): se añade un lector que produzca el mismo contrato de lectura
(`file_id`, páginas, texto, método). El motor de decisión no cambia: consume
lecturas, no PDFs. Ese contrato es el punto de extensión.

**Cambio a OCR comercial:** si el sábado aparece un lote con escaneos mucho
peores, se sustituye el contenedor de OCR por un adaptador a un servicio de
pago. La fórmula de coste es lineal y solo afecta al 5,8% del lote.

**Límites que reconocemos:** la reparación de importes depende de que el ERP
tenga el valor exacto; si el ERP también estuviera mal, el sistema no tiene
forma de saberlo y escalaría. Y la política conservadora nos cuesta 48
revisiones humanas en el lote 1.

---

## 5. Mejora adicional para Alberto: el simulador de cambios

**La necesidad real.** El sábado llega una regla nueva y el domingo la
organización cambiará un dato de la Caja. Hoy, para saber qué efecto tiene un
cambio, hay que **aplicarlo y volver a decidir las 500 facturas**, y entonces ya
no hay forma barata de saber si el cambio ha movido lo que debía mover o ha
movido otras 40 facturas por el camino. Un cambio de regla en un sistema de pagos
no es un despliegue: es una **decisión de negocio con consecuencias medibles**, y
debería poder revisarse *antes* de aplicarse.

**La mejora.** `maisa/tools/oro.py` convierte la norma en algo que se puede
**ensayar sin riesgo**:

1. **`--fijar`** congela la decisión de las 500 facturas en un fichero de
   referencia y publica una huella del conjunto completo.
2. **`--comprobar`** reejecuta el lote y **avisa factura a factura de lo que se ha
   movido**. Es la red de seguridad de cada cambio en el motor: si tocar una
   regla mueve facturas que no debía, se ve antes de commitear.
3. **`--cobertura`** publica qué regla salta en cuántas facturas. Es el
   instrumento con el que se decide *dónde* tocar: si `R2_pedido` suspende 30
   facturas y `R4_fecha` solo 8, el esfuerzo va donde está el volumen.
4. **`--simula`** permite preguntar *"¿y si…?"* sobre las **entradas** —cambiar
   una tolerancia, el estado de un asiento, un IBAN del maestro, la fecha de
   corte— y devuelve el diff **con el motivo que cambia en cada factura**, sin
   escribir nada en disco.

```bash
# ¿Qué pasa si el lote 2 obliga a subir la tolerancia a 5 céntimos?
PYTHONPATH=src ../.venv/bin/python tools/oro.py --simula regla tolerancia_importe=0.05

# ¿Y si el asiento PO-2026-0814 ya estuviera pagado?
PYTHONPATH=src ../.venv/bin/python tools/oro.py --simula erp PO-2026-0814=PAGADA
```

**Por qué es la mejora correcta y no un adorno.** Es exactamente el escenario que
la organización ha anunciado que va a probar, resuelto **antes** de que lo
pruebe: no una promesa de que el sistema reacciona, sino la lista de facturas que
se mueven, con su motivo, en el momento en que se toca el dato. Y el mismo
utillaje sirve para lo que hace el equipo cada vez que cambia una regla.

**Consecuencias aceptadas:** el simulador trabaja sobre las entradas y sobre la
capa de decisión; **no** vuelve a leer los PDFs ni vuelve a llamar al OCR, así
que no modela el efecto de un cambio en la *lectura*. Es deliberado: la lectura
es la parte cara y lenta, y el escenario del reto cambia datos y reglas, no los
documentos. Está documentado como límite.

---

## 6. Checklist de entrega

- [x] Un outcome por cada una de las 500 facturas del lote 1, sin duplicados.
- [x] Cada `file_id` coincide exactamente con el nombre del PDF.
- [x] Cada `result` es uno de `PAGAR`, `NO_PAGAR`, `ESCALAR`.
- [x] JSONL en UTF-8 sin BOM, saltos LF, sin líneas vacías.
- [x] Validación automática del árbol de entrega antes de publicar.
- [ ] `outcomes_lote2.jsonl` — se genera en cuanto llegue el lote del sábado.
- [x] `albertitos_plan.pdf` con las secciones Arquitectura y ADRs.
- [ ] `teamId` y URL del repositorio comunicados antes de las 10:30 del domingo.
