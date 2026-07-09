# tools/procedimentos.py — lê o documento de procedimentos da empresa, guardado
# como PDF na Google Drive.
#
# Exige que o ficheiro esteja partilhado como "qualquer pessoa com o link pode
# ver" — assim não é preciso OAuth à Google, só o ID do ficheiro.
import io, os, time
import httpx
from pypdf import PdfReader

_cache = {}  # {"texto": (timestamp, texto)}
TTL = 3600  # segundos — reler a cada hora chega para um documento institucional

def procedimentos_empresa() -> str:
    """Texto completo do manual de procedimentos da empresa (extraído do PDF)."""
    if "texto" in _cache:
        ts, texto = _cache["texto"]
        if time.time() - ts < TTL:
            return texto
    ficheiro_id = os.environ["PROCEDIMENTOS_DOC_ID"]
    r = httpx.get("https://drive.google.com/uc",
                  params={"export": "download", "id": ficheiro_id},
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    leitor = PdfReader(io.BytesIO(r.content))
    texto = "\n".join(pagina.extract_text() or "" for pagina in leitor.pages).strip()
    _cache["texto"] = (time.time(), texto)
    return texto
