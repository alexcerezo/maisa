# Capacidad y coste: lo medido, no lo prometido

Generado por `maisa/tools/bench.py` el 2026-09-20T08:02:12+0000 sobre 2 vCPU / 11.65 GB. Todos los tiempos son de pared, medidos con `time.perf_counter()` en esta maquina y con la carga registrada en `docs/bench.json`.

> Regla de la casa: si una cifra no esta en la columna *medido*, es una extrapolacion y va marcada como tal.

## 1. Lote completo de 500 (cache caliente) por reparto y trabajadores

3 repeticiones por configuracion, end-to-end (`python -m maisa.procesa`), escribiendo `outcomes.jsonl` y contando las lineas emitidas. Se miden los **dos repartos** de la lectura en la misma sesion y alternandose dentro de cada numero de trabajadores (`hilos` es el reparto historico; `procesos` reparte el directorio entre procesos), para que la deriva de carga de una maquina compartida no se confunda con la mejora.

### Reparto: `hilos`

| trabajadores | n | min (s) | mediana (s) | max (s) | desv. típ. | rango rel. | facturas/s (mediana) |
|---|---|---|---|---|---|---|---|
| 1 | 3 | 11.03 | **11.60** | 14.82 | 2.04 | 32.6 % | 43.1 |
| 2 | 3 | 9.22 | **9.94** | 10.36 | 0.57 | 11.4 % | 50.3 |
| 4 | 3 | 7.82 | **8.14** | 12.12 | 2.40 | 52.8 % | 61.4 |
| 8 | 3 | 7.54 | **7.77** | 8.55 | 0.53 | 12.9 % | 64.3 |

### Reparto: `procesos`

| trabajadores | n | min (s) | mediana (s) | max (s) | desv. típ. | rango rel. | facturas/s (mediana) |
|---|---|---|---|---|---|---|---|
| 1 | 3 | 8.59 | **10.93** | 12.00 | 1.75 | 31.2 % | 45.7 |
| 2 | 3 | 6.98 | **9.44** | 10.01 | 1.61 | 32.2 % | 53.0 |
| 4 | 3 | 4.70 | **5.35** | 5.85 | 0.58 | 21.5 % | 93.4 |
| 8 | 3 | 3.45 | **4.31** | 4.89 | 0.72 | 33.3 % | 116.1 |

- `hilos`: mejor en **8 trabajador(es)** con **7.77 s** (64.3 facturas/s).
- `procesos`: mejor en **8 trabajador(es)** con **4.31 s** (116.1 facturas/s).

**La comparacion es la medida.** A igualdad de trabajadores, el reparto por procesos hace el trabajo de CPU en paralelo de verdad; los hilos de Python no reparten `pypdf` entre nucleos porque comparten GIL. El contraste de las dos tablas de arriba, medidas alternandose, es la evidencia de que el GIL era el cuello y de cuanto se ha recuperado.

- Mejor configuracion medida: **--modos procesos --trabajadores 8** con **4.31 s** de mediana (116.1 facturas/s) y una dispersion de 33.3 % entre pasadas.
- Con `--traza-hash` (auditoria encadenada de 1002 eventos): **6.66 s** en una pasada a 4 trabajadores, +2.35 s sobre la mediana sin traza.

## 2. Donde se va el tiempo (desglose por fase, 4 trabajadores)

| fase | segundos | reparto |
|---|---|---|
| cargar Excel + snapshot (fijo) | 0.332 | 6.7 % |
| leer los 500 PDF (texto + caché OCR) | 4.090 | 82.8 % |
| decidir las 500 facturas | 0.517 | 10.5 % |
| emitir el JSONL | 0.002 | 0.0 % |
| **total** | **4.941** | 100 % |

- Decidir una factura cuesta **1.03 ms** (regex + aritmetica + precedencia; sin modelo de lenguaje).

## 3. Reparto capa_texto vs OCR (de la traza, campo `escalon_lectura`)

| escalón | facturas | reparto |
|---|---|---|
| capa_texto (sin OCR) | 471 | 94.2 % |
| OCR necesario (caché o servicio) | 29 | 5.8 % |
| **total** | 500 | 100 % |

- La capa de texto resuelve **471 facturas a 8.76 ms/factura** (114 facturas/s) con 4 trabajadores: es exacta y no cuesta servicio externo.
- Una escaneada **ya vista** (servida de cache por sha256) cuesta **7.78 ms/factura**, es decir 1.1 veces menos que un escaneo nuevo: ese es el ahorro que compra la cache.
- Detalle de escalones en el lote: `{'capa_texto': 471, 'cache_ocr': 29}`.

## 4. OCR en frio (cache temporal, PDFs copiados a temporal)

| configuración | facturas | total (s) | s/factura (mediana) | min | max | facturas/s |
|---|---|---|---|---|---|---|
| serial (1 hilo) | 10 | 38.65 | **3.88** | 3.32 | 4.45 | 0.259 |
| 4 hilos | 10 | 38.88 | **14.87** | 8.06 | 18.39 | 0.257 |

- Servicio OCR directo (`POST http://127.0.0.1:8866/ocr`, sin nuestro pipeline): **4.35 s/factura** de mediana (min 3.49, max 4.68, n=5).
- Eso es un techo de **827 facturas escaneadas/hora por ranura** de OCR.
- Paralelizar el escalon OCR de 1 a 4 hilos acelera **x0.99**, luego el contenedor **no** paraleliza: atiende de una en una. Consecuencia: subir `--trabajadores` no compra OCR; se compra con mas ranuras de OCR o con la cache.
- La cache real (`maisa/.cache/ocr`) no se ha tocado: la medida usa un directorio de cache de `tempfile` y copias de los PDFs en temporal.

## 5. Coste unitario por factura, escalon a escalon

Lo que cuesta **una** factura segun por donde entre. Es la tabla que convierte el modelo de coste en una decision de negocio: la palanca no es *procesar mas rapido*, es **no llamar al OCR cuando la capa de texto ya es exacta**.

| escalón de lectura | coste OCR | tiempo de pared | vCPU·s de OCR | recurso externo |
|---|---|---|---|---|
| capa de texto (94,2 % del lote) | 0 | 8.76 ms | 0 | ninguna |
| escaneada ya vista (cache sha256) | 0 | 7.78 ms | 0 | disco local |
| escaneada nueva (OCR en frio) | 3.88 s | 3.88 s | 3.88 vCPU·s | contenedor OCR |

- Factura con capa de texto: **0 vCPU·s de OCR** y 8.76 ms de CPU. El motor de reglas anade 1.03 ms. Es el 94,2 % del lote.
- Escaneada nueva: **3.88 vCPU·s de OCR** por factura. Es 443 veces el coste de una factura de texto. Ahi esta todo el gasto del sistema.
- Escaneada ya vista: **0 vCPU·s** (la cache es por `sha256` del PDF, no por nombre) y 7.78 ms. Reejecutar el lote completo es gratis: por eso el escenario del sabado no asusta.

## 6. Extrapolacion a 5.000, 50.000, 1.000.000 de facturas

**Todo lo de esta seccion es extrapolado, no medido.** Modelo explicito:

```
T(N) = t_fijo + N * (f_texto*c_texto + f_ocr*c_ocr_del_regimen/S_ocr + c_decision)   [S_ocr solo si el termino es OCR en frio]
```

- `A_estacionario`: c_ocr_del_regimen = c_cache_ocr (la escaneada ya esta en cache)
- `B_lote_nuevo`: c_ocr_del_regimen = c_ocr_frio, dividido por S_ocr si paraleliza
- `C_peor_caso`: f_texto = 0, f_ocr = 1 y c_ocr_del_regimen = c_ocr_frio

| constante | valor | origen |
|---|---|---|
| `t_fijo` (carga de entradas) | 0.332 s | medido |
| `c_texto` (factura con capa de texto) | 8.76 ms/factura | medido |
| `c_cache_ocr` (escaneada ya vista) | 7.78 ms/factura | medido |
| `c_ocr_frio` (escaneada nueva, serial) | 3.88 s/factura | medido |
| `S_ocr` (speedup del OCR) | x0.99 | medido |
| `c_decision` | 1.034 ms/factura | medido |
| `f_texto` / `f_ocr` | 0.942 / 0.058 | medido |

**Contraste del modelo con la realidad a 500 facturas** (regimen A_estacionario_cache_caliente): medido 4.31 s, modelo 5.2 s, error +20.7 % -- el modelo se desvia: tomar la cifra extrapolada con pinzas y mirar los supuestos.

### Escenario `A_estacionario_cache_caliente`

lo de cada dia: mismo reparto que La Caja y las escaneadas ya vistas (la cache es por sha256 del PDF, asi que no se vuelven a pagar)

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 49.0 | 102.04 |
| 50 000 | 487.0 | 102.66 |
| 1 000 000 | 9 734.3 | 102.73 |

### Escenario `B_lote_nuevo_mix_de_la_caja`

un lote nuevo con el mismo mix: el 5,8 % de escaneadas paga OCR en frio

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 1 177.7 | 4.25 |
| 50 000 | 11 774.5 | 4.25 |
| 1 000 000 | 235 482.8 | 4.25 |

### Escenario `C_peor_caso_todo_escaneado_nuevo`

peor caso: el 100 % de los PDFs son escaneos que nadie ha visto antes

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 19 505.5 | 0.26 |
| 50 000 | 195 052.0 | 0.26 |
| 1 000 000 | 3 901 034.0 | 0.26 |

### Cuello de botella y coste

- **Cuello de botella:** el servicio OCR. 4.35 s de servicio por factura escaneada = 0.230 facturas/s por ranura. Nada de nuestro proceso paraleliza mas rapido que el contenedor aguanta.
- **Coste por factura escaneada: 4.35 vCPU·s del servicio OCR.** 10.000 escaneadas = 43 528 vCPU·s; 1.000.000 = 4 352 800 vCPU·s.
- **Factura de texto: 0 s de OCR.** Es la palanca de coste mas grande que tenemos: no gastar vision cuando la capa de texto ya es exacta.
- En euros: `coste_EUR = N_escaneadas * s_OCR_por_factura * precio_EUR_por_vCPU_s  (el precio NO se mide aqui: no tenemos tarifa de esta maquina)`. No damos un numero en EUR porque no tenemos la tarifa de esta maquina; quien lo sepa, multiplica.

## 7. Que hariamos con 10x el volumen (y con 100x)

10x el lote de La Caja son 5 000 facturas; 100x son 50.000. Lo que cambia al crecer **no es el motor de reglas** (lineal y de 1.03 ms/factura), es el escalon de lectura. Por orden de rentabilidad:

1. **Repartir por `file_id` entre procesos: hecho.** El lote ya se lee con procesos, no con hilos (ver §1): el directorio se trocea y la salida se concatena en orden en el padre, sin coordinacion, porque el motor es **sin estado**. Lo que queda por escalar aqui es replicar el lote entero en varias maquinas: a 5 000 facturas se shardea el directorio en N trozos y se lanza un proceso por trozo, dentro o fuera de esta maquina.
2. **La cache de OCR es la palanca grande.** El escenario del sabado (40 facturas nuevas + regla nueva) y el 'reprocesar todo' son gratis: 7.78 ms por escaneada ya vista. A 50.000 facturas con el mix de La Caja el gasto de OCR es de 12 623 vCPU·s, una vez; despues, cero.
3. **Separar el escalon de OCR en su propio pool.** Es el unico termino que no es lineal con la CPU disponible: 4.35 s por factura y ranura, y el contenedor solo acelera x0.99. A 10x escala se le dan ranuras dedicadas (o varias replicas del contenedor) en vez de competir con la lectura de texto por los mismos nucleos.
4. **Subir el umbral de la capa de texto con cuidado.** Cada punto de `calidad_texto_minima` que se baja manda mas facturas a OCR: es la variable que mueve el coste de 0 a 3.88 vCPU·s por factura. La politica conservadora es no bajarlo.
5. **Shardear las entradas antes que el motor.** `t_fijo` (Excel + snapshot) son 0.332 s a 516 asientos: a 1 M de facturas el cuello pasa a ser cargar el maestro en cada worker, y ahi toca indice en memoria compartida o lectura por rango.

- Coste del peor caso a 5 000 facturas (todo escaneado nuevo): 19 506 s = 5.4 h en un solo hilo de OCR. Con el mix real de La Caja: 1 178 s. El modelo completo esta en el JSON (`extrapolado.escenarios`), no en esta prosa.

## 8. Guion de pitch (2 minutos, con estas cifras)

Todo lo que sigue sale de la tabla de arriba, medido hoy en esta maquina.

1. **Que hace** (15 s). Un lote de 500 facturas PDF entra y sale un JSONL con `PAGAR` / `NO_PAGAR` / `ESCALAR`, una linea por factura, sin duplicados y validado. El motor de decision es determinista: ninguna etiqueta sale de un modelo de lenguaje.
2. **Lo que cuesta** (30 s). **116 facturas/s** en caliente: el lote entero en **4.3 s**. Y el reparto importa: **471 de 500 facturas (94.2 %)** se resuelven leyendo la capa de texto del PDF, a **8.8 ms y 0 vCPU·s de OCR**. Solo **29 (5.8 %)** necesitan vision, y esas se cachean por `sha256` del PDF.
3. **La palanca de coste** (25 s). Una factura de texto cuesta **0**; una escaneada nueva cuesta **3.88 vCPU·s**; una escaneada ya vista, **7.8 ms**. Por eso reejecutar no cuesta: la cache convierte el segundo lote en el primero. El cuello de botella esta identificado y medido: el servicio OCR, 827 facturas/hora por ranura.
4. **Como escala** (30 s). A 5 000 facturas el modelo da 1 178 s con el mix de La Caja, y el motor es sin estado: se shardea por `file_id` y se replican procesos. El error del modelo contra la medida real a 500 es **+20.7 %**, asi que la extrapolacion no es una promesa de folleto.
5. **Lo que no decimos** (20 s). No damos EUR porque no tenemos la tarifa de esta maquina: damos vCPU·s y la formula. No medimos la calidad del OCR (aciertos), solo el tiempo. Y el simulador de cambios trabaja sobre datos y reglas, no sobre la lectura. Estan en la seccion de supuestos.

**Frase de cierre:** *el lote entero en 4.3 segundos, el 94 % sin pagar OCR, y el reprocesado gratis porque la cache es por contenido.*

## 9. Supuestos de la extrapolacion (declarados, no escondidos)

- El reparto de La Caja (94,2 % capa de texto / 5,8 % escaneadas) se mantiene.
- La cache OCR es por sha256 del PDF: un documento ya visto no se vuelve a pagar, asi que en regimen estacionario el OCR se paga una vez por documento nuevo.
- El escalon OCR paraleliza con el factor S_ocr medido; si el contenedor es single-thread real, S_ocr es 1 y el escenario C no mejora al subir trabajadores.
- El coste de decision es lineal en el numero de facturas (una factura, una decision; no hay estado compartido entre facturas).
- La maquina es la de este hackathon (2 vCPU, 11 GB). Mas nucleos aceleran la lectura (el lote va por procesos desde el cambio de reparto) y el OCR solo si se le dan ranuras propias: el contenedor OCR no paraleliza por si mismo.
- Se asume que el Excel y el snapshot crecen poco: t_fijo se mide a 516 asientos y 11 proveedores; a 1 M de facturas habria que shardear las entradas.
- El coste por factura escaneada se mide con el contenedor OCR de este entorno; otro motor o otro hardware cambian c_ocr, no la estructura del modelo.

## 10. Que NO hemos medido

- Coste en EUR: no tenemos la tarifa de esta maquina, asi que damos vCPU·s y la formula.
- Latencia del ERP real: el lote se mide contra el snapshot; una consulta en vivo paga ademas el rate-limit de 10 req/s del bridge (medido en otro sitio, no aqui).
- Tipos de archivo distintos de PDF (emails, hojas de calculo): el pipeline actual no los lee; no hay nada que cronometrar.
- Comportamiento con varios procesos en paralelo sobre la misma cache OCR.
- La calidad del OCR (aciertos): esto mide tiempo, no acierto.

