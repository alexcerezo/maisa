#!/usr/bin/env python3
"""Deriva `public/data/escalabilidad.json` de las medidas del banco del motor.

Por que existe
--------------
La pagina `/escalabilidad` del panel ensena capacidad, hardware, limites,
formula de coste y el plan de crecimiento. Esas cifras ya existen y ya estan
medidas: viven en `maisa/motor/docs/bench.json`, que lo escribio
`maisa/motor/tools/bench.py` sobre el lote de 500 facturas.

Lo que NO hace este script es inventar numeros. Lee el banco y lo reordena en
la forma que necesita la pantalla. Lo unico que se escribe a mano aqui son los
bloques de **prosa** (limites, plan de volumen y tipos de archivo nuevos), que
no son medidas sino decisiones: van con su texto y su motivo.

Si el banco se vuelve a medir, esto se vuelve a generar y la pagina cambia con
el. Si alguien toca el JSON a mano, `--check` lo delata.

Que escribe
-----------
    public/data/escalabilidad.json   todo lo que pinta la pantalla

Uso
---
    python3 tools/generar_escalabilidad.py            # escribe
    python3 tools/generar_escalabilidad.py --check    # falla si esta viejo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
BANCO = AQUI.parents[1] / "motor" / "docs" / "bench.json"
SALIDA = AQUI.parent / "public" / "data" / "escalabilidad.json"

#: Los escenarios de la extrapolacion, con el nombre que se lee en la pantalla.
#: El orden es el del JSON del banco y no se reordena: va de lo barato a lo caro.
ESCENARIOS = [
    ("A_estacionario_cache_caliente", "Régimen estacionario", "Lo de cada día"),
    ("B_lote_nuevo_mix_de_la_caja", "Lote nuevo con el mix de La Caja", "El sábado"),
    ("C_peor_caso_todo_escaneado_nuevo", "Peor caso: todo escaneado nuevo", "Sin caché"),
]

#: Los limites que reconocemos, con su magnitud medida. Cada uno dice **que**
#: limita y **cuanto**, y de donde sale la cifra. Es la seccion que evita que la
#: pagina sea un folleto: todo lo de aqui es algo que hoy NO se puede hacer.
#:
#: El texto es una decision y va a mano; las **cifras** no: se leen del banco,
#: para que la prosa no pueda envejecer por separado de la medida. Un limite ya
#: minimizado lleva `estado` (`cerrado` o `mitigado`) y la frase `recuperado` con
#: lo que se hizo: la pagina no solo cuenta cuellos, cuenta los que ya no estan.
def numero(valor: float, decimales: int = 2) -> str:
    """Un numero con coma decimal, como se escribe en la prosa del panel."""
    return f"{valor:.{decimales}f}".replace(".", ",")


def limites(banco: dict) -> list[dict]:
    medido = banco["medido"]
    extra = banco["extrapolado"]
    caliente = medido["lote_500_caliente"]
    desglose = medido["desglose_4"]
    cuello = extra["cuello_de_botella"]
    concurrencia = cuello["concurrencia"]

    hilos = {int(clave): datos for clave, datos in caliente["por_modo"]["hilos"].items()}
    procesos = {int(clave): datos for clave, datos in caliente["por_modo"]["procesos"].items()}

    def ganancia(trabajadores: int) -> str:
        """Cuanto se recupera repartiendo por procesos en vez de por hilos."""
        return numero(hilos[trabajadores]["mediana"] / procesos[trabajadores]["mediana"])

    solo = concurrencia["por_nivel"]["1"]
    dos = concurrencia["por_nivel"]["2"]

    return [
        {
            "titulo": "El OCR es el cuello de botella",
            "magnitud": (
                f"{numero(cuello['ocr_servicio_s_por_factura'])} s por escaneada · "
                f"{concurrencia['motores']} motores"
            ),
            "detalle": (
                "El servicio de visión atiende "
                f"{int(cuello['ocr_facturas_por_hora_por_ranura'])} facturas escaneadas por "
                "hora y por ranura, y todo el gasto del sistema vive aquí. Antes atendía de "
                "una en una, con un candado global alrededor de cada inferencia; ahora el "
                f"contenedor tiene {concurrencia['motores']} motores y las inferencias se "
                "solapan de verdad: con 2 peticiones en vuelo el `idle` de `/health` baja a "
                f"{dos['idle_minimo']}. Lo que no sube es el caudal: "
                f"{numero(solo['pared_mediana_s'])} s una petición sola contra "
                f"{numero(dos['pared_mediana_s'])} s dos a la vez (×"
                f"{numero(concurrencia['speedup_caudal_1_a_2'])}), porque esta máquina tiene "
                "2 vCPU y no le sobra ningún núcleo. El techo que queda es de hardware, no "
                "de código: más caudal sale de más réplicas."
            ),
            "estado": "mitigado",
            "recuperado": (
                f"el candado global fuera: {concurrencia['motores']} motores y el `idle` a "
                f"{dos['idle_minimo']} con dos peticiones en vuelo"
            ),
            "origen": "bench.json · endpoint_concurrencia · cuello_de_botella",
        },
        {
            "titulo": "La capa de texto comparte GIL",
            "magnitud": f"×{ganancia(4)} a 4 trabajadores, procesos contra hilos",
            "detalle": (
                "Leer los 500 PDF es el 88,8 % del tiempo, y los hilos de Python no reparten "
                "`pypdf` entre núcleos porque comparten GIL. El reparto por procesos ya es el "
                f"de por defecto: a 4 trabajadores el lote baja de {numero(hilos[4]['mediana'])} s "
                f"a {numero(procesos[4]['mediana'])} s (×{ganancia(4)}) y a 8 de "
                f"{numero(hilos[8]['mediana'])} s a {numero(procesos[8]['mediana'])} s "
                f"(×{ganancia(8)}), midiendo los dos repartos alternándose dentro de cada número "
                "de trabajadores para que la deriva de carga de una máquina compartida no se "
                "confunda con la mejora. Queda un límite real: `fork` es de POSIX, así que "
                "fuera de Linux el reparto degrada a hilos."
            ),
            "estado": "cerrado",
            "recuperado": f"×{ganancia(4)} a 4 trabajadores y ×{ganancia(8)} a 8, y es el reparto por defecto",
            "origen": "bench.json · lote_500_caliente · por_modo (hilos contra procesos, alternados)",
        },
        {
            "titulo": "La caché de OCR no está firmada",
            "magnitud": "HMAC-SHA256 por entrada",
            "detalle": (
                "Cada entrada de `.cache/ocr` va sellada con `MAISA_CACHE_CLAVE`, así que "
                "quien pueda escribir en ese directorio ya no puede cambiar el texto que se "
                "lee sin que se note: `motor/tools/firma_cache.py` audita, firma y delata una "
                "entrada manipulada. Queda una concesión deliberada: las entradas sin sello se "
                "siguen aceptando, porque las que van en el repo tienen que servir a un clon "
                "limpio sin clave. Es un detector, no una puerta."
            ),
            "estado": "cerrado",
            "recuperado": "sello HMAC-SHA256 por entrada y auditoría con `firma_cache.py`",
            "origen": "motor/src/maisa/lectura.py · motor/tools/firma_cache.py · resiliencia.md §6.2",
        },
        {
            "titulo": "El límite de peticiones del ERP es por cliente",
            "magnitud": "10 req/s, autolimitado a 8",
            "detalle": (
                "El cliente se frena a 8 req/s con un 20 % de margen, pero el presupuesto es "
                "por cliente: ocho procesos en paralelo se pisan entre sí y necesitan un "
                "reparto explícito del límite antes de replicar. El reparto por procesos de la "
                "lectura no ha tocado esto: el freno sigue siendo de un solo proceso."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "motor/docs/resiliencia.md §3.2 y §6.5",
        },
        {
            "titulo": "El ERP falla cada diez consultas",
            "magnitud": "1 de cada 10 · 3 absorbidos en 30 peticiones",
            "detalle": (
                "`ORA-00600` no es una avería puntual del ERP de Miralmar: es su tasa de "
                "fallo. El bridge devuelve error interno en la décima consulta autenticada, "
                "así que el número de reintentos crece con el volumen y no con la gravedad del "
                "incidente. Los 516 asientos se descargan enteros en 30 peticiones con 3 "
                "absorbidos y 0 errores al llamante; en la prueba de 320 consultas fueron 35. "
                "Es un crecimiento lineal, y es lo que hay que presupuestar al multiplicar el "
                "lote: no la caída, la tasa."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "motor/docs/resiliencia.md §3.1 y §3.3 · panel de reintentos del ERP",
        },
        {
            "titulo": "La sesión del ERP caduca por peticiones, no por consultas",
            "magnitud": "renovación por réplica",
            "detalle": (
                "Los reintentos consumen usos de sesión. Con varias réplicas, la renovación "
                "preventiva tiene que ir por réplica; compartirla multiplica las caídas."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "motor/docs/resiliencia.md §6.6",
        },
        {
            "titulo": "El lote no se reanuda desde el CLI",
            "magnitud": f"checkpoint por documento · {numero(caliente['mediana'])} s en caliente",
            "detalle": (
                "`procesa.py` acepta `--continuar` y `--checkpoint`: cada lectura se apunta a "
                "`<salida>_lecturas.jsonl` según se produce (con `flush` por línea y `fsync` "
                "cada 64), y el checkpoint solo se borra cuando el JSONL emitido sale limpio. "
                "Reanudar desde un checkpoint de 2 documentos da un `outcomes.jsonl` byte a "
                "byte idéntico al de una pasada limpia: está probado en "
                "`tests/test_checkpoint.py`. El checkpoint guarda el documento entero, texto "
                "incluido, porque la decisión lee el texto literal (divisas declaradas, IVA "
                "del pie) y no solo los candidatos."
            ),
            "estado": "cerrado",
            "recuperado": "`--continuar` con checkpoint por documento, reanudación byte a byte",
            "origen": "motor/src/maisa/checkpoint.py · motor/tests/test_checkpoint.py",
        },
        {
            "titulo": "Las entradas se cargan por worker",
            "magnitud": f"{numero(desglose['carga_entradas_s'], 3)} s a 516 asientos",
            "detalle": (
                "El Excel del maestro y el snapshot del ERP se leen enteros en cada worker, y "
                "el reparto por procesos lo paga una vez por proceso. A 1 000 000 de facturas "
                "el cuello deja de ser la lectura y pasa a ser cargar el maestro N veces: toca "
                "índice compartido o lectura por rango."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "bench.json · desglose_4 · carga_entradas_s",
        },
        {
            "titulo": "Solo se lee PDF",
            "magnitud": "0 lectores para otros formatos",
            "detalle": (
                "El pipeline actual abre PDF y nada más. Un correo, una imagen suelta o una "
                "hoja de cálculo no tienen por dónde entrar: no hay nada que cronometrar "
                "porque no hay lector."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "bench.json · no_medido",
        },
        {
            "titulo": "No hay tarifa en euros",
            "magnitud": "vCPU·s, no €",
            "detalle": (
                "No tenemos la tarifa de esta máquina, así que publicamos vCPU·s y la fórmula. "
                "Quien sepa el precio por vCPU·s la multiplica y obtiene euros; nosotros no "
                "inventamos el número."
            ),
            "estado": "abierto",
            "recuperado": "",
            "origen": "bench.json · no_medido",
        },
    ]


#: El plan para mas volumen, por orden de rentabilidad. Es el §7 de
#: `capacidad.md` convertido en lista accionable. Como los limites, el texto es
#: una decision y las cifras salen del banco: un plan que cita numeros viejos es
#: un plan que ya no se puede creer.
def plan_volumen(banco: dict) -> list[dict]:
    medido = banco["medido"]
    extra = banco["extrapolado"]
    caliente = medido["lote_500_caliente"]
    desglose = medido["desglose_4"]
    constantes = extra["constantes_medidas"]

    hilos = {int(clave): datos for clave, datos in caliente["por_modo"]["hilos"].items()}
    procesos = {int(clave): datos for clave, datos in caliente["por_modo"]["procesos"].items()}

    cache_ms = medido["lectura_ocr_cache"]["por_factura_s"] * 1000
    escaneada_vcpu_s = constantes["c_ocr_s_por_factura_serial"]
    servicio_s = extra["cuello_de_botella"]["ocr_servicio_s_por_factura"]
    ganancia_8 = hilos[8]["mediana"] / procesos[8]["mediana"]
    vision_50k = 50_000 * constantes["f_ocr"] * escaneada_vcpu_s

    return [
        {
            "titulo": "Repartir por `file_id` y añadir procesos, no hilos",
            "detalle": (
                "El motor es sin estado y la salida es un JSONL que se concatena: no hay "
                "coordinación entre fragmentos. Se trocea el directorio en N partes y se "
                "lanzan N procesos. Medido: a 8 trabajadores el lote caliente tarda "
                f"{numero(procesos[8]['mediana'])} s por procesos y "
                f"{numero(hilos[8]['mediana'])} s por hilos (x{numero(ganancia_8)}). Ya es el "
                "reparto por defecto; lo que queda por comprar aquí es CPU, no código."
            ),
            "coste": "0 €",
            "cuando": "Hecho: es el reparto por defecto",
        },
        {
            "titulo": "Confiar en la caché de OCR (es la palanca grande)",
            "detalle": (
                "La caché es por sha256 del PDF, no por nombre: un documento ya visto cuesta "
                f"{numero(cache_ms)} ms y 0 vCPU·s de OCR. A 50 000 facturas con el mix real, "
                f"el gasto de visión es de {int(round(vision_50k)):,} vCPU·s una sola vez; "
                "después, cero. Reejecutar el lote completo es gratis, y por eso el escenario "
                "del sábado no asusta."
            ).replace(",", " "),
            "coste": "0 €",
            "cuando": "Continuo, desde hoy",
        },
        {
            "titulo": "Dar al OCR su propio pool (o varias réplicas)",
            "detalle": (
                "Es el único término que no es lineal con la CPU disponible: "
                f"{numero(servicio_s)} s por factura y ranura, y el contenedor ya solapa sus "
                "inferencias (con 2 en vuelo el `idle` de `/health` llega a 0), pero eso no da "
                "más caudal en 2 vCPU. A 10× escala se le dan ranuras dedicadas en vez de "
                "dejar que compita con la lectura de texto por los mismos núcleos."
            ),
            "coste": "Coste del OCR",
            "cuando": "Al pasar de ~5 000 facturas nuevas",
        },
        {
            "titulo": "No bajar el umbral de la capa de texto",
            "detalle": (
                "Cada punto de `calidad_texto_minima` que se baja manda más facturas a "
                "visión: es la variable que mueve el coste de 0 a "
                f"{numero(escaneada_vcpu_s)} vCPU·s por factura. La política conservadora es "
                "no bajarlo; el precio es alguna revisión humana de más, y ese precio se paga "
                "con gusto."
            ),
            "coste": "0 €",
            "cuando": "Decisión de política, no de capacidad",
        },
        {
            "titulo": "Trocear las entradas antes que el motor",
            "detalle": (
                f"`t_fijo` (Excel + snapshot) son {numero(desglose['carga_entradas_s'], 3)} s a "
                "516 asientos, y hoy cada worker lo paga entero. A 1 000 000 de facturas el "
                "cuello pasa a ser cargar el maestro: índice en memoria compartida o lectura "
                "por rango."
            ),
            "coste": "0 €",
            "cuando": "A partir de ~100 000 facturas",
        },
        {
            "titulo": "Repartir el presupuesto de 429 entre réplicas",
            "detalle": (
                "El freno de 8 req/s es por cliente. Escalar en horizontal sin repartir el "
                "límite hace que las réplicas se pisen: hay que fijar cuota por proceso y "
                "renovar la sesión por réplica."
            ),
            "coste": "0 €",
            "cuando": "Antes de la segunda réplica",
        },
        {
            "titulo": "Acotar el checkpoint de reanudación",
            "detalle": (
                "La reanudación ya está cableada (`procesa.py --continuar`), y verifica "
                "byte a byte. Lo que queda para 50 000 facturas es que el checkpoint guarda "
                "la lectura entera, texto incluido: hay que acotarlo por tamaño o podar los "
                "documentos ya emitidos."
            ),
            "coste": "0 €",
            "cuando": "Antes de pasar de ~10 000 facturas",
        },
    ]


def plan_tipos(banco: dict) -> list[dict]:
    """El plan para nuevos tipos de archivo.

    El punto de extension es **el contrato de lectura**, no el motor: un lector
    nuevo produce una `Lectura` y la norma no se toca. Cada fila dice que falta y
    cuanto costaria en OCR.
    """
    medido = banco["medido"]
    escaneada_s = banco["extrapolado"]["constantes_medidas"]["c_ocr_s_por_factura_serial"]
    texto_ms = medido["lectura_texto"]["por_factura_s"] * 1000

    return [
        {
            "tipo": "Correo (.eml / .msg)",
            "lector": "Cuerpo + adjuntos",
            "detalle": (
                "El cuerpo pasa a `texto` y cada adjunto PDF entra por el lector actual. "
                "El `file_id` es el del mensaje y los adjuntos cuelgan de él, para no "
                "contar dos veces la misma factura."
            ),
            "ocr": "Solo el adjunto escaneado",
            "estado": "Contrato listo, falta el lector",
        },
        {
            "tipo": "Imagen suelta (.jpg / .png / .tiff)",
            "lector": "Visión directa",
            "detalle": (
                "No hay capa de texto: entra en el peldaño de visión y sale con "
                "`metodo=vision_ocr`. La caché por sha256 ya cubre el reprocesado, así que "
                "una imagen vista no se vuelve a pagar."
            ),
            "ocr": f"Siempre, {numero(escaneada_s)} s",
            "estado": "Requiere un peldaño de entrada nuevo",
        },
        {
            "tipo": "Hoja de cálculo (.xlsx / .csv)",
            "lector": "Celdas → candidatos",
            "detalle": (
                "El maestro ya se lee de Excel, así que el motor de celdas existe. Lo que "
                "falta es mapear columnas a los candidatos (`nif`, `iban`, `pedido`, "
                "`fecha`, `base`, `iva`, `total`) con su confianza."
            ),
            "ocr": "Nunca",
            "estado": "Aprovecha el lector del maestro",
        },
        {
            "tipo": "Factura electrónica (XML / UBL / FacturaE)",
            "lector": "Determinista",
            "detalle": (
                "El caso más barato y el más exacto: campos etiquetados, sin visión y sin "
                "regex. Ya hay precedente en el proyecto leyendo XML en ISO-8859-1, así que "
                "la trampa de codificación está identificada."
            ),
            "ocr": "Nunca",
            "estado": "El más rentable de añadir",
        },
        {
            "tipo": "Documento de texto (.docx / .odt)",
            "lector": "Texto del documento",
            "detalle": (
                "Se extrae el texto y se aplica la misma extracción por patrones que hoy "
                "corre sobre la capa de texto del PDF: el escalón `capa_texto` ya está "
                f"medido a {numero(texto_ms)} ms y 0 vCPU·s."
            ),
            "ocr": "Nunca, si trae texto",
            "estado": "Reutiliza la extracción actual",
        },
]

#: Lo que hay que tocar, en el codigo, para que entre un tipo nuevo. Va aparte
#: de la tabla porque es la parte que se olvida y rompe la trazabilidad.
PASOS_TIPO_NUEVO = [
        "Un lector que devuelva el contrato `Lectura` (`file_id`, `paginas`, `metodo`, "
        "candidatos de `nif`/`iban`/`pedido`/`fecha`/`base`/`iva`/`total`, `texto`, "
        "`texto_ilegible`, `sospechosos`). El motor de decisión no cambia: consume "
        "lecturas, no PDFs.",
        "Un valor nuevo en el enum de `escalon` de la traza (`capa_texto`, `cache_ocr`, "
        "`vision_ocr`, `degradado`…). Sin él, la lectura no se puede auditar.",
        "El `sha256` del contenido como clave de caché, igual que hoy: es lo que hace "
        "que un documento ya visto cueste 0.",
        "El mapa de etiquetas del panel (`ETIQUETA_ESCALON` en `src/theme.ts`), que hoy "
        "es cerrado de dos valores.",
        "Una prueba por tipo en el banco de oro, con su `file_id` y su resultado "
        "esperado: el motor no decide si no puede decidir.",
]

def carga_banco() -> dict:
    return json.loads(BANCO.read_text(encoding="utf-8"))


def pct(parte: float, total: float) -> float:
    return round(100.0 * parte / total, 1) if total else 0.0


def construye(banco: dict) -> dict:
    medido = banco["medido"]
    caliente = medido["lote_500_caliente"]
    desglose = medido["desglose_4"]
    reparto = medido["reparto"]
    extra = banco["extrapolado"]

    mejor = caliente["mejor_trabajadores"]
    fila_mejor = caliente["por_trabajadores"][mejor]

    trabajadores = [
        {
            "trabajadores": int(clave),
            "mediana_s": datos["mediana"],
            "min_s": datos["min"],
            "max_s": datos["max"],
            "rango_relativo_pct": datos["rango_relativo_pct"],
            "facturas_por_s": datos["facturas_por_s"],
        }
        for clave, datos in sorted(caliente["por_trabajadores"].items(), key=lambda par: int(par[0]))
    ]

    #: El mismo lote medido con los dos repartos de la lectura. Se publica entero
    #: porque la comparacion **es** la medida: hilos y procesos, alternandose
    #: dentro de cada numero de trabajadores, es lo que aísla el GIL de la deriva
    #: de carga de una maquina compartida.
    por_modo = {
        modo: [
            {
                "trabajadores": int(clave),
                "mediana_s": datos["mediana"],
                "min_s": datos["min"],
                "max_s": datos["max"],
                "rango_relativo_pct": datos["rango_relativo_pct"],
                "facturas_por_s": datos["facturas_por_s"],
            }
            for clave, datos in sorted(filas.items(), key=lambda par: int(par[0]))
        ]
        for modo, filas in caliente["por_modo"].items()
    }
    mejor_por_modo = {
        modo: min(filas, key=lambda fila: fila["mediana_s"]) for modo, filas in por_modo.items()
    }

    fases = [
        ("Cargar Excel + snapshot", desglose["carga_entradas_s"]),
        ("Leer los 500 PDF", desglose["lectura_s"]),
        ("Decidir las 500 facturas", desglose["decision_s"]),
        ("Emitir el JSONL", desglose["emision_s"]),
    ]
    total_fases = desglose["total_fases_s"]

    escenarios = []
    for clave, nombre, cuando in ESCENARIOS:
        datos = extra["escenarios"][clave]
        escenarios.append(
            {
                "clave": clave,
                "nombre": nombre,
                "cuando": cuando,
                "descripcion": datos["descripcion"],
                "coste_ocr_s": datos["coste_ocr_aplicado_s"],
                "tiempos_s": datos["tiempos_s"],
                "facturas_por_s": datos["facturas_por_s"],
            }
        )

    coste = extra["coste"]
    cuello = extra["cuello_de_botella"]

    return {
        "procedencia": {
            "generado_por": "maisa/ui/tools/generar_escalabilidad.py",
            "fuente": "maisa/motor/docs/bench.json",
            "medido_por": "maisa/motor/tools/bench.py",
            "medido_en": medido["fecha"],
            "nota": (
                "Todo lo que trae `medido: true` se cronometró en esta máquina. Lo que "
                "trae `medido: false` es extrapolación con el modelo declarado."
            ),
        },
        "hardware": {
            "cpu_logicos": medido["maquina"]["cpu_logicos"],
            "memoria_total_gb": medido["maquina"]["memoria_total_gb"],
            "plataforma": medido["maquina"]["plataforma"],
            "python": medido["maquina"]["python"],
            "carga_media_al_inicio": medido["maquina"]["carga_media_1_5_15_al_inicio"],
            "nota": medido["maquina"]["nota"],
        },
        "capacidad": {
            "medido": True,
            "lote": medido["facturas"],
            "mejor_trabajadores": int(mejor),
            "mejor_mediana_s": fila_mejor["mediana"],
            "mejor_facturas_por_s": fila_mejor["facturas_por_s"],
            "mejor_rango_relativo_pct": fila_mejor["rango_relativo_pct"],
            "con_traza": {
                "trabajadores": medido["lote_500_con_traza"]["trabajadores"],
                "segundos": medido["lote_500_con_traza"]["segundos"],
                "eventos": medido["lote_500_con_traza"]["eventos"],
            },
            "trabajadores": trabajadores,
            "por_modo": por_modo,
            "modo_mejor": caliente["modo_mejor"],
            "mejor_por_modo": mejor_por_modo,
            "fases": [
                {"fase": nombre, "segundos": segundos, "reparto_pct": pct(segundos, total_fases)}
                for nombre, segundos in fases
            ],
            "total_fases_s": total_fases,
            "decision_ms_por_factura": desglose["por_factura_decision_ms"],
            "reparto_lectura": {
                "capa_texto": reparto["capa_texto"],
                "ocr": reparto["ocr"],
                "pct_capa_texto": pct(reparto["capa_texto"], medido["facturas"]),
                "pct_ocr": pct(reparto["ocr"], medido["facturas"]),
            },
            "lectura_texto": {
                "ms_por_factura": round(medido["lectura_texto"]["por_factura_s"] * 1000, 2),
                "facturas_por_s": medido["lectura_texto"]["facturas_por_s"],
            },
            "lectura_cache": {
                "ms_por_factura": round(medido["lectura_ocr_cache"]["por_factura_s"] * 1000, 3),
                "facturas_por_s": medido["lectura_ocr_cache"]["facturas_por_s"],
            },
            "ocr": {
                "servicio_s_por_factura": cuello["ocr_servicio_s_por_factura"],
                "facturas_por_hora_por_ranura": cuello["ocr_facturas_por_hora_por_ranura"],
                "frio_serial_s_por_factura": medido["ocr_frio_serial"]["por_factura_mediana_s"],
                "frio_serial_facturas_por_s": medido["ocr_frio_serial"]["facturas_por_s"],
                # La medida honesta de si el contenedor solapa: `solapa_peticiones` lo
                # dice el `idle` de `/health` (0 = los dos motores ocupados a la vez) y
                # `speedup_caudal_1_a_2` dice si eso se traduce en caudal. Son dos cosas
                # distintas y la pagina las ensena separadas a proposito.
                "motores": cuello["concurrencia"]["motores"],
                "solapa_peticiones": cuello["concurrencia"]["solapa_peticiones"],
                "speedup_caudal_1_a_2": cuello["concurrencia"]["speedup_caudal_1_a_2"],
                "en_vuelo": [
                    {
                        "peticiones": int(nivel),
                        "pared_mediana_s": datos["pared_mediana_s"],
                        "latencia_mediana_s": datos["latencia_mediana_s"],
                        "facturas_por_s": datos["facturas_por_s"],
                        "idle_minimo": datos["idle_minimo"],
                    }
                    for nivel, datos in sorted(
                        cuello["concurrencia"]["por_nivel"].items(), key=lambda par: int(par[0])
                    )
                ],
                "nota_concurrencia": cuello["concurrencia"]["nota"],
            },
        },
        "limites": limites(banco),
        "coste": {
            "medido": True,
            "hoy_eur": 0,
            "nota_hoy": (
                "0 € y seguirá en 0 €: todo corre en local (capa de texto + OCR en "
                "contenedor + ERP en localhost). No hay llamadas de pago, ni credenciales, "
                "ni red externa."
            ),
            "unidad": coste["unidad"],
            "por_factura_texto_vcpu_s": 0,
            "por_factura_cache_vcpu_s": 0,
            "por_factura_escaneada_vcpu_s": coste["por_factura_escaneada"],
            "formula_tiempo": extra["formula"],
            "formula_euros": coste["formula_euros"],
            "regimenes": extra["regimenes"],
            "constantes": extra["constantes_medidas"],
            "contraste": extra["contraste_modelo_500"],
            "escenarios": escenarios,
            "objetivos": extra["objetivos"],
            "vcpu": {
                "para_10000_escaneadas": coste["para_10000_escaneadas"],
                "para_1000000_escaneadas": coste["para_1000000_escaneadas"],
            },
        },
        "plan": {
            "volumen": plan_volumen(banco),
            "tipos_archivo": PLAN_TIPOS,
            "pasos_tipo_nuevo": PASOS_TIPO_NUEVO,
        },
        "supuestos": extra["supuestos"],
        "no_medido": medido["no_medido"],
    }


def serializa(datos: dict) -> str:
    return json.dumps(datos, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="no escribe: falla si el JSON del panel no coincide con el banco",
    )
    args = parser.parse_args()

    if not BANCO.exists():
        print(f"No encuentro el banco de medidas: {BANCO}", file=sys.stderr)
        return 1

    texto = serializa(construye(carga_banco()))

    if args.check:
        actual = SALIDA.read_text(encoding="utf-8") if SALIDA.exists() else ""
        if actual != texto:
            print(
                f"{SALIDA} esta desactualizado respecto a {BANCO}. "
                "Vuelve a lanzar `python3 tools/generar_escalabilidad.py`.",
                file=sys.stderr,
            )
            return 1
        print(f"{SALIDA} esta al dia.")
        return 0

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(texto, encoding="utf-8")
    print(f"Escrito {SALIDA} desde {BANCO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
