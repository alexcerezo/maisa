# Lote 2: runbook del sabado a las 18:00

El sabado a las 18:00 llegan **40 facturas nuevas y una regla nueva**. El domingo
La Caja puede **cambiar un dato** de una factura ya entregada. Este documento es
el procedimiento para absorber las dos cosas **sin tocar una linea de Python**:
la norma vive en `maisa/config/reglas.toml` y el motor es sin estado.

Lo que se entrega son tres ficheros y solo tres. El que cambia es
`outcomes_lote2.jsonl`.

---

## 0. Que llega y donde se pone

| Que | Donde | Quien lo trae |
|---|---|---|
| 40 PDF nuevos | `corpus/maisa/facturas_lote2/` | el enunciado |
| Regla nueva (texto) | se traduce a `maisa/config/reglas.toml` | nosotros |
| Estado del ERP del sabado | `/tmp/asientos.json` (snapshot) o el bridge vivo | organizacion |

`valida_entrega.py` busca el corpus del lote 2, por este orden, en
`facturas_lote2`, `lote2` y `facturas_extra` (funcion `corpus_lote2_por_defecto`).
Si el directorio no se llama asi, hay que pasarlo a mano con `--corpus-lote2`.

---

## 1. Primero se decide la regla, y se decide **en seco**

Antes de escribir nada, se pregunta al simulador que pasaria:

```bash
cd maisa
# "¿y si la tolerancia pasara de 1 centimo a 5?"
PYTHONPATH=src ../.venv/bin/python tools/oro.py --simula regla tolerancia_importe=0.05

# "¿y si un pedido ya PAGADO dejara de ser NO_PAGAR?"
PYTHONPATH=src ../.venv/bin/python tools/oro.py --simula regla reglas.R5_estado.si_ya_pagada=ESCALAR
```

La salida dice **cuantas** de las 500 se mueven y, factura a factura, **que regla**
cambia de veredicto (`-`/`+` sobre la traza de hechos). Ver `docs/simulador.md`.

Criterio de aceptacion de la regla nueva:

1. Si **no mueve ninguna** de las 500 de La Caja: se aplica y el lote 1 queda intacto.
2. Si **mueve alguna**: es una decision explicita, no un efecto colateral. Hay que
   elegir y dejarlo escrito: (a) el lote 1 se queda con `norma_v3` congelada y solo
   el lote 2 se emite con la norma nueva, o (b) se reemite el lote 1 y se dice por que.
   La recomendacion por defecto es (a): la organizacion registra el commit del lote 1
   a las 10:30 y **un cambio de norma no debe reescribir un resultado ya entregado**.
3. Si mueve **mas del 10 %** del lote: sospechar de la regla antes que del motor.
   El simulador dice exactamente por que regla, asi que el debate se hace con
   evidencia y no con opiniones.

---

## 2. Escribir la regla en `reglas.toml` (datos, no codigo)

```toml
version = "norma_v4"          # 1) subir SIEMPRE la version
fuente  = "<enunciado del sabado>"

[umbrales]
nuevo_umbral = 0.05           # 2) el numero nuevo, con su nombre de negocio

[reglas.R7_lo_que_sea]
descripcion = "lo que dice el enunciado, en una frase"
si_falla    = "ESCALAR"       # 3) que hacer cuando NO se cumple
si_pasa     = "PAGAR"         #    (si aplica)
```

- **Subir `version` es obligatorio.** Toda decision y toda traza declaran con que
  version se tomaron (`Decisor` -> `version_norma`), asi que un cambio de version
  invalida en cascada lo que dependa de ella sin recompilar nada.
- Si la regla nueva describe un **hecho que no admite duda** (pagar dos veces,
  factura duplicada), va en `[hechos_duros]`, no en `[reglas]`: los hechos duros
  no escalan, niegan (`NO_PAGAR`). Esa distincion esta en `reglas.toml` y en la seccion 2.3 del plan.
- Los valores de `si_falla`/`si_pasa` tienen que existir en `[precedencia]`
  (`PAGAR` < `ESCALAR` < `NO_PAGAR`). El simulador rechaza cualquier otro valor
  antes de que llegue al motor.
- **No se toca Python.** `norma.Politica.carga` lee el TOML; `Decisor` solo
  consulta `politica_de(regla, clave)`. Si para expresar la regla hace falta
  codigo nuevo, es que la regla no esta bien entendida todavia.
- Toda regla nueva necesita un `motivo` legible: es lo que sale en la traza y lo
  que el humano lee al escalar.

---

## 3. Ejecutar el lote 2

```bash
cd maisa
PYTHONPATH=src ../.venv/bin/python -m maisa.procesa \
    --facturas ../corpus/maisa/facturas_lote2 \
    --salida /tmp/out/outcomes_lote2.jsonl \
    --lote 2 --trabajadores 4 --traza-hash
```

- **Que se reejecuta:** solo el lote 2. El motor no guarda estado entre facturas y
  la traza es un fichero por lote, asi que la traza del lote 1 no se toca.
- **`--lote 2`** etiqueta los eventos de la traza (`lote: 2`) para que los dos lotes
  se puedan auditar juntos sin confundirlos. El nombre del fichero lo pone `--salida`.
- **Coste esperado (medido en `docs/capacidad.md`):** 40 facturas con capa de texto
  son ~0,3 s. Cada escaneo **nuevo** anade ~4,1 s de OCR porque el contenedor atiende
  de una en una. Las escaneadas ya vistas estan en la cache por `sha256` y cuestan
  ~3 ms. Si las 40 son de texto, el lote 2 entero cuesta menos que un cafe.

---

## 4. Validar antes de dar por bueno

```bash
cd maisa
PYTHONPATH=src ../.venv/bin/python tools/valida_entrega.py ../entrega --publicable \
    --corpus-lote2 ../corpus/maisa/facturas_lote2
```

- **No omitas `--corpus-lote2`.** Sin el, la cobertura del lote 2 no se contrasta y
  el validador solo mira que el fichero exista: un `outcomes_lote2.jsonl` con 3
  lineas de 40 pasaria.
- `--publicable` anade lo que la spec exige del repositorio: raiz con **solo**
  los tres ficheros de la entrega, y que no se haya colado codigo,
  ejecutables ni credenciales.
- El lote 2 debe tener **una linea por factura**, sin duplicados, con `result`
  dentro del enum `PAGAR` / `NO_PAGAR` / `ESCALAR`.
- Un `outcomes_lote2.jsonl` **vacio es un bloqueante**, y con razon: la spec pide
  que la raiz contenga los tres ficheros y el jurado ejecuta su verificador sobre
  los dos JSONL.

---

## 5. La forma de la linea: decidida, dos claves

La entrega lleva **exactamente `file_id` + `result`**, en las 500 lineas del lote 1
y en las del lote 2. Antes el motor anadia `motivos` cuando los habia y la entrega
mezclaba dos formas (439 lineas de dos claves y 61 de tres); eso ya no ocurre:
`emit.linea()` no acepta campos extra, asi que la mezcla no puede volver por
descuido.

La explicabilidad no se pierde: los motivos, los hechos y los campos leidos siguen
en la traza (`outputs/outcomes_traza.jsonl`, y con `--traza-hash` la version
encadenada por hash), que es material de auditoria y del pitch. Se separa asi lo que
se **valida** (la entrega, de forma binaria) de lo que se **explica** (la traza),
que es justo lo que pide la spec: `file_id` y `result` son lo unico obligatorio.

---

## 6. Versionar

1. `maisa/config/reglas.toml` con `version = "norma_v4"`: es el artefacto que explica
   el cambio, y va en el repositorio de trabajo (no en el de entrega).
2. Commit en la raiz del repo con los tres ficheros, y anotar en el mensaje la version de
   la norma y el **sello de la traza** del lote 2 (`trace.sello`), que es el ancla
   que permite demostrar despues que la traza no se toco.
3. Si el plan cambia (nueva regla = nuevo ADR), regenerar el PDF:
   `tools/md_a_pdf.py docs/albertitos_plan.md /tmp/albertitos_plan.pdf`.
4. Si el lote 1 se reemite (opcion (b) del paso 1), volver a pasar
   `oro.py --fijar` para congelar la nueva referencia; si no, `--comprobar` avisara
   de una deriva que ya no es un fallo sino un cambio de norma.

---

## 7. Ensayo en seco (checklist con cronometro)

| # | Paso | Comando | Criterio de OK |
|---|---|---|---|
| 1 | Predecir el efecto de la regla | `oro.py --simula regla ...` | se sabe cuantas facturas mueve |
| 2 | Editar la norma | `$EDITOR config/reglas.toml` | `version` subida |
| 3 | No romper el lote 1 | `oro.py --comprobar` | exit 0, o deriva explicada por el paso 1 |
| 4 | Correr el lote 2 | `python -m maisa.procesa --lote 2 ...` | exit 0 y una linea por PDF |
| 5 | Validar la entrega | `valida_entrega.py --publicable --corpus-lote2` | exit 0 |
| 6 | Comprobar la traza | `trace.verifica(outcomes_lote2_traza.jsonl)` | sin problemas |
| 7 | Commit | `git commit` en la raiz del repo | raiz con los tres ficheros + `maisa/` |

---

## 8. Que puede salir mal (y que ya esta probado)

Cada fila apunta a la evidencia ejecutada de `docs/resiliencia.md`.

| Sintoma | Causa | Que se hace |
|---|---|---|
| El pipeline no arranca: `ModuleNotFoundError` | el hijo se queda sin el `site-packages` del entorno virtual | `PYTHONPATH` incluye `src` **y** el `site-packages`; el arnes ya lo hace solo |
| `ErrorERP: SES-401` y no se crea la salida | no hay snapshot y el bridge no responde | usar `--snapshot /tmp/asientos.json` (516 asientos) y seguir; **nunca** entregar un JSONL a medias |
| `ConnectionError` en una lectura | contenedor OCR caido | levantar el contenedor; lo ya visto se sirve de cache en ~3 ms |
| La traza del lote 2 acusa `cadena_rota` | se escribio encima de una traza rota | `trace.Registro` se niega a continuar y dice la linea exacta; regenerar el lote |
| `sello_distinto` | alguien reescribio la traza de forma coherente | el sello publicado delata la reescritura aunque `verifica()` pase |
| `outcomes_lote2.jsonl` con menos lineas que PDF | el lote 2 se corto | `--publicable --corpus-lote2` lo dice: no se publica |

---

## 9. Rollback

- **La norma:** `reglas.toml` esta versionado. Volver a `norma_v3` es revertir un
  fichero de datos; no hay que recompilar ni desplegar nada.
- **El lote 1:** no se toca. Su `outcomes.jsonl` esta congelado y su deriva se mide
  con `oro.py --comprobar`.
- **La cache de OCR:** es regenerable por `sha256`. Si se corrompe, se borra y el
  sistema vuelve a pagar vision una vez por documento; no hay dato de negocio dentro.
