# API/BFF de Albertitos: unica superficie expuesta a Internet.
#
# Construir:  docker build -t albertitos-api maisa/api
# Correr:     docker compose -f maisa/api/docker-compose.yml up -d
#
# La imagen no lleva ni secretos ni datos: la URI de Mongo y las rutas de los
# datos entran por variables de entorno y por volumenes de solo lectura.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Rutas por defecto DENTRO del contenedor. Son las mismas que monta
# docker-compose.yml (/datos/*, en solo lectura). Sin esto, los defaults
# derivados del paquete apuntarian a /outputs, /data/facturas y /ui porque
# dentro de la imagen `app/config.py` cuelga de la raiz.
#
# `UI_DIR` apunta a `dist`, el BUILD del panel (`npm run build` en maisa/ui). Si
# apuntase a la carpeta del panel, `/` serviria la plantilla de Vite sin
# construir (la que carga `/src/main.tsx`) y el navegador daria una pagina en
# blanco: esa plantilla existe igual aunque no se haya construido nada.
ENV OUTPUTS_DIR=/datos/outputs \
    FACTURAS_DIR=/datos/facturas \
    UI_DIR=/datos/ui/dist

# Las dependencias primero: si cambia el codigo, esta capa se reutiliza.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Usuario sin privilegios (uid fijo para que el healthcheck y los volumenes
# sean predecibles). El proceso no escribe en el disco del anfitrion: los
# volumenes van en solo lectura y lo que sube va a Mongo (GridFS).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin apiuser
USER apiuser

EXPOSE 8000

# Liveness: el proceso sirve HTTP. Se usa /health (siempre 200 mientras el
# proceso viva y con el detalle de cada dependencia) en lugar de /health/ready,
# que devuelve 503 cuando Mongo o el OCR estan caidos: eso es una condicion de
# "no listo" para un orquestador, no un motivo para reiniciar el contenedor.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
