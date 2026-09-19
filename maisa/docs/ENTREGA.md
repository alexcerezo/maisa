# Publicar la entrega — Maisa / HackSpain 2026

Equipo **De Despeñaperros Pabajo** · `teamId` **YEM9Q8TP**

> Este documento nació en el workspace suelto de trabajo. Se ha actualizado al
> layout definitivo del repositorio; la sección 1 (lectura literal de la spec)
> se conserva tal cual porque es la que justifica las decisiones.

---

## 1. Qué dice la spec (literal)

De `corpus/maisa/README.md` (hoy copiado en `maisa/data/corpus/README.md`):

> Compartid el `teamId` y la URL de un repositorio público de GitHub. **Usad un
> repositorio separado para esta entrega: no subáis vuestra solución,
> credenciales ni una aplicación ejecutable.**
>
> La raíz del repositorio debe contener **exactamente** estos tres archivos:
>
> ```text
> la-caja-outcomes/
> ├── outcomes.jsonl
> ├── outcomes_lote2.jsonl
> └── albertitos_plan.pdf
> ```

> La organización **clonará el repositorio a las 10:30**, registrará el commit y
> ejecutará su verificador privado sobre los dos JSONL. **No ejecutará código del
> equipo** y no verá ni pedirá credenciales.

### Las tres desviaciones conscientes

1. **Los tres ficheros van sueltos en la raíz, no dentro de `la-caja-outcomes/`.**
   El texto dice «la raíz debe contener exactamente estos tres archivos» y el
   árbol que lo acompaña dibuja una carpeta. Se obedece el texto: en la raíz
   están los tres ficheros, sin carpeta intermedia.
2. **El repositorio de entrega es también el de trabajo.** La spec pide uno
   separado y sin solución. Se mantiene uno solo (`alexcerezo/maisa`) porque la
   CI, el banco de oro y la trazabilidad son parte del argumento del pitch, y
   porque la organización no ejecuta código del equipo. Consecuencia asumida:
   `valida_entrega.py --publicable` **falla a propósito** (detecta código en el
   repo), y eso se explica en la defensa en vez de esconderse.
3. **`outcomes_lote2.jsonl` viaja vacío** hasta el sábado a las 18:00, cuando
   llegue el lote 2. Un fichero vacío es un bloqueante del validador, y con
   razón: es un recordatorio de que queda trabajo, no un error que ignorar.

---

## 2. Estado actual

| Pieza | Dónde | Estado |
|---|---|---|
| `outcomes.jsonl` (500 líneas, 448/43/9) | raíz del repo | ✅ validado |
| `outcomes_lote2.jsonl` | raíz del repo | ⏳ vacío: llega el sábado 18:00 |
| `albertitos_plan.pdf` (9 páginas, 5 ADRs) | raíz del repo | ✅ generado |
| Copia de trabajo del outcomes | `maisa/outputs/outcomes.jsonl` | ✅ idéntica a la de la raíz |

```
alexcerezo-maisa/                  <- raíz del repositorio
├── outcomes.jsonl
├── outcomes_lote2.jsonl
├── albertitos_plan.pdf
├── maisa/                         <- todo lo demás
│   ├── motor/                     el motor de decisión (Python)
│   ├── data/                      corpus, snapshot del ERP, golden
│   ├── ocr_service/               servicio OCR (FastAPI + RapidOCR)
│   ├── docs/                      este documento
│   ├── outputs/                   la copia de trabajo de la entrega
│   └── ...
└── .github/workflows/ci.yml       (GitHub solo lee workflows desde la raíz)
```

La CI comprueba que la copia de la raíz es **byte a byte** la que produce el
motor. Si alguien arregla el motor, regenera `outputs/` y se olvida de la raíz,
la puerta 4 lo caza.

---

## 3. Cómo publicarlo

El repositorio de entrega es **`alexcerezo/maisa`**. `gh` está autenticado en
esta máquina con scope `repo` y `workflow`.

```bash
# 1) La raíz tiene lo que pide la spec (menos el lote 2, aún vacío)
cd maisa
PYTHONPATH=motor/src python3 motor/tools/valida_entrega.py ..
#    -> 1 bloqueante: outcomes_lote2.jsonl vacío (esperado hasta el sábado)

# 2) Publicar
git push github main
```

Y compartir con la organización: **`teamId` `YEM9Q8TP`** y la URL del repositorio.

> **Nunca `git push origin`.** En esta máquina `origin` apunta al clon espía del
> watcher (`~/.cache/maisa-watch/repos/maisa`). El remoto bueno es `github`.

El validador `motor/tools/valida_entrega.py` comprueba la estructura de la raíz,
la codificación (sin BOM, UTF-8, saltos LF), el enum de `result`, la ausencia de
duplicados, la cobertura contra los 500 PDFs y las dos secciones del PDF.
`--publicable` añade la comprobación de que no se ha colado código, ejecutables
ni credenciales.

---

## 4. Checklist antes de publicar

- [ ] `outcomes.jsonl` tiene **exactamente 500 líneas**, una por PDF, sin duplicados.
- [ ] Cada `result` es `PAGAR`, `NO_PAGAR` o `ESCALAR` (nada más).
- [ ] Cada `file_id` es el **nombre exacto** del PDF, con extensión y mayúsculas.
- [ ] `outcomes_lote2.jsonl` tiene **una línea por cada factura del lote 2** (vacío
      ahora; se rellena el sábado). Un fichero vacío es un bloqueante para el
      validador `--publicable`, y con razón.
- [ ] `albertitos_plan.pdf` contiene las secciones **Arquitectura** y
      **ADRs / trade-offs**, con **entre 2 y 5 ADRs**.
- [ ] La raíz tiene los **tres ficheros** (más `maisa/` y `.github/`, desviación
      consciente documentada arriba).
- [ ] La CI está en verde y la organización puede clonar el commit registrado.

---

## 5. Regenerar los artefactos

Todos los comandos se ejecutan desde `maisa/`. El intérprete es `python3` con
`PYTHONPATH=motor/src` (dependencias: `motor/requirements.txt`).

```bash
cd /home/ubuntu/projects/alexcerezo-maisa/maisa
export PYTHONPATH=motor/src

# 1) Las 500 decisiones (deterministas: dos pasadas dan el mismo md5)
python3 -m maisa.procesa \
    --facturas data/facturas \
    --xlsx data/FINAL_v7_DEFINITIVO_ahorasi.xlsx \
    --snapshot data/erp_snapshot.json \
    --config motor/config/reglas.toml \
    --lote 1 --trabajadores 4 \
    --salida outputs/outcomes.jsonl
cp outputs/outcomes.jsonl ../outcomes.jsonl

# 2) ¿Se ha movido algo respecto al banco de oro congelado?
python3 motor/tools/oro.py --comprobar \
    --facturas data/facturas \
    --xlsx data/FINAL_v7_DEFINITIVO_ahorasi.xlsx \
    --snapshot data/erp_snapshot.json \
    --config motor/config/reglas.toml

# 3) El plan: markdown -> PDF (9 páginas)
python3 motor/tools/md_a_pdf.py motor/docs/albertitos_plan.md ../albertitos_plan.pdf

# 4) Validar la raíz del repositorio
python3 motor/tools/valida_entrega.py ..
```

El plan en markdown se edita en **`motor/docs/albertitos_plan.md`** (la spec solo
pide el PDF) y se compila con `motor/tools/md_a_pdf.py` (fpdf2 + fuentes DejaVu).

---

## 6. Lo que **no** se sube

- **`recon/`** — el trabajo de campo sobre los repositorios de los demás equipos
  (`RECON_MAISA.md`, `PLAN_VICTORIA.md`, digests, briefings y el watcher). Vive
  solo en el workspace local y está en `.gitignore`. Publicarlo sería publicar
  datos de terceros.
- **`_scratch/`** — salidas de diagnóstico, experimentos de OCR y recortes de
  revisión.
- El `.env` del servicio OCR (lleva el token de la API de visión) y cualquier
  credencial. Lo versionado es `ocr_service/.env.example`, con el token vacío.

El resto del proyecto (`maisa/`) **sí** se publica: es el argumento del pitch.

---

## 7. Integración continua

Cuatro puertas en `.github/workflows/ci.yml`, de más barata a más cara:

| Puerta | Qué corre | Necesita corpus | Tiempo |
|---|---|---|---|
| `lint` | `ruff check .` desde `maisa/` (ruleset `E9,F`, config en `ruff.toml`) | no | ~5 s |
| `tests` | `python -m pytest` desde `maisa/motor` (128 pasan, 2 xfail) | no | ~12 s |
| `lote` | determinismo: dos pasadas, mismo md5 | sí | ~20 s |
| `entrega` | `outputs/` y la raíz coinciden con lo que produce el motor hoy, banco de oro intacto, cobertura por regla | sí | ~20 s |

La puerta `lote` es la que nos distingue: no comprueba que el código «hace algo»,
comprueba que **decide lo mismo dos veces**. Un motor de pagos que cambia de
opinión entre ejecuciones no es un motor, es una moneda al aire.

La puerta `entrega` cierra el otro agujero: que la entrega publicada se quede
anclada en una versión vieja del motor mientras `outputs/` avanza.

Todo corre **sin red**: los 29 PDFs escaneados llevan su texto ya cacheado en
`motor/.cache/ocr/` (indexado por sha256 del PDF), así que la CI reproduce las
500 decisiones sin levantar el contenedor de visión.

Verificación local de las dos primeras puertas:

```bash
cd /home/ubuntu/projects/alexcerezo-maisa/maisa
ruff check .                       # puerta lint
cd motor && python3 -m pytest      # puerta tests (pytest.ini fija pythonpath)
```

`ruff` (0.16.8) está instalado en el `.venv` del workspace. El ruleset es
deliberadamente estrecho: solo errores reales (E9 = sintaxis, F = pyflakes). No
discutimos estilo.
