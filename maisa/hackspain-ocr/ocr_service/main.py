# FastAPI OCR service. Contract: POST /ocr (multipart "file" = PDF) -> {file_id, pages, lines}.
# Never returns 500; on failure returns {"lines": [], "pages": 0}. See spec_y_plan.md 3.3.

from fastapi import FastAPI, UploadFile, File

app = FastAPI()


@app.post("/ocr")
async def ocr(file: UploadFile = File(...)):
    # TODO: pypdfium2 -> render pages @ 250 DPI -> RapidOCR singleton -> lines[]
    return {"file_id": file.filename, "pages": 0, "lines": []}
