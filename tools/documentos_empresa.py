# tools/documentos_empresa.py — Documentos e Ficheiros do Basecamp, espalhados
# por vários projetos, como base de conhecimento da empresa para a Alma consultar.
#
# Usa o endpoint global de recordings (o mesmo que tarefas_e_cards_atrasados já
# usa para Todos e Cards) filtrado por type=Document e type=Upload — dá acesso a
# tudo o que a conta da Alma já vê em qualquer projeto, sem ter de percorrer as
# pastas (Vaults) de cada projeto uma a uma.
import io, os, time
from email import policy
from email.parser import BytesParser
from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document as DocxDocument
from tools import basecamp, visao

_cache = {}  # {"lista": (timestamp, lista)}
TTL = 900  # segundos — documentos de empresa não mudam a cada minuto

TIPOS_DE_FICHEIRO_LEGIVEIS = {
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "message/rfc822",
}

# quando alguém arrasta um email (.eml) para o Basecamp, o browser nem
# sempre reporta um content_type útil (fica "application/octet-stream" ou
# vazio) — a extensão do ficheiro é o sinal mais fiável nesses casos. Só
# serve de reserva quando o content_type não identifica nada por si só.
_EXTENSAO_PARA_TIPO = {".eml": "message/rfc822"}
_TIPOS_INCONCLUSIVOS = {"", "application/octet-stream", "binary/octet-stream"}

def _tipo_efetivo(ctype: str, filename: str) -> str:
    """O content_type a usar de facto para decidir como extrair o texto —
    normalmente o que o Basecamp devolve, mas com reserva pela extensão do
    nome do ficheiro quando esse content_type não diz nada (ex: um .eml
    identificado só como "application/octet-stream")."""
    ctype = ctype or ""
    if ctype not in _TIPOS_INCONCLUSIVOS:
        return ctype
    ext = os.path.splitext(filename or "")[1].lower()
    return _EXTENSAO_PARA_TIPO.get(ext, ctype)

def _texto_simples(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

def _extrair_email(bruto: bytes) -> str:
    """Extrai de um ficheiro de email (.eml) os campos úteis (de/para/
    assunto/data) e o corpo em texto — prefere a versão texto simples do
    email, e limpa o HTML da versão em html quando não há texto simples."""
    msg = BytesParser(policy=policy.default).parsebytes(bruto)
    cabecalho = [
        f"De: {msg.get('from', '(desconhecido)')}",
        f"Para: {msg.get('to', '(desconhecido)')}",
        f"Assunto: {msg.get('subject', '(sem assunto)')}",
        f"Data: {msg.get('date', '(sem data)')}",
    ]
    corpo = msg.get_body(preferencelist=("plain", "html"))
    texto_corpo = ""
    if corpo is not None:
        texto_corpo = corpo.get_content()
        if corpo.get_content_type() == "text/html":
            texto_corpo = _texto_simples(texto_corpo)
    return ("\n".join(cabecalho) + "\n\n" + texto_corpo.strip()).strip()

def _listar_bruto() -> list[dict]:
    if "lista" in _cache:
        ts, lista = _cache["lista"]
        if time.time() - ts < TTL:
            return lista
    itens = []
    for tipo in ("Document", "Upload"):
        registos = basecamp._get_paginado(
            f"{basecamp._base_url()}/projects/recordings.json",
            params={"type": tipo, "status": "active"}, etiqueta=tipo)
        for r in registos:
            itens.append({
                "id": r["id"],
                "tipo": "documento" if tipo == "Document" else "ficheiro",
                "titulo": r.get("title") or r.get("filename") or "(sem título)",
                "projeto": (r.get("bucket") or {}).get("name"),
                "pasta": (r.get("parent") or {}).get("title"),
                "url": r["url"],
                "app_url": r.get("app_url"),
                "content_type": r.get("content_type"),
                "filename": r.get("filename"),
                "download_url": r.get("download_url"),
            })
    _cache["lista"] = (time.time(), itens)
    return itens

def procurar_documentos_empresa(pesquisa: str) -> list[dict]:
    """Procura documentos e ficheiros da empresa no Basecamp (id, título, projeto, pasta),
    em todos os projetos onde a Alma tem acesso. Filtra por título, projeto ou pasta
    conterem o termo indicado — há mais de mil documentos no total, por isso listar tudo
    de uma vez não é prático; usa um termo relacionado com o que procuras (ex: nome do
    documento, do projeto ou do tema). Devolve no máximo 40 resultados."""
    termo = pesquisa.lower().strip()
    correspondem = [
        item for item in _listar_bruto()
        if termo in item["titulo"].lower()
        or termo in (item.get("projeto") or "").lower()
        or termo in (item.get("pasta") or "").lower()
    ]
    return [{k: v for k, v in item.items() if k in ("id", "tipo", "titulo", "projeto", "pasta")}
            for item in correspondem[:40]]

def _extrair_por_tipo(bruto: bytes, ctype: str) -> str:
    """Extrai texto de bytes crus dado o content_type — partilhado entre
    ficheiros (Uploads) e anexos embutidos dentro de Documentos nativos do
    Basecamp (ver _ler_conteudo)."""
    if ctype in visao.TIPOS_DE_IMAGEM:
        return visao.descrever_imagem(bruto, ctype)
    if ctype == "application/pdf":
        leitor = PdfReader(io.BytesIO(bruto))
        texto = "\n".join(pagina.extract_text() or "" for pagina in leitor.pages).strip()
        if not texto:
            # sem texto extraível — provavelmente um PDF só de design/imagem;
            # tenta ler a primeira página como imagem antes de desistir.
            try:
                texto = visao.descrever_imagem(visao.renderizar_primeira_pagina_pdf(bruto), "image/png")
            except Exception as e:
                texto = f"(não consegui extrair texto nem imagem deste PDF: {e})"
        return texto
    if ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc = DocxDocument(io.BytesIO(bruto))
        return "\n".join(paragrafo.text for paragrafo in doc.paragraphs).strip()
    if ctype in ("text/plain", "text/csv"):
        return bruto.decode("utf-8", errors="ignore")
    if ctype == "message/rfc822":
        return _extrair_email(bruto)
    return None

def _ler_conteudo(item: dict) -> str:
    """Extrai o texto de um documento/ficheiro (item de _listar_bruto()) —
    partilhado entre ler_documento_empresa (um id específico) e outros
    sítios que precisem de ler vários documentos de uma vez (ex: um projeto
    inteiro tratado como fonte de confiança). Devolve None quando o tipo de
    ficheiro não é legível (quem chamar decide o que fazer nesse caso)."""
    if item["tipo"] == "documento":
        # um Documento nativo do Basecamp pode ter o texto todo escrito nele
        # próprio, ou pode ser só um invólucro com o conteúdo real num
        # ficheiro anexado lá dentro (PDF, Word, imagem) — lê os dois e
        # junta, em vez de assumir que é sempre um ou outro.
        completo = basecamp.obter_recording(item["url"])
        texto_wrapper = _texto_simples(completo.get("content", "")).strip()
        partes = [texto_wrapper] if texto_wrapper else []
        for anexo in completo.get("content_attachments") or []:
            ctype_anexo = _tipo_efetivo(anexo.get("content_type"), anexo.get("filename"))
            try:
                bruto_anexo = basecamp._get_bytes(anexo["download_url"])
                texto_anexo = _extrair_por_tipo(bruto_anexo, ctype_anexo)
                if texto_anexo:
                    partes.append(texto_anexo)
            except Exception as e:
                partes.append(f"(erro ao ler um anexo deste documento: {e})")
        texto_final = "\n\n".join(partes).strip()
        return texto_final[:6000] if texto_final else None

    ctype = _tipo_efetivo(item.get("content_type"), item.get("filename"))

    if ctype in visao.TIPOS_DE_IMAGEM:
        bruto = basecamp._get_bytes(item["download_url"])
        return visao.descrever_imagem(bruto, ctype)

    if ctype not in TIPOS_DE_FICHEIRO_LEGIVEIS:
        return None

    bruto = basecamp._get_bytes(item["download_url"])
    return _extrair_por_tipo(bruto, ctype)[:6000]

def ler_documento_empresa(id: int) -> dict:
    """Lê o conteúdo de texto de um documento ou ficheiro da empresa, pelo id
    (de listar_documentos_empresa). Suporta documentos nativos do Basecamp,
    PDF, Word (.docx), email (.eml), texto simples e CSV."""
    item = next((i for i in _listar_bruto() if i["id"] == id), None)
    if not item:
        return {"erro": "documento não encontrado — confirma o id com procurar_documentos_empresa"}

    conteudo = _ler_conteudo(item)
    if conteudo is None:
        if item["tipo"] == "documento":
            return {"erro": "este documento parece estar vazio (sem texto e sem anexos legíveis)",
                    "titulo": item["titulo"], "app_url": item.get("app_url")}
        ctype = item.get("content_type") or ""
        return {"erro": f"não consigo ler o conteúdo deste tipo de ficheiro ({ctype or item.get('filename')})",
                "titulo": item["titulo"], "app_url": item.get("app_url")}
    return {"titulo": item["titulo"], "conteudo": conteudo}

def ler_anexos_registo_basecamp(url: str) -> dict:
    """Lê o conteúdo dos ficheiros anexados diretamente a um registo do
    Basecamp — a descrição de uma tarefa/card, OU um comentário (ex: um PDF
    de desenho técnico ou especificações de um produto partilhado num
    comentário, não só na tarefa em si). `url` é o url da própria API desse
    registo (o campo "url" que já vem no contexto ou em ler_comentarios —
    nunca inventes ou reconstruas este url a partir só do id: o Basecamp
    aninha os recordings sob o bucket do projeto, um formato tipo
    ".../recordings/{id}.json" na raiz não existe e dá sempre 404). Não é
    para documentos/ficheiros avulsos — para isso usa
    procurar_documentos_empresa/ler_documento_empresa."""
    try:
        recording = basecamp.obter_recording(url)
    except Exception as e:
        return {"erro": f"não consegui aceder a este registo do Basecamp: {e}"}

    anexos = recording.get("content_attachments") or []
    if not anexos:
        return {"anexos": [], "aviso": "este registo não tem ficheiros anexados diretamente"}

    resultados = []
    for anexo in anexos:
        nome = anexo.get("filename") or anexo.get("name") or "(sem nome)"
        ctype = _tipo_efetivo(anexo.get("content_type"), nome)
        try:
            bruto = basecamp._get_bytes(anexo["download_url"])
            texto = _extrair_por_tipo(bruto, ctype)
            resultados.append({"ficheiro": nome, "conteudo": (texto or "(sem texto legível)")[:6000]})
        except Exception as e:
            resultados.append({"ficheiro": nome, "erro": str(e)})
    return {"anexos": resultados}

TOOLS_DOCUMENTOS_EMPRESA = [
    {
        "name": "procurar_documentos_empresa",
        "description": "Procura documentos e ficheiros da empresa guardados no Basecamp, em todos os projetos (id, tipo, título, projeto, pasta), por um termo no título/projeto/pasta. Usa isto para descobrir que documentos existem antes de leres um com ler_documento_empresa.",
        "input_schema": {
            "type": "object",
            "properties": {"pesquisa": {"type": "string"}},
            "required": ["pesquisa"]
        }
    },
    {
        "name": "ler_documento_empresa",
        "description": "Lê o conteúdo de texto de um documento ou ficheiro da empresa, pelo id devolvido por procurar_documentos_empresa. Suporta documentos nativos do Basecamp, PDF (mesmo quando o PDF é só design/imagem sem texto), Word (.docx), email (.eml — lê de/para/assunto/data e o corpo do email), imagens (JPG, PNG, GIF, WebP — descritas/transcritas por visão), texto simples e CSV — outros formatos (folhas de cálculo, etc.) devolvem um erro com o link para abrir manualmente.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"]
        }
    },
    {
        "name": "ler_anexos_registo_basecamp",
        "description": "Lê o conteúdo dos ficheiros anexados diretamente a um registo do Basecamp — a descrição de uma tarefa/card, OU um comentário específico (ex: um PDF de desenho técnico ou especificações de um produto, suporta os mesmos formatos que ler_documento_empresa). Usa isto quando a pergunta precisar de informação que só está nesses anexos (ex: \"qual o tamanho da prateleira?\", medidas, especificações) — não leias por rotina em toda tarefa/card, só quando a pergunta for mesmo sobre isso. `url` é sempre o url da própria API desse registo (o campo \"url\" que já vem no contexto, ou o de um comentário específico devolvido por ler_comentarios) — nunca inventes este url a partir só de um número/id, o Basecamp aninha os recordings sob o bucket do projeto e um url reconstruído à mão dá sempre 404.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "o url da própria API da tarefa/card ou do comentário — nunca inventado"}},
            "required": ["url"]
        }
    }
]
