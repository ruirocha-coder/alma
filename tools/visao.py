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

def renderizar_primeira_pagina_pdf(bruto: bytes) -> bytes:
    """Converte a primeira página de um PDF em imagem PNG — usado quando o PDF
    não tem texto extraível (provavelmente é só design/imagem)."""
    doc = fitz.open(stream=bruto, filetype="pdf")
    pixmap = doc[0].get_pixmap(dpi=150)
    return pixmap.tobytes("png")
