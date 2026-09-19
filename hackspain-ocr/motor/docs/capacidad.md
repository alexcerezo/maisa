# Capacidad y coste: lo medido, no lo prometido

Generado por `maisa/tools/bench.py` el 2026-09-19T11:11:53+0000 sobre 2 vCPU / 11.65 GB. Todos los tiempos son de pared, medidos con `time.perf_counter()` en esta maquina y con la carga registrada en `docs/bench.json`.

> Regla de la casa: si una cifra no esta en la columna *medido*, es una extrapolacion y va marcada como tal.

## 1. Lote completo de 500 (cache caliente) por numero de trabajadores

3 repeticiones por configuracion, end-to-end (`python -m maisa.procesa`), escribiendo `outcomes.jsonl` y contando las lineas emitidas.

| trabajadores | n | min (s) | mediana (s) | max (s) | desv. típ. | rango rel. | facturas/s (mediana) |
|---|---|---|---|---|---|---|---|
| 1 | 3 | 4.67 | **4.76** | 5.20 | 0.29 | 11.2 % | 105.1 |
| 2 | 3 | 4.02 | **4.39** | 4.72 | 0.35 | 16.1 % | 113.9 |
| 4 | 3 | 3.66 | **3.69** | 3.98 | 0.18 | 8.7 % | 135.5 |
| 8 | 3 | 3.61 | **3.78** | 3.88 | 0.14 | 7.1 % | 132.4 |

- Mejor configuracion medida: **--trabajadores 4** con **3.69 s** de mediana (135.5 facturas/s) y una dispersion de 8.7 % entre pasadas.
- Con `--traza-hash` (auditoria encadenada de 1002 eventos): **4.74 s** en una pasada a 4 trabajadores, +1.05 s sobre la mediana sin traza.

## 2. Donde se va el tiempo (desglose por fase, 4 trabajadores)

| fase | segundos | reparto |
|---|---|---|
| cargar Excel + snapshot (fijo) | 0.105 | 2.8 % |
| leer los 500 PDF (texto + caché OCR) | 3.568 | 95.4 % |
| decidir las 500 facturas | 0.063 | 1.7 % |
| emitir el JSONL | 0.003 | 0.1 % |
| **total** | **3.739** | 100 % |

- Decidir una factura cuesta **0.12 ms** (regex + aritmetica + precedencia; sin modelo de lenguaje).

## 3. Reparto capa_texto vs OCR (de la traza, campo `escalon_lectura`)

| escalón | facturas | reparto |
|---|---|---|
| capa_texto (sin OCR) | 471 | 94.2 % |
| OCR necesario (caché o servicio) | 29 | 5.8 % |
| **total** | 500 | 100 % |

- La capa de texto resuelve **471 facturas a 7.09 ms/factura** (141 facturas/s) con 4 trabajadores: es exacta y no cuesta servicio externo.
- Una escaneada **ya vista** (servida de cache por sha256) cuesta **3.04 ms/factura**, es decir 2.3 veces menos que un escaneo nuevo: ese es el ahorro que compra la cache.
- Detalle de escalones en el lote: `{'capa_texto': 471, 'cache_ocr': 29}`.

## 4. OCR en frio (cache temporal, PDFs copiados a temporal)

| configuración | facturas | total (s) | s/factura (mediana) | min | max | facturas/s |
|---|---|---|---|---|---|---|
| serial (1 hilo) | 10 | 42.11 | **4.08** | 3.64 | 5.28 | 0.237 |
| 4 hilos | 10 | 40.96 | **14.53** | 4.81 | 19.16 | 0.244 |

- Servicio OCR directo (`POST http://127.0.0.1:8866/ocr`, sin nuestro pipeline): **4.47 s/factura** de mediana (min 3.84, max 5.17, n=5).
- Eso es un techo de **806 facturas escaneadas/hora por ranura** de OCR.
- Paralelizar el escalon OCR de 1 a 4 hilos acelera **x1.03**, luego el contenedor **no** paraleliza: atiende de una en una. Consecuencia: subir `--trabajadores` no compra OCR; se compra con mas ranuras de OCR o con la cache.
- La cache real (`maisa/.cache/ocr`) no se ha tocado: la medida usa un directorio de cache de `tempfile` y copias de los PDFs en temporal.

## 5. Coste unitario por factura, escalon a escalon

Lo que cuesta **una** factura segun por donde entre. Es la tabla que convierte el modelo de coste en una decision de negocio: la palanca no es *procesar mas rapido*, es **no llamar al OCR cuando la capa de texto ya es exacta**.

| escalón de lectura | coste OCR | tiempo de pared | vCPU·s de OCR | recurso externo |
|---|---|---|---|---|
| capa de texto (94,2 % del lote) | 0 | 7.09 ms | 0 | ninguna |
| escaneada ya vista (cache sha256) | 0 | 3.04 ms | 0 | disco local |
| escaneada nueva (OCR en frio) | 4.08 s | 4.08 s | 4.08 vCPU·s | contenedor OCR |

- Factura con capa de texto: **0 vCPU·s de OCR** y 7.09 ms de CPU. El motor de reglas anade 0.12 ms. Es el 94,2 % del lote.
- Escaneada nueva: **4.08 vCPU·s de OCR** por factura. Es 575 veces el coste de una factura de texto. Ahi esta todo el gasto del sistema.
- Escaneada ya vista: **0 vCPU·s** (la cache es por `sha256` del PDF, no por nombre) y 3.04 ms. Reejecutar el lote completo es gratis: por eso el escenario del sabado no asusta.

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
| `t_fijo` (carga de entradas) | 0.105 s | medido |
| `c_texto` (factura con capa de texto) | 7.09 ms/factura | medido |
| `c_cache_ocr` (escaneada ya vista) | 3.04 ms/factura | medido |
| `c_ocr_frio` (escaneada nueva, serial) | 4.08 s/factura | medido |
| `S_ocr` (speedup del OCR) | x1.03 | medido |
| `c_decision` | 0.125 ms/factura | medido |
| `f_texto` / `f_ocr` | 0.942 / 0.058 | medido |

**Contraste del modelo con la realidad a 500 facturas** (regimen A_estacionario_cache_caliente): medido 3.69 s, modelo 3.6 s, error -2.6 % -- el modelo reproduce la realidad dentro del ruido de la maquina.

### Escenario `A_estacionario_cache_caliente`

lo de cada dia: mismo reparto que La Caja y las escaneadas ya vistas (la cache es por sha256 del PDF, asi que no se vuelven a pagar)

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 35.0 | 142.83 |
| 50 000 | 349.1 | 143.22 |
| 1 000 000 | 6 980.3 | 143.26 |

### Escenario `B_lote_nuevo_mix_de_la_caja`

un lote nuevo con el mismo mix: el 5,8 % de escaneadas paga OCR en frio

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 1 183.9 | 4.22 |
| 50 000 | 11 838.1 | 4.22 |
| 1 000 000 | 236 759.0 | 4.22 |

### Escenario `C_peor_caso_todo_escaneado_nuevo`

peor caso: el 100 % de los PDFs son escaneos que nadie ha visto antes

| facturas | tiempo (s) | facturas/s |
|---|---|---|
| 5 000 | 19 824.4 | 0.25 |
| 50 000 | 198 243.4 | 0.25 |
| 1 000 000 | 3 964 865.7 | 0.25 |

### Cuello de botella y coste

- **Cuello de botella:** el servicio OCR. 4.47 s de servicio por factura escaneada = 0.224 facturas/s por ranura. Nada de nuestro proceso paraleliza mas rapido que el contenedor aguanta.
- **Coste por factura escaneada: 4.47 vCPU·s del servicio OCR.** 10.000 escaneadas = 44 667 vCPU·s; 1.000.000 = 4 466 700 vCPU·s.
- **Factura de texto: 0 s de OCR.** Es la palanca de coste mas grande que tenemos: no gastar vision cuando la capa de texto ya es exacta.
- En euros: `coste_EUR = N_escaneadas * s_OCR_por_factura * precio_EUR_por_vCPU_s  (el precio NO se mide aqui: no tenemos tarifa de esta maquina)`. No damos un numero en EUR porque no tenemos la tarifa de esta maquina; quien lo sepa, multiplica.

## 7. Que hariamos con 10x el volumen (y con 100x)

10x el lote de La Caja son 5 000 facturas; 100x son 50.000. Lo que cambia al crecer **no es el motor de reglas** (lineal y de 0.12 ms/factura), es el escalon de lectura. Por orden de rentabilidad:

1. **Repartir por `file_id` y anadir procesos, no hilos.** El lote ya se procesa con `--trabajadores`; a 5 000 facturas se shardea el directorio en N trozos y se lanzan N procesos. El motor es **sin estado** y la salida es un JSONL que se concatena: no hay coordinacion. Medido en esta maquina, el lote caliente baja de 4.76 s a 3.69 s al pasar de 1 a 4 trabajadores (x1.29): el trabajo paraleliza, pero los hilos comparten GIL; el siguiente escalon es repartir el directorio entre procesos, no hilos.
2. **La cache de OCR es la palanca grande.** El escenario del sabado (40 facturas nuevas + regla nueva) y el 'reprocesar todo' son gratis: 3.04 ms por escaneada ya vista. A 50.000 facturas con el mix de La Caja el gasto de OCR es de 12 953 vCPU·s, una vez; despues, cero.
3. **Separar el escalon de OCR en su propio pool.** Es el unico termino que no es lineal con la CPU disponible: 4.47 s por factura y ranura, y el contenedor solo acelera x1.03. A 10x escala se le dan ranuras dedicadas (o varias replicas del contenedor) en vez de competir con la lectura de texto por los mismos nucleos.
4. **Subir el umbral de la capa de texto con cuidado.** Cada punto de `calidad_texto_minima` que se baja manda mas facturas a OCR: es la variable que mueve el coste de 0 a 4.08 vCPU·s por factura. La politica conservadora es no bajarlo.
5. **Shardear las entradas antes que el motor.** `t_fijo` (Excel + snapshot) son 0.105 s a 516 asientos: a 1 M de facturas el cuello pasa a ser cargar el maestro en cada worker, y ahi toca indice en memoria compartida o lectura por rango.

- Coste del peor caso a 5 000 facturas (todo escaneado nuevo): 19 824 s = 5.5 h en un solo hilo de OCR. Con el mix real de La Caja: 1 184 s. El modelo completo esta en el JSON (`extrapolado.escenarios`), no en esta prosa.

## 8. Guion de pitch (2 minutos, con estas cifras)

Todo lo que sigue sale de la tabla de arriba, medido hoy en esta maquina.

1. **Que hace** (15 s). Un lote de 500 facturas PDF entra y sale un JSONL con `PAGAR` / `NO_PAGAR` / `ESCALAR`, una linea por factura, sin duplicados y validado. El motor de decision es determinista: ninguna etiqueta sale de un modelo de lenguaje.
2. **Lo que cuesta** (30 s). **136 facturas/s** en caliente: el lote entero en **3.7 s**. Y el reparto importa: **471 de 500 facturas (94.2 %)** se resuelven leyendo la capa de texto del PDF, a **7.1 ms y 0 vCPU·s de OCR**. Solo **29 (5.8 %)** necesitan vision, y esas se cachean por `sha256` del PDF.
3. **La palanca de coste** (25 s). Una factura de texto cuesta **0**; una escaneada nueva cuesta **4.08 vCPU·s**; una escaneada ya vista, **3.0 ms**. Por eso reejecutar no cuesta: la cache convierte el segundo lote en el primero. El cuello de botella esta identificado y medido: el servicio OCR, 806 facturas/hora por ranura.
4. **Como escala** (30 s). A 5 000 facturas el modelo da 1 184 s con el mix de La Caja, y el motor es sin estado: se shardea por `file_id` y se replican procesos. El error del modelo contra la medida real a 500 es **-2.6 %**, asi que la extrapolacion no es una promesa de folleto.
5. **Lo que no decimos** (20 s). No damos EUR porque no tenemos la tarifa de esta maquina: damos vCPU·s y la formula. No medimos la calidad del OCR (aciertos), solo el tiempo. Y el simulador de cambios trabaja sobre datos y reglas, no sobre la lectura. Estan en la seccion de supuestos.

**Frase de cierre:** *el lote entero en 3.7 segundos, el 94 % sin pagar OCR, y el reprocesado gratis porque la cache es por contenido.*

## 9. Supuestos de la extrapolacion (declarados, no escondidos)

- El reparto de La Caja (94,2 % capa de texto / 5,8 % escaneadas) se mantiene.
- La cache OCR es por sha256 del PDF: un documento ya visto no se vuelve a pagar, asi que en regimen estacionario el OCR se paga una vez por documento nuevo.
- El escalon OCR paraleliza con el factor S_ocr medido; si el contenedor es single-thread real, S_ocr es 1 y el escenario C no mejora al subir trabajadores.
- El coste de decision es lineal en el numero de facturas (una factura, una decision; no hay estado compartido entre facturas).
- La maquina es la de este hackathon (2 vCPU, 11 GB). Mas nucleos no aceleran la capa de texto (GIL + pypdf) pero si el OCR (I/O + proceso externo).
- Se asume que el Excel y el snapshot crecen poco: t_fijo se mide a 516 asientos y 11 proveedores; a 1 M de facturas habria que shardear las entradas.
- El coste por factura escaneada se mide con el contenedor OCR de este entorno; otro motor o otro hardware cambian c_ocr, no la estructura del modelo.

## 10. Que NO hemos medido

- Coste en EUR: no tenemos la tarifa de esta maquina, asi que damos vCPU·s y la formula.
- Latencia del ERP real: el lote se mide contra el snapshot; una consulta en vivo paga ademas el rate-limit de 10 req/s del bridge (medido en otro sitio, no aqui).
- Tipos de archivo distintos de PDF (emails, hojas de calculo): el pipeline actual no los lee; no hay nada que cronometrar.
- Comportamiento con varios procesos en paralelo sobre la misma cache OCR.
- La calidad del OCR (aciertos): esto mide tiempo, no acierto.

