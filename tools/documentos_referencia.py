# tools/documentos_referencia.py — os documentos de referência da empresa,
# atualizados e escolhidos manualmente pela equipa (ao contrário de
# procurar_documentos_empresa, que varre tudo o que já foi carregado no
# Basecamp ao longo dos anos, incluindo muito material desatualizado).
#
# Alguns destes documentos são ficheiros anexados dentro de um Documento
# nativo do Basecamp (não vivem no campo "content" em si, que é só um
# invólucro de texto), por isso lê-se o anexo PDF diretamente.
import io, os, time
import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from tools import basecamp, procedimentos, visao

_cache = {}  # {"conteudo": (timestamp, dict)}
TTL = 3600  # segundos

def _extrair_pdf(bruto: bytes) -> str:
    leitor = PdfReader(io.BytesIO(bruto))
    texto = "\n".join(pagina.extract_text() or "" for pagina in leitor.pages).strip()
    if texto:
        return texto
    # sem texto extraível — provavelmente um PDF só de design/imagem; tenta
    # ler a primeira página como imagem antes de desistir.
    return visao.descrever_imagem(visao.renderizar_primeira_pagina_pdf(bruto), "image/png")

def _texto_simples(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

def _ler_ficheiro_basecamp(url: str) -> str:
    """Lê o texto de um Upload (ficheiro anexado diretamente) do Basecamp."""
    item = basecamp.obter_recording(url)
    bruto = basecamp._get_bytes(item["download_url"])
    return _extrair_pdf(bruto)

def _ler_documento_com_anexo_basecamp(url: str) -> str:
    """Lê um Documento nativo do Basecamp cujo conteúdo real está num PDF
    anexado dentro dele (o texto do documento em si é só um invólucro)."""
    item = basecamp.obter_recording(url)
    anexos = [a for a in (item.get("content_attachments") or []) if a.get("content_type") == "application/pdf"]
    if not anexos:
        return _texto_simples(item.get("content", ""))
    bruto = basecamp._get_bytes(anexos[0]["download_url"])
    return _extrair_pdf(bruto)

def _ler_pdf_drive(ficheiro_id: str) -> str:
    """Lê um PDF público (link partilhado) da Google Drive, pelo seu id."""
    r = httpx.get("https://drive.google.com/uc", params={"export": "download", "id": ficheiro_id},
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    return _extrair_pdf(r.content)

# Registo curado à mão — atualizar sempre que a equipa confirmar um novo
# documento de referência atual, ou substituir um destes.
_DOCUMENTOS = [
    {
        "nome": "BS Livro Digital Princípios",
        "ler": lambda: _ler_ficheiro_basecamp("https://3.basecampapi.com/3313526/buckets/603157/uploads/7173504299.json"),
    },
    {
        "nome": "Estratégia Expoente 2026",
        "ler": lambda: _ler_pdf_drive("1Kg1Z7akKlhhaAjExzWuxxjJRzys6jTa5"),
    },
    {
        "nome": "BS Tom de Voz",
        "ler": lambda: _ler_documento_com_anexo_basecamp("https://3.basecampapi.com/3313526/buckets/25290433/documents/5480069484.json"),
    },
    {
        "nome": "Manual de Procedimentos Interior Guider",
        "ler": procedimentos.procedimentos_empresa,
    },
    {
        "nome": "Parâmetros Boasafra",
        "ler": lambda: _ler_documento_com_anexo_basecamp("https://3.basecampapi.com/3313526/buckets/603157/documents/1169176166.json"),
    },
    {
        "nome": "Proteção de Químicos",
        "ler": lambda: _ler_ficheiro_basecamp("https://3.basecampapi.com/3313526/buckets/603157/uploads/1169164158.json"),
    },
]

def documentos_referencia_empresa() -> dict:
    """Devolve o conteúdo dos documentos de referência da empresa — atuais e
    confirmados pela equipa como fiáveis, ao contrário de qualquer outro
    documento encontrado por procurar_documentos_empresa, que pode estar
    desatualizado."""
    if "conteudo" in _cache:
        ts, dados = _cache["conteudo"]
        if time.time() - ts < TTL:
            return dados
    dados = {}
    for doc in _DOCUMENTOS:
        try:
            texto = doc["ler"]().strip()
            dados[doc["nome"]] = texto[:6000] if texto else (
                "(este documento não tem texto que se consiga extrair automaticamente — "
                "provavelmente é um ficheiro gráfico/design; não inventes o conteúdo, "
                "diz que não consegues ler este documento e sugere abri-lo diretamente)")
        except Exception as e:
            dados[doc["nome"]] = f"(erro ao ler este documento: {e})"
    _cache["conteudo"] = (time.time(), dados)
    return dados

TOOLS_DOCUMENTOS_REFERENCIA = [
    {
        "name": "documentos_referencia_empresa",
        "description": "Lê os documentos de referência atuais e fiáveis da empresa (princípios, tom de voz, estratégia, procedimentos, parâmetros de marca, proteção de químicos) — confirmados pela equipa como atualizados. Usa isto como fonte de confiança; qualquer outro documento encontrado por procurar_documentos_empresa pode estar desatualizado, usa-o com cautela.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]
