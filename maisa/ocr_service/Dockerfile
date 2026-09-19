# OCR API - CPU / ARM64 (aarch64)
#
# RapidOCR + ONNX Runtime sobre los modelos PP-OCRv5.
# Imagen ligera (~700 MB) comparada con la ruta PaddleX (~2 GB+).
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/ocr \
    # 2 nucleos fisicos -> limitar pools de hilos evita sobre-suscripcion
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

# libgl1/libglib2.0-0: requeridas por opencv-python (dependencia de rapidocr)
# libgomp1: runtime OpenMP usado por onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Pre-descarga los modelos ONNX por defecto dentro de la imagen.
# Ventajas: la imagen funciona sin red en el primer arranque y el health-check
# no depende de que la descarga termine.
RUN python -c "from app.server import warmup; warmup()"

# Usuario sin privilegios
RUN groupadd -g 1000 ocr \
    && useradd -m -s /bin/bash -u 1000 -g 1000 ocr \
    && mkdir -p /home/ocr/test_files \
    && chown -R ocr:ocr /home/ocr /app \
    && chown -R ocr:ocr /usr/local/lib/python3.12/site-packages/rapidocr/models

# Directorio de temporales de subida (OCR_TMP_DIR).
# Tiene que existir AQUI, con el dueño ya correcto: Docker crea un volumen
# nombrado copiando el contenido (dueño incluido) del punto de montaje de la
# imagen. Si no existe, lo crea el demonio como root:root y el contenedor, que
# corre como usuario `ocr`, no puede escribir ni un byte.
RUN mkdir -p /tmp/ocr-work && chown -R ocr:ocr /tmp/ocr-work

USER ocr

EXPOSE 8866

# 0.0.0.0 obligatorio para que el puerto sea accesible desde fuera del contenedor
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8866"]