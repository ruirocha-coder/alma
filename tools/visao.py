# tools/visao.py — descreve/transcreve imagens usando a visão do Claude, para
# documentos que não têm texto extraível de outra forma (fotos, ficheiros de
# imagem, PDFs que são só design/imagem sem camada de texto).
import base64, io
import anthropic
import fitz  # PyMuPDF
from PIL import Image

_client = anthropic.Anthropic()

MAX_DIMENSAO = 1568  # recomendado pela Anthropic para custo/qualidade

TIPOS_DE_IMAGEM = {"image/jpeg", "image/png", "image/gif", "image/webp"}

def _preparar_imagem(bruto: bytes, content_type: str) -> tuple[bytes, str]:
    """Redimensiona/normaliza a imagem antes de enviar para a API (evita
    imagens demasiado grandes)."""
    try:
        img = Image.open(io.BytesIO(bruto))
        img.thumbnail((MAX_DIMENSAO, MAX_DIMENSAO))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return bruto, content_type

def descrever_imagem(bruto: bytes, content_type: str) -> str:
    """Descreve/transcreve o conteúdo de uma imagem usando a visão do Claude."""
    bruto, media_type = _preparar_imagem(bruto, content_type)
    imagem_b64 = base64.b64encode(bruto).decode()
    resposta = _client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": imagem_b64}},
                {"type": "text", "text": "Descreve o conteúdo desta imagem em detalhe. Se tiver texto, "
                                         "transcreve-o na íntegra. Se for um documento de design/marca, "
                                         "descreve também os elementos visuais relevantes (cores, layout, "
                                         "exemplos) além de qualquer texto."}
            ]
        }]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

# um documento sem texto extraível (design/scan) pode ter várias páginas
# relevantes — descrever só a primeira perdia tudo o resto (ex: um contrato
# ou proposta escaneada de várias páginas). Limitado para não disparar
# dezenas de chamadas de visão num PDF enorme.
LIMITE_PAGINAS_PDF_ESCANEADO = 5

def renderizar_paginas_pdf(bruto: bytes, limite: int = LIMITE_PAGINAS_PDF_ESCANEADO) -> list[bytes]:
    """Converte até `limite` páginas de um PDF em imagens PNG."""
    doc = fitz.open(stream=bruto, filetype="pdf")
    return [doc[i].get_pixmap(dpi=150).tobytes("png") for i in range(min(limite, len(doc)))]

def descrever_pdf_escaneado(bruto: bytes) -> str:
    """Descreve/transcreve um PDF sem texto extraível, página a página (até
    LIMITE_PAGINAS_PDF_ESCANEADO) — usado quando o PDF não tem texto
    extraível (provavelmente é só design/imagem/scan), para não perder
    conteúdo que esteja para além da primeira página."""
    partes = []
    for i, pagina_png in enumerate(renderizar_paginas_pdf(bruto), start=1):
        try:
            partes.append(f"[Página {i}]\n{descrever_imagem(pagina_png, 'image/png')}")
        except Exception as e:
            partes.append(f"[Página {i}] (erro ao descrever esta página: {e})")
    return "\n\n".join(partes)
