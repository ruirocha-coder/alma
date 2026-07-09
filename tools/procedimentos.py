# tools/procedimentos.py — lê o documento de procedimentos da empresa (Google Docs).
#
# Exige que o documento esteja partilhado como "qualquer pessoa com o link pode
# ver" — assim não é preciso OAuth à Google, só o ID do documento.
import os, time
import httpx

_cache = {}  # {"texto": (timestamp, texto)}
TTL = 3600  # segundos — reler a cada hora chega para um documento institucional

def procedimentos_empresa() -> str:
    """Texto completo do documento de procedimentos da empresa."""
    if "texto" in _cache:
        ts, texto = _cache["texto"]
        if time.time() - ts < TTL:
            return texto
    doc_id = os.environ["PROCEDIMENTOS_DOC_ID"]
    r = httpx.get(f"https://docs.google.com/document/d/{doc_id}/export",
                  params={"format": "txt"}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    texto = r.text.strip()
    _cache["texto"] = (time.time(), texto)
    return texto
