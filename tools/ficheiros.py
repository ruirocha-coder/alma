# tools/ficheiros.py — extrai texto de um ficheiro em bruto (bytes), qualquer
# que seja a origem (aqui: anexos enviados na consola de chat). Reaproveita a
# mesma lógica de leitura já usada para documentos do Basecamp.
import io, os
from pypdf import PdfReader
from docx import Document as DocxDocument
from tools import visao

TIPOS_DE_TEXTO = {"text/plain", "text/csv", "text/markdown"}
EXTENSOES_DE_TEXTO = (".txt", ".csv", ".md")

# o browser nem sempre reporta um content_type de imagem fiável para um
# upload (fica genérico ou vazio, dependendo do browser/sistema) — a
# extensão do nome do ficheiro é o sinal de reserva, tal como já se faz
# para anexos do Basecamp (ver tools/documentos_empresa._tipo_efetivo).
_EXTENSAO_PARA_TIPO_IMAGEM = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
}

def extrair_texto(bruto: bytes, content_type: str, filename: str = "") -> str | None:
    """Devolve o texto extraído do ficheiro, ou None se o tipo não for suportado."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    nome = (filename or "").lower()
    extensao = os.path.splitext(nome)[1]

    if content_type in visao.TIPOS_DE_IMAGEM or extensao in _EXTENSAO_PARA_TIPO_IMAGEM:
        media_type = content_type if content_type in visao.TIPOS_DE_IMAGEM else _EXTENSAO_PARA_TIPO_IMAGEM[extensao]
        return visao.descrever_imagem(bruto, media_type)

    if content_type == "application/pdf" or nome.endswith(".pdf"):
        leitor = PdfReader(io.BytesIO(bruto))
        texto = "\n".join(pagina.extract_text() or "" for pagina in leitor.pages).strip()
        if texto:
            return texto
        # sem texto extraível — provavelmente um PDF só de design/imagem/
        # scan; descreve página a página em vez de só a primeira
        try:
            return visao.descrever_pdf_escaneado(bruto)
        except Exception as e:
            return f"(não consegui extrair texto nem imagem deste PDF: {e})"

    if (content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or nome.endswith(".docx")):
        doc = DocxDocument(io.BytesIO(bruto))
        return "\n".join(paragrafo.text for paragrafo in doc.paragraphs).strip()

    if content_type in TIPOS_DE_TEXTO or nome.endswith(EXTENSOES_DE_TEXTO):
        return bruto.decode("utf-8", errors="ignore")

    return None
