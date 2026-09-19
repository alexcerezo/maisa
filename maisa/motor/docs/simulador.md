# El simulador de cambios (`--simula`)

> Esta es la **mejora adicional para Alberto** (+10 de bonus) del enunciado, y
> a la vez una herramienta de resiliencia: permite ensayar un cambio de datos o
> de norma **sin tocar el lote y sin escribir nada en disco**.
>
> El planteamiento está en `albertitos_plan.md` §5 ("Mejora adicional para
> Alberto: el simulador de cambios"), que **no se edita aquí**: ese documento es
> la propuesta y este es el manual de la herramienta ya construida.

---

## 1. La pregunta que responde

> "¿Qué pasaría con esta factura —y con las otras 499— si cambiara el dato X?"

Hoy, para saberlo, hay que **aplicar el cambio y reejecutar el lote**, y entonces
ya no hay forma barata de saber si movió lo que debía mover o si movió otras 40
facturas por el camino. `--simula` contesta la misma pregunta **antes** de
aplicar nada, y contesta lo importante: no solo *cuántas* facturas se mueven,
sino **por qué regla**.

```bash
cd maisa
PYTHONPATH=src ../.venv/bin/python tools/oro.py --simula regla tolerancia_importe=0.05
```

## 2. Cómo se usa

```
--simula AMBITO CLAVE=VALOR [CLAVE=VALOR ...]   # uno o varios cambios a la vez
--factura FILE_ID                               # obligatorio en el ambito factura
```

Cuatro ámbitos, según **qué** se toca:

| Ámbito | Qué modifica | Forma de la clave |
|---|---|---|
| `regla` | la norma (`reglas.toml`), en memoria | `umbral`, `reglas.<REGLA>.<clave>`, `hechos_duros.<nombre>`, `precedencia.<RESULTADO>` |
| `erp` | un asiento del ERP | `PO-2026-0084=VALOR` (estado) o `PO-2026-0084.importe=VALOR` |
| `maestro` | un proveedor del maestro | `P002.iban=VALOR` o por NIF: `A41220987.iban=VALOR` |
| `factura` | un campo **ya leído** de un PDF | `total=VALOR` (o `importe=`), `iban=`, `fecha=`, `pedido=`, `estado_erp=`… |

Claves aceptadas por ámbito (las mismas que valida el programa; si te
equivocas, el error las lista):

- **`regla`** — escalares: `tolerancia_importe`, `similitud_minima_nif`,
  `similitud_minima_iban`, `confianza_minima_campo`, `hoy`, `version`.
  Puntuales: `reglas.R2_pedido.si_falla=NO_PAGAR`,
  `reglas.R5_estado.si_ya_pagada=ESCALAR`,
  `hechos_duros.pago_duplicado=ESCALAR`, `precedencia.ESCALAR=2`.
- **`erp`** — campos de un asiento: `estado`, `importe`, `nif`, `proveedor`, `fecha`.
- **`maestro`** — campos de un proveedor: `iban`, `nif`, `razon_social`,
  `ciudad`, `condiciones`.
- **`factura`** — leídos: `nif`, `iban`, `pedido`, `fecha`, `base`, `iva`,
  `total` (alias `importe`), `num_factura`; señales: `texto_ilegible`,
  `sospechosos`, `metodo`; puente al ERP: `estado_erp`.

### Las tres garantías

1. **No escribe nada.** Ni JSONL, ni traza, ni fichero temporal: el resultado se
   imprime y el proceso termina. El banco de oro y la entrega quedan intactos.
2. **No relee los PDFs ni llama al OCR.** Parte de la lectura ya hecha en el
   lote y vuelve a decidir. Por eso un ensayo de 500 facturas tarda lo que
   tarda leer los PDFs en caliente (≈ 4 s), no lo que tarda el OCR.
3. **No cambia la versión de la norma.** `norma_v3.1` sigue siendo la vigente
   después de simular; el ensayo vive solo en memoria del proceso.

## 3. Cómo se lee la salida

```
simulacion : regla tolerancia_importe=10.0
lote       : .../corpus/maisa/facturas (500 facturas, lote 1, 4 trabajadores)
norma      : norma_v3.1 (.../motor/config/reglas.toml)
cambios aplicados (en memoria, nada escrito en disco):
  - politica.tolerancia = 10.0
decision   : {'ESCALAR': 43, 'NO_PAGAR': 9, 'PAGAR': 448}  ->  {'ESCALAR': 42, 'NO_PAGAR': 9, 'PAGAR': 449}
movidas    : 1 de 500 facturas cambian de resultado | 1 cambian su traza de hechos

  factura_2018.pdf             ESCALAR -> PAGAR
      - R2_pedido            FALLA  el total de la factura NO cuadra con el importe del pedido
      + R2_pedido            OK     el total de la factura cuadra con el importe del pedido
      antes  : importe descuadrado: factura 4295.50 vs pedido 4295.10
      ahora  : (sin motivos)
```

- `decision` es el **reparto antes → después**: lo primero que mira el negocio.
- `movidas` separa dos cosas distintas: facturas que **cambian de resultado** y
  facturas cuya **traza de hechos** cambia (la evidencia, aunque el resultado no
  se mueva). Un cambio puede mover la segunda sin mover la primera.
- Cada factura movida lista **qué hecho desaparece (`-`) y cuál aparece (`+`)**,
  y los **motivos antes/ahora** en lenguaje de negocio. Es la respuesta a *"¿por
  qué?"*, no solo a *"¿cuántas?"*.
- Se imprimen hasta 40 facturas y se resume el resto (`... y 11 facturas mas`).
  El límite es de impresión, no de cálculo.

## 4. Seis ensayos reales (medidos el 2026-09-19)

Los seis se lanzaron de verdad contra el lote 1 completo (500 PDFs) con el motor
en su revisión de ese momento. Reparto de partida en los seis:
`PAGAR 448 · ESCALAR 43 · NO_PAGAR 9`.

Ese reparto es **el mismo de la entrega**, y no por casualidad: el simulador
declara al decisor la duplicidad de pedido del lote con el mismo
`procesa.marca_pedidos_repetidos` que usa el proceso real. Sin ese paso —que
faltaba— arrancaba de `PAGAR 445 · ESCALAR 46`, y sus deltas no eran los de la
entrega (§5.5).

### 4.1 Subir la tolerancia de importe a 10 € (`regla`)

```
movidas    : 1 de 500 facturas cambian de resultado | 1 cambian su traza de hechos
  factura_2018.pdf   ESCALAR -> PAGAR
      - R2_pedido   FALLA  el total de la factura NO cuadra con el importe del pedido
      + R2_pedido   OK     el total de la factura cuadra con el importe del pedido
      antes : importe descuadrado: factura 4295.50 vs pedido 4295.10
```

**Lectura de negocio:** una tolerancia de 10 € "arregla" exactamente **una**
factura del lote (0,40 € de descuadre). Si alguien propone subirla "para que
pasen menos cosas", esto es lo que compra: una factura, y a cambio deja de
revisar un descuadre real.

### 4.2 Convertir en NO_PAGAR el fallo de `R2_pedido` (`regla`)

```
movidas    : 16 de 500 facturas cambian de resultado | 0 cambian su traza de hechos
decision   : {'ESCALAR': 43, ...}  ->  {'ESCALAR': 27, 'NO_PAGAR': 25, 'PAGAR': 448}
  2026-0811-B_catering.pdf   ESCALAR -> NO_PAGAR
      = R2_pedido   FALLA  el total de la factura NO cuadra con el importe del pedido
      = R3_iva      FALLA  total != base + IVA
```

**Lectura de negocio:** endurecer la política mueve **16** facturas de ESCALAR a
NO_PAGAR. Y aquí el simulador destapa algo que un "diff de resultados" no vería:
**no cambia ni un hecho**. Los mismos hechos probados, decididos al revés. Eso
significa que el cambio es puro endurecimiento de política, reversible, y que no
ha tocado la evidencia.

### 4.3 Marcar como pagado un asiento del ERP (`erp`)

```
simulacion : erp PO-2026-0084=PAGADA
  - asiento PO-2026-0084.estado: PENDIENTE -> PAGADA
movidas    : 1 de 500 facturas cambian de resultado | 1 cambian su traza de hechos
  FA-1926_transportes.pdf   PAGAR -> NO_PAGAR
      - R5_estado                OK     el pedido esta PENDIENTE en el ERP
      + R5_estado/pago_duplicado FALLA  [duro] el pedido ya figura PAGADA en el ERP
      ahora : PO-2026-0084 ya esta PAGADA en el ERP: no se paga dos veces
```

**Lectura de negocio:** es el ensayo del domingo ("la organización cambiará un
dato de la Caja"): si ese asiento estuviera ya pagado, **una** factura deja de
pagarse y el motivo sale con nombre y apellidos. El cambio es *quirúrgico*: 1
factura de 500.

### 4.4 Rebajar el hecho duro del pago duplicado (`regla`, dos cambios)

```
--simula regla reglas.R5_estado.si_ya_pagada=ESCALAR hechos_duros.pago_duplicado=ESCALAR
movidas    : 9 de 500 facturas cambian de resultado | 0 cambian su traza de hechos
  2026-03-28_P002.pdf   NO_PAGAR -> ESCALAR
      = R5_estado/pago_duplicado  FALLA [duro] el pedido ya figura PAGADA en el ERP
```

**Lectura de negocio — el caso que justifica la herramienta.** Cambiar
**solo** `reglas.R5_estado.si_ya_pagada` no mueve **nada** (0 facturas), porque
`pago_duplicado` está en `[hechos_duros]` y un hecho duro se resuelve por su
propia tabla, no por la política de la regla. Para mover las 9 facturas hay que
cambiar **las dos** entradas. Un humano mirando `reglas.toml` habría creído que
con tocar `R5_estado` bastaba; el simulador lo dice en 4 segundos y sin
desplegar. **Nueve facturas mal pagadas se evitan aquí.**

### 4.5 Cambiar el IBAN de un proveedor en el maestro (`maestro`)

```
simulacion : maestro A41220987.iban=ES0000000000000000000000
  - proveedor P002.iban: 'ES7621000813610123456789' -> 'ES0000000000000000000000'
movidas    : 45 de 500 facturas cambian de resultado | 51 cambian su traza de hechos
  2026-01-12_P002.pdf   PAGAR -> ESCALAR
      - R1_identidad  OK     IBAN de la factura coincide con el maestro
      + R1_identidad  FALLA  el IBAN de la factura NO coincide con el del maestro
      ahora : IBAN de abono distinto del maestro: posible desvio de pago
```

**Lectura de negocio:** un IBAN mal tecleado en el maestro —o **cambiado a
propósito**— convierte 45 pagos en 45 escalados. Es el ensayo del fraude de
facturación y, de paso, la prueba de que la regla de identidad **sí** cubre la
superficie que dice cubrir. Nótese la asimetría: 45 resultados cambian, pero 51
facturas cambian su traza (6 ya estaban escaladas por otro motivo y ahora
arrastran un motivo más).

### 4.6 Corregir el total de una factura (`factura`)

```
simulacion : factura total=4295.10        (--factura factura_2018.pdf)
  - factura_2018.pdf.total: ['4295.50'] -> ['4295.10']
movidas    : 0 de 500 facturas cambian de resultado | 1 cambian su traza de hechos
  factura_2018.pdf   ESCALAR (igual)
      - R2_pedido   FALLA  el total de la factura NO cuadra con el importe del pedido
      - R3_iva      OK     total = base + IVA
      + R2_pedido   OK     el total de la factura cuadra con el importe del pedido
      + R3_iva      FALLA  total != base + IVA
      antes : importe descuadrado: factura 4295.50 vs pedido 4295.10
      ahora : aritmetica incoherente: 3550.00 + 745.50 != 4295.10
```

**Lectura de negocio:** arreglar un dato **rompe otra regla**. El resultado final
no cambia (sigue ESCALAR), pero el motivo sí: el simulador demuestra que "el
resultado es el mismo" no significa "no ha pasado nada". Sin la traza de hechos
esta factura habría pasado por un arreglo limpio cuando en realidad sigue
teniendo una anomalía, ahora distinta.

## 5. Límites declarados (honestidad, no humildad)

1. **No modela la lectura.** El simulador trabaja sobre las entradas (norma,
   maestro, asientos) y la capa de decisión; **no** vuelve a leer los PDFs ni
   llama al OCR. Un cambio en la *extracción* no se puede ensayar así. Es
   deliberado: la lectura es la parte cara y lenta, y el escenario del reto
   cambia datos y reglas, no documentos.
2. **`calidad_texto_minima` no se puede simular.** Vive en la **escalera de
   lectura** (`lectura.lee`), no en la capa de decisión: cambiarlo obliga a
   releer los PDFs. El programa lo rechaza con un mensaje explícito en vez de
   fingir que lo ha aplicado:
   ```
   calidad_texto_minima vive en la ESCALERA DE LECTURA (lectura.lee), no en la
   capa de decision: cambiarlo obliga a releer los PDFs y el simulador no relee.
   ```
3. **La firma de hechos incluye la evidencia.** Dos hechos con el mismo motivo
   pero distinta evidencia (por ejemplo, el estado del asiento dentro de
   `datos`) cuentan como cambio de traza. Es a propósito: la traza no es "qué se
   comprobó" sino "con qué evidencia".
4. **Un ensayo no es una promesa.** El simulador dice qué facturas se mueven y
   por qué regla; no dice si la nueva norma es *mejor*. Eso sigue siendo una
   decisión de negocio.
5. **Hereda el camino de decisión, no lo reimplementa.** Cualquier cosa que
   decida el **lote entero** —hoy, la duplicidad de pedido (Norma, punto 5)—
   tiene que estar en `procesa` y no *dentro* de `procesa`, para que el
   simulador la comparta. Cuando se duplicó, el simulador arrancó de
   `PAGAR 445 · ESCALAR 46 · NO_PAGAR 9` en vez del reparto real y todos sus
   deltas quedaron desplazados en dos facturas. Está corregido: el reparto de
   partida del §4 es el de la entrega, y `procesa.marca_pedidos_repetidos` es
   ahora el único sitio donde se decide.

## 6. Recetas para el lote 2 (sábado)

| Pregunta del negocio | Comando |
|---|---|
| ¿Cuánto mueve la regla nueva? | `--simula regla reglas.R7_nueva.si_falla=ESCALAR` |
| ¿Y si el proveedor nuevo no está en el maestro? | `--simula maestro <NIF>.iban=<IBAN>` |
| ¿Y si el asiento llega ya pagado? | `--simula erp <PO>=PAGADA` |
| ¿Y si el importe del pedido cambia? | `--simula erp <PO>.importe=1234.56` |
| ¿Y si la factura llegara bien leída? | `--factura <FILE_ID> --simula factura total=1234.56` |

Antes de tocar `reglas.toml`: `--simula` para ver el alcance → aplicar el cambio
en el TOML → `--comprobar` para confirmar que **solo** se movieron esas facturas
(red de seguridad del banco de oro, ver `lote2.md`).

## 7. Procedencia de las medidas de este documento

Salida completa y sin recortes en `maisa/_scratch_res/simula_demos.txt`
(temporal). Medidas del **2026-09-19 12:59** con el motor en:

```
src/maisa/norma.py     sha256 1d989b63c503b915  (35195 bytes)
src/maisa/lectura.py   sha256 4e66b880a52ad6bc  (5607 bytes)
src/maisa/texto.py     sha256 03931b4e5c32fd3f  (16792 bytes)
src/maisa/procesa.py   sha256 3462ff955745bd99  (8881 bytes)
src/maisa/emit.py      sha256 574f0cb8977eef6e  (3941 bytes)
```

Como el motor se sigue afinando, cualquier número de este documento se puede
reproducir con los comandos de la §4; si el reparto de partida ya no es
`PAGAR 448 · ESCALAR 43 · NO_PAGAR 9`, el motor ha cambiado, no el simulador.

Estas medidas son **posteriores** a las de las 11:38: entre una y otra se corrigió
un defecto del extractor —los separadores de `base`, `IVA` y `total` no admitían
el salto de línea que el OCR de visión introduce entre la etiqueta y su importe—.
Cinco facturas que escalaban solo por eso pasan a `PAGAR`, así que el reparto de
partida cambió de `PAGAR 443 · ESCALAR 48 · NO_PAGAR 9` a
`PAGAR 448 · ESCALAR 43 · NO_PAGAR 9`. El ensayo §4.2 es el que más lo nota:
mueve **16** facturas en vez de 29, y la cuenta cuadra exactamente con la
cobertura por regla —`R2_pedido` suspende hoy en 16 facturas y suspendía en 29—:
las 13 que faltan ya leen su total, así que el pedido les cuadra y dejan de
depender del endurecimiento.
