import io
import re
import requests
from pypdf import PdfReader

HEADERS = {
    "User-Agent": "NairaSheetsDividendFeed/1.0 (+public NGX corporate disclosure indexing)"
}

# Minimum characters from pdftotext before we consider it a real text layer.
# Below this threshold the PDF is treated as a scan and OCR is attempted.
MIN_TEXT_CHARS = 50


def _ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    """
    OCR fallback for scanned/image PDFs.
    Rasterises each page and runs pytesseract on it.
    Returns extracted text or empty string on failure.
    """
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        from PIL import Image

        images = convert_from_bytes(pdf_bytes, dpi=200)
        pages = []
        for image in images:
            try:
                text = pytesseract.image_to_string(image, lang="eng")
                pages.append(text or "")
            except Exception:
                pages.append("")
        return "\n".join(pages)

    except ImportError as exc:
        # OCR dependencies not installed — log and return empty
        print(f"[OCR] Dependencies missing ({exc}) — skipping OCR fallback", flush=True)
        return ""
    except Exception as exc:
        print(f"[OCR] Failed: {exc}", flush=True)
        return ""


def download_pdf_text(url: str) -> str:
    r = requests.get(url, timeout=45, headers=HEADERS)
    r.raise_for_status()

    pdf_bytes = r.content
    reader = PdfReader(io.BytesIO(pdf_bytes))

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")

    text = "\n".join(pages)

    # Patch 36: OCR fallback for scanned/image PDFs.
    # If pypdf extracts fewer than MIN_TEXT_CHARS the PDF has no usable
    # text layer — rasterise and OCR instead.
    if len(text.strip()) < MIN_TEXT_CHARS:
        print(f"[OCR] Text layer too short ({len(text.strip())} chars) — attempting OCR", flush=True)
        ocr_text = _ocr_pdf_bytes(pdf_bytes)
        if ocr_text.strip():
            print(f"[OCR] Success — extracted {len(ocr_text.strip())} chars", flush=True)
            print(f"[OCR] Preview: {ocr_text.strip()[:600]}", flush=True)
            return ocr_text
        else:
            print("[OCR] No text recovered — returning original extraction", flush=True)

    return text


def compact(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
