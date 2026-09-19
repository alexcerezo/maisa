# Resiliencia y recuperación — Maisa

**Rúbrica:** Resiliencia y recuperación (10 puntos).

**Cómo se reproduce todo, con un solo comando:**

```bash
cd maisa
# con el bridge ERP de Alberto CAIDO (caso normal): 61/61 OK, exit 0
PYTHONPATH=src:/home/ubuntu/projects/PaddleOCR/.venv/lib/python3.12/site-packages \
    python3 tools/evidencia_resiliencia.py

# con el bridge levantado (python3 corpus/maisa/alberto_erp.py): 68/68 OK, exit 0
PYTHONPATH=src:/home/ubuntu/projects/PaddleOCR/.venv/lib/python3.12/site-packages \
    python3 tools/evidencia_resiliencia.py --erp-vivo
```

La herramienta tarda ~4 min (incluye un lote frío de 234 s de OCR real). `--rapido` recorta
el lote. `--erp-vivo` añade 7 comprobaciones contra el bridge de Alberto en
`http://127.0.0.1:8009` (bloque H); sin él, la herramienta mide todo lo que no necesita ERP,
incluido el caso "ERP caído" (bloque D, que exige el bridge **apagado**).

La idea que sostiene el documento: **el motor no intenta ser infalible, intenta fallar de
forma declarada y recuperable**. Nunca publica una decisión a medias, nunca convierte un
error en un valor por defecto silencioso, y todo lo que decide queda en una traza que se
puede auditar línea a línea.

---

## 0. Mapa de escenarios

| # | Escenario | Síntoma si no se tratara | Mecanismo | Evidencia |
|---|-----------|--------------------------|-----------|-----------|
| 1 | El proceso muere a mitad de lote (SIGKILL) | Lote perdido; traza truncada ilegible | Escritura con `flush` línea a línea + `Registro(continuar=True)` | §1 · bloque A — 3 SIGKILL, prefijos de 65/61/60 líneas sin `cadena_rota`, reanudación 68 = 65 + 3 sin hueco de `seq` |
| 2 | Alguien edita la traza para cambiar una decisión | El log dice `PAGAR` y nadie lo nota | Cadena de hashes + sello publicado | §2 · bloques B y C — `hash_roto` en la línea exacta; la reescritura coherente la delata el sello |
| 3 | ERP devuelve `ORA-00600` | Caída del lote | Reintento con backoff, presupuesto acotado | §3.1 · H — 3 reintentos en la descarga completa, 0 errores |
| 4 | ERP devuelve `ERP-429` (límite 10 req/s) | Caída por ráfaga | Freno de cliente a 8 req/s + respeto de `Retry-After` | §3.2 · H4 — 73 esperas 429, 0 errores internos |
| 5 | La sesión ERP caduca (900 s / 300 usos) | El lote se queda sin datos a mitad | Renovación preventiva + `SES-401 → login → reintento` | §3.3 · H1–H3 — 320 consultas, 1 relogin en la 271, 0 fallos |
| 6 | El ERP entero está caído | No se puede decidir | Snapshot en disco + fallo declarado si no hay ninguno | §3.4 · D y H5 — 516 asientos del snapshot, exit 0; sin snapshot, `ErrorERP` exit 1 y **sin** JSONL |
| 7 | Un PDF está corrupto | *(hoy: el lote entero muere)* | **Fail-stop** (no decide mal) — mitigación propuesta | §4 · F — `PdfStreamError`, exit 1, 0 ficheros escritos |
| 8 | La caché de OCR se corrompe | Texto basura tomado por bueno | `sha256` del PDF en la entrada + reconstrucción | §5.1 · G — JSON roto y sha falso se rehacen solos; el texto manipulado **sí** pasa |
| 9 | El servicio de OCR no responde | Documento "vacío" tomado por bueno | `ConnectionError` ruidoso, jamás texto vacío | §5.2 · E0 — excepción declarada; con caché, 2,6 ms |
| 10 | Se pierde la caché de OCR | 863 s de OCR en cada arranque | Caché reconstruida byte a byte | §5.3 · E — frío 234,55 s vs caliente 4,14 s (×56,6), 29/29 idénticas |
| 11 | Cambia un dato (lo que pide el sábado) | Miedo a tocar el motor | Modo `--simula` de `tools/oro.py` | `docs/simulador.md` |

---

## 1. Muerte a mitad del lote (SIGKILL) y reanudación

**Síntoma.** El contenedor se reinicia, el OOM killer entra, alguien hace `kill -9`. Un
proceso que escribe su salida de golpe al final pierde el lote entero.

**Mecanismo.** Dos piezas, ninguna nueva:

1. El pipeline escribe **línea a línea con `flush`** (`trace.Registro` y `emit.linea`), así
   que una muerte brusca deja un **prefijo legible**, no un fichero a medias. La última
   línea siempre acaba en salto de línea.
2. `Registro(ruta, continuar=True)` **reanuda sobre un prefijo válido**: carga los eventos
   ya escritos, arranca el `seq` donde estaba y encadena `hash_prev` con la cabeza
   anterior. Si el prefijo estuviera roto, no reanuda: lanza `TrazaError`.

**Evidencia** (`tools/evidencia_resiliencia.py`, bloque A). Tres lotes completos de 500
facturas matados con `SIGKILL` a mitad:

| Intento | Eventos escritos | Exit | s | Tipos escritos | `verifica` | `outcomes.jsonl` |
|---------|-----------------|------|---|----------------|-----------|------------------|
| 1 | 65 | −9 | 4,55 | `decision 32, lectura 32, lote 1` | ninguno | no existe |
| 2 | 61 | −9 | 4,41 | `decision 30, lectura 30, lote 1` | ninguno | no existe |
| 3 | 60 | −9 | 4,46 | `decision 29, lectura 30, lote 1` | ninguno | no existe |

Y sobre el prefijo de 65 líneas:

```
prefijo de partida                 65 lineas, sello 9f1153b1b2fbd55f...
eventos cargados al reanudar       65
primer seq nuevo                   65
hash_prev del primer evento nuevo  9f1153b1b2fbd55f...     <- es la cabeza anterior
lineas tras reanudar               68
problemas tras reanudar            ninguno
sello final                        817543b8e0a4da16e4f1648ae3dd48440262ca11f194b71e1867ef62eb37a7d6
```

Lo que demuestra:

- El prefijo **no acusa `cadena_rota`**: lo que se escribió, se escribió bien.
- No hay evento `fin` → se distingue un lote completo de uno interrumpido sin mirar nada más.
- El `seq` **continúa sin hueco** (65 tras 64) y el primer evento nuevo encadena con el
  hash de la cabeza anterior: la traza reanudada es **una sola cadena**, no dos pegadas.
- El prefijo **no se reescribe** (comprobado byte a byte).
- `outcomes.jsonl` no existe hasta que el lote termina: **nunca hay una entrega parcial
  publicada por accidente**. Un consumidor que lea el fichero no puede confundir un lote
  truncado con un lote bueno.
- Continuar sobre una traza **ya rota** no se permite: `TrazaError: la traza ya esta rota
  (1 problemas, el primero: linea 3: hash_roto ...)`.

**Coste de la recuperación.** Se reejecuta el lote (4,14 s con la caché caliente) o se
reanuda desde el prefijo. No hay trabajo perdido más allá de los milisegundos del
intervalo de `flush`.

---

## 2. Traza manipulada

**Síntoma.** Alguien con acceso al fichero cambia `PAGAR` por `NO_PAGAR` en una línea, o
reescribe la traza entera para que cuadre. El riesgo real no es el fichero: es que la
decisión publicada ya no sea la que el motor tomó.

**Mecanismo.** Cada evento lleva `hash_prev` + su propio `hash` (que cubre `hash_prev`,
`seq`, `ts`, `tipo`, `file_id`, `datos`). `trace.verifica()` recalcula y compara, y además
comprueba que `hash_prev` es el hash del evento anterior. El **sello** es el hash de la
cabeza, que se publica fuera del log (en la entrega, en el acta).

**Evidencia** (bloques B y C, sobre la traza de referencia de 1002 eventos, sello
`888ee32cc573c259b14668d1fa3a78f3b02e41a8b48168a37ec92ecfcc43254d`):

| Ataque | Detección | Línea señalada |
|--------|-----------|----------------|
| Editar `datos.result` de la línea 3 (`2026-01-08_P001.pdf`: `PAGAR → NO_PAGAR`) | `hash_roto`: declarado `4782f677799d…` ≠ recalculado `d7d43406a04f…` | **3** |
| Cambiar el `hash_prev` de la última línea (1002) | `hash_roto` (`888ee32cc573…` ≠ `3b5afc34a604…`) + `cadena_rota` (`ffffffffffff…` no es el hash anterior `5b026f970e53…`) | 1002 |
| Un byte distinto en la cabeza | `sello_distinto`: `f88ee32cc573…` frente a `888ee32cc573…` | 1002 |
| Añadir un evento legítimo al final | `sello_distinto` (`92ae060c07d6…`); la traza sigue siendo válida | 1003 |
| **Reescritura coherente** (recalcular toda la cadena) | `verifica()` **sin** sello: limpio; **con** el sello publicado (`765c74261b62…` ≠ `888ee32cc573…`): `sello_distinto` | 1002 |
| Tocar una línea del **medio** | `hash_roto` en esa línea (`90283a732485…` ≠ `90d0f527a933…`); el sello **no** cambia | 4 |

Lecturas honestas de la tabla:

- El mensaje **nombra la línea exacta** y los dos hashes, así que un auditor no tiene que
  buscar a mano. La manipulación es **semántica**: el log pasa a decir `NO_PAGAR` para esa
  factura y solo `verifica()` lo delata.
- **Por qué una línea del medio no da `cadena_rota`**: el `hash_prev` de la línea 4 apunta
  al hash *declarado* de la 3, que no ha cambiado. Las dos comprobaciones son
  independientes a propósito; la del medio la caza el hash propio.
- **Límite declarado:** el sello es un **ancla de la cabeza**, no un checksum del fichero.
  Ancla la traza *entera* si se publica fuera del log (que es como se usa). Para el
  contenido línea a línea lo que vale es `verifica()`. Una reescritura coherente pasa
  `verifica()` sin sello y la delata el sello. Está demostrado, no supuesto.
- **La traza sella con qué se decidió, no solo qué se decidió.** El primer evento guarda
  `config`, `xlsx`, `snapshot`, `erp_url`, `version_norma`, `trabajadores` y `lote`. Dos
  corridas con las **mismas 500 decisiones** dan sellos distintos si cambia el origen del
  dato: medido hoy, `58e0e3bf3744b965…` (ERP vivo) frente a `accc0c5cf371a970…` (snapshot),
  y el único campo distinto en el evento `lote` es `erp_url` (`http://127.0.0.1:8009` vs
  `null`) además del `ts`. Es decir: **no se puede reproducir un sello sin reproducir la
  procedencia**, que es exactamente lo que se quiere de un log de auditoría.

---

## 3. ERP: fallos del bridge y caída total

El bridge de Alberto (`corpus/maisa/alberto_erp.py`) es deliberadamente hostil: falla cada
10 consultas con `ORA-00600`, caduca el token a los **900 s o 300 usos**, aplica **10 req/s**
y responde `ERP-429` con `Retry-After`. Todas las medidas de esta sección son contra el
bridge **vivo** en `http://127.0.0.1:8009` (bloque H, `--erp-vivo`).

### 3.1 `ORA-00600` (fallo interno cada 10 consultas)

**Mecanismo.** `_consulta()` reintenta con backoff exponencial acotado
(`sleep(min(4.0, 0.2 * intento) * jitter)`) dentro de un presupuesto de intentos. Si se
agota, **se propaga** `ErrorERP` marcado `reintentable=True`: nunca se devuelve una lista
de asientos incompleta.

**Evidencia** (bloque H, descarga completa de los 516 asientos paginada):

```
asientos descargados               516
segundos                           4.501
telemetria                         {"peticiones_http": 30, "logins": 1,
                                    "reintentos_ora_00600": 3, "esperas_429": 0,
                                    "relogins": 0, "segundos": 4.5, "errores": []}
[OK   ] el lote de asientos se descarga entero pese a los ORA-00600 -- 516 asientos
[OK   ] los ORA-00600 se absorben sin error al llamante -- 3 reintentos
```

516 asientos en 30 peticiones HTTP con 3 `ORA-00600` absorbidos y **0 errores**. El coste
del reintento es invisible al llamante: 4,50 s para todo el ERP.

### 3.2 `ERP-429` (límite de 10 req/s)

**Mecanismo.** El cliente se autolimita a **8 req/s** (80 % del límite, 20 % de margen) con
`_espera_ritmo()`, y cuando aun así recibe 429 respeta `Retry-After` con jitter.

**Evidencia** (bloque H4). Un solo cliente no llega a disparar el 429 (el freno funciona).
El caso interesante es el que rompe el supuesto de "un cliente":

```
-- H4. 8 clientes en paralelo: mas de 10 req/s contra el bridge
segundos                           16.838
peticiones totales                 182
esperas 429                        73
errores internos no recuperados    0
excepciones al llamante            5
[OK   ] el cliente se frena solo cuando el bridge devuelve 429 -- 73 esperas
[OK   ] ningun fallo silencioso: lo que no se recupera se declara -- 5 excepciones
```

8 clientes × 12 consultas = 96 consultas, 182 peticiones HTTP reales, **73 esperas por 429**
y **0 errores internos sin recuperar**. Cinco consultas agotaron su presupuesto de
reintentos y se propagaron como `ErrorERP: ERP-429: rate limit` / `ORA-00600` —
**declaradas al llamante**, no silenciadas. Conclusión operativa: el límite de 10 req/s es
un recurso compartido y el motor lo respeta **por cliente**; para escalar en horizontal hay
que repartir el presupuesto entre procesos (ver `capacidad.md` §7).

### 3.3 Caducidad de sesión: 900 s y 300 usos

**Mecanismo.** Dos capas:

- **Preventiva**: `_token_valido()` renueva a los 870 s o 295 usos, antes de que el
  servidor se queje.
- **Reactiva**: si aun así llega `SES-401`, se hace `login()` y se reintenta la misma
  consulta sin perderla.

**Evidencia** (bloques H1–H3, con el cliente saboteado para que no se adelante):

```
-- H1. token caducado en el servidor -> relogin transparente
relogins                           1
segundos                           4.539
errores                            []

-- H2. caducidad por USOS (300 en el servidor), sin adelantarse
consultas lanzadas                 320
relogins disparados                1
primer relogin en la consulta      271
reintentos ORA-00600               35
esperas 429                        0
excepciones al llamante            0

-- H3. renovacion preventiva contra enterarse por el servidor
peticiones (preventivo: login+consulta) 2
peticiones (reactivo: 401+login+consulta) 4
errores                            []
```

Lectura:

- **320 consultas seguidas y 0 excepciones al llamante**: la caducidad de sesión es
  invisible. Aparece **un** `SES-401` y se resuelve solo. El contador del servidor cuenta
  **todas** las peticiones autenticadas, reintentos incluidos: 271 consultas + 29
  reintentos `ORA-00600` = 300 usos, que es justo donde salta. Saber esto importa para
  dimensionar: **los reintentos consumen presupuesto de sesión**.
- Renovar por adelantado cuesta **2 peticiones** (login + consulta); enterarse por el
  servidor cuesta **4** (401 + login + consulta + reintento) y un error declarado. La capa
  preventiva ahorra una ida y vuelta por cada 300 usos: barato y medido.
- El tiempo (900 s) no se puede esperar en un test, así que se mide el mecanismo: con el
  token envejecido 1000 s, el cliente renueva **antes** de la consulta y no hay ni un 401.

### 3.4 El ERP entero está caído

**Síntoma.** El bridge no está levantado (`Connection refused`), o hay red pero el ERP
tiene un problema real.

**Mecanismo.** El ERP **no es una dependencia dura para decidir**: hay un snapshot en disco
con los 516 asientos. Si no hay snapshot y la URL es inalcanzable, el proceso **falla
declarado**.

**Evidencia** (bloque D, corrida por defecto con el bridge **apagado** — comprobado en d1):

```
-- d1) hay ERP vivo?
   sonda a http://127.0.0.1:8009      sin respuesta ([Errno 111] Connection refused)
   snapshot en disco                  /tmp/asientos.json (81263 bytes)

-- d2) lote completo arrancando del snapshot, con el bridge caido
   codigo de salida                   0
   * facturas   : 500
   * resultado  : {'PAGAR': 443, 'ESCALAR': 48, 'NO_PAGAR': 9}
   * erp        : 516 asientos
   * validacion : OK
   * traza hash : 1002 eventos, OK
   [OK   ] el motor no necesita ERP vivo si hay snapshot -- exit 0
   [OK   ] y ademas funciona apuntando a un ERP que no responde -- el bridge no estaba levantado durante la prueba

-- d3) sin snapshot y con una URL inalcanzable: fallo declarado
   codigo de salida                   1
   segundos hasta fallar              8.16
   ultima linea de la salida          maisa.erp.ErrorERP: SES-401: no se pudo obtener token tras varios intentos
   fichero de salida                  NO se creo
```

Es decir: **el ERP caído no tumba la decisión** (snapshot + exit 0 + validación OK), y si no
hay con qué decidir **no se inventa una entrega**: exit 1, excepción con código legacy
(`SES-401`), sin `outcomes.jsonl` vacío. `ErrorERP` es capturable sin mirar el texto
(`d4`: `reintentable=False`) y trae el código del ERP.

**Contraste snapshot vs ERP vivo** (bloque H5, mismo motor y misma `norma_v3.1`):

```
-- H5. el mismo lote contra el ERP vivo y contra el snapshot
exit (vivo / snapshot)             0 / 0
facturas comparadas                500
facturas con otra decision         0
```

**0 diferencias** en las 500 decisiones entre leer el ERP vivo y leer el snapshot. El bridge
es determinista: el snapshot es un sustituto fiel, no una aproximación. (Los sellos difieren
por `erp_url` y `ts`, como se explica en §2.)

---

## 4. Un PDF está corrupto

**Evidencia** (bloque F): dos PDF rotos de verdad —uno truncado al 33 %, uno de 0 bytes— en
un lote de 3 junto a uno bueno.

```
PDF bueno                          2026-01-08_P001.pdf (1904 bytes)
PDF truncado                       rota_truncada.pdf (634 bytes)
PDF vacio                          rota_vacia.pdf (0 bytes)
lectura.lee(rota_truncada.pdf)     pypdf.errors.PdfStreamError: Stream has ended unexpectedly
lectura.lee(rota_vacia.pdf)        pypdf.errors.EmptyFileError: Cannot read an empty file
lote de 3 (1 bueno + 2 rotos): exit 1
outcomes.jsonl escrito             False
eventos en la traza del lote fallido 1
tipos de evento                    {'lote': 1}
problemas de verifica              ninguno
[OK   ] un PDF corrupto hace fallar el proceso (fail-stop) -- exit 1
[OK   ] no se publica un outcomes.jsonl a medias
[OK   ] el fallo es ruidoso y dice el fichero
[OK   ] la traza del lote interrumpido queda integra y sin `fin` -- 1 eventos

-- recuperacion: apartar el fichero roto y reejecutar
mismo lote sin los rotos: exit     0
facturas emitidas                  1
```

**Síntoma.** Un PDF truncado (descarga a medias) o de 0 bytes.

**Qué pasa hoy.** `lee_lote()` usa `pool.map` sin guarda por fichero, así que la excepción
de `pypdf` **sube y mata el lote entero**: exit 1, y las otras 499 facturas buenas no se
emiten. El lado bueno es que es un **fail-stop limpio**: no se decide mal, no se escribe
`outcomes.jsonl`, la traza del lote fallido queda con su único evento `lote` **íntegra y sin
`fin`** (se distingue de un lote bueno), y el error dice el fichero y el motivo.

**Mitigación propuesta** (quirúrgica, en `lectura.lee_lote`, 6 líneas — **no aplicada**
porque `src/maisa/*` está fuera del alcance de este trabajo):

```python
def _lee_seguro(ruta: Path, umbral: float) -> Documento:
    try:
        return lee(ruta, umbral)
    except Exception:                            # PDF ilegible, no una factura rara
        return Documento(lectura=Lectura(file_id=ruta.name, paginas=0,
                                         metodo="ilegible", texto_ilegible=True),
                         sha256="", escalon="ilegible", cache=False,
                         segundos=0.0, calidad=0.0)   # -> la norma lo manda a ESCALAR
```

Con eso el lote sigue (499 decisiones + 1 `ESCALAR` con motivo `documento ilegible`) en vez
de perderlo entero. Mientras no esté, la recuperación operativa es la que demuestra el
bloque F: **apartar el fichero y reejecutar** — el pipeline es idempotente (mismo lote →
mismas decisiones) y la caché de OCR no se ensucia, porque el fallo ocurre antes de
escribir nada.

**Por qué es aceptable aun así para el reto:** el fallo es ruidoso y localizable
(`PdfStreamError` + nombre de fichero), no silencioso; y en el peor caso se pierde tiempo,
nunca corrección.

---

## 5. Caché de OCR: corrupción, caída y pérdida

La caché vive en `maisa/.cache/ocr`, indexada por **`sha256` del PDF** (un fichero
renombrado no vuelve a pagar OCR) y contiene `{"sha256": …, "texto": …}`.

### 5.1 Caché corrupta o manipulada

**Evidencia** (bloque G):

```
PDF en cache                       copia_2026_0518.pdf
entrada                            b738972687b4819d34799d86da87fd279194b805a43aa7d6a5d0a2610e6692d2.json (338 bytes)
(a) JSON truncado                  escalon=vision_ocr cache=False 15.85s
(b) sha256 que no cuadra           escalon=vision_ocr cache=False 4.37s
decision con la cache legitima     ESCALAR
motivos con la cache legitima      ['documento no legible: el escaneo no permite leer el IBAN de abono',
                                    'NIF del emisor no legible', 'IBAN no legible',
                                    'total de factura no legible', 'fecha no legible']
(c) texto cambiado, sha intacto    escalon=cache_ocr cache=True 0.01s
decision con la cache manipulada   ESCALAR
motivos que publica                ['pedido no identificable']
entrada de cache restaurada        True
```

| Caso | Resultado | ¿Se recupera? |
|------|-----------|----------------|
| (a) JSON truncado a la mitad | `json.JSONDecodeError` capturado → se rehace por OCR (15,85 s) | **Sí, sola** |
| (b) JSON válido con `sha256` falso | La entrada no cuadra con el PDF → se rehace por OCR (4,37 s) | **Sí, sola** |
| (c) JSON válido, `sha256` correcto, **texto cambiado** | Se lee como `cache_ocr` en 0,01 s | **No** |

El caso (c) es un **límite declarado**, no un olvido: la caché está anclada al `sha256` del
**PDF**, no al del **texto**. Quien pueda escribir en `.cache/ocr` puede cambiar lo que el
motor *lee*. Y el daño no es solo la etiqueta — es el **motivo**: la misma decisión
`ESCALAR` sale con una justificación **falsa** (`pedido no identificable` donde la verdad
era `documento no legible: el escaneo no permite leer el IBAN de abono`). Un auditor que lea
el motivo ve algo que no pasó.

**Arreglo propuesto** (2 líneas, misma idea que la cadena de la traza): añadir
`sha256_texto` a la entrada y rechazarla si no cuadra — así (c) se comporta como (b) y se
rehace sola. **No aplicado**: `src/maisa/*` está fuera de alcance.

### 5.2 El servicio de OCR no responde

**Mecanismo.** El escalón de visión lanza `requests.ConnectionError`; **jamás** devuelve
texto vacío, porque un texto vacío se convertiría en una factura "sin datos" y acabaría en
una decisión inventada.

```
-- E0. SERVICIO DE OCR INALCANZABLE
   con el OCR inalcanzable            requests.exceptions.ConnectionError
   mensaje                            HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded...
   [OK   ] el escalon de vision falla de forma ruidosa, no devuelve texto vacio
   el mismo PDF con la cache          escalon=cache_ocr cache=True 2.6 ms
   [OK   ] con la cache presente el PDF se lee sin tocar el servicio
```

Y si el OCR cae en mitad de un lote, la caché es lo que salva la entrega: los 471 PDF con
capa de texto no lo tocan nunca y los 29 escaneados ya están en caché (2,6 ms cada uno).

### 5.3 Pérdida de la caché

```
-- E. OCR CAIDO / CACHE: LOTE CALIENTE CONTRA LOTE FRIO
   entradas de cache antes            29
   segundos (lote frio)               234.55
   llamadas al servicio OCR (frio)    29
   segundos dentro de OCR (frio)      863.73
   entradas de cache reconstruidas    29
   segundos (lote caliente)           4.14
   llamadas al servicio OCR (caliente) 0
   factor de aceleracion              56.6x
   [OK   ] el contenido reconstruido es identico byte a byte
   [OK   ] la cache original queda restaurada byte a byte -- 29 entradas
```

La caché **se reconstruye sola** al primer lote frío y el resultado es **idéntico byte a
byte**: la pérdida de la caché cuesta 234,55 s una vez (29 llamadas de OCR), no corrección.
Nótese la aritmética de la concurrencia: 863,73 s de OCR dentro de 234,55 s de pared → el
escalón de OCR sí va en paralelo, aunque el contenedor sea el cuello (ver `capacidad.md` §5).
El número absoluto depende de la carga del contenedor de OCR en ese momento (mediciones del
mismo día: 143,42 s y 234,55 s); lo estable es la **forma**: 0 llamadas en caliente frente a
29 en frío, y ×31,5 a ×56,6 de factor.

---

## 6. Límites honestos (lo que no está cubierto)

1. **Un PDF corrupto mata el lote** (§4). Fail-stop limpio, pero mata el lote. Arreglo
   propuesto en 6 líneas, no aplicado.
2. **La caché de OCR no está firmada** (§5.1c): quien escriba en `.cache/ocr` puede cambiar
   el texto leído y el motivo que se publica. Arreglo propuesto, no aplicado.
3. **El sello ancla la cabeza, no el fichero** (§2): una reescritura coherente pasa
   `verifica()` sin sello. Por eso el sello se publica **fuera** del log; si no se publica,
   solo queda `verifica()`.
4. **No hay reintento del lote entero** a nivel de proceso: si el proceso muere, alguien (o
   el orquestador) tiene que relanzarlo. La reanudación existe (`continuar=True`) pero **no
   está cableada al CLI de `procesa.py`**: hoy se reejecuta el lote, que cuesta 4,14 s
   caliente. A 50 000 facturas esto deja de ser gratis (ver `capacidad.md` §6).
5. **El presupuesto de 429 es por cliente**: 8 clientes en paralelo se pisan entre sí
   (§3.2). Un escalado horizontal necesita un reparto explícito del límite.
6. **La sesión ERP caduca por peticiones, no por consultas**: los reintentos consumen usos
   (§3.3). Con muchas réplicas, la renovación preventiva debe ir por réplica.

---

## 7. Guion de 90 segundos (para el pitch)

1. **"El motor no decide si no puede decidir."** Un PDF corrupto no produce una factura
   vacía: exit 1 y ni un `outcomes.jsonl` a medias. *(§4)*
2. **"Nada se escribe a medias."** `kill -9` a mitad de lote: el prefijo queda íntegro, sin
   `cadena_rota`, y se reanuda encima encadenando el hash (68 = 65 + 3, `seq` sin hueco).
   Nunca hay entrega parcial publicada. *(§1)*
3. **"El log se puede auditar."** Cambiar una decisión en la traza señala la línea exacta; y
   si alguien reescribe toda la cadena, el sello publicado lo delata. *(§2)*
4. **"El ERP se puede caer."** 516 asientos con `ORA-00600` cada 10 consultas y 73 esperas
   por 429, con 0 errores internos; y con el ERP caído, el snapshot da las **mismas 500
   decisiones**. *(§3)*
5. **"Y lo sabemos porque lo medimos."** `tools/evidencia_resiliencia.py` → **61/61 OK** con
   el bridge apagado y **68/68 OK** con `--erp-vivo`, exit 0 en ambos casos; y cada límite de
   este documento está declarado con el comando que lo demuestra.

---

## Anexo: procedencia de las medidas

| Qué | Bloque | Herramienta |
|-----|--------|-------------|
| Lote de referencia (4,14 s, 1002 eventos, sello `888ee32cc573…`) y caché caliente/fría | 0 y E | `tools/evidencia_resiliencia.py` |
| Muerte a mitad de lote y reanudación | A | ídem |
| Manipulación de la traza y sello | B y C | ídem |
| ERP caído, snapshot y fallo declarado | D | ídem (con el bridge **apagado**) |
| OCR inalcanzable | E0 | ídem |
| PDF corrupto | F | ídem |
| Caché corrupta o manipulada | G | ídem |
| `ORA-00600`, `ERP-429`, caducidad de sesión, vivo vs snapshot | H (H1–H5) | ídem con `--erp-vivo` y `python3 corpus/maisa/alberto_erp.py` |

Corridas finales: **2026-09-19 11:59–12:03**, `61/61 OK` (por defecto, bridge caído) y
**11:52–11:53**, `68/68 OK` (`--erp-vivo`), exit 0 en ambos casos. Motor al medir:
`norma.py 6867fbece01a08a5` · `lectura.py 4e66b880a52ad6bc` · `texto.py 5501ed225899ceb3` ·
`procesa.py 50ad16f668db89cd` · `emit.py 574f0cb8977eef6e` · `erp.py 4363333f2cf94423` ·
`trace.py 382350ad94ced8cd` · `reglas.toml a1933ebcde417995` (**norma_v3.1**). Si el motor
cambia, los números de este documento cambian con él: vuelve a lanzar el comando.

> Nota de honestidad: una corrida anterior del mismo día (11:23) daba
> `{'PAGAR': 448, 'ESCALAR': 43, 'NO_PAGAR': 9}` porque el motor aún no era `norma_v3.1`.
> Las cifras de este documento son de la versión actual y se reproducen con el comando de
> arriba.
