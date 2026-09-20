# Motor de decisión · norma de pagos v3

Decide **PAGAR / ESCALAR / NO_PAGAR** sobre las 500 facturas del lote de Maisa
cruzando tres fuentes: el PDF de la factura, el maestro de proveedores con los
pedidos (Excel) y los asientos del ERP.

Este directorio es la implementación que se ejecuta y se entrega. El motor
anterior en Rust (`maisa/src/`) se conserva como **legado documental**:
su diseño y sus contratos están recogidos en `TRASPASO.md`.

No es un esqueleto — tiene las reglas implementadas y 57 tests en verde — pero
**nunca resolvió el corpus real**: el lector del maestro (`src/excel.rs`) y la
observabilidad (`src/obs.rs`) son placeholders de dos líneas, y su modo lote
está declarado en el propio código como *"lo que se puede probar sin OCR, sin
ERP y sin Excel"*. Por eso `outputs/outcomes.jsonl` sigue a 0 bytes desde el
commit inicial: el Rust solo se ha ejecutado contra el lote de ejemplo de 10
líneas.

## Arranque rápido

```bash
python -m pip install -r maisa/motor/requirements.txt

cd maisa
PYTHONPATH=motor/src python -m maisa.procesa \
  --facturas data/facturas \
  --xlsx     data/FINAL_v7_DEFINITIVO_ahorasi.xlsx \
  --snapshot data/erp_snapshot.json \
  --config   motor/config/reglas.toml \
  --lote 1 --trabajadores 4 \
  --salida   outputs/outcomes.jsonl
```

Tarda **~3,6 s** para las 500 facturas a 4 trabajadores y **no necesita red**.
Los 29 PDFs escaneados llevan su texto ya cacheado en `.cache/ocr/`, indexado
por el sha256 del PDF: la caché es portable y por eso la CI puede reproducir el
lote entero sin levantar el contenedor de visión. Si aparece un PDF escaneado
nuevo, se cae al servicio de OCR (`http://127.0.0.1:8866`) y se rellena la caché.

## Reparto actual

```
PAGAR     448
ESCALAR    43
NO_PAGAR    9
```

Las 9 `NO_PAGAR` son exactamente los 9 pedidos que el ERP ya da por `PAGADA`.
Ninguna decisión de `PAGAR` se apoya en texto que venga del propio documento.

## Cómo se decide

La norma v3 se aplica como **datos**, no como código disperso:
`config/reglas.toml` contiene los umbrales, la precedencia de estados y las
reglas activables. Un cambio de criterio es un cambio de fichero, no un
despliegue.

| Regla | Qué exige | Si falla |
|---|---|---|
| R1 | NIF del emisor en el maestro **y** IBAN de abono coincidente | ESCALAR |
| R2 | El pedido existe en el ERP, es del proveedor, importes conciliados (0,01 €) | ESCALAR |
| R3 | IVA bien calculado: `total = base + IVA` (0,01 €) | ESCALAR |
| R4 | Fecha existente en el calendario y no futura respecto a `hoy` | ESCALAR |
| R5 | Estado del asiento en el ERP es `PENDIENTE`; **nunca pagar dos veces** | PAGAR / NO_PAGAR |
| R6 | Cualquier anomalía que un humano deba ver | ESCALAR **con motivo** |

Además de las seis reglas, el motor reconoce cinco **hechos** que el PDF puede
declarar por sí mismo y que un humano leería de un vistazo:

- `si_instruccion` — el documento trae una orden ("pagar hoy", "no pagar", un
  fichero de autorización incrustado). El texto de un documento es **dato**,
  nunca control: se registra y se escala.
- `si_documento_no_legible` — la identidad no se puede confirmar porque el NIF o
  el IBAN no vienen correctamente delimitados.
- `si_pedido_repetido` — el mismo pedido aparece en dos facturas del lote.
- `si_pendiente_revision` — el pedido está en la hoja de revisión del Excel.
- `pago_duplicado` — **hecho duro**: el asiento ya está `PAGADA`. Es el único
  hecho que fuerza `NO_PAGAR` y prevalece sobre todo lo demás.

## Trazabilidad

Cada decisión se puede explicar hacia atrás sin volver a leer el PDF:

```bash
PYTHONPATH=motor/src python -m maisa.trace linaje outputs/outcomes_traza.jsonl
```

La traza es un encadenado de eventos con hash: identifica **esta ejecución**
(contiene marca de tiempo), no el lote. Los `outcomes.jsonl` sí son
byte-idénticos entre ejecuciones, y eso es lo que la CI comprueba.

### Cómo de bien lee el motor

La misma traza sirve para medir la lectura sin volver a tocar los PDF: cada nota
de la norma es o bien una **reparación** (el escaneo venía mal y el motor lo
ancló en el ERP o en la aritmética del documento) o bien un **hueco** (el dato no
existe en el maestro, así que no hay nada que leer).

```bash
PYTHONPATH=motor/src python motor/tools/censo_extraccion.py --verbose
```

Salida del lote de 500: 16 facturas (3.2%) con 20 reparaciones —13 de importe, 4
de pedido y 3 de NIF— y 15 (3.0%) con hueco —10 IBAN ajenos al maestro, 3
pedidos inexistentes en el ERP y 2 NIF desconocidos. Es una medida, no un
umbral: no bloquea la entrega.

El censo solo cuenta reparaciones de verdad. Una nota de «importe recompuesto»
que repita el mismo importe a los dos lados de los dos puntos no es una
reparación, y un escaneo que el lector no supo medir no vale 0.0: la media de
`calidad_lectura` se calcula sobre las 471 facturas con capa de texto y el censo
declara cuántas quedan fuera, para que 0.9903 no se lea como «y los escaneos,
vete a saber».

### El único contraste que mira desde fuera

`oro.py` y `valida_entrega.py` comparan nuestro criterio con el nuestro. El
contraste externo se hace con una referencia que **no se versiona**: se pasa como
dato de entrada y la herramienta dice si cada decisión cae en el conjunto de
resultados que esa referencia admite. Acepta el envoltorio de un `oracle.json`
ajeno (`verdict.acceptable`, `verdict.primary`, `findings`) o un JSONL plano de
decisiones, autodetectados por estructura.

```bash
PYTHONPATH=motor/src python motor/tools/conformidad.py \
    --outcomes outputs/outcomes.jsonl --referencia <referencia.json|referencia.jsonl>
```

Sobre el lote de 500 y la referencia externa que usamos: 489/500 coinciden con su
resultado preferido, 499/500 caen dentro de lo admisible (99.8%) y queda **un**
`FUERA_ALTO` — el escaneo del que no se lee ni NIF ni fecha y del que la
referencia solo admite `ESCALAR`/`NO_PAGAR`. Ese desacuerdo está atribuido en
`docs/albertitos_plan.md`. Como es una medida y no una puerta, su exit 1 es
esperado: no se puede colgar de CI sin lista blanca.

### Recortar la cola de revisión sin decidir ningún pago

El motor manda 43 de las 500 facturas a `ESCALAR`: esas son la **cola de revisión
humana**. De las 43, solo 11 las leyó el OCR (el resto trae capa de texto y la
norma escala por otras causas), y de esas 11 hay un grupo que no hace falta abrir
a mano: el motor local no supo leer un identificador, la segunda lectura (la
nube) lo aporta, el maestro lo confirma y **la escalada desaparece**.

```bash
cd maisa/motor && PYTHONPATH=src ../../.venv/bin/python tools/cola_revision.py
```

```
  escaladas por la norma:            43
    de ellas, leidas por OCR:        11
      confirmables sin abrir:        4
      desvio de pago (NO recortar):  4
      aporta pero sigue escalando:   1
      sin evidencia que aportar:    2
      la nube no respondio:          0

  cola a revisar a mano: 43 -> 39
```

Lo escribe en `maisa/outputs/outcomes_cola.jsonl`, un **sidecar**: un fichero
aparte, **no** una entrega. La entrega (`outcomes.jsonl`) tiene exactamente dos
claves (`file_id`, `result`) y la CI reproduce el lote dos veces exigiendo el
mismo `md5` y comprueba con `cmp` que el fichero versionado es idéntico al que
produce el motor; la evidencia no cabe ahí y **no debe caber**. La API lo lee si
existe y lo sirve como `segunda_lectura` (ver `api/README.md`); si no existe, todo
se revisa a mano como siempre.

Cuatro reglas sostienen el recorte, y las cuatro son conjuntas:

1. **El local no resolvió** el identificador (si lo resolvió, no hay nada que
   aportar).
2. **La segunda lectura lo aporta** y el maestro lo confirma: el pedido se
   resuelve contra el vocabulario del maestro (con reparación de confusiones de
   OCR, nunca dígito a dígito); el NIF y el IBAN exigen coincidencia **exacta**.
3. **La escalada desaparece**: el lote híbrido se decide entero con un decisor
   propio y se exige `resultado != "ESCALAR"`. Sin esto se cuela un recorte
   falso: una factura que escala por cinco motivos y a la que la nube solo le
   resuelve el pedido **sigue escalando**, y marcar eso como confirmable sería
   mentir. (Pasó con `fax_2026_0411`, y por eso el gate es del motor de reglas y
   no una heurística sobre los motivos.)
4. **El IBAN no se tapa nunca** si el local leyó uno, aunque sea distinto del
   maestro: ese IBAN ajeno es la **única señal de fraude** del sistema, y borrarla
   para «arreglar» el campo sería el peor de los errores posibles. El importe,
   igual: **nunca** sale de la nube.

El resultado son 4 confirmables (`scan_002`, `scan_011`, `scan_017`, `scan_022`) y
4 desvíos intocables (`reimpresion_0712`, `scan_016`, `scan_018`, `scan_029`, los
cuatro con IBAN ajeno al maestro). La cola baja de 43 a 39. **Ninguna decisión
cambia**: `outcomes.jsonl` y la traza quedan byte a byte igual.

> Esto **anota**, no decide ni cierra. La revisión humana sigue siendo humana: el
> estado `PENDIENTE`/`RESUELTA` lo marca una persona en Mongo y el motor no lo
> toca. Y **la nube todavía no puede decidir un pago**: se midió sobre 29
> facturas, y de las 4 que solo la nube resuelve no hay muestra suficiente para
> saber con qué frecuencia acierta un identificador coherente pero falso. Mientras
> eso siga sin medirse, la segunda lectura solo rellena huecos que el maestro
> confirma y el importe lo sigue poniendo el local.

## Determinismo: la única invariante que no se negocia

Un motor de pagos que cambia de opinión entre ejecuciones no es un motor, es una
moneda al aire. La CI reproduce el lote **dos veces** y exige el mismo `md5sum`;
además exige que el `outputs/outcomes.jsonl` versionado sea exactamente lo que el
motor produce hoy. Si alguien toca una regla y no regenera la entrega, la CI
falla.

## Contenido

```
motor/
├── config/reglas.toml   la norma v3 como datos
├── src/maisa/           el motor (11 módulos)
├── tests/               suite + banco de oro (tests/oro/)
├── tools/               oro.py, valida_entrega.py, conformidad.py, censo_extraccion.py,
│                        cola_revision.py, bench_motores.py, md_a_pdf.py, bench.py,
│                        evidencia_resiliencia.py
├── docs/                arquitectura, capacidad, resiliencia, lote 2, simulador
├── .cache/ocr/          texto de los 29 escaneados, indexado por sha256
├── .cache/motores/      lecturas de local y nube de esos 29, para medir sin repetir
└── .cache/segunda/      lecturas de nube de la cola (se crea al usarlo; antes
                         reutiliza .cache/motores/ si la lectura ya se pagó)
```

| Módulo | Responsabilidad |
|---|---|
| `lectura.py` | escalera de lectura: capa de texto → caché OCR → servicio de visión |
| `texto.py` | extracción de campos, instrucciones y metadatos del PDF |
| `normaliza.py` | normalización de NIF, IBAN, pedido, importes y fechas |
| `excel.py` | maestro de proveedores y pedidos (14 hojas, 12 de ellas rotas a propósito) |
| `erp.py` | cliente del ERP + snapshot offline |
| `norma.py` | **el núcleo**: las seis reglas, los hechos y la precedencia |
| `trace.py` | linaje de eventos encadenado por hash |
| `emit.py` | validación y escritura del JSONL |
| `segunda_lectura.py` | la guarda asimétrica del IBAN y el gate de «confirmable»: una sola implementación para la medición y la producción |
| `procesa.py` | orquestación y CLI |
