from fastapi import FastAPI, Request
from paddleocr import PaddleOCR
import tempfile
import os
import uvicorn

app = FastAPI()
# Cargamos el modelo
ocr = PaddleOCR(use_textline_orientation=True, lang='es')

@app.post("/ocr")
async def process_ocr(request: Request):
    # 1. Leer los bytes (sea PDF o Imagen) que envía Rust
    body = await request.body()
    
    # 2. Guardarlos en un archivo temporal
    # Usamos .pdf, pero PaddleOCR es lo bastante listo para procesarlo aunque sea una imagen
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(body)
        tmp_path = tmp.name
        
    try:
        # 3. Inferencia mágica: Pasamos la ruta del archivo. 
        # Si es un PDF, procesará todas las páginas automáticamente.
        result = ocr.predict(tmp_path)        
        # 4. Extraer el texto.
        # En PDFs o multi-página, result es una lista de páginas. 
        # Cada página es una lista de líneas detectadas.
        texts = []
        if result:
            for page in result:
                if page: # Asegurar que la página no esté vacía
                    for line in page:
                        texts.append(line[1][0])
                        
        raw_text = "\n".join(texts)
        
        return {
            "emisor": "Por detectar", 
            "total": 0.0, 
            "raw_text": raw_text
        }
    finally:
        # 5. Siempre, siempre, borramos el archivo temporal para no llenar el disco
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)