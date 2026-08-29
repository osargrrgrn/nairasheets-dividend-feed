import io
import re
import requests
from pypdf import PdfReader

HEADERS = {
    "User-Agent": "NairaSheetsDividendFeed/1.0 (+public NGX corporate disclosure indexing)"
}

def download_pdf_text(url: str) -> str:
    r = requests.get(url, timeout=45, headers=HEADERS)
    r.raise_for_status()

    reader = PdfReader(io.BytesIO(r.content))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages)

def compact(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
