# tools/portal_projeto.py — portal de acompanhamento de projeto (Interior
# Guider), gerado a partir de um card do Basecamp e servido como uma página
# HTML autónoma (guardada em Postgres, como os PDFs/Excel de
# tools/documentos_gerados.py, nunca em disco).
#
# CONTRATO (pedido do Rui, 2026-08-05):
#  - A ALMA só substitui os dados factuais (cliente, valores, textos,
#    estado das fases). O texto comercial fixo (o que cada validação
#    significa, os emails de contacto, a legenda de cada fase) fica em
#    código — nunca reescrito pelo modelo por cliente, para nunca variar
#    o compromisso legal/comercial de uma proposta para outra.
#  - A aritmética (crédito, percentagens de pagamento) é feita em JS, no
#    browser do cliente — nunca pelo LLM, nunca aqui em Python.
#  - O estado de cada fase ("validada"/"aguarda"/"prevista") só pode vir
#    de uma marca explícita e literal no card do Basecamp (um comentário
#    "VALIDADO: <Fase>") — nunca de inferência sobre o histórico da
#    conversa. Essa leitura é feita pela Alma antes de chamar esta
#    função; esta função só valida a consistência do que lhe é passado.
import base64
import io
import json
import os
import re

import fitz
import httpx
from PIL import Image

import db
from tools import basecamp, tempo

_TAMANHO_MAX_IMAGEM_PX = 1600

_EMAIL_ESTUDIO = "studio@interiorguider.com"

_SUB_PADRAO = ("Acompanhamento do projeto. Cada fase abre aqui à medida "
               "que avança e é validada.")

_CONTACTO_ROTULO = "Contactar o designer"

# texto comercial fixo por fase — nunca reescrito por cliente (ver
# contrato acima). A ordem desta lista é a ordem real das fases; a
# validação de sequência em _validar_fases_estado depende disto.
_FASES_DEF = [
    {"id": "honorarios", "titulo": "Honorários", "acao": "Aceitar honorários",
     "obs": "A aceitação dá início ao diagnóstico psicoestético e ao desenvolvimento do projeto.",
     "assunto_email": "Aceitação de honorários"},
    {"id": "conceito", "titulo": "Conceito", "acao": "Validar conceito",
     "obs": "Ao validar o conceito, confirma a direção estética e autoriza o desenvolvimento do projeto sobre esta base.",
     "assunto_email": "Validação do conceito"},
    {"id": "projeto", "titulo": "Projeto", "acao": "Validar projeto",
     "obs": "Ao validar o projeto, confirma os ambientes e a especificação apresentados. É esta validação que fecha o orçamento final.",
     "assunto_email": "Validação do projeto"},
    {"id": "orcamento", "titulo": "Orçamento", "acao": "Validar e adjudicar",
     "obs": "A adjudicação confirma a compra integral, aplica o crédito e inicia as encomendas com o pagamento da primeira fase.",
     "assunto_email": "Adjudicação"},
]
_ESTADOS_VALIDOS = ("validada", "aguarda", "prevista")


def _normalizar_texto(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().strip().lower()


def _imagem_pagina_para_base64(doc, pagina, imagens_permitidas: set) -> str:
    imagens = [im for im in pagina.get_images(full=True) if (im[2], im[3]) not in imagens_permitidas]
    if not imagens:
        return None
    xref = max(imagens, key=lambda im: im[2] * im[3])[0]
    info = doc.extract_image(xref)
    try:
        imagem = Image.open(io.BytesIO(info["image"])).convert("RGB")
    except Exception:
        return None
    if max(imagem.size) > _TAMANHO_MAX_IMAGEM_PX:
        imagem.thumbnail((_TAMANHO_MAX_IMAGEM_PX, _TAMANHO_MAX_IMAGEM_PX))
    buffer = io.BytesIO()
    imagem.save(buffer, format="JPEG", quality=85)
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _baixar_pdf_base64(download_url: str) -> dict:
    """Descarrega um PDF do Basecamp e devolve-o como data URI — para
    documentos do portal (apresentação, orçamento detalhado) que a
    cliente tem de poder descarregar sem acesso ao Basecamp. Nunca
    guardes/uses o download_url original diretamente num link do portal:
    exige autenticação que a cliente não tem."""
    try:
        bruto = basecamp._get_bytes(download_url)
    except Exception as exc:
        return {"erro": f"não consegui descarregar o PDF em {download_url}: {exc}"}
    return {"pdf_base64": f"data:application/pdf;base64,{base64.b64encode(bruto).decode('ascii')}"}


def _extrair_imagens_conceito_pdf(download_url: str) -> dict:
    """Extrai, de um PDF "Conceito Psicoestético [Nome cliente]" anexado a
    um card do Basecamp, uma imagem por cada página de ambiente (ex:
    "Sala", "Cozinha", "Suite Master" — uma página por espaço, cada uma
    com o seu próprio render/fotografia guia). Nunca a do "Moodboard"
    (a colagem de materiais) nem a da página de estilo (a que só tem uma
    linha "Estilo A | Estilo B | Estilo C") — essas não são imagens de
    ambiente. A primeira imagem de ambiente (pela ordem do documento) é a
    que se usa como preview da fase "Conceito"; as restantes associam-se
    aos ambientes do projeto pelo nome da página.

    Uso interno de gerar_portal_projeto — nunca exposta como tool ao
    modelo. Motivo (bug real, 2026-08-06): quando isto era uma tool
    separada, a Alma tinha de copiar o base64 devolvido (centenas de KB)
    para o argumento seguinte, e truncava-o sem dar por isso — a imagem
    ficava corrompida/em branco no portal, sem nenhum erro visível. Ao
    receber aqui só o download_url (uma string curta), a extração e a
    codificação para base64 ficam inteiramente em código, sem nunca
    passar pelos tokens do modelo.

    A extração é determinística: cada página deste template tem um
    cabeçalho fixo "CONCEITO PSICOESTÉTICO" seguido de um título curto
    (o nome do ambiente, ou "Moodboard", ou a linha de estilo com "|").
    Só as páginas com esse cabeçalho E um título que não seja "Moodboard"
    nem contenha "|" contam como página de ambiente; delas extrai-se a
    maior imagem embutida, ignorando o logótipo pequeno que se repete em
    quase todas as páginas (detetado por aparecer 3+ vezes com as mesmas
    dimensões). Se o PDF não tiver esta estrutura (ex: um template mais
    antigo, "Imagem Guia"), cai para a maior imagem de todo o documento,
    sem título — só serve então como imagem de preview, sem associação a
    ambientes. Devolve {"erro": "..."} se não conseguir descarregar,
    abrir o PDF, ou não encontrar nenhuma imagem.
    """
    try:
        bruto = basecamp._get_bytes(download_url)
    except Exception as exc:
        return {"erro": f"não consegui descarregar o PDF em {download_url}: {exc}"}

    try:
        doc = fitz.open(stream=bruto, filetype="pdf")
    except Exception as exc:
        return {"erro": f"não consegui abrir o ficheiro como PDF: {exc}"}

    # o PDF original, embutido como data URI — para o link de download do
    # próprio documento no portal (a cliente não tem acesso ao Basecamp,
    # por isso o download_url original não lhe serve; o ficheiro tem de
    # estar dentro da própria página, tal como as imagens).
    pdf_base64 = f"data:application/pdf;base64,{base64.b64encode(bruto).decode('ascii')}"

    from collections import Counter
    frequencia_dims = Counter()
    for pagina in doc:
        for im in pagina.get_images(full=True):
            frequencia_dims[(im[2], im[3])] += 1
    dims_recorrentes = {d for d, c in frequencia_dims.items() if c >= 3}

    paginas_ambiente = []
    for pagina in doc:
        blocos = pagina.get_text("blocks")
        textos = [b[4].strip() for b in blocos if b[4].strip()]
        if not any(_normalizar_texto(t) == "conceito psicoestetico" for t in textos):
            continue
        # a capa também tem o texto "Conceito Psicoestético" (é o título do
        # documento) — só as páginas de conteúdo têm o rodapé com "|"
        # ("Conceito Psicoestético | Criativa ... | ..."); usa isso para
        # nunca confundir a capa com uma página de ambiente.
        if not any("|" in t for t in textos):
            continue
        candidatos_titulo = [t for t in textos
                             if _normalizar_texto(t) != "conceito psicoestetico" and "|" not in t and len(t) < 40]
        if not candidatos_titulo:
            continue
        titulo = min(candidatos_titulo, key=len)
        if _normalizar_texto(titulo) == "moodboard":
            continue
        imagem_b64 = _imagem_pagina_para_base64(doc, pagina, dims_recorrentes)
        if imagem_b64:
            paginas_ambiente.append({"titulo": titulo, "imagem_base64": imagem_b64})

    if paginas_ambiente:
        return {"paginas": paginas_ambiente, "pdf_base64": pdf_base64}

    # PDF sem a estrutura "Conceito Psicoestético" (ex: outros templates,
    # como "CONCEITO"/"Imagem GUIA") — nunca adivinhar aqui qual imagem é o
    # conceito. Bug real, 2026-08-26 (Mafalda Pinheiro): a maior imagem do
    # documento era uma fotografia "antes" do apartamento da própria
    # cliente (maior em pixels do que os renders reais, que estavam
    # noutras páginas) — ia parar ao portal como se fosse o conceito.
    # Sem forma fiável de distinguir "antes" de render sem perceber o
    # layout de cada template, é preferível falhar alto e pedir para
    # escolher a imagem à mão na página de edição do que arriscar mostrar
    # a fotografia errada à cliente.
    return {"erro": ("este PDF não tem a estrutura \"Conceito Psicoestético\" que permite escolher a imagem "
                     "automaticamente — para nunca arriscar mostrar a imagem errada à cliente (ex: uma "
                     "fotografia \"antes\" em vez do render), não adivinho aqui qual imagem usar. Usa a "
                     "página de edição do portal para carregar a imagem certa à mão.")}


# páginas cujo título (ver _extrair_imagem_apresentacao_pdf) nunca são um
# ambiente real, mesmo tendo o rodapé recorrente do documento — moodboard
# (colagem de materiais), plantas técnicas e a página de anexos/materiais.
# "conceito" também bloqueado: bug real, 2026-08-26 (Mafalda Pinheiro) — a
# página de introdução "CONCEITO" de uma apresentação de projeto completa
# reutiliza a fotografia "antes" da própria cliente como ilustração, e essa
# era a maior imagem do documento a seguir à capa; sem este bloqueio, a
# função apanhava-a como se fosse o primeiro render de ambiente.
_TITULOS_BLOQUEADOS_APRESENTACAO = {"moodboard", "anexos", "conceito"}
_PREFIXOS_BLOQUEADOS_APRESENTACAO = ("planta",)


def _extrair_imagem_apresentacao_pdf(download_url: str) -> dict:
    """Extrai, do PDF "Apresentação do Projeto" anexado a um card do
    Basecamp, a primeira imagem de ambiente (a que serve de capa da fase
    "Projeto" no portal — tal como a imagem de conceito serve de capa da
    fase "Conceito"). Nunca a do "Moodboard", de uma "Planta" (desenho
    técnico) nem da página de "Anexos" — verificado ao vivo contra um
    PDF real (2026-08-07): estas três nunca são a imagem certa.

    Ao contrário do PDF do conceito (uma página por ambiente, sempre com
    o mesmo cabeçalho fixo "CONCEITO PSICOESTÉTICO"), o template de
    apresentação varia por designer — não há um cabeçalho fixo a
    procurar. Em vez disso, deteta-se o texto que se REPETE em quase
    todas as páginas de conteúdo (o rodapé "Projecto de Design de
    Interiores..." num PDF real inspecionado) para distinguir páginas de
    conteúdo da capa/página final (que não têm esse rodapé); dentro
    dessas, o título da página é o texto curto que não é o rodapé. Se o
    documento não tiver esse rodapé recorrente (template sem essa
    estrutura), cai para a maior imagem de todo o documento, sem título
    — mesmo comportamento de reserva de _extrair_imagens_conceito_pdf.

    Uso interno de gerar_portal_projeto — nunca exposta como tool ao
    modelo (mesmo motivo de _extrair_imagens_conceito_pdf: nunca passar
    um base64 grande pelos tokens do modelo)."""
    try:
        bruto = basecamp._get_bytes(download_url)
    except Exception as exc:
        return {"erro": f"não consegui descarregar o PDF em {download_url}: {exc}"}

    try:
        doc = fitz.open(stream=bruto, filetype="pdf")
    except Exception as exc:
        return {"erro": f"não consegui abrir o ficheiro como PDF: {exc}"}

    pdf_base64 = f"data:application/pdf;base64,{base64.b64encode(bruto).decode('ascii')}"

    from collections import Counter
    n_paginas = len(doc)

    textos_por_pagina = []
    for pagina in doc:
        blocos = pagina.get_text("blocks")
        textos_por_pagina.append([b[4].strip() for b in blocos if b[4].strip()])

    frequencia_texto = Counter()
    for textos in textos_por_pagina:
        for t in textos:
            frequencia_texto[_normalizar_texto(t)] += 1
    rodape_recorrente = {t for t, c in frequencia_texto.items() if c >= max(3, n_paginas * 0.4)}

    frequencia_dims = Counter()
    for pagina in doc:
        for im in pagina.get_images(full=True):
            frequencia_dims[(im[2], im[3])] += 1
    dims_recorrentes = {d for d, c in frequencia_dims.items() if c >= 3}

    def _pagina_valida(titulo: str) -> bool:
        t = _normalizar_texto(titulo)
        return t not in _TITULOS_BLOQUEADOS_APRESENTACAO and not t.startswith(_PREFIXOS_BLOQUEADOS_APRESENTACAO)

    if rodape_recorrente:
        for pagina, textos in zip(doc, textos_por_pagina):
            if not any(_normalizar_texto(t) in rodape_recorrente for t in textos):
                continue  # capa ou página final, sem o rodapé de conteúdo
            candidatos_titulo = [t for t in textos
                                 if _normalizar_texto(t) not in rodape_recorrente and "|" not in t and len(t) < 40]
            if not candidatos_titulo:
                continue
            titulo = min(candidatos_titulo, key=len)
            if not _pagina_valida(titulo):
                continue
            imagem_b64 = _imagem_pagina_para_base64(doc, pagina, dims_recorrentes)
            if imagem_b64:
                return {"imagem_base64": imagem_b64, "pdf_base64": pdf_base64}

    # sem rodapé recorrente detetável (template sem essa estrutura) — cai
    # para a maior imagem de todo o documento, sem título, só como capa.
    candidatas = [(p, im) for p in doc for im in p.get_images(full=True) if (im[2], im[3]) not in dims_recorrentes]
    if not candidatas:
        candidatas = [(p, im) for p in doc for im in p.get_images(full=True)]
    if not candidatas:
        return {"erro": "não encontrei nenhuma imagem dentro deste PDF"}
    pagina_maior, _ = max(candidatas, key=lambda pi: pi[1][2] * pi[1][3])
    imagem_b64 = _imagem_pagina_para_base64(doc, pagina_maior, dims_recorrentes)
    if not imagem_b64:
        return {"erro": "não encontrei nenhuma imagem dentro deste PDF"}
    return {"imagem_base64": imagem_b64, "pdf_base64": pdf_base64}


def _validar_fases_estado(fases_estado: dict) -> str:
    """Devolve uma mensagem de erro (string) se `fases_estado` não tiver
    exatamente as 4 fases esperadas, estados válidos, data sempre que
    "validada", e uma sequência logicamente possível (nunca uma fase
    posterior validada antes de uma anterior) — ou "" se estiver tudo bem."""
    chaves_esperadas = {f["id"] for f in _FASES_DEF}
    if set(fases_estado.keys()) != chaves_esperadas:
        return f"fases_estado tem de ter exatamente estas chaves: {sorted(chaves_esperadas)}"

    viu_nao_validada = False
    viu_aguarda = False
    for fase in _FASES_DEF:
        info = fases_estado[fase["id"]]
        estado = info.get("estado")
        if estado not in _ESTADOS_VALIDOS:
            return f"estado inválido em '{fase['id']}': {estado!r} (tem de ser {_ESTADOS_VALIDOS})"
        if estado == "validada":
            if viu_nao_validada:
                return (f"sequência inconsistente: '{fase['id']}' está validada mas uma fase "
                        f"anterior não está — não é possível validar uma fase antes das que a precedem")
            if not info.get("data"):
                return f"fase '{fase['id']}' está validada mas falta a data"
        else:
            viu_nao_validada = True
            if estado == "aguarda":
                if viu_aguarda:
                    return "só pode haver uma fase 'aguarda' (a próxima em aberto) — encontrei mais do que uma"
                viu_aguarda = True
    return ""

def _mailto(assunto: str, ref: str) -> str:
    from urllib.parse import quote
    return f"mailto:{_EMAIL_ESTUDIO}?subject={quote(assunto + ' ' + ref)}"

def _numero_referencia_card(card_id: int) -> str:
    """O card_id do Basecamp (ex: 10240449388) não diz nada a ninguém —
    quem identifica um cliente/projeto no dia a dia é o número que já vai
    no próprio título do card (ex: "RS | Mafalda Pinheiro 24092025" →
    "24092025"), sempre no formato ddmmaaaa. Pedido do Rui (2026-08-26):
    usar esse número, não o card_id, como referência do portal.

    Vai buscar o título real do card à API (nunca confia numa cópia que a
    Alma possa ter transcrito errado) — se por algum motivo não conseguir
    (card já não existe, API em baixo, título sem esse padrão), cai para
    o card_id em vez de bloquear a geração do portal por causa disto."""
    try:
        projeto = _encontrar_projeto_interior_guider()
        r = httpx.get(f"https://3.basecampapi.com/{os.environ['BASECAMP_ACCOUNT_ID']}"
                      f"/buckets/{projeto['id']}/card_tables/cards/{card_id}.json",
                      headers=basecamp._headers(), timeout=15)
        r.raise_for_status()
        titulo = r.json().get("title", "")
        m = re.search(r"\b(\d{8})\b", titulo)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[portal_projeto] não consegui obter o número de referência do card {card_id}, "
             f"a usar o card_id: {e!r}")
    return str(card_id)

_PROJETO_INTERIOR_GUIDER = None

def _encontrar_projeto_interior_guider() -> dict:
    """Cache simples em memória — o bucket do projeto "Interior Guider"
    (onde vivem todos os cards com portal) não muda durante a vida do
    processo, e chamar isto em cada validação/edição de portal não
    justifica um pedido à API de cada vez."""
    global _PROJETO_INTERIOR_GUIDER
    if _PROJETO_INTERIOR_GUIDER is None:
        # "Interior Guider" sozinho é substring de "Marketing Interior
        # Guider" também — sem o "@" cai nesse projeto errado (mesmo bug
        # de fundo documentado em basecamp._encontrar_projeto). O nome
        # exato deste projeto, tal como está no Basecamp, é "@ Interior
        # Guider" — é onde vivem os cards com portal de projeto.
        _PROJETO_INTERIOR_GUIDER = basecamp._encontrar_projeto("@ Interior Guider")
    return _PROJETO_INTERIOR_GUIDER

def gerar_portal_projeto(utilizador: str, card_id: int, cliente: str, validade: str,
                         honorarios_total: float, honorarios_total_com_iva: bool, honorarios_linhas: list,
                         ambientes: list, fases_estado: dict,
                         valor_produto: float = None, valor_produto_com_iva: bool = False,
                         conceito_pdf_download_url: str = None, conceito_materiais: str = None,
                         conceito_leitura: str = None, documento_apresentacao_download_url: str = None,
                         documento_orcamento_download_url: str = None) -> dict:
    """Gera o portal de acompanhamento de um projeto Interior Guider (página
    HTML autónoma, o link que o cliente abre) a partir dos dados já lidos
    do card do Basecamp, e devolve um url para partilhares no comentário de
    resposta. `card_id` é o id numérico do card (nunca inventado — vem do
    contexto da tarefa/card onde foste mencionada); a referência do portal
    é sempre derivada dele (IG-{card_id}), nunca escrita à mão, para nunca
    haver duas referências diferentes para o mesmo projeto.

    ANTES de decidires qualquer valor monetário: chama
    listar_pdfs_anexados_por_data (nunca tentes tu mesma perceber, lendo o
    texto corrido dos comentários, qual PDF é mais recente — isso já deu
    respostas inconsistentes entre tentativas, porque a ordenação por
    data ficava a cargo da leitura de texto em vez de código). Dessa
    lista já ordenada, usa o PRIMEIRO ficheiro cujo nome corresponda ao
    que procuras (ex: contém "Fee"/"honorário" para honorários, "Product"/
    "orçamento" para o orçamento de produto) — nunca uma versão anterior
    na lista, mesmo que a encontres primeiro na tua leitura do card. Bug
    real, 2026-08-05: usar um PDF de uma proposta antiga deu um valor de
    honorários errado, quando havia uma versão mais recente anexada
    depois. As Notas do card podem também ter um link para uma Google
    Sheet/Doc (não tens ferramenta para abrir isso) — não é um problema
    desde que a lista de PDFs tenha um ficheiro recente com a mesma
    informação; só precisas de sinalizar incerteza ao humano se não
    encontrares NENHUM PDF com o valor final, só esse link.

    Todos os valores monetários mostrados nesta página são o que o CLIENTE
    paga — têm de incluir IVA sempre. `honorarios_total_com_iva` e
    `valor_produto_com_iva` são confirmações explícitas (True só se o
    valor que estás a passar em `honorarios_total`/`valor_produto` JÁ
    inclui IVA) — nunca marques True por suposição, nem calcules o valor
    com IVA tu mesma (23% em Portugal continental, mas nunca faças essa
    conta aqui): se só encontraste o valor sem IVA, ou não tens a certeza,
    passa False e falta o valor com IVA no teu comentário de resposta em
    vez de arriscar. A função recusa-se a gerar o portal se
    `honorarios_total_com_iva` vier False, ou se `valor_produto_com_iva`
    vier False quando `valor_produto` não é nulo — precisamente para
    nunca publicar um valor ambíguo ou incorreto ao cliente.

    `conceito_pdf_download_url`, `conceito_leitura` e `conceito_materiais`
    só podem vir do PDF "Conceito Psicoestético [Nome cliente]" anexado
    ao card
    (procura-o com listar_pdfs_anexados_por_data, filtrando pelo nome
    conter "Conceito Psicoestético" — usa o mais recente se houver mais
    do que um). NUNCA escrevas tu mesma um texto descritivo/poético sobre
    o conceito a partir de notas informais ou da conversa com a cliente
    — isso já aconteceu (2026-08-06) e é exatamente o tipo de invenção
    que esta ferramenta não pode ter: o texto mostrado à cliente tem de
    ser o que a designer já escreveu, não uma composição tua.
    `conceito_pdf_download_url` é o "download_url" desse PDF tal como veio
    de listar_pdfs_anexados_por_data (nunca um download_url obtido de
    outro lado, ex: de um preview_url ou de um link dentro do HTML de um
    comentário — esses podem apontar a um domínio/token diferente e dar
    404). Passa só o url — a extração e a codificação das imagens são
    feitas aqui dentro, em código; nunca tentes tu mesma extrair/descrever
    imagens ou copiar bytes de imagem para este ou para outro argumento
    (bug real, 2026-08-06: copiar um base64 de algumas centenas de KB de
    uma chamada para a outra ficou truncado sem erro visível, e a imagem
    apareceu em branco no portal). A imagem mostrada na fase "Conceito" é
    SEMPRE a primeira imagem de ambiente do PDF (nunca o moodboard, que é
    a colagem de materiais/texturas, nem a página de estilo) — bug real,
    2026-08-06: um teste mostrou o moodboard na fase "Conceito" quando
    devia mostrar a primeira imagem de espaço (ex: a Sala). As restantes
    imagens de ambiente do mesmo PDF são associadas automaticamente a
    cada item de `ambientes` pelo nome — nunca precisas de indicar tu
    mesma qual imagem pertence a qual ambiente. O próprio PDF do conceito
    fica também disponível para a cliente descarregar na fase "Conceito"
    (embutido na página, tal como as imagens — a cliente não tem acesso
    ao Basecamp, por isso não seria possível abrir o download_url
    original diretamente).
    `conceito_materiais` e `conceito_leitura` são ambos opcionais (deixa
    a None) — só os preenchas se existir texto literal e real no PDF ou
    num comentário da designer (ex: uma linha curta "Natural | Eclético |
    Introvertido"). NUNCA componhas tu mesma uma linha de estilo quando
    o PDF não tem esse formato — bug real, 2026-08-06: um projeto com um
    PDF de "imagem guia" mais antigo não tinha essa linha, e em vez de
    deixar o campo vazio foi escrita uma composição própria ("Natural |
    Contemporâneo | Harmonioso") só para preencher o campo; isso é
    exatamente o tipo de invenção que não pode acontecer, mesmo
    assinalada depois na resposta — o texto errado já foi para o portal.
    Se não existir texto literal, deixa ambos os campos a None; a
    imagem (via conceito_pdf_download_url) chega para tornar a fase
    "conceito" visível ao cliente. Se nem a imagem existir ainda, a fase
    "conceito" não pode estar "aguarda" (o cliente veria uma secção sem
    conteúdo real) — mantém-la "prevista" e explica ao humano que falta
    anexar o PDF antes de a abrir ao cliente.

    `valor_produto` é opcional (deixa a None) quando ainda não existe
    nenhum orçamento de produto para este projeto (ex: fase inicial,
    ainda só o conceito em curso) — nunca passes 0 como substituto de
    "ainda não há orçamento", 0€ mostrado ao cliente parece uma
    afirmação real de que o produto não custa nada. Só podes deixar
    `valor_produto` a None se a fase "orcamento" em `fases_estado` for
    "prevista" (ainda não é a próxima em aberto) — se for "aguarda" ou
    "validada", o cliente vai ver essa secção e precisa de um valor real.

    `fases_estado` tem de ter exatamente as chaves "honorarios", "conceito",
    "projeto", "orcamento", cada uma com {"estado": "validada"|"aguarda"|
    "prevista", "data": "10 de agosto" (só obrigatório se "validada")}. O
    estado de cada fase só pode vir de um comentário literal "VALIDADO:
    <Fase>" no card (ex: "VALIDADO: Conceito") — NUNCA de uma leitura geral
    da conversa; se não encontrares essa marca para uma fase, o estado dela
    é "aguarda" (se for a próxima em aberto) ou "prevista" (as seguintes) —
    nunca "validada" sem essa marca explícita. Só pode haver uma fase
    "aguarda" (a próxima em aberto); não pode haver uma fase "validada"
    depois de uma que não esteja.

    `honorarios_linhas`: lista de {"titulo","descricao","valor"} — os itens
    reais dos honorários, tal como aparecem no documento/proposta. `valor_produto`
    é o TOTAL do orçamento de produto (sem honorários) — só o total, nunca o
    detalhe peça a peça (esse fica só no PDF do orçamento, ver
    `documento_orcamento_download_url`). `ambientes`: lista de {"nome","nota",
    "imagem" (url, opcional)}. Todos os valores monetários e textos têm de vir
    explicitamente do card/documentos — nunca inventados nem calculados
    aqui (a aritmética de crédito/pagamentos é feita em JS, no browser do
    cliente).

    `documento_apresentacao_download_url` e `documento_orcamento_download_url`
    são os "download_url" desses PDFs, tal como vêm de
    listar_pdfs_anexados_por_data (nunca um download_url obtido de outro
    lado — ver a mesma nota em `conceito_pdf_download_url`). São
    descarregados e embutidos aqui dentro, tal como o PDF do conceito —
    a cliente não tem acesso ao Basecamp, por isso um download_url
    original nunca lhe serviria diretamente.

    Tal como a fase "conceito" precisa do PDF do conceito, a fase
    "projeto" precisa de `documento_apresentacao_download_url` (o PDF de
    apresentação do projeto) e a fase "orcamento" precisa de
    `documento_orcamento_download_url` (o PDF do orçamento discriminado)
    para poderem estar "aguarda" ou "validada" — sem o respetivo PDF
    anexado ao card, mantém essa fase como "prevista" (fica visível à
    cliente a cinzento, em modo demonstrativo, em vez de aberta sem
    conteúdo real). Do PDF de apresentação extrai-se automaticamente,
    aqui dentro, a primeira imagem de ambiente (nunca o moodboard nem uma
    planta técnica) para servir de capa da fase "projeto" — tal como a
    imagem de conceito serve de capa da fase "conceito"; nunca precisas
    de indicar tu mesma essa imagem.

    NORMA para encontrar o PDF certo com listar_pdfs_anexados_por_data:
    o PDF de apresentação do projeto (para `documento_apresentacao_download_url`)
    tem sempre a palavra "Projeto" no nome (ex: "IG Apresentação PROJETO
    [Nome cliente]") — nunca confundas com o PDF do conceito, que também
    costuma começar por "Apresentação" mas nunca tem "Projeto" no nome
    (ex: "IG Apresentação IMAGEM GUIA [Nome cliente]", ver
    `conceito_pdf_download_url`). Se houver mais do que um com "Projeto"
    no nome, usa o mais recente.

    REGRA ESPECIAL E ABSOLUTA sobre fases já validadas: se este card já
    tiver um portal gerado antes, e alguma fase já lá estiver "validada"
    (porque a cliente clicou mesmo no botão dela), NUNCA passes essa
    mesma fase como "aguarda" ou "prevista" aqui — mantém-na "validada",
    com a mesma data. Bug real, 2026-08-26 (Mafalda Pinheiro): ao chamar
    esta função só para abrir a fase "orçamento", as fases "conceito" e
    "projeto" (já validadas pela cliente, uma delas com um clique real
    dela no botão) foram recompostas como "prevista" — a cliente, ao
    voltar ao portal, via as próprias validações desfeitas. Esta função
    recusa-se agora a gravar nesse caso (ver verificação a seguir); mas
    o mais seguro é sempre perceberes o estado atual antes de chamares,
    para nunca dependeres só desta rede de segurança."""
    erro = _validar_fases_estado(fases_estado)
    if erro:
        return {"erro": erro}
    registo_existente = db.obter_documento_gerado_por_card_id(card_id)
    if registo_existente and registo_existente["formato"] == "html":
        try:
            fases_existentes = json.loads(registo_existente["conteudo_markdown"])["projeto"]["fases"]
        except Exception:
            fases_existentes = []
        for f in fases_existentes:
            if f["estado"] == "validada" and fases_estado.get(f["id"], {}).get("estado") != "validada":
                return {"erro": (f"a fase \"{f['id']}\" já está validada pela cliente (a {f.get('data')}) no "
                                 f"portal atual — não posso desfazer isso. Mantém fases_estado[\"{f['id']}\"] "
                                 f"como {{\"estado\": \"validada\", \"data\": \"{f.get('data')}\"}} e chama "
                                 f"outra vez.")}
    if not honorarios_total_com_iva:
        return {"erro": ("honorarios_total_com_iva tem de ser True — o valor mostrado ao cliente tem de incluir "
                         "IVA. Confirma o valor final (com IVA) na fonte certa (ver Notas do card) antes de "
                         "chamares esta função outra vez.")}
    if valor_produto is not None and not valor_produto_com_iva:
        return {"erro": ("valor_produto_com_iva tem de ser True quando valor_produto não é nulo — o valor "
                         "mostrado ao cliente tem de incluir IVA. Confirma o valor final (com IVA) na fonte "
                         "certa (ver Notas do card) antes de chamares esta função outra vez.")}
    if valor_produto is None and fases_estado["orcamento"]["estado"] != "prevista":
        return {"erro": ("valor_produto é obrigatório quando a fase \"orcamento\" não é \"prevista\" — o "
                         "cliente vai ver essa secção e precisa de um valor real, nunca 0 como substituto de "
                         "\"ainda não há orçamento\".")}
    if fases_estado["conceito"]["estado"] != "prevista" and conceito_pdf_download_url is None:
        return {"erro": ("conceito_pdf_download_url é obrigatório quando a fase \"conceito\" não é \"prevista\" "
                         "— o cliente vai ver essa secção e precisa pelo menos da imagem, extraída do PDF "
                         "\"Conceito Psicoestético\"/\"Imagem Guia\" (usa listar_pdfs_anexados_por_data para "
                         "encontrar o download_url). Se esse PDF ainda não está anexado ao card, mantém a fase "
                         "\"conceito\" como \"prevista\" em vez disso.")}
    if fases_estado["projeto"]["estado"] != "prevista" and documento_apresentacao_download_url is None:
        return {"erro": ("documento_apresentacao_download_url é obrigatório quando a fase \"projeto\" não é "
                         "\"prevista\" — o cliente vai ver essa secção e precisa pelo menos da imagem do "
                         "projeto, extraída do PDF de apresentação (usa listar_pdfs_anexados_por_data para "
                         "encontrar o download_url, procurando por \"Apresentação\"). Se esse PDF ainda não "
                         "está anexado ao card, mantém a fase \"projeto\" como \"prevista\" em vez disso.")}
    if fases_estado["orcamento"]["estado"] != "prevista" and documento_orcamento_download_url is None:
        return {"erro": ("documento_orcamento_download_url é obrigatório quando a fase \"orcamento\" não é "
                         "\"prevista\" — o cliente vai ver essa secção e precisa do PDF do orçamento "
                         "discriminado (usa listar_pdfs_anexados_por_data para encontrar o download_url, "
                         "procurando por \"Orçamento\"/\"ORÇ\"). Se esse PDF ainda não está anexado ao card, "
                         "mantém a fase \"orcamento\" como \"prevista\" em vez disso.")}

    conceito_imagem = None
    documento_conceito = None
    imagens_por_ambiente = {}
    if conceito_pdf_download_url is not None:
        resultado_imagens = _extrair_imagens_conceito_pdf(conceito_pdf_download_url)
        if "erro" in resultado_imagens:
            return {"erro": f"não consegui extrair a imagem do conceito: {resultado_imagens['erro']}"}
        paginas = resultado_imagens["paginas"]
        conceito_imagem = paginas[0]["imagem_base64"]
        documento_conceito = resultado_imagens["pdf_base64"]
        for pagina in paginas:
            if pagina["titulo"]:
                imagens_por_ambiente[_normalizar_texto(pagina["titulo"])] = pagina["imagem_base64"]

    def _imagem_ambiente(nome: str) -> str:
        nome_norm = _normalizar_texto(nome)
        for titulo_norm, imagem in imagens_por_ambiente.items():
            if titulo_norm in nome_norm or nome_norm in titulo_norm:
                return imagem
        return None

    ambientes_com_imagem = [{"nome": a["nome"], "nota": a["nota"],
                            "imagem": _imagem_ambiente(a["nome"]) or a.get("imagem")} for a in ambientes]

    documento_apresentacao = None
    projeto_imagem = None
    if documento_apresentacao_download_url is not None:
        resultado = _extrair_imagem_apresentacao_pdf(documento_apresentacao_download_url)
        if "erro" in resultado:
            return {"erro": f"não consegui obter o documento de apresentação: {resultado['erro']}"}
        documento_apresentacao = resultado["pdf_base64"]
        projeto_imagem = resultado["imagem_base64"]

    documento_orcamento = None
    if documento_orcamento_download_url is not None:
        resultado = _baixar_pdf_base64(documento_orcamento_download_url)
        if "erro" in resultado:
            return {"erro": f"não consegui obter o documento de orçamento: {resultado['erro']}"}
        documento_orcamento = resultado["pdf_base64"]

    return _construir_e_gravar(utilizador, card_id, cliente, validade, honorarios_total, honorarios_linhas,
                               ambientes_com_imagem, fases_estado, valor_produto, conceito_imagem,
                               conceito_materiais, conceito_leitura, documento_apresentacao, documento_orcamento,
                               documento_conceito, projeto_imagem)


def _construir_e_gravar(utilizador: str, card_id: int, cliente: str, validade: str, honorarios_total: float,
                        honorarios_linhas: list, ambientes: list, fases_estado: dict, valor_produto: float,
                        conceito_imagem: str, conceito_materiais: str, conceito_leitura: str,
                        documento_apresentacao: str, documento_orcamento: str, documento_conceito: str = None,
                        projeto_imagem: str = None) -> dict:
    """Constrói o JSON `projeto`, renderiza o HTML e grava — partilhado
    por gerar_portal_projeto (extração a partir do PDF) e
    atualizar_portal_projeto_edicao (valores já editados à mão pela
    designer na página de edição, sem voltar a tocar no PDF)."""
    ref = f"IG-{_numero_referencia_card(card_id)}"
    fases_json = [{
        "id": f["id"], "titulo": f["titulo"], "acao": f["acao"], "obs": f["obs"],
        "estado": fases_estado[f["id"]]["estado"],
        "data": fases_estado[f["id"]].get("data"),
    } for f in _FASES_DEF]
    acoes_json = {f["id"]: _mailto(f["assunto_email"], ref) for f in _FASES_DEF}

    projeto = {
        "ref": ref,
        "cardId": card_id,
        "cliente": cliente,
        "sub": _SUB_PADRAO,
        "contacto": {"rotulo": _CONTACTO_ROTULO, "href": _mailto("Projeto", ref)},
        "validade": validade,
        "honorarios": {"total": honorarios_total, "linhas": [
            {"t": l["titulo"], "d": l["descricao"], "v": l["valor"]} for l in honorarios_linhas
        ]},
        "conceito": {"imagem": conceito_imagem, "leitura": conceito_leitura, "materiais": conceito_materiais},
        "documentos": {"apresentacao": documento_apresentacao, "orcamento": documento_orcamento,
                      "conceito": documento_conceito},
        "ambientes": ambientes,
        "projetoImagem": projeto_imagem,
        "valorProduto": valor_produto,
        "fases": fases_json,
        "acoes": acoes_json,
    }

    # "</" dentro de um valor de texto fecharia a tag <script> a meio — só
    # pode acontecer se um texto citar HTML/markup, mas mesmo assim nunca
    # deve partir a página (bug de segurança comum ao embutir JSON em JS).
    projeto_json = json.dumps(projeto, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE.replace("__PROJETO_JSON__", projeto_json)

    titulo = f"Portal — {cliente}"
    conteudo_fonte = json.dumps({"card_id": card_id, "projeto": projeto}, ensure_ascii=False)
    id_gerado = db.guardar_ou_atualizar_documento_gerado(utilizador, titulo, html.encode("utf-8"), conteudo_fonte,
                                                          card_id, formato="html")
    app_url = os.environ["ALMA_APP_URL"].rstrip("/")
    url = f"{app_url}/documentos-gerados/{id_gerado}"
    url_edicao = f"{app_url}/documentos-gerados/{id_gerado}/editar"
    return {"titulo": titulo, "url": url, "url_edicao": url_edicao, "ref": ref}


def _validar_campos_edicao(honorarios_total_com_iva: bool, valor_produto, valor_produto_com_iva: bool,
                           fases_estado: dict, conceito_imagem, documento_apresentacao, documento_orcamento) -> str:
    """As mesmas regras de gerar_portal_projeto, reaproveitadas por
    atualizar_portal_projeto_edicao — nunca duplicadas à parte, para as
    duas nunca poderem divergir sobre o que é seguro publicar."""
    erro = _validar_fases_estado(fases_estado)
    if erro:
        return erro
    if not honorarios_total_com_iva:
        return "honorarios_total_com_iva tem de estar confirmado — o valor mostrado à cliente tem de incluir IVA."
    if valor_produto is not None and not valor_produto_com_iva:
        return ("valor_produto_com_iva tem de estar confirmado quando valor_produto não é nulo — o valor "
               "mostrado à cliente tem de incluir IVA.")
    if valor_produto is None and fases_estado["orcamento"]["estado"] != "prevista":
        return ("é obrigatório um valor de orçamento de produto quando a fase \"Orçamento\" não está como "
               "\"por abrir\" — a cliente vai ver essa secção e precisa de um valor real.")
    if fases_estado["orcamento"]["estado"] != "prevista" and not documento_orcamento:
        return ("é obrigatório o documento de orçamento discriminado quando a fase \"Orçamento\" não está como "
               "\"por abrir\" — a cliente vai ver essa secção e precisa do PDF do orçamento.")
    if fases_estado["conceito"]["estado"] != "prevista" and not conceito_imagem:
        return ("é obrigatória uma imagem de conceito quando a fase \"Conceito\" não está como \"por abrir\" "
               "— a cliente vai ver essa secção e precisa de uma imagem.")
    if fases_estado["projeto"]["estado"] != "prevista" and not documento_apresentacao:
        return ("é obrigatório o documento de apresentação do projeto quando a fase \"Projeto\" não está como "
               "\"por abrir\" — a cliente vai ver essa secção e precisa do PDF/imagem do projeto.")
    return ""


def atualizar_portal_projeto_edicao(id_documento: int, editado_por: str, campos: dict) -> dict:
    """Grava as alterações feitas na página de edição do portal (uso
    interno, chamado pelo endpoint POST /documentos-gerados/{id}/editar
    em main.py — nunca pela Alma/LLM). Ao contrário de
    gerar_portal_projeto, não volta a tocar no PDF do Basecamp: as
    imagens que a designer não trocar mantêm-se as que já lá estavam."""
    registo = db.obter_documento_gerado(id_documento)
    if not registo or registo["formato"] != "html" or registo["card_id"] is None:
        return {"erro": "este documento não é um portal de projeto editável"}
    card_id = registo["card_id"]

    erro = _validar_campos_edicao(campos["honorarios_total_com_iva"], campos.get("valor_produto"),
                                  campos.get("valor_produto_com_iva", False), campos["fases_estado"],
                                  campos["conceito"].get("imagem"), campos.get("documento_apresentacao"),
                                  campos.get("documento_orcamento"))
    if erro:
        return {"erro": erro}

    return _construir_e_gravar(editado_por, card_id, campos["cliente"], campos["validade"],
                               campos["honorarios_total"], campos["honorarios_linhas"], campos["ambientes"],
                               campos["fases_estado"], campos.get("valor_produto"),
                               campos["conceito"].get("imagem"), campos["conceito"].get("materiais"),
                               campos["conceito"].get("leitura"), campos.get("documento_apresentacao"),
                               campos.get("documento_orcamento"), campos["conceito"].get("documento"),
                               campos.get("projeto_imagem"))


def validar_fase_portal(card_id: int, fase: str) -> dict:
    """Chamada pelo endpoint público POST /portal/{card_id}/validar-fase
    (main.py), a partir do botão que a cliente vê no portal — nunca pela
    Alma/LLM. Marca a fase como validada com a data de hoje, avança a fase
    seguinte para "aguarda" só se esta já tiver o conteúdo obrigatório para
    ser mostrada (as mesmas regras de _validar_campos_edicao — nunca
    duplicadas à parte), grava mantendo o mesmo link, e avisa a equipa com
    um comentário no card do Basecamp (um erro a postar o comentário nunca
    impede a validação de ficar gravada — a cliente não tem culpa disso)."""
    registo = db.obter_documento_gerado_por_card_id(card_id)
    if not registo or registo["formato"] != "html":
        return {"erro": "portal não encontrado"}

    projeto = json.loads(registo["conteudo_markdown"])["projeto"]
    fases_estado = {f["id"]: ({"estado": f["estado"], "data": f["data"]} if f.get("data")
                              else {"estado": f["estado"]}) for f in projeto["fases"]}

    if fase not in fases_estado:
        return {"erro": f"fase desconhecida: {fase!r}"}
    if fases_estado[fase]["estado"] != "aguarda":
        return {"erro": "esta fase já não está a aguardar validação — atualiza a página."}

    titulo_fase = next(f["titulo"] for f in _FASES_DEF if f["id"] == fase)
    fases_estado[fase] = {"estado": "validada", "data": tempo.data_extenso_hoje()}

    ids_fases = [f["id"] for f in _FASES_DEF]
    indice_seguinte = ids_fases.index(fase) + 1
    seguinte = ids_fases[indice_seguinte] if indice_seguinte < len(ids_fases) else None

    honorarios_linhas = [{"titulo": l["t"], "descricao": l["d"], "valor": l["v"]}
                         for l in projeto["honorarios"]["linhas"]]
    valor_produto = projeto.get("valorProduto")
    conceito_imagem = projeto["conceito"].get("imagem")
    documento_apresentacao = projeto["documentos"].get("apresentacao")
    documento_orcamento = projeto["documentos"].get("orcamento")

    aviso = None
    if seguinte and fases_estado[seguinte]["estado"] == "prevista":
        tentativa = dict(fases_estado)
        tentativa[seguinte] = {"estado": "aguarda"}
        # com_iva a True: são valores já publicados, confirmados quando o
        # portal foi gerado — esta chamada só verifica se a fase seguinte
        # já tem o conteúdo (imagem/valor/documento) obrigatório para abrir agora.
        if not _validar_campos_edicao(True, valor_produto, True, tentativa, conceito_imagem,
                                      documento_apresentacao, documento_orcamento):
            fases_estado = tentativa
        else:
            aviso = (f"a fase \"{titulo_fase}\" foi validada, mas a fase seguinte ainda não abriu — "
                     "falta completar o conteúdo dela na página de edição do portal.")

    resultado = _construir_e_gravar(
        "cliente (via portal)", card_id, projeto["cliente"], projeto["validade"],
        projeto["honorarios"]["total"], honorarios_linhas, projeto["ambientes"], fases_estado,
        valor_produto, conceito_imagem, projeto["conceito"].get("materiais"), projeto["conceito"].get("leitura"),
        documento_apresentacao, documento_orcamento,
        projeto["documentos"].get("conceito"), projeto.get("projetoImagem"))

    comentario = (f"A cliente validou a fase \"{titulo_fase}\" no portal do projeto "
                  f"({resultado['ref']}), a {tempo.data_extenso_hoje()}.")
    if aviso:
        # sem isto, o aviso só existia na resposta HTTP da chamada — que
        # ninguém da equipa vê — e a fase seguinte ficava presa sem botão
        # nenhum para a cliente, sem ninguém dar por isso (bug real,
        # 2026-08-26: portal do Gerel Yunden preso assim, sem aviso a
        # ninguém). Tem de ir para o Basecamp, que a equipa acompanha.
        comentario += f"\n\n⚠️ {aviso}"
    try:
        basecamp.comentar(card_id, comentario)
    except Exception as exc:
        aviso = (aviso + " " if aviso else "") + f"(não consegui postar o comentário no Basecamp: {exc})"

    if aviso:
        resultado["aviso"] = aviso
    return resultado

TOOLS_PORTAL_PROJETO = [
    {
        "name": "gerar_portal_projeto",
        "description": (
            "Gera o portal de acompanhamento de um projeto Interior Guider "
            "(a página que o cliente vai ver) a partir dos dados lidos do "
            "card do Basecamp, e devolve um url. Usa isto só quando "
            "pedirem explicitamente o link/portal para um projeto "
            "concreto, e só depois de teres lido a descrição, os "
            "comentários e os anexos desse card — nunca com dados "
            "inventados. IMPORTANTE: chama primeiro "
            "listar_pdfs_anexados_por_data (nunca tentes tu mesma "
            "perceber, lendo os comentários, qual PDF é mais recente — "
            "isso já deu respostas inconsistentes entre tentativas) — "
            "dessa lista já ordenada, usa o primeiro ficheiro relacionado "
            "com honorários/orçamento, nunca uma versão anterior na "
            "lista mesmo que a encontres primeiro (uma proposta inicial "
            "fica desatualizada assim que sai uma fatura/versão final "
            "mais recente). As Notas do card podem ter também um link "
            "para uma Google Sheet/Doc que não consegues abrir — não é "
            "um problema desde que a lista de PDFs tenha um ficheiro "
            "recente com a mesma informação; só precisas de dizer no teu "
            "comentário de resposta que precisas de confirmação humana "
            "se não encontrares NENHUM PDF com o valor final. O estado "
            "de cada fase só pode vir de um comentário literal "
            "\"VALIDADO: <Fase>\" nesse card "
            "(ex: \"VALIDADO: Conceito\") — nunca de uma leitura geral/"
            "informal da conversa; sem essa marca, a fase fica \"aguarda\" "
            "(se for a próxima em aberto) ou \"prevista\". Os valores "
            "monetários e o total do orçamento têm de vir de onde "
            "estiverem explicitamente escritos (um documento/comentário "
            "com o valor final) — nunca calculados ou estimados por ti — "
            "e têm de incluir IVA (é o que o cliente paga); os campos "
            "\"..._com_iva\" confirmam isso e a função recusa-se a gerar o "
            "portal se algum vier False. Se faltar informação clara para "
            "outro campo qualquer, chama a função mesmo assim com o que "
            "tiveres a certeza, deixa os campos incertos vazios/nulos, e "
            "diz claramente no teu comentário de resposta quais campos "
            "precisam de ser confirmados antes de o link ir para o cliente. "
            "O resultado tem dois urls: \"url\" (a página que a cliente vê "
            "— só este vai para a cliente) e \"url_edicao\" (uma página só "
            "para a equipa corrigir/completar campos à mão, sem teres de "
            "gerar o portal outra vez — partilha este só internamente, "
            "num comentário do Basecamp, NUNCA com a cliente)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer", "description": "id numérico do card do Basecamp (nunca inventado)"},
                "cliente": {"type": "string", "description": "nome do cliente, tal como está no card"},
                "validade": {"type": "string", "description": "até quando a proposta/orçamento é válido, ex: \"Proposta válida até 15 de outubro de 2026.\""},
                "honorarios_total": {"type": "number", "description": "valor final COM IVA — o que o cliente paga"},
                "honorarios_total_com_iva": {"type": "boolean", "description": "True só se `honorarios_total` já inclui IVA — nunca True por suposição; nunca calcules o IVA tu mesma, passa False se só tiveres o valor sem IVA"},
                "honorarios_linhas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string"},
                            "descricao": {"type": "string"},
                            "valor": {"type": "number"}
                        },
                        "required": ["titulo", "descricao", "valor"]
                    }
                },
                "conceito_leitura": {"type": "string", "description": "texto real e literal (do PDF \"Conceito Psicoestético\" ou de um comentário da designer) a descrever o conceito por escrito — nunca uma composição tua. Omite/deixa nulo se não existir esse texto; não inventes um parágrafo"},
                "conceito_materiais": {"type": "string", "description": "a linha curta de estilo tal como está escrita no PDF, copiada literalmente (ex: \"Natural | Eclético | Introvertido\") — opcional; omite/deixa nulo se o PDF não tiver essa linha nesse formato, nunca compões uma tu mesma"},
                "conceito_pdf_download_url": {"type": "string", "description": "o campo \"download_url\" de listar_pdfs_anexados_por_data para o PDF \"Conceito Psicoestético [Nome cliente]\" anexado ao card (usa o mais recente, se houver mais do que um) — a função extrai a imagem do moodboard internamente, nunca passes uma imagem já extraída. Obrigatório sempre que a fase \"conceito\" não for \"prevista\""},
                "valor_produto": {"type": "number", "description": "total do orçamento de produto (sem honorários), valor final COM IVA, tal como está escrito no documento/comentário — nunca calculado. Omite (ou não passes) se ainda não existir nenhum orçamento de produto para este projeto — nunca passes 0 como substituto disso; só podes omitir se a fase \"orcamento\" em fases_estado for \"prevista\""},
                "valor_produto_com_iva": {"type": "boolean", "description": "True só se `valor_produto` já inclui IVA — nunca True por suposição; nunca calcules o IVA tu mesma, passa False se só tiveres o valor sem IVA"},
                "ambientes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nome": {"type": "string"},
                            "nota": {"type": "string"},
                            "imagem": {"type": "string", "description": "normalmente NÃO precisas de preencher isto — a função associa automaticamente a imagem certa do PDF do conceito a cada ambiente pelo nome. Só usa este campo numa exceção (ex: uma imagem à parte, sem ser desse PDF) — nunca inventada"}
                        },
                        "required": ["nome", "nota"]
                    }
                },
                "documento_apresentacao_download_url": {"type": "string", "description": "o campo \"download_url\" de listar_pdfs_anexados_por_data para o PDF de apresentação do projeto, se houver anexado no card — opcional. Tem sempre a palavra \"Projeto\" no nome do ficheiro (ex: \"IG Apresentação PROJETO [Nome cliente]\") — nunca confundir com o PDF do conceito, que também costuma começar por \"Apresentação\" mas nunca tem \"Projeto\" no nome. O PDF é descarregado e embutido no portal aqui dentro; nunca passes um url que a cliente não conseguiria abrir sozinha"},
                "documento_orcamento_download_url": {"type": "string", "description": "o campo \"download_url\" de listar_pdfs_anexados_por_data para o PDF do orçamento detalhado, se houver anexado no card — opcional. O PDF é descarregado e embutido no portal aqui dentro; nunca passes um url que a cliente não conseguiria abrir sozinha"},
                "fases_estado": {
                    "type": "object",
                    "description": "estado de cada uma das 4 fases fixas — chaves obrigatórias: honorarios, conceito, projeto, orcamento",
                    "properties": {
                        "honorarios": {"type": "object", "properties": {"estado": {"type": "string", "enum": ["validada", "aguarda", "prevista"]}, "data": {"type": "string", "description": "por extenso em português, ex: \"18 de junho de 2026\" — nunca em formato ISO (\"2026-06-18\")"}}, "required": ["estado"]},
                        "conceito": {"type": "object", "properties": {"estado": {"type": "string", "enum": ["validada", "aguarda", "prevista"]}, "data": {"type": "string", "description": "por extenso em português, ex: \"18 de junho de 2026\" — nunca em formato ISO (\"2026-06-18\")"}}, "required": ["estado"]},
                        "projeto": {"type": "object", "properties": {"estado": {"type": "string", "enum": ["validada", "aguarda", "prevista"]}, "data": {"type": "string", "description": "por extenso em português, ex: \"18 de junho de 2026\" — nunca em formato ISO (\"2026-06-18\")"}}, "required": ["estado"]},
                        "orcamento": {"type": "object", "properties": {"estado": {"type": "string", "enum": ["validada", "aguarda", "prevista"]}, "data": {"type": "string", "description": "por extenso em português, ex: \"18 de junho de 2026\" — nunca em formato ISO (\"2026-06-18\")"}}, "required": ["estado"]}
                    },
                    "required": ["honorarios", "conceito", "projeto", "orcamento"]
                }
            },
            "required": ["card_id", "cliente", "validade", "honorarios_total", "honorarios_total_com_iva",
                        "honorarios_linhas", "ambientes", "fases_estado"]
        }
    }
]

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Projeto — Interior Guider</title>
<meta property="og:title" content="Projeto — Interior Guider">
<meta property="og:description" content="Acompanhamento do projeto de interiores.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --paper:#fdfaee; --ink:#1C1A17; --stone:#8E877C; --line:#E5E0D7;
    --clay:#B96D4E; --err:#B94E4E;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{background:var(--paper);color:var(--ink);font-family:'Jost',system-ui,sans-serif;
       font-size:16px;line-height:1.75;font-weight:300;-webkit-font-smoothing:antialiased}
  .wrap{max-width:720px;margin:0 auto;padding:0 28px}
  a{color:inherit}

  header{padding:44px 0 0}
  header img{width:100%;height:auto;display:block}
  .ref{font-size:15px;color:var(--stone)}

  .nome{padding:60px 0 40px}
  .nome h1{font-weight:400;font-size:20px}
  .nome .ref{display:block;margin-top:10px}
  .nome p{margin-top:14px;color:var(--stone);font-size:15px}

  .tiles{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--line)}
  .tile{padding:18px 16px 16px;border-left:1px solid var(--line);text-decoration:none;color:inherit;display:block;transition:background-color .15s}
  .tile:first-child{border-left:none}
  .tile:hover{background:#F5F2EC}
  .tile .n{font-size:11px;color:var(--stone)}
  .tile .t{font-size:14.5px;font-weight:400;margin-top:6px}
  .tile .e{font-size:11px;margin-top:10px;color:var(--stone);line-height:1.4}
  .tile.validada .e{color:var(--clay)}
  .tile.aguarda{background:#F5F2EC}
  .tile.aguarda .t{font-weight:500}
  .tile.prevista{opacity:.34;pointer-events:none}

  .fase{border-top:1px solid var(--line);padding:44px 0;scroll-margin-top:16px}
  .fase:first-of-type{border-top:none}
  .fase.prevista{opacity:.32}
  .demo{pointer-events:none}
  .fase-topo{display:flex;justify-content:space-between;align-items:center;gap:16px}
  .fase-topo h2{font-weight:400;font-size:20px}
  .fase-topo h2 .n{display:block;font-size:12px;color:var(--stone);font-weight:300;margin-bottom:4px}
  .estado{font-size:12px;color:var(--stone);white-space:nowrap}
  .estado.ok{color:var(--clay)}
  .corpo{margin-top:26px}

  .imagem{aspect-ratio:16/10;background:linear-gradient(135deg,#EDE8DF,#C9BEAC);margin-bottom:22px}
  .imagem img{width:100%;height:100%;object-fit:cover;display:block}
  .leitura{font-size:17px;line-height:1.7;text-align:justify}
  .leitura+.leitura{margin-top:14px}
  .materiais{margin-top:16px;font-size:13px;color:var(--stone)}

  .amb{margin-top:34px}
  .amb:first-of-type{margin-top:0}
  .amb .img{aspect-ratio:16/9;background:linear-gradient(135deg,#E9E2D5,#CBBBA3)}
  .amb .img img{width:100%;height:100%;object-fit:cover;display:block}
  .amb h3{font-weight:400;font-size:17px;margin-top:14px}
  .amb p{font-size:14px;color:var(--stone);margin-top:3px;max-width:50ch}

  .linhas{width:100%}
  .l{display:flex;justify-content:space-between;gap:20px;padding:12px 0;border-bottom:1px solid var(--line);font-size:15px}
  .l:last-child{border-bottom:none}
  .l:has(+ .l.destaque){border-bottom:none}
  .l .d{display:block;font-size:12.5px;color:var(--stone);margin-top:1px}
  .l .v{white-space:nowrap}
  .l.credito{color:var(--clay)}
  .l.destaque{border-bottom:none;padding-top:16px;border-top:1px solid var(--ink);margin-top:4px}

  .docs{margin-top:28px}
  .doc{padding:14px 0;border-bottom:1px solid var(--line);font-size:14.5px;text-decoration:none;
       display:flex;justify-content:space-between;gap:14px;transition:color .15s}
  .doc:hover{color:var(--clay)}
  .doc span:last-child{color:var(--stone);font-size:12.5px;white-space:nowrap}
  .doc.off{opacity:.4;pointer-events:none}

  .credito-bloco{margin-top:30px;padding-left:18px;border-left:2px solid var(--clay)}
  .credito-bloco h3{font-weight:400;font-size:19px;max-width:34ch;line-height:1.35}
  .credito-bloco h3 em{font-style:normal;color:var(--clay)}
  .credito-bloco p{margin-top:10px;font-size:14px;color:var(--stone);max-width:52ch}

  .nota{margin-top:22px;font-size:13px;color:var(--stone);max-width:56ch}
  .nota b{font-weight:500;color:var(--ink)}

  .exemplo{margin-top:26px;padding:18px 20px;border:1px dashed var(--line);border-radius:8px;background:rgba(0,0,0,.015)}
  .exemplo-selo{display:inline-block;font-size:12px;color:var(--clay);font-weight:500;letter-spacing:.01em}

  .pag-tit{margin-top:36px;font-size:13px;font-weight:500;color:var(--stone);display:flex;align-items:center;gap:14px}
  .pag-tit::after{content:"";flex:1;height:1px;background:var(--line)}
  .pag{margin-top:16px;display:grid;grid-template-columns:repeat(3,1fr);gap:0;border:1px solid var(--line)}
  .pf{padding:20px 18px;border-left:1px solid var(--line)}
  .pf:first-child{border-left:none}
  .pf-topo{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
  .pf-topo .pct{font-size:22px;font-weight:300;color:var(--clay)}
  .pf-topo .val{font-size:14.5px;font-weight:400;white-space:nowrap}
  .pf-q{margin-top:10px;font-size:12.5px;font-weight:500}
  .pf p{margin-top:5px;font-size:12.5px;color:var(--stone);line-height:1.55}

  .validar{margin-top:34px;padding-top:26px;border-top:1px solid var(--line)}
  .validar .conv{font-size:13px;color:var(--stone);max-width:48ch}
  .btn{display:inline-block;margin-top:14px;background:var(--ink);border:1px solid var(--ink);color:var(--paper);
       text-decoration:none;font-size:15px;font-weight:400;padding:15px 32px;transition:.15s;
       font-family:inherit;cursor:pointer}
  .btn:hover{background:transparent;color:var(--ink)}
  .btn:focus-visible{outline:2px solid var(--clay);outline-offset:3px}
  .btn:disabled{opacity:.5;cursor:default}
  .btn:disabled:hover{background:var(--ink);color:var(--paper)}
  .validar-msg{margin-top:10px;font-size:12.5px;color:var(--err)}
  .espera{margin-top:24px;font-size:13.5px;color:var(--stone)}

  footer{border-top:1px solid var(--line);margin-top:20px;padding:26px 0 60px;
         font-size:12px;color:var(--stone);display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
  footer a{text-decoration:none;border-bottom:1px solid var(--line)}

  @media(max-width:560px){
    .tiles{grid-template-columns:repeat(2,1fr)}
    .tile:nth-child(3){border-left:none}
    .tile:nth-child(3),.tile:nth-child(4){border-top:1px solid var(--line)}
    .fase-topo{flex-direction:column;gap:4px}
    .pag{grid-template-columns:1fr}
    .pf{border-left:none;border-top:1px solid var(--line)}
    .pf:first-child{border-top:none}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAERcAAANrCAYAAADCWhiMAAAACXBIWXMAAC4jAAAuIwF4pT92AAAgAElEQVR4nOzcMXLbWBaG0dcqrYEr6BABiw6mqqMprcIhAjJw5h1ML0GZAilg0MGswuXUgVQMuApuoiewYas16jZFAnzAj3MSKxF0DT5dRe8rBQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAqOyXP3779ffaQwAAAAAAAAAAAAAAAAAAAAAA/bsupfyn9hAAAAAAAAAAAAAAAAAAAAAAQP+uag8AAAAAAAAAAAAAAAAAAAAAAAxDXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQSlwEAAAAAAAAAAAAAAAAAAAAAEKJiwAAAAAAAAAAAAAAAAAAAABAKHERAAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAocRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAECo69oDAAAAAAAAAAAAAAAAAADAEJp2M+jzD7vHctg9DfozgOEMuSPsB2BMxEUAAAAAAAAAAAAAAAAAYMKadlOadl17jKPstw9lv72vPQYzMvTvxn5bxANgwobcEfYDMCZXtQcAAAAAAAAAAAAAAAAAAAAAAIZxXXsAAAAAAAAAAAAAAAAAAAAAuLT99mGwZx92j4M9G+CtxEUAAAAAAAAAAAAAAAAAAACYnf32vvYIABdxVXsAAAAAAAAAAAAAAAAAAAAAAGAY4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEuq49AAAAAAAAAAAAAAAAAABwusPusey3tac4zmH3WHsEAACYHXERAAAAAAAAAAAAAAAAAJiww+6pHHZPtccAAABG6qr2AAAAAAAAAAAAAAAAAAAAAADAMMRFAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAAhBIXAQAAAAAAAAAAAAAAAAAAAIBQ4iIAAAAAAAAAAAAAAAAAAAAAEEpcBAAAAAAAAAAAAAAAAAAAAABCiYsAAAAAAAAAAAAAAAAAAAAAQChxEQAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAAKHERQAAAAAAAAAAAAAAAAAAAAAglLgIAAAAAAAAAAAAAAAAAAAAAIQSFwEAAAAAAAAAAAAAAAAAAACAUOIiAAAAAAAAAAAAAAAAAAAAABBKXAQAAAAAAAAAAAAAAAAAAAAAQomLAAAAAAAAAAAAAAAAAAAAAEAocREAAAAAAAAAAAAAAAAAAAAACCUuAgAAAAAAAAAAAAAAAAAAAAChxEUAAAAAAAAAAAAAAAAAAAAAIJS4CAAAAAAAAAAAAAAAAAAAAACEEhcBAAAAAAAAAAAAAAAAAAAAgFDiIgAAAAAAAAAAAAAAAAAAAAAQ6rr2AAAAAAAAAAAAAAAAAAAAfLVYrspi+e7Z16s3P+OweyqH3dO3rx+/fw01PD/TpZTStOuzn/n8jJdSyn57f/YzgeHZB1DPL3/89uuftYcAAAAAAAAAAAAAAAAAAJirpt2cHBJ5i/324du/87l4/f7zl0Gfv98+zOp9/kwXDrjEeX5NFxkQ1Zm2lwGKzvMQxX///a9LjsQJnn+OfURE3up5dMSenjY7oR/iIgAAAAAAAAAAAAAAAABAlL+7hHqOvmMFi+Xqe1Skhu7S9ZgvXPfxOQ59of355fXTvn/6EYxLxXFOMafYSNNuen/mJfZDN/dbz1BfIYEh3ltnDufuJftgPKa6E06NVImL/Jy4CAAAAAAAAAAAAAAAAAAQpWk3vUcl9tuHXi7VDjHbufbbh1Feth7ju+pbX+fq0sYcEPgnU33fx3j/+Uvvzxzisv6p4YCX+pptiPfWST5vz015H4zxb19fprITSunnDImL/Nx17QEAAAAAAAAAAAAAAAAAANKNOZTxda51Oeyeyn57H3vRmvN0UYixnuNjNO26NO3Xs96ddy5jqgEKXpeyD7q/ffbB5dkJlycuAgAAAAAAAAAAAAAAAAAwkMVy9f0C7dgtlqtyc3tXDrun8unjh9rjlMPusey35z1j6Ivv3aX007//scdphjGlM3ys7kJ7067LfvsgKjCQhAAFf2UfcA47oS5xEQAAAAAAAAAAAAAAAACAAdzc3k3yAvZiuSrvP3+pfsn63HBHKZeJi6ReRE+MCLymadeiAj2by9mZk7l8pvbBMOZyfsZOXAQAAAAAAAAAAAAAAAAAoEeL5arc3N7VHuNsTbsui+Wq7Lf3Z0c+mI65XgIXFTjfXM9Osrl+pvZBf6YaWkskLgIAAAAAAAAAAAAAAAAAE7ZYrspi+W7Qn3HYPYpLHKlpN6Vp17XH6E0XSvn08YMzMAMugf+ICjjzb5O2++ZurlGRl+yD09kJ4yMuAgAAAAAAAAAAAAAAAAATtli+G/zy5n5bXKo9QnKY4eb2rhx2T+XTxw+1R2EAXUSGH7ozv9/e23//QIQijyjE/7MPjmcnjNdV7QEAAAAAAAAAAAAAAAAAAKYuOSzSEaDIdHN753P9G92Zb9pN7VFGqWk3s9h9c/HjvAuLvMY++Dk7Ydyuaw8AAAAAAAAAAAAAAAAAADBVi+WqNO1mNhdpu8vVnz5+qD0KZxKLOV7TrstiuXLuv5nb3puDpt2IihzJPnidqMj4XdUeAAAAAAAAAAAAAAAAAABgiro4w9wu04pSTF/TbnyGb7RYrsr7z19m9/v+0lz3XrKb2zthkTeyD37wLqZDXAQAAAAAAAAAAAAAAAAA4I3mHtiY+/9/yoQEzvP1/W1qj1GF3/ssQjHnm/M++B8796/bxtUuenjF8C2EKdOk+jDYm6ALASm+CCxyDWkCTEEW6nQR5wLYsSCLAZxCxbkCF0J8uhQiZgOD3bv0+CK+U8hjy39kUxJnrTUzzwMYlpF41svhcNHN+oVgTxgacREAAAAAAAAAAAAAAAAAgAdymNah4iESEjiNolxN7tlfbraTe81jJixyOlPcD0Lwb4AhEhcBAAAAAAAAAAAAAAAAAHgAh2k/crh4GGbzRfjj73+EBE5oSs++CMW4TOnZjWVq91RsaJjERQAAAAAAAAAAAAAAAAAAjuSQ/Zdm80UoynXqMbjH1A69xzSFe2vPG5cpPLOpTOXe2hOGS1wEAAAAAAAAAAAAAAAAAOAIRbl2oPYeRblybzI0lcPuKY35HosIjMuYn9VcjP0e//H3P/aEARMXAQAAAAAAAAAAAAAAAAD4jtl8EYpylXqMrI35QPUQjf2Qe07Geq9FBMZjrM9ojtxrciUuAgAAAAAAAAAAAAAAAADwHQ7ZH8eB6nwU5Tr1CJMiKECuPJvxuefkSFwEAAAAAAAAAAAAAAAAAICTmM0XQiwZWG623ocEBAXIjWcyHfee3DxPPQAAAAAAAAAAAAAAAAAAAOOx3GzD1flZ6jEmK+ewSFsfQlsf3v988+HnYxXl+s7Pq5POdipdYOehrw36cPczQ3z2A3IiLgIAAAAAAAAAAAAAAAAAkJGm2n/4+b4Aw+2B5Rd3fs4rJlGU69BUu9RjTE6Oz0JT7R8VEvn6tXZf/FyU6+xe93KzDdeXF4ICJJVzaOiuY77zOne/+z7+Oe/XaD8gF+IiAAAAAAAAAAAAAAAAAAAJtfUhtPXhQTGO7u/clVNkoShX4iKRzeaLsNxsU48RQggfnucYh+nvPmdFuQ5Fuep9zWMU5TpcX16kHoOJ6r4PctN9dz02OPS1775OUa7f/57HHnDXcrMNV+dnqcdg4sRFAAAAAAAAAAAAAAAAAAASaKr9SQMcuUUWinItMBJRd7A+pZhRka9pql1oql2YzRfJ4wpd7EVg5Gmaap96hMG5ff7zCWw8JqD1GN31uz1gNn+R1X2wH5yGPeHxxEUAAAAAAAAAAAAAAAAAACKJFV/oIgvLzTZZYKEoV+IikaQOaaSOinyurQ/h+vIieWTkNnCwyOa+5K6LBtg3niaH0FAIt+9nW98kef7vBk1yiG2FYD94jO59TPUcjY24CAAAAAAAAAAAAAAAAABABE21j35oPnVgoSjXQgE9u31/0x2cv768yPbQdxcZSRkXWG624er8LMnaQ5AyQDFGqUNDIaT5rvuWLraVQ2TEfvB9ucWqxuRZ6gEAAAAAAAAAAAAAAAAAAMasCxykOmzdrZ/ioG7qg9xTUJTrJOu29SFcnZ8N4gB4U+2SRlBSvUc5a6p9uDo/ExE4odShodTfdd/TVLv3z9w+6RzLzTbp+rnq9oScg1VDJy4CAAAAAAAAAAAAAAAAANCTlGGPz6ULjAgr9GU2X4TZfBF93evLi3B9eRF93adIHdlJ8T7l6G5UhNNKudd2e0IO33Xfkzo2lGrfzpU9IR5xEQAAAAAAAAAAAAAAAACAHuQYYEhxoNoh6v4sN9voaw4lIHCfVJ/LqUd2BAT6lSpY0daHcHV+Nrg9oYsNNdU+yfpT3w9C+Pjs2BPiERcBAAAAAAAAAAAAAAAAADixnAMMsWcTF+lH7MPp3WH8XJ/rh+heS0yp4g+pfYw4CAj0KUWsoqn22QW0Hqqpdklew1T3g06O8bUpEBcBAAAAAAAAAAAAAAAAADihIQQYYs+Y4uD72BXlKtpaYwqLdFIERqb2ORjjc5OjFKGKMQVjUj2ny8026no5aOtDuDo/syckIi4CAAAAAAAAAAAAAAAAAHAiQzpIHzOsEPvg+9jFjlSMJSLwudiBkRQRiFSuLy+ix1umKvZ+MKTvuWOlCoxMKTjUVHt7QmLiIgAAAAAAAAAAAAAAAAAAJ9BU+8EduI510HcqQYVYinIVba0xhgTuih0YmUJMYOzPTE5iB2vG/t7Gfn0x9/KUri8vRhupGhJxEQAAAAAAAAAAAAAAAACAJ2rrwyAPzrb1IdpB6ilEFWKIeR/HHhLoxPwcxI5BxDaVZyYX9oPTixkbCmH88a2pPDdDIC4CAAAAAAAAAAAAAAAAAPBEsQ8jn9KQZ5+iWAfRm2o/qQPhMQ/Az+YvoqwTm4hAXDFDNTEDPDmI+b045vCWPSEv4iIAAAAAAAAAAAAAAAAAAE8whjhHU+17X6MoV72vMXaxYgJtfQhNtet9ndzEes1j/CyICMQXK1LT1odRfM89RMzXHDMSE5M9IT/iIgAAAAAAAAAAAAAAAAAAj9TWh1Ecnp1iSGKIYsUEpvo83EZV+g/thBBCUa6jrBODiEAasSI1UwuLdGJ+v8fa22Npqr09IUPiIgAAAAAAAAAAAAAAAAAAjzSmCEOMg8BjCiqkECMmMPVD4U21i/L6Z/NF72vEMJbA0tDEen6mGhbpxHr9sUIxMdxGmsbzb6MxERcBAAAAAAAAAAAAAAAAAHiEsR2qdxg4b7FiAp6DOPdgDHGRtj5MPj6Rymz+ovc1xvYd91ixnvEx7AkhCNLkTFwEAAAAAAAAAAAAAAAAAOARxhZhiHGIfCyHp1OIERNoqn3vawxBrKhCUa57X6NPY9sDh6QoV72v4f29FWs/iLHH901YJG/iIgAAAAAAAAAAAAAAAAAADxTrsHFsfcclxEUeT0wgLvfi28a6Bw5BjH3U+/upGPtBjD2+T56Z/ImLAAAAAAAAAAAAAAAAAAA8kAO0xBQjJtB3WGZoYhyUH3JM4PryIvUIkzWbv+h9DXGdTwlnfJ9nJn/iIgAAAAAAAAAAAAAAAAAADzTWQ7RjfV1DFyMm0NY3va8xND4PXydEk1bfURohja+LsR8U5br3NfrQVHvPzACIiwAAAAAAAAAAAAAAAAAAPICD9U8z1MPTKc3mi97XcDD8SzHuyRA/D6Ir4+b9/TrRlft5ZoZBXAQAAAAAAAAAAAAAAAAAgA/EU/LTd1zEe34/9+ZT7kdaMWI0Ahr36zuiUZSrXq/fB8/LcIiLAAAAAAAAAAAAAAAAAAA8QN+HiyG2tr5JPUK2+r43Q4sJ2P/GTTzm24Q0vmRPGA5xEQAAAAAAAAAAAAAAAACAIzlYTGxFue59Dc/1/dybj9yL9GbzRa/XFxr6vr4DLDH2/FOyLwyHuAgAAAAAAAAAAAAAAAAAwJEcomVsPNPf5x7dch/S6z8u4j3meH2HVjgtcREAAAAAAAAAAAAAAAAAAD5o65vUIxCRmMD39X2PinLd6/VPpal2qUegR0IRx/E5+Mi/F4ZFXAQAAAAAAAAAAAAAAAAA4EhTOEgrNpGXolylHgGAO/r8nhzSnu/fC8MiLgIAAAAAAAAAAAAAAAAAcCQHaRmbKQRznqqpdqlHSK6p9qlHmLyiXPd6fc/58fxbwD0YInERAAAAAAAAAAAAAAAAAACYKAfEAXgo3x3DIy4CAAAAAAAAAAAAAAAAAAbF3tQAACAASURBVADAvdr6JvUIkA2fB4ZIXAQAAAAAAAAAAAAAAAAAAOAb2vrQ27WLctXbtU+lz9dPek21Tz3CoPg8hNBUu9Qj8EDiIgAAAAAAAAAAAAAAAAAAR3CYmNiKcp16BN7z+QdisffTB3ERAAAAAAAAAAAAAAAAAIAjiAsAADBE4iIAAAAAAAAAAAAAAAAAADBBgjkwHEW5Sj0CMGDiIgAAAAAAAAAAAAAAAAAAMEHiIhzDcwJf8rlgaJ6nHgAAAAAAAAAAAAAAAAAAAIhvNl+EolynHmMQZvNF6hGSEVGAL7X1YdL7AsMjLgIAAAAAAAAAAAAAAAAAABM0my8cjgeACXiWegAAAAAAAAAAAAAAAAAAAAAAoB/iIgAAAAAAAAAAAAAAAAAAAABHms0XqUeAB3meegAAAAAAAAAAAAAAAAAAACCNq/Oz1CMADI64CEPzLPUAAAAAAAAAAAAAAAAAAAAAwP2aap96BGDAxEUAAAAAAAAAAAAAAAAAACBDbX2TegQAIrP30wdxEQAAAAAAAAAAAAAAAAAAyFBbH1KPAEzAbL5IPQJ32Pvpg7gIAAAAAAAAAAAAAAAAAAAATJS4yMMU5Tr1CPBg4iIAAAAAAAAAAAAAAAAAAAAAMFLiIgAAAAAAAAAAAAAAAAAAMFFFuU49AnCEtr7p9fqz+aLX64+Je8UQiYsAAAAAAAAAAAAAAAAAAECm2vqQegQgA33vBbP5i16vPyZ9xkWaat/btZk2cREAAAAAAAAAAAAAAAAAAMiUuAgQQ5/BjDFxnxgqcREAAAAAAAAAAAAAAAAAAJioolylHgE4Up+xIdGM48zmL1KPAI8iLgIAAAAAAAAAAAAAAAAAAJlq65vUIwCZ6DMuEkIIRbnu9fpj0HeQqal2vV6f6RIXAQAAAAAAAAAAAAAAAACATPUdEwghhNl80fsaQP7sBd/m/jBk4iIAAAAAAAAAAAAAAAAAADBhs/mL1CMAR2iqXa/Xn80XAhrfUJTrXq8fIybFdImLAAAAAAAAAAAAAAAAAABAxppq3+v1i3LV6/WB4RAbul/f4RVxEfokLgIAAAAAAAAAAAAAAAAAABPX96F54DTEhtIoynXva7T1Te9rMF3iIgAAAAAAAAAAAAAAAAAAkLGm2vW+RoyD88Aw2A++FCO60taH3tdgusRFAAAAAAAAAAAAAAAAAAAgc30fOp/NF71eHziNOLGh/kMaQ7LcbHtfQ1iEvomLAAAAAAAAAAAAAAAAAABA5mIcPC/Kde9rAE8XYz+IEdQYgtl8ESW+JC5C38RFAAAAAAAAAAAAAAAAAAAgc021632NolxFOUQPPE2MEEWsqEbuYkWXYuzxTJu4CAAAAAAAAAAAAAAAAAAADECMoECsg/TA48UKUSw32yjr5Koo11ECKzH2dhAXAQAAAAAAAAAAAAAAAACAAYhxAH02X0Q5TA88TawgxVQDI7P5IhTlKspasWIxTJu4CAAAAAAAAAAAAAAAAAAADECsA+hTjQnAkMTaD6YaHIq5D8YKxTBt4iIAAAAAAAAAAAAAAAAAADAQTbWPso7ACOQtZpBiudlOKjASc/+LtaeDuAgAAAAAAAAAAAAAAAAAAAxEW99EWWc2X4SiXEdZC3icmGGKqQRGYr/OptpFW4tpExcBAAAAAAAAAAAAAAAAAICBaOtDaOtDlLWKcjWJmAAMVewwxdgDI/HDIvHiMCAuAgAAAAAAAAAAAAAAAAAAAxIzKDD2mAAMXexAxVj3hBSvK3Ychml7nnoAAAAAAAAAAAAAAAAAAADgeG19CG19iHYQfrnZhuvLi9DWhyjrxTabL8Js/iLKWm19M9r7SBpNtQtFuYq65tj2hDRhkbhRGBAXAQAAAAAAAAAAAAAAAACAgWmqXVhuttHWG1tMoDObL6Lex+vLm2hrMR1NtU8SGGmqfWiqXdR1Tyn25/+uId83hulZ6gEAAAAAAAAAAAAAAAAAAICHaetD9NDHcrNNdhC/D0W5jvp6UrxnTEOqUEVRrsJysw2z+SLJ+k8R+/N/V1Ptk6zLtImLAAAAAAAAAAAAAAAAAADAAF1fXkRfczZfjCIwstxsQ1Guoq6ZKgDBNKTYD0L4uCcU5TrJ+g/1cd64n/9OWx/sBSQhLgIAAAAAAAAAAAAAAAAAAAPVVPvoa87mi/DH3/+E2XwRfe2n6sICsWdvqn1o60PUNZmWtj4kfcaKcpXks3Ws7rOfekZhEVJ5nnoAAAAAAAAAAAAAAAAAAADgcZpqF2bzRZLD8svNNrT1IVxfXkRf+zFSRQXa+iAoQBTXlxfhj7//SbZ+F/Donvkcgjqz+SIU5TqL6InIECk9Sz0AAAAAAAAAAAAAAAAAAADweCnDFbP5Ivzx9z+hKNfJZvieolyHP/7+J1lcQFiEmHKI/XSRkeVmm2xvuDtDDmERkSFSe556AAAAAAAAAAAAAAAAAAAA4PHa+hCuLy/CcrNNNkNRrkJRrkJT7bM5QF+U61CUq6QzNNU+tPUh6QxMS1sfQlsfsghqzOaLMJsvQlGuPszV5/5wu96L5J/7r8llX2S6xEUAAAAAAAAAAAAAAAAAAGDgcgkKpI6M5BQX6DukAPfpYkOp94O77oZGQrgN74QQQlvfPDrAU5TrT66dK5EhciAuAgAAAAAAAAAAAAAAAAAAI5BTUKCLjHTRkz4jGzkFRe66vrxIPQIT1lS7sNxsU49xr4+f108/t92e8bncAyL3ERkiF+IiAAAAAAAAAAAAAAAAAAAwErkFBbogQBcSaKp9CCGEtr75akDgGEW5/uTaORIWIbW2PnwIDg1Jzp/rh+reA8iBuAgAAAAAAAAAAAAAAAAAAIxE7kGBLjISwuqL/9bWh68GRz7+nWG4vrx4dDgFTqmtD6Gp9oP7DI2FsAg5ERcBAAAAAAAAAAAAAAAAAIARyT0wcp/ZfBFm80XqMZ5EWITcNNVuFJ+toREWITfPUg8AAAAAAAAAAAAAAAAAAACcVhcYIR5hEXLl2YzL/SZH4iIAAAAAAAAAAAAAAAAAADBCAiPxiAmQO89oHO4zuRIXAQAAAAAAAAAAAAAAAACAkRIY6Z+YAEPhWe2X+0vOxEUAAAAAAAAAAAAAAAAAAGDEBEb6IybA0HhmT6/bY91XciYuAgAAAAAAAAAAAAAAAAAAIycwcnpiAgzV9eWF/eBEhEUYCnERAAAAAAAAAAAAAAAAAACYAIfgT6OtD+Hq/Mx9ZNAEh57OPWRIxEUAAAAAAAAAAAAAAAAAAGAiBEaeRkyAMRHKebzrywt7AYMiLgIAAAAAAAAAAAAAAAAAABPjYPzDuWeM1fXlRWiqfeoxBkGgiaESFwEAAAAAAAAAAAAAAAAAgAlq60O4Oj9zSP473CemoKl2ohnf0VR794jBep56AAAAAAAAAAAAAAAAAAAAIJ3ry4swmy/CcrNNPUpW2voQmmonJMBktPUhXF9ehKJch6JcpR4nG/YCxuBZ6gEAAAAAAAAAAAAAAAAAAIC02voQrs7PQlPtU4+Shabah+vLCzEBJqmpduHq/MzzH27jS/YCxkBcBAAAAAAAAAAAAAAAAAAACCF8jApMNTLSVPv3r3+XehRIbsphjW4vmOJrZ5yepx4AAAAAAAAAAAAAAAAAAADIS1PtQlvfhNn8RSjKVepxetdUe0ER+Iq2PoTry4swmy9CUa7DbL5IPVKv7AWMlbgIAAAAAAAAAAAAAAAAAADwhbY+hLY+hKbahaJcjzIyIiQAx7kbGRlbdKjb59r6kHoU6I24CAAAAAAAAAAAAAAAAAAA8E1NtQtNtQuz+SIU5TrM5ovUIz1aU+1DW98ICcAjfB4duo2NDHM/sBcwJeIiAAAAAAAAAAAAAAAAAADAUdr6EK4vL0IIYVBhAREBOL2m2n34eSj7gb2Aqfrhr19/+U/qIQAAAAAAAAAAAAAAAAAAgOG6jQq8CEW5Sj1KCOE2gnL7S0QAYivK9fvf0+8H9gK4JS4CAAAAAAAAAAAAAAAAAACcVBcbCaH/wEAXDwghhKba9boW8HBdbOT25/72A3sB3E9cBAAAAAAAAAAAAAAAAAAAiOZuaOAx2vrmQ0AAGK67EaLHsBfA8cRFAAAAAAAAAAAAAAAAAAAAAGCknqUeAAAAAAAAAAAAAAAAAAAAAADoh7gIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAAACMlLgIAAAAAAAAAAAAAAAAAAAAAIyUuAgAAAAAAAAAAAAAAAAAAAAAjJS4CAAAAAAAAAAAAAAAAAAAkLXf/vw9/PjzT6nHOMqQZgVgGsRFAAAAAAAAAAAAAAAAAACAbHWxjiFEO4Y0KwDTIS4CAAAAAAAAAAAAAAAAAABk6fNIR87RjiHNCsC0iIsAAAAAAAAAAAAAAAAAAADZuS/OkWO0Y0izAjA94iIAAAAAAAAAAAAAAAAAAEBWvhflyCnaMaRZAZgmcREAAAAAAAAAAAAAAAAAACAbx8Y4coh2DGlWAKZLXAQAAAAAAAAAAAAAAAAAAMjCQyMcv/35e4/TfH/th84qMAJACuIiAAAAAAAAAAAAAAAAAABAco+Nb6QIjDxlVoERAGITFwEAAAAAAAAAAAAAAAAAAJJ6SnTjx59/ihoYeWogRGAEgNjERQAAAAAAAAAAAAAAAAAAgGROEduIFRj517//+yRhkJgxFAB4nnoAAAAAAAAAAAAAAAAAAIBjzeaLMJu/+OzPi5Ndv60Poa0Pd/5888mfgdP73//3PyeJbXSBkdcvX51gqq9f/1///q+TXKuvGQHga37469df/pN6CAAAAAAAAAAAAAAAAACAu+5GRIpylXiaW3fDI021SzwNjEsXBjmFd2/enjzeccr5Xr98Fd69eXuSawHAMcRFAAAAAAAAAAAAAAAAAIDkupjI7e+L1OMcran2738XG4GnyjUwIizCU9yNZfXB9w9wDHERAAAAAAAAAAAAAAAAACCJolwPLibyPU21D219E9r6kHoUGKTcAiPCIjxVUa5DUa56u/7V+Vlv1wbG43nqAQAAAAAAAAAAAAAAAACA6RhjUOSu2wPkt4fIhUbg4d69eRvevXkbfvz5pydfqwuDPDYwIiwCwFiIiwAAAAAAAAAAAAAAAAAAvRp7UOQ+QiPwOK9fvgq//fl70sDIKcMiXTAFAFIRFwEAAAAAAAAAAAAAAAAATu42JvLifWCDLjTS1ofQ1ofQVLvUI0HWUgZGTh0WeWjYBABO7VnqAQAAAAAAAAAAAAAAAACA8ZjNF2G52YblZiss8hWz+SIU5Sr88fc/oSjXqceBrL1++Sq8e/P2JNc6NhgiLALAGImLAAAAAAAAAAAAAAAAAABPdjcqMpsvUo8zCCIj8H0xAyPCIgCMlbgIAAAAAAAAAAAAAAAAAPBooiJPJzIC3xYjMCIsAsCYiYsAAAAAAAAAAAAAAAAAAA8mKnJ6IiNwvz4DI8IiAIyduAgAAAAAAAAAAAAAAAAA8CCiIv3qIiPuL3zq1IGR7pewCABj9zz1AAAAAAAAAAAAAAAAAADAMBTlOhTlKvUYk7HcbENbH0JT7UJbH1KPA1l4/fJV+O3P38OPP//05GudKioSgrAIAHl7lnoAAAAAAAAAAAAAAAAAACBvs/kiLDdbYZEEPt77depRIBuvX74K7968TT3GB8IiAOROXAQAAAAAAAAAAAAAAAAAuFdRrsNysw2z+SL1KJNWlCvvA9yRS2BEWASAIRAXAQAAAAAAAAAAAAAAAAC+MJsvwnKzDUW5Sj0K7318T9apR4EspA6MCIsAMBTiIgAAAAAAAAAAAAAAAADAJ7qIxWy+SD0KX1GUq7DcbFOPAVlIFRgRFgFgSMRFAAAAAAAAAAAAAAAAAIAPlputcMUAzOaL8Mff/wjAQIgfGBEWAWBoxEUAAAAAAAAAAAAAAAAAgDCbL8JysxWrGBjvGdyKFRgRFgFgiMRFAAAAAAAAAAAAAAAAAGDihEWGbbnZhuVmm3oMSC5GYERYBIAhEhcBAAAAAAAAAAAAAAAAgAnrwiIMm/cRbvUZ/xAWAWCoxEUAAAAAAAAAAAAAAAAAYKKKci1IMSICI3CrjwjI65evwrs3b09+XQCIQVwEAAAAAAAAAAAAAAAAACZoudmGolylHoMTExiBEN69eSsEAgB3PE89AAAAAAAAAAAAAAAAAAAQ13KzDbP5IvUYvWrrQ2jrwzf/n7HGVbrAyPXlRepRIInf/vw9/PjzTye/5uuXr0RLABgkcREAAAAAAAAAAAAAAAAAmJCxhEWaan/n590jr/Hl3yvK9Z2fhxsfERhhqvoIi9y9tsAIAEP0w1+//vKf1EMAAAAAAAAAAAAAAAAAAP0balikrQ/vf92Etj5EX382X4TZ/MX734d1/9r6IDDCZPQZFrnr//6fl72vwXgU5brXWNXV+Vlv1wbG43nqAQAAAAAAAAAAAAAAAACA/hXlelBhjKbaJ4uJfK6Lm3S62Eifh8VPZTZfhOVmKzDC6MUKi3RrvX75KspaAHAKP/z16y//ST0EAAAAAAAAAAAAAAAAANCfLjCRu7Y+hKbaZREUOdZsvhhEuKWtDwIjjFbMsEjn3Zu3AiMcpSjXvcaors7Pers2MB7PUg8AAAAAAAAAAAAAAAAAAPRnCGGRptqHq/OzcH15MaiwSAgfox1X52ehqfapx7nXEJ4DeIwUYZEQQvjx55/Cb3/+Hn1dAHgMcREAAAAAAAAAAAAAAAAAGKmcgxJtffgQFWmqXepxTqKpdllHRmbzRSjKdeox4GRShUU6AiMADIW4CAAAAAAAAAAAAAAAAACMVK4hievLi3B9eTGaqMjnco6MFOUqzOaL1GPAk/34809JwyJ35xAYASB34iIAAAAAAAAAAAAAAAAAMELLzTa7iERT7cPV+Vlo60PqUaLINTKS47MBD5Fb0CO3eQDgc+IiAAAAAAAAAAAAAAAAADAyRbnOKh7R1of3kY1d6lGS6CIjOUVVinKdegR4lFOGPF6/fBVev3x1kmsJjACQM3ERAAAAAAAAAAAAAAAAABiR2XwRinKVeowPri8vwvXlReoxspDTvbh9TgRGGJZTh0XevXkb3r15KzACwOiJiwAAAAAAAAAAAAAAAADAiCw329QjhBBCaOtDuDo/C219SD1KVnK6L0W5CrP5IvUYcJRThju6qMjdPwuMADBm4iIAAAAAAAAAAAAAAAAAMBK5hEWuLy/C9eVF6jGydn15EZpqn3qMbJ4Z+JZTh0W+FhIRGAFgzMRFAAAAAAAAAAAAAAAAAGAEZvNFmM0XqccI15cXoa0PqccYhKbaZRFhERghZzHCIsf+94cQGAEgJ+IiAAAAAAAAAAAAAAAAADACqQMRbX0IV+dnwiIP1NaH5IGRXMI08LmYYZG7/9+7N29PsqbACAC5EBcBAAAAAAAAAAAAAAAAgIErynXS9XMIZAxZDmGW1M8QfC5FWKTz+uUrgREARkVcBAAAAAAAAAAAAAAAAAAGbDZfhKJcJVtfWOR0ri8vkgVGbp8jgRHykDIs0jl1YORf//7vk1wLAB5DXAQAAAAAAAAAAAAAAAAABixlEEJY5PRSBkZSRmqgk0NYpHPKwMi//v1f4ceffzrJtRgWeyuQA3ERAAAAAAAAAAAAAAAAABio2XwRZvNFkrWFRfqT8r6mjNVATmGRzikDI7/9+bvACABJiIsAAAAAAAAAAAAAAAAAwEAtN9sk6wqL9C/V/S3KVZJ1IcewSEdgBIChExcBAAAAAAAAAAAAAAAAgAEqynWSdYVF4kh5n1M9W0xXzmGRjsAIAEMmLgIAAAAAAAAAAAAAAAAAA1SUqyTrNtUuybpT1NaH0FT76OumeraYrtzDIp1TB0YAIBZxEQAAAAAAAAAAAAAAAAAYmKJcJ1n3+vIitPUhydpT1VS7JPc81TPGNJ0iCNJ3WKRzqsBIjFkBoPPDX7/+8p/UQwAAAAAAAAAAAAAAAAAAx/vj73+ir9nWh3B9eRF9XUKYzRdhudlGX/fq/Cz6mkzXjz//FH778/dH/d1YYZG7fvvz9/Djzz896u+eKlAyVrP5IszmLz78uShXj75WU+0//NzWN0liTX1/Z9urv+7zSNZTnqMQbv8ddPf5aardk64HsYmLAAAAAAAAAAAAAAAAAMCAFOX6yQdkH0pYJL0U73tT7R2eJqrHBEZShEU6jwmMCIt8qguJ3P6+iLZuFx2JsceJi/Qv1XMUwqfREd+Zw/Z52OhzqQJFp/I89QAAAAAAAAAAAAAAAAAA/5+9u0duWwsTNHzG5TVgBRMiQFGBqyZyaRUOEYiBMi2iF6BMARmgajpw0GtQOXUgFgNsYMLhLGI64IUt61oSJRHnD89T1eXu4OocggcAk+9t4HSxh2ZDMCybg3HYRB+annOttl/P9rdLH/491WtD0B+R6p6fQiFvCYykCotMa78lMCIsctT26yQRiD/3cPXr3ykO4V1XjpQxkb/v5biH6VyNw3YR76I53kOx7sPpd8hbwm3jEIr+TsVFAAAAAAAAAAAAAAAAAKAQKYZojwOy5Q5S1mQcNuHy9i7aetN5m+P7f8sw71uVPvx7qqa7mO06powsvCUwkjIs8ngPpwRGlh4WabrVr6hIbqZnndBI3qaQxZzvj3M57rH+0Mgc38dc16qk8zMXcREAAAAAAAAAAAAAAAAAKMT0/2U9JgPW+ZiG3mMO5zfdRZUD0eTtlMBITrGO1wIjOe01trZfFzXM/zg0Mg5b78AMTFGaHMM0p3gaGnGmXnbO3x2CIn8SFwEAAAAAAAAAAAAAAACAQsQerL2/uY66Hq/znbAULwVGcox1PBcYyXGvczvGOdbFxiAmbX8lMpJQaWGaU0xn6rDfhXHYiHfNpPQgzVzERQAAAAAAAAAAAAAAAACgAG2/jrreYb8z9Aok9bfASM6xjqeBkZz3OodaoiJPiYzEVWNU5KmmW4XL2zuRkTNbwtn5iE+pNwAAAAAAAAAAAAAAAAAAvC72sLYBaiAHU2AkhDJiHSXt9Zzafh0ub++qC4s81vZX4duPn9FjX0vR9ut/ru9y4hBTZKT2e2duTbda3Nl5j8+pNwAAAAAAAAAAAAAAAAAAvKzpVlGHTg/7XTjsd9HWA3jJ//s//zf813/879TbOFlJe/2oKY6wJG1/FZpuFcZh4115Bks8Q09N12ActuJub9B0q9D2a2GWE31KvQEAAAAAAAAAAAAAAAAA4GVNdxF1PYOtALxkiiEsNQoxff62X6feStGWfIb+pu2vwrcfP8UyTtD263B5e+davcHn1BsAAAAAAAAAAAAAAAAAAF7W9lfR1jrsd+Gw30VbD4CyTGENju/npluF+5vr1FspijP0ssvbu3DY75yrv2i6VWj7tajIO3xKvQEAAAAAAAAAAAAAAAAAIB/jsEm9BQAydXl7JwrxRNOtwrcfP8UOTuQMnca5+rcpSuOavI+4CAAAAAAAAAAAAAAAAABkrO3X0dY67HfhsN9FWw+AMhjqf53r8zJn6H3EWI5ch48TFwEAAAAAAAAAAAAAAACAjMUcwhUWAeApUYjTuU5/5wx9zHT9lsrZOQ9xEQAAAAAAAAAAAAAAAADIWMxhynHYRFsLgPwtPWrwHkIIf3KGzmOJgZamW4VvP34u6jPPSVwEAAAAAAAAAAAAAAAAADIVc5jysN9FWwuA/IlCvN/SIhDPuby9c4bOaEmBEc+f8xMXAQAAAAAAAAAAAAAAAIBMNd1FtLXERQCYGOz/uKVEIJ6z9M8/p9qvrefPPMRFAAAAAAAAAAAAAAAAACBTMQdHx2ETbS0A8mWw/3yWeh1rj1/koNZr7PkzH3ERAAAAAAAAAAAAAAAAAMhUrKHRw34XZR0A8mawn4+qNXqRoxqvdW2fJyfiIgAAAAAAAAAAAAAAAACwcOIiAAiL8FFNtxKHiKzGwAjzEBcBAAAAAAAAAAAAAAAAgAy1/TraWof9Q7S1AMiPsAgf5QylE/M3I+X6nHoDAAAAAAAAAAAAAAAAAEBah/0u9RYASEicgI/IPSxy2O9+/dYZh82b/tumW4WmuwghhND2V2ff2zlM1//+5jr1VsiYuAgAAAAAAAAAAAAAAAAALJiwCMCyXd7ehaZbpd7Gqx4HIo7/98Oz77CnsZRjICL/z1iq3OI001l56Yy89W+F8DtMMn3enGIjqw8xRAAAIABJREFUAiO8RlwEAAAAAAAAAAAAAAAAADIUa2BVXARguXKObozD9p9/N+/4b5//b3IMQ5QspzjNOGzfdV7evs7m179tv87mPpr24bcdfyMuAgAAAAAAAAAAAAAAAAAAsDBNtwqXt3ept/GHcdiGw/5h1jjC4zDEMcZwITTyTrlENWJFRf6+9nHdplv9Co2kdHl7F75//ZJ0DzWY4kaPHfYPCXZyPuIiAAAAAAAAAAAAAAAAALBgqYZxAUir7deptxBCCOGw34XDfpfkffR47SkMkToOUZLUcZqUUZGnDvtduL+5ziIycnl7F+5vrpOtX5opJJLLWZqLuAgAAAAAAAAAAAAAAAAAAMCC5BDRmKIeh/0u6T4mU1gghzhECVKGRaaQR45yiIxM93cu91aOcgrTxCIuAgAAAAAAAAAAAAAAAACZaft16i0AULGUYYgQQri/uc42fDDFIdp+Hdr+KvV2spQyTpPz2Xks9Tm6vL0L379+ib5uznILGsX2KfUGAAAAAAAAAAAAAAAAAIA0xmGbegsARJYyYHXY78L3r1+KGO4fh00xe40txRkq6ew8Ng6bcH9znWRtsbqjKfRSSphmLp9TbwAAAAAAAAAAAAAAAAAAAID5Nd0qtP1VkrVLHey/v7kOTbcKl7d3qbeShbZfh6ZbRV1zikOUatp/7GvX9lfhsH8o8r47h8N+F8Zhs9jP/9Sn1BsAAAAAAAAAAAAAAAAAAABgfm2/jr7mFFYoecC/hs9wLrHjNKWHRSapzlCKez4H47B1zz4hLgIAAAAAAAAAAAAAAAAAAFC5pluFpltFXbOmKEdNn+W9Yocq7m+uqwiLPBb7DKW471O7v7kO47BJvY3siIsAAAAAAAAAAAAAAAAAAABULnYYYopx1GbJgZG2v4q21mG/q/Y6xz5Dse/9VA77Xfj+9Uu15+ajxEUAAAAAAAAAAAAAAAAAAAAq13SraGvVGhaZLDEwEjNQUfv5CSGEcdhEW6vpVlHv/xSWcGY+6nPqDQAAAAAAAAAAAAAAAAAAacQcbAWIYe4AQqnPTWGI87u/uQ6Xt3fVRxsmbX8VZZ2lnJ/pc17e3kVZr+kuqg3iLOXMfJS4CAAAAAAAAAAAAAAAAABkJtYAL0Bt5n5+lhsXifdeWdKQ/1ICIzE/35LOT8zASNtfFfv8eomwyOnERQAAAAAAAAAAAAAAAACAxWv7deotZK/GoWRYgpjPtyUO+Y/DJkocIqVYZ2iJ5+ew34XDfhcl4NL266re5cIibyMuAgAAAAAAAAAAAAAAAAAsXttfpd5C9moaSIYliREtCCGEcdiGw34XZa2cTIGDmgMjMc7QFNlYovub6/Dtx8/Z14n1LIhBWOTtPqXeAAAAAAAAAAAAAAAAAAAAAPOIFYZYcoCo5jBG26+jrLP0UESMz19TXGTJz5v3+px6AwAAAAAAAAAAAAAAAAAAqY3DNvUWzqLtr1JvAchIrDCEQf9jHOLbj5+pt3F2MYIUtbyDP2IK1Mx9vdt+Xfz9en9zXW3MZ07iIgAAAAAAAAAAAAAAAACQmXHYRolENN3KcOY/Sh+0nYiLAI/FCkN4lxzFen/HFOcM1fEO/qhx2ITL27tZ14jxfc5pirDwdp9SbwAAAAAAAAAAAAAAAAAASKPpLlJvAYAZCUPEVdu1aPv17GuMw3b2NUoRI5xRelyktnssJnERAAAAAAAAAAAAAAAAAACAyghDpFHTNRGniS/G9YjxbJjDOGxnj6/UTFwEAAAAAAAAAAAAAAAAAACANxOG+LearsnccZGaQiznctjvBDSeUdO9lYK4CAAAAAAAAAAAAAAAAAAAQGXa/mrWvy8M8bwars3cYZEQQjjsH2Zfo0Rzx0XmfjbMoYZ7KjVxEQAAAAAAAAAAAAAAAACACsQYBAeYCEM8bxw2qbfwYU13Mfsac0c0SlXD+Tk31+TjxEUAAAAAAAAAAAAAAAAAYKFK/P9cz/NiDIIDZYgRGxKGeJnr8zLX52VzX5+SgmTjsE29hSqIiwAAAAAAAAAAAAAAAABAZg77h9RbAKBgc8eGDPu/rvR4xtwBstKvz9zmj4uUEyTzu/g8xEUAAAAAAAAAAAAAAAAAIDMGbgHImWH/143DJvUWsuYMvcz1+c3v4vMQFwEAAAAAAAAAAAAAAACABWu6VeotcCa+S2DS9lez/n3D/qdxnZ7n2rxs7utTym+Gcdim3kI1xEUAAAAAAAAAAAAAAAAAYMGa7iL1FjiTUgaFgbIZ9j9dqQGNtl+n3gIzK+U3w2H/kHoL1RAXAQAAAAAAAAAAAAAAAIAMlTqQDADwEr9xTuM6uQbnJC4CAAAAAAAAAAAAAAAAABmKNUzZ9ldR1mFebb9OvQVgIQ77h9RbKMY4bFJvIUuCEadxnTgncREAAAAAAAAAAAAAAAAAAIBKNN1q1r8veADEMA7b1FuoirgIAAAAAAAAAAAAAAAAAGTosH+Itlbbr6OtxTzmjgkA5Wi6i9RboHDeKcvge14WcREAAAAAAAAAAAAAAAAAyNBhv4u2luHS8vkOAfI0DtvUW3gz75Q8jMNm1r8vRLQs4iIAAAAAAAAAAAAAAAAAsHCGiMvm+wNiiRm+ol6H/UPqLcDiiIsAAAAAAAAAAAAAAAAAQKbGYRttLYGKcjXdReotAAshLsI5OEecQoTmvMRFAAAAAAAAAAAAAAAAAACBioK1/VXqLQAAnJUIzXl9Tr0BAAAAAAAAAAAAAAAAAODvxmETLRzR9ldhHDZR1uJ8mm6VegsA8CZtv069BVgccREAAAAAAAAAAAAAAAAAIIRwHPYVGClL012k3gIAvEmscBrw26fUGwAAAAAAAAAAAAAAAAAAnjcO22hrNd0q2lqchwFtAABeIy4CAAAAAAAAAAAAAAAAABk77B+irdV0K4GRgviuAAA4xefUGwAAAAAAAAAAAAAAAAAAnnfY76Ku1/brcH9zHXVN3qft16m3AABv9v3rl9RbgMX5lHoDAAAAAAAAAAAAAAAAAMDLYgZGmm4Vmm4VbT3ex/cEpOC5wzmIY0F84iIAAAAAAAAAAAAAAAAAkLlx2ERdz9Bv/pruIvUWgAUSF3k71wzIgbgIAAAAAAAAAAAAAAAAAGTusN9FXa/pVoahM9f2V6m3AMAJSnyfjsM29RaAMxMXAQAAAAAAAAAAAAAAAIACxB70bft11PU4ne8GeMlh/5B6CwBkRlwEAAAAAAAAAAAAAAAAAAoQe1i86Vah6VZR1+R1TbcKbX+VehtAxg773ax/X+DodN6jQC7ERQAAAAAAAAAAAAAAAACgAIf9bvaB8acMkOfHdwJQjqa7SL2FLImuQHziIgAAAAAAAAAAAAAAAABQiNhxkaZbiVlkpOlWBrL5pe2vUm+BhXL26nfYP8z6973LID5xEQAAAAAAAAAAAAAAAAAoxDhsoq/Z9leGgDMh9HIa5xXix6j4u1JDLM4P1EdcBAAAAAAAAAAAAAAAAAAKMg7b6GuKWqTX9mvRjBMt4Tot4TPyMXPHIbwXXuc+fZnrA3GJiwAAAAAAAAAAAAAAAABAQcZhE33NpluFy9u76Oty1HSr0PZXqbdBRpruIvUWWDhhiNeVfp/OHagp/fpAacRFAAAAAAAAAAAAAAAAAKAwcw/8/k3TrQyTJ9L269RbIDPuRV4zd4jKGXxd6VGo+eMizhDEJC4CAAAAAAAAAAAAAAAAAIWZe2j8OZe3d4aBI3PN36f2a1b756MMwkfPc4++zjWCuMRFAAAAAAAAAAAAAAAAAKAwh/0uHPa7JGtf3t4lWXeJmm5V7fD13Oe36S5m/fspCTpwqrnvs7a/mvXvl6yG+zRGyKyG6wSlEBcBAAAAAAAAAAAAAAAAgALd31wnW1tgZH5Nt6r6Os8fF6kzyhJC3Z+N84oRoXIe/63mMNS5uU4Qj7gIAAAAAAAAAAAAAAAAABRqHLZJ1q09fJGa6/txNQ+s1/zZOK/D/mH2Ndp+Pfsapanpmsz9O8PzDOIRFwEAAAAAAAAAAAAAAACAQo3DJtnaAhjzcF3Pp8ah9ZqiBczvsN/NvkbTrZzLR5puVdWzR6AG6iEuAgAAAAAAAAAAAAAAAAAFG4dtsrWFMM5vKUPWMcI4TXcx+xqxtf1V6i1QmBjvCOfyt9qe4TECNc4PxCEuAgAAAAAAAAAAAAAAAAAFG4dNlOHf5wiMnMd0HZtulXor1ahtYL22aAFxHPYPUdbxHjjeozU+w+MERjzfYG7iIgAAAAAAAAAAAAAAAABQuHHYJF1fYORjlhoWMbD+NrXFUogjVnyq6VaLe4Y91nSrau/RGL8x2v5q0ecHYhAXAQAAAAAAAAAAAAAAAIDCHfa7MA7bpHtoulX49uOn4eA3WnKYJUb0oJbzuNQzwnnEej8s+ZzWFDJ6KlagpuZrCDkQFwEAAAAAAAAAAAAAAACACozDJtoA8Esub++qCTrMbclhkViablX8eazhM5DWYf8Qba0lPtOW8N6LEajxrIN5iYsAAAAAAAAAAAAAAAAAQCXGYZN6CyGE46D1EgfM38I1inde234dZZ25LP2c8HGH/S5afGpp0aSlBDFiBWqWEGqBVMRFAAAAAAAAAAAAAAAAAKASh/0ujMM29TZCCMeB628/fhoSfmIavHddjmIED5puVWxgZEmRBuYVMz5V8j33FksKqcQM1Czh7EAK4iIAAAAAAAAAAAAAAAAAUJFx2EQbAD7F5e3dYoavX9P2a2GRJ+INq18Vd93bfl3cnslX7PdCiffcWywpLDKJFahZ4rWFGMRFAAAAAAAAAAAAAAAAAKAy9zfXqbfwh6ZbhW8/foa2X6feShLToHTbX6XeSnYO+4doa5V0/ppu5bxwduOwjbre8blXzn13qqXGLw77XbRIzVKvMczpc+oNAAAAAAAAAAAAAAAAAADnd39znd1gbttfhaZbhXHYRBtQTukYiFiHplul3kq2pmH1GNdoGlbPLb7zlKF65jIOm+jRmum5n/t9d6q2Xy86/DMOm2jPp1Ke2R8RM74zDptoa5EncREAAAAAAAAAAAAAAAAAqNBhv8syMDINCx/2u6ojIzkMoMeKdnxUzH3mPqwuLMLcUrwXcr/vTnV5e1fEM3VOMYNQIdRzdv4m5nmq9bcWb/Mp9QYAAAAAAAAAAAAAAAAAgHlMQ8A5mgaGL2/vQtuvU2/nLJpuFdp+Hb79+Jk8LDIO22y/+6fGYRN1vVwDHtP9AHNK9V5oulX49uNnkXGOkvc+h1TP7Jquf+zPE/s7I0/iIgAAAAAAAAAAAAAAAABQsfub66wjE8cgx9U/QY51kcPDf4ZS0kZFQjjGA0obJI59RnMLjNQ2OE/eUj4fSovotP26qP3GkCJQU0tgJEWoJufQHHGJiwAAAAAAAAAAAAAAAABA5XIPjEza/upRpCPv0MgxirIO3378zG7g+f7mOvUW3ixF7CCHYfUUg+aQOjYwnfu2Xyfbw2um53sOwagcpXrPlBaneSxVqKbE3wTM43PqDQAAAAAAAAAAAAAAAAAA87u/uU4eUjhV063+2edxqHsctv/8Gz9A8dg0CJ/zsHmpQ8RT7CD2+ZwCI+OwjXq+pjhNCfcjdbq/uQ7ffvxMuoe2vwptfxX9/nvJdF+6N183/a6IbYrTlBJOS/m8L/U3AfMQFwEAAAAAAAAAAAAAAACAhSgpMPLYFPOY/p1iI4f9w2yDxcfh8os/1s1dKYPWzxmHTZJB9RCO33HTrcI4bGa9hqIi5CRVHOKpHCIjbb8u5lmfi1RRqMnl7V047HezP7ffK/Xzfvp+YCIuAgAAAAAAAAAAAAAAAAALUmpg5LHfA+B/DoJP0ZGP/93ylB4WCSH9oHrTrX6FFs4dOZgGzEu+76hP6nvuqSkyMu1r7tCI+/Lj7m+uw7cfP5OtPz23U4ZpnkodFZnc31wnXZ/8iIsAAAAAAAAAAAAAAAAAwMLUEBj5m5LjIB8xDeLXYBw2vwIfKT2NHBz2D2+6xm2//vV3IGep4xB/MwU/pvtnCke99T7899+8CCG4L89t+k2R0uNn9jhskrwTc4rVCIvwN+IiAAAAAAAAAAAAAAAAALBAtQZGluaw31U1RDzFPHI5l78HxX/HCP4Wc8lloBzeI4c4xEt+x0D+jIK8FFZyT8ZzDHpss4i2NN3q11keh+2HgjSnyDEkdfzcdQTHOC9xEQAAAAAAAAAAAAAAAABYKIGRstUWFpmMwybr0IFoAbWZniU533d/417Mxzhssvs+jsGPY/TjcYhmHDYf+JvHmEhun3VyDL28//NRN3ERAAAAAAAAAAAAAAAAAFgwgZEy1RoWCWEajt7+MxgOxDDFF7wLeK+cf088joE8fbeMw/bZ/66k91DNvws4D3ERAAAAAAAAAAAAAAAAAFi4+5vr0HSrcHl7l3ornGAJA8TjsPljGByYX85xCMpQ4hkqKSDynCX8LuDjPqXeAAAAAAAAAAAAAAAAAACQnsHUMtzfXC/mexqHTeotZG8pZ4F47m+uw2G/S70NCubZHZffb5xKXAQAAAAAAAAAAAAAAAAACCEcB1S/f/1isDxTSxv6NzD9ssN+t6jzQDxLe9bEsKRnmWd3PK41byEuAgAAAAAAAAAAAAAAAAD84f7mOozDNvU2eGSpw/4CGn9noJy5LfWZM4clXkvPqPm5xryVuAgAAAAAAAAAAAAAAAAA8C/jsFnkQHRuDvtd+P71y6K/B+fwTwbKicW993FLvobeX/PxHuA9xEUAAAAAAAAAAAAAAAAAgL+ahlfHYZt6K4s0DlvDw/9Y8oD+YwbKic29936u3ZHrcF7eA7yXuAgAAAAAAAAAAAAAAAAA8KJx2BgOjuwYddmk3kZWln4GDZSTyv3NtbP3Rkt/Xj0lVHYeomN8hLgIAAAAAAAAAAAAAAAAAPCqKWxgOHheh/0ufP/6xVD6M5Y6sC8sQmrO4Gk8w583hcp4H9ExPkpcBAAAAAAAAAAAAAAAAAA42ThswvevX0RGZnB/c23w+gRLC4yIOpAL4YyXuVdfN10jZ+h07jvORVwEAAAAAAAAAAAAAAAAAHizcdgYED6TcdgaHH6jpZw9sQJyJIT0b67J6abnmkjZ65wrzklcBAAAAAAAAAAAAAAAAAB4l2lAeCmhh3P7PWC9Sb2VItU+dD0O26o/H2U77HeiSOH3c3zp1+E9RMqe5/5iDp9TbwAAAAAAAAAAAAAAAAAAKNs0XN10q9D269B0q9RbytphvwvjsDE0fAbT2avt3Bm4pxRLfvaPw/ZNcajDfre4a/Sax78fLm/vUm8nOb8PmNOn1BsAAAAAAAAAAAAAAAAAAOowDQl///oljMM29XayM10f4Yjzmq5rDWfusN+F71+/OB8UpaZ78BS/P+/pYZHpv+PvpmffUs7QU34fEMPn1BsAAAAAAAAAAAAAAAAAAOozDpswDpvQ9uvQdKvQdKvUW0rmsN+FcdgYGJ7Z8Ro//DpzpTFUTukeP/fb/ir1dmbhPp3XEs7QY34fEJO4CAAAAAAAAAAAAAAAAAAwm3HY/PrflzIsPBmH7R+fn/kd9rtwf3Mdmm5VTGTEOaE2Ncal3Kdx1XiGHhMVIQVxEQAAAAAAAAAAAAAAAAAgimlY+DgofFFlaGQctuGwfzAwnFgJkRGxAmo3ne9Sn/mH/e5XBII0Sj9DT3nuk5K4CAAAAAAAAAAAAAAAAAAQ1dOB7bZf//NvmUPDgiL5ehwZyWUw3XA5S/P4mT/FfnIM/kw80/Pz+Azl9Dw/hfNELv7Hf/6v//n/U28CAAAAAAAAAAAAAAAAAGCSe2xkHLb//CsQUaIUg+mGy+P59uPnrH//+9cvs/79JZnuxRxiI+7RMuV0hiZ+I5ArcREAAAAAAAAAAAAAAAAAIGvT8PAkdhTi9/9uULhGc8RsDvvdP/8jVhDb9H3OxXNgPo+f9XM+56f7MwTfZ20en6EYwRG/ESiJuAgAAAAAAAAAAAAAAAAAULRzxgQMBxPCv4M2pxASgXm85358yrOdc/xW8JynZOIiAAAAAAAAAAAAAAAAAAAAAFCpT6k3AAAAAAAAAAAAAAAAAAAAAADMQ1wEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAAAAAFApcREAAAAAAAAAAAAAAAAAAAAAqJS4CAAAAAAAAAAAAAAAAAAAAABUSlwEAAAAAAAAAAAAAAAAAAAAAColLgIAAAAAAAAAAAAAAAAAAAAAlRIXAQAAAAAAAAAAAAAAAAAAAIBKiYsAAAAAAAAAAAAAAAAAAAAAQKXERQAAAAAAAAAAAAAAAAAAAACgUuIiAAAAAAAAAAAAAAAAAPx3O3dwgkAQBVHwKxvYZCZmsEEIpulVPK8MPKpufesIHgAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAARfsSUAAAQbElEQVQAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHiIgAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAAFHHzDx3nwAAAAAAAAAAAAAAAAAAAAAArnf7HufrvWZmbXkCAAAAAAAAAAAAAAAAAAAAAFzq+NlrZh4bfgAAAAAAAAAAAAAAAAAAAAAAF7vvPgAAAAAAAAAAAAAAAAAAAAAA/Ie4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEiYsAAAAAAAAAAAAAAAAAAAAAQJS4CAAAAAAAAAAAAAAAAAAAAABEfQDByiNC7P/kIQAAAABJRU5ErkJggg==" alt="Interior Guider">
  </header>

  <div class="nome">
    <h1 id="cliente"></h1>
    <span class="ref" id="ref"></span>
    <p id="sub"></p>
  </div>

  <nav class="tiles" id="tiles" aria-label="Fases do projeto"></nav>

  <main id="fases"></main>

  <footer>
    <span>Interior Guider · Vila Nova de Gaia</span>
    <a id="contacto" href="#"></a>
  </footer>
</div>

<script>
const projeto = __PROJETO_JSON__;

const eur = v => v.toLocaleString('pt-PT') + ' €';
const $ = id => document.getElementById(id);

// PDFs grandes (a apresentação do projeto passa facilmente dos 4-5MB em
// base64) não abrem de forma fiável como href="data:..." direto — o
// Safari/iOS em particular falha ou fica preso a abrir um separador em
// branco com data URIs grandes. Converter para Blob antes de abrir
// resolve isto em todos os browsers testados.
function abrirDocumento(dataUri, nomeFicheiro){
  try {
    const [cabecalho, base64] = dataUri.split(',');
    const mime = cabecalho.match(/data:(.*?);/)[1];
    const bytes = atob(base64);
    const array = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) array[i] = bytes.charCodeAt(i);
    const blob = new File([array], nomeFicheiro, {type: mime});
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    // último recurso: se algo correr mal a converter, tenta na mesma a
    // navegação direta em vez de deixar o botão sem fazer nada
    window.open(dataUri, '_blank', 'noopener');
  }
  return false;
}

const totalProduto = projeto.valorProduto;
const temProduto   = totalProduto != null;
const credito      = temProduto ? Math.round(totalProduto/10) : 0;
const totalAPagar  = temProduto ? totalProduto - credito : 0;
const p50 = Math.round(totalAPagar*.5),
      p40 = Math.round(totalAPagar*.4),
      p10 = totalAPagar - p50 - p40;

// exemplo ilustrativo mostrado na fase de orçamento antes de existir um
// valor real (ver conteudo.orcamento) — 10.000€ fixo, só para explicar o
// mecanismo (crédito + faseamento do pagamento) à cliente; nunca é um
// valor real, por isso é sempre rotulado como exemplo.
const DEMO_VALOR = 10000;
const demoCredito = Math.round(DEMO_VALOR/10);
const demoAPagar  = DEMO_VALOR - demoCredito;
const demoP50 = Math.round(demoAPagar*.5),
      demoP40 = Math.round(demoAPagar*.4),
      demoP10 = demoAPagar - demoP50 - demoP40;

$('ref').textContent = projeto.ref;
$('cliente').textContent = projeto.cliente;
$('sub').textContent = projeto.sub;
const ct = $('contacto');
ct.textContent = projeto.contacto.rotulo;
ct.href = projeto.contacto.href;

const rotuloTile = f =>
  f.estado==="validada" ? `validado a ${f.data}` :
  f.estado==="aguarda"  ? "a aguardar validação" : "por abrir";

$('tiles').innerHTML = projeto.fases.map((f,i)=>`
  <a class="tile ${f.estado}" href="#${f.id}">
    <div class="n">${String(i+1).padStart(2,'0')}</div>
    <div class="t">${f.titulo}</div>
    <div class="e">${rotuloTile(f)}</div>
  </a>`).join('');

const conteudo = {
  honorarios: () => `
    <div class="linhas">
      ${projeto.honorarios.linhas.map(l=>`
        <div class="l"><span>${l.t}<span class="d">${l.d}</span></span><span class="v">${eur(l.v)}</span></div>`).join('')}
      <div class="l destaque"><span>Honorários de projeto</span><span class="v">${eur(projeto.honorarios.total)}</span></div>
    </div>
    <div class="credito-bloco">
      <h3>Crédito na compra Interior Guider</h3>
      <p>Os honorários cobrem o diagnóstico, o desenho e a especificação. Na compra de 100% da especificação com o Interior Guider, aplica-se um crédito de 1€ por cada 10€ do conjunto, que abate diretamente ao orçamento. O valor consta da fase de orçamento.</p>
    </div>`,

  conceito: () => `
    <div class="imagem">${projeto.conceito.imagem?`<img src="${projeto.conceito.imagem}" alt="Imagem guia">`:''}</div>
    ${projeto.conceito.leitura?projeto.conceito.leitura.split(/\n\s*\n/).map(p=>`<p class="leitura">${p.trim()}</p>`).join(''):''}
    ${projeto.conceito.materiais?`<p class="materiais">${projeto.conceito.materiais}</p>`:''}
    <div class="docs">
      <a class="doc ${projeto.documentos.conceito?'':'off'}" href="#" ${projeto.documentos.conceito?`onclick="return abrirDocumento(projeto.documentos.conceito, 'Conceito psicoestético - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span>Conceito psicoestético</span><span>PDF</span></a>
    </div>`,

  projeto: () => `
    ${projeto.ambientes.map(a=>`
      <div class="amb">
        <div class="img">${(projeto.documentos.apresentacao && a.imagem)?`<img src="${a.imagem}" alt="${a.nome}">`:''}</div>
        <h3>${a.nome}</h3>
        <p>${a.nota}</p>
      </div>`).join('')}
    <div class="docs">
      <a class="doc ${projeto.documentos.apresentacao?'':'off'}" href="#" ${projeto.documentos.apresentacao?`onclick="return abrirDocumento(projeto.documentos.apresentacao, 'Apresentação do projeto - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span>Apresentação do projeto</span><span>PDF</span></a>
    </div>`,

  orcamento: () => !temProduto ? `
    <div class="credito-bloco">
      <h3>O orçamento de produto ainda não está disponível.</h3>
      <p>Esta secção fica disponível quando o orçamento do conjunto de produto estiver definido. Entretanto, o crédito de 1€ por cada 10€ do conjunto mantém-se garantido na compra de 100% da especificação com o Interior Guider.</p>
    </div>
    <div class="exemplo">
      <span class="exemplo-selo">Exemplo ilustrativo — com ${eur(DEMO_VALOR)}, um valor fictício só para mostrar como esta fase funciona</span>
      <div class="linhas" style="margin-top:16px">
        <div class="l"><span>Ambiente completo<span class="d">100% da especificação · inclui entrega, montagem e garantia única</span></span><span class="v">${eur(DEMO_VALOR)}</span></div>
        <div class="l credito"><span>Crédito na compra Interior Guider<span class="d">1€ por cada 10€ do conjunto</span></span><span class="v">− ${eur(demoCredito)}</span></div>
        <div class="l destaque"><span>Valor a pagar</span><span class="v">${eur(demoAPagar)}</span></div>
      </div>
      <div class="pag-tit">Como se paga</div>
      <div class="pag">
        <div class="pf">
          <div class="pf-topo"><span class="pct">50%</span><span class="val">${eur(demoP50)}</span></div>
          <div class="pf-q">Na adjudicação</div>
          <p>Confirma a decisão e inicia a produção das encomendas.</p>
        </div>
        <div class="pf">
          <div class="pf-topo"><span class="pct">40%</span><span class="val">${eur(demoP40)}</span></div>
          <div class="pf-q">Encomenda concluída</div>
          <p>Quando o material está pronto a entregar.</p>
        </div>
        <div class="pf">
          <div class="pf-topo"><span class="pct">10%</span><span class="val">${eur(demoP10)}</span></div>
          <div class="pf-q">Entrega e montagem</div>
          <p>Com a instalação concluída em sua casa.</p>
        </div>
      </div>
    </div>` : `
    <div class="credito-bloco">
      <h3>Comprando o projeto completo, <em>${eur(credito)}</em> abatem ao seu orçamento.</h3>
      <p>Na compra de 100% da especificação com o Interior Guider, aplica-se um crédito de 1€ por cada 10€ do conjunto. A compra parcial não dá direito ao crédito e fica a preço de tabela.</p>
    </div>
    <div class="linhas" style="margin-top:28px">
      <div class="l"><span>Ambiente completo<span class="d">100% da especificação · inclui entrega, montagem e garantia única</span></span><span class="v">${eur(totalProduto)}</span></div>
      <div class="l credito"><span>Crédito na compra Interior Guider<span class="d">1€ por cada 10€ do conjunto</span></span><span class="v">− ${eur(credito)}</span></div>
      <div class="l destaque"><span>Valor a pagar</span><span class="v">${eur(totalAPagar)}</span></div>
    </div>
    <div class="docs">
      <a class="doc ${projeto.documentos.orcamento?'':'off'}" href="#" ${projeto.documentos.orcamento?`onclick="return abrirDocumento(projeto.documentos.orcamento, 'Orçamento detalhado - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span>Orçamento detalhado</span><span>PDF</span></a>
    </div>
    <div class="pag-tit">Como se paga</div>
    <div class="pag">
      <div class="pf">
        <div class="pf-topo"><span class="pct">50%</span><span class="val">${eur(p50)}</span></div>
        <div class="pf-q">Na adjudicação</div>
        <p>Confirma a decisão e inicia a produção das encomendas.</p>
      </div>
      <div class="pf">
        <div class="pf-topo"><span class="pct">40%</span><span class="val">${eur(p40)}</span></div>
        <div class="pf-q">Encomenda concluída</div>
        <p>Quando o material está pronto a entregar.</p>
      </div>
      <div class="pf">
        <div class="pf-topo"><span class="pct">10%</span><span class="val">${eur(p10)}</span></div>
        <div class="pf-q">Entrega e montagem</div>
        <p>Com a instalação concluída em sua casa.</p>
      </div>
    </div>
    <p class="nota"><b>Condição do crédito:</b> aplica-se apenas à compra de 100% da especificação de fornecimento. Peças pré-existentes do cliente foram integradas na fase de desenho e não entram neste valor. A compra parcial fica a preço de tabela, sem crédito. ${projeto.validade}</p>`
};

$('fases').innerHTML = projeto.fases.map((f,i)=>{
  const n = String(i+1).padStart(2,'0');
  let estado, bloco;

  if(f.estado === "validada"){
    estado = `<span class="estado ok">validado a ${f.data}</span>`;
    bloco  = conteudo[f.id]() + `
      <div class="validar">
        <p class="conv">${f.obs}</p>
        <a class="btn" href="${projeto.acoes[f.id]}">${f.acao}</a>
      </div>`;
  } else if(f.estado === "aguarda"){
    estado = `<span class="estado">a aguardar a sua validação</span>`;
    bloco  = conteudo[f.id]() + `
      <div class="validar">
        <p class="conv">${f.obs}</p>
        <button type="button" class="btn" onclick="validarFase('${f.id}', this)">${f.acao}</button>
        <p class="validar-msg" id="msg-${f.id}"></p>
      </div>`;
  } else {
    const anterior = projeto.fases[i-1];
    estado = `<span class="estado">por abrir</span>`;
    bloco  = `<div class="demo">${conteudo[f.id]()}</div>
      <p class="espera">Esta secção abre para validação depois${anterior ? ` — ${anterior.titulo.toLowerCase()}` : ''}.</p>`;
  }

  return `<section class="fase ${f.estado==='prevista'?'prevista':''}" id="${f.id}">
    <div class="fase-topo"><h2><span class="n">${n}</span>${f.titulo}</h2>${estado}</div>
    <div class="corpo">${bloco}</div>
  </section>`;
}).join('');

async function validarFase(faseId, botao){
  const textoOriginal = botao.textContent;
  const msg = document.getElementById('msg-' + faseId);
  botao.disabled = true;
  botao.textContent = 'A validar…';
  if (msg) msg.textContent = '';
  try {
    const r = await fetch(`/portal/${projeto.cardId}/validar-fase`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fase: faseId})
    });
    const corpo = await r.json();
    if (!r.ok) throw new Error(corpo.erro || 'não foi possível validar');
    location.href = location.pathname + '?v=' + Date.now();
  } catch (e) {
    botao.disabled = false;
    botao.textContent = textoOriginal;
    if (msg) msg.textContent = e.message || 'Falha de rede — tenta outra vez.';
  }
}
</script>
</body>
</html>
"""


def pagina_edicao(id_documento: int, projeto: dict) -> str:
    """Página interna (nunca linkada ao cliente) onde a equipa corrige ou
    completa os campos de um portal já gerado, sem precisar de pedir à
    Alma para gerar tudo de novo. Ao gravar, chama
    atualizar_portal_projeto_edicao, que mantém o mesmo link (ver
    db.guardar_ou_atualizar_documento_gerado)."""
    projeto_json = json.dumps(projeto, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE_EDICAO.replace("__PROJETO_JSON__", projeto_json).replace("__ID__", str(id_documento))
    return html


_TEMPLATE_EDICAO = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="utf-8">
<title>Editar portal — uso interno</title>
<meta name="robots" content="noindex, nofollow">
<style>
  :root{--paper:#FBFAF8; --ink:#1C1A17; --stone:#8E877C; --line:#E5E0D7; --clay:#B96D4E; --ok:#5A7D5A; --err:#B94E4E;}
  *{box-sizing:border-box}
  body{background:var(--paper);color:var(--ink);font-family:'Jost',system-ui,sans-serif;
      max-width:760px;margin:0 auto;padding:36px 20px 100px}
  h1{font-size:24px;font-weight:500;margin:0 0 4px}
  .aviso{background:#FBEFE8;border:1px solid #E9D3C3;border-radius:6px;padding:12px 14px;font-size:13px;
        color:var(--stone);margin:16px 0 32px}
  section{border-top:1px solid var(--line);padding:24px 0}
  section h2{font-size:15px;font-weight:600;margin:0 0 14px;text-transform:uppercase;letter-spacing:.04em;color:var(--stone)}
  label{display:block;font-size:12.5px;color:var(--stone);margin-bottom:4px}
  input[type=text],input[type=number],textarea{
      width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:5px;background:#fff;
      font-family:inherit;font-size:14.5px;color:var(--ink)}
  textarea{resize:vertical;min-height:70px}
  .campo{margin-bottom:14px}
  .linha2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .linha3{display:grid;grid-template-columns:2fr 3fr 1fr;gap:10px;align-items:end}
  .check{display:flex;align-items:center;gap:8px;font-size:13.5px;color:var(--ink);margin:10px 0}
  .check input{width:auto}
  .linha-tabela{border:1px solid var(--line);border-radius:6px;padding:14px;margin-bottom:10px;position:relative}
  .remover{position:absolute;top:8px;right:10px;background:none;border:none;color:var(--stone);cursor:pointer;font-size:16px}
  .remover:hover{color:var(--err)}
  .add{background:none;border:1px dashed var(--line);border-radius:5px;padding:8px 14px;font-size:13px;
      color:var(--stone);cursor:pointer;width:100%;margin-top:4px}
  .add:hover{border-color:var(--clay);color:var(--clay)}
  .miniatura{max-width:160px;max-height:110px;border-radius:5px;display:block;margin-bottom:8px;border:1px solid var(--line)}
  .fase-bloco{border:1px solid var(--line);border-radius:6px;padding:14px;margin-bottom:10px}
  .fase-bloco h3{font-size:14px;margin:0 0 10px;font-weight:500}
  select{padding:9px 10px;border:1px solid var(--line);border-radius:5px;background:#fff;font-family:inherit;font-size:14.5px}
  .rodape{position:sticky;bottom:0;background:var(--paper);border-top:1px solid var(--line);
        padding:16px 0;margin-top:20px;display:flex;align-items:center;gap:14px}
  .guardar{background:var(--ink);color:#fff;border:none;border-radius:5px;padding:12px 26px;
          font-size:14.5px;cursor:pointer}
  .guardar:disabled{opacity:.5;cursor:default}
  .estado-msg{font-size:13.5px}
  .estado-msg.ok{color:var(--ok)}
  .estado-msg.err{color:var(--err)}
  .estado-msg a{color:inherit;text-decoration:underline}
</style>
</head>
<body>

<h1>Editar portal — <span id="tit-cliente"></span></h1>
<div class="aviso">Página de uso interno — nunca partilhes este link com a cliente. Serve para corrigir ou completar campos sem pedir à Alma para gerar tudo outra vez. Gravar mantém o mesmo link do portal.</div>

<section>
  <h2>Identificação</h2>
  <div class="campo"><label>Cliente</label><input type="text" id="f-cliente"></div>
  <div class="campo"><label>Validade da proposta</label><input type="text" id="f-validade"></div>
</section>

<section>
  <h2>Honorários</h2>
  <div class="campo linha2">
    <div><label>Total (com IVA)</label><input type="number" step="0.01" id="f-honorarios-total"></div>
    <div class="check" style="margin-top:22px"><input type="checkbox" id="f-honorarios-com-iva" checked>
      <label style="margin:0">Este valor já inclui IVA</label></div>
  </div>
  <div id="linhas-honorarios"></div>
  <button type="button" class="add" onclick="addLinhaHonorario()">+ adicionar linha de honorários</button>
</section>

<section>
  <h2>Conceito</h2>
  <div class="campo">
    <label>Imagem do conceito</label>
    <div id="preview-conceito"></div>
    <input type="file" accept="image/*" id="f-conceito-imagem-ficheiro">
  </div>
  <div class="campo"><label>Leitura do conceito (texto, opcional)</label><textarea id="f-conceito-leitura"></textarea></div>
  <div class="campo"><label>Materiais/estilo (linha curta, opcional)</label><input type="text" id="f-conceito-materiais"></div>
  <div class="campo">
    <label>Documento do conceito (PDF, para a cliente poder descarregar)</label>
    <div id="preview-conceito-doc"></div>
    <input type="file" accept="application/pdf" id="f-conceito-doc-ficheiro">
  </div>
</section>

<section>
  <h2>Ambientes</h2>
  <div id="linhas-ambientes"></div>
  <button type="button" class="add" onclick="addAmbiente()">+ adicionar ambiente</button>
</section>

<section>
  <h2>Documentos</h2>
  <div class="campo">
    <label>Imagem do projeto (capa da fase "Projeto")</label>
    <div id="preview-projeto-imagem"></div>
    <input type="file" accept="image/*" id="f-projeto-imagem-ficheiro">
  </div>
  <div class="campo">
    <label>Apresentação do projeto (PDF)</label>
    <div id="preview-doc-apresentacao"></div>
    <input type="file" accept="application/pdf" id="f-doc-apresentacao-ficheiro">
  </div>
  <div class="campo">
    <label>Orçamento detalhado (PDF)</label>
    <div id="preview-doc-orcamento"></div>
    <input type="file" accept="application/pdf" id="f-doc-orcamento-ficheiro">
  </div>
</section>

<section>
  <h2>Orçamento de produto</h2>
  <div class="campo linha2">
    <div><label>Valor total (com IVA) — deixa vazio se ainda não existir</label>
      <input type="number" step="0.01" id="f-valor-produto"></div>
    <div class="check" style="margin-top:22px"><input type="checkbox" id="f-valor-produto-com-iva">
      <label style="margin:0">Este valor já inclui IVA</label></div>
  </div>
</section>

<section>
  <h2>Estado das fases</h2>
  <div id="fases-blocos"></div>
</section>

<div class="rodape">
  <button class="guardar" id="btn-guardar" onclick="guardar()">Guardar alterações</button>
  <span class="estado-msg" id="msg-estado"></span>
</div>

<script>
const projeto = __PROJETO_JSON__;
const idDocumento = "__ID__";
const FASES = [
  {id:"honorarios", titulo:"Honorários"}, {id:"conceito", titulo:"Conceito"},
  {id:"projeto", titulo:"Projeto"}, {id:"orcamento", titulo:"Orçamento"},
];

document.getElementById('tit-cliente').textContent = projeto.cliente;
document.getElementById('f-cliente').value = projeto.cliente || '';
document.getElementById('f-validade').value = projeto.validade || '';
document.getElementById('f-honorarios-total').value = projeto.honorarios.total ?? '';
document.getElementById('f-conceito-leitura').value = projeto.conceito.leitura || '';
document.getElementById('f-conceito-materiais').value = projeto.conceito.materiais || '';
document.getElementById('f-valor-produto').value = projeto.valorProduto ?? '';
document.getElementById('f-valor-produto-com-iva').checked = projeto.valorProduto != null;

let conceitoImagemAtual = projeto.conceito.imagem || null;
if (conceitoImagemAtual) {
  document.getElementById('preview-conceito').innerHTML =
    `<img class="miniatura" src="${conceitoImagemAtual}" alt="Imagem atual do conceito">`;
}
document.getElementById('f-conceito-imagem-ficheiro').addEventListener('change', function(e){
  const ficheiro = e.target.files[0];
  if (!ficheiro) return;
  const leitor = new FileReader();
  leitor.onload = () => {
    conceitoImagemAtual = leitor.result;
    document.getElementById('preview-conceito').innerHTML =
      `<img class="miniatura" src="${conceitoImagemAtual}" alt="Nova imagem do conceito">`;
  };
  leitor.readAsDataURL(ficheiro);
});

let projetoImagemAtual = projeto.projetoImagem || null;
if (projetoImagemAtual) {
  document.getElementById('preview-projeto-imagem').innerHTML =
    `<img class="miniatura" src="${projetoImagemAtual}" alt="Imagem atual do projeto">`;
}
document.getElementById('f-projeto-imagem-ficheiro').addEventListener('change', function(e){
  const ficheiro = e.target.files[0];
  if (!ficheiro) return;
  const leitor = new FileReader();
  leitor.onload = () => {
    projetoImagemAtual = leitor.result;
    document.getElementById('preview-projeto-imagem').innerHTML =
      `<img class="miniatura" src="${projetoImagemAtual}" alt="Nova imagem do projeto">`;
  };
  leitor.readAsDataURL(ficheiro);
});

function configurarUploadPdf(idPreview, idFicheiro, valorInicial) {
  const estado = {valor: valorInicial || null};
  const preview = document.getElementById(idPreview);
  function atualizar(nome) {
    preview.textContent = estado.valor ? `PDF atual: ${nome || 'anexado'}` : '(sem documento)';
  }
  atualizar(estado.valor ? 'já anexado' : null);
  document.getElementById(idFicheiro).addEventListener('change', function(e){
    const ficheiro = e.target.files[0];
    if (!ficheiro) return;
    const leitor = new FileReader();
    leitor.onload = () => {
      estado.valor = leitor.result;
      atualizar(ficheiro.name);
    };
    leitor.readAsDataURL(ficheiro);
  });
  return estado;
}
const conceitoDocumento = configurarUploadPdf('preview-conceito-doc', 'f-conceito-doc-ficheiro', projeto.documentos.conceito);
const docApresentacao = configurarUploadPdf('preview-doc-apresentacao', 'f-doc-apresentacao-ficheiro', projeto.documentos.apresentacao);
const docOrcamento = configurarUploadPdf('preview-doc-orcamento', 'f-doc-orcamento-ficheiro', projeto.documentos.orcamento);

function addLinhaHonorario(dados) {
  dados = dados || {t:'', d:'', v:''};
  const div = document.createElement('div');
  div.className = 'linha-tabela linha-honorario';
  div.innerHTML = `
    <button type="button" class="remover" onclick="this.parentElement.remove()">&times;</button>
    <div class="linha3">
      <div><label>Título</label><input type="text" class="lh-titulo" value="${dados.t.replace(/"/g,'&quot;')}"></div>
      <div><label>Descrição</label><input type="text" class="lh-descricao" value="${(dados.d||'').replace(/"/g,'&quot;')}"></div>
      <div><label>Valor</label><input type="number" step="0.01" class="lh-valor" value="${dados.v}"></div>
    </div>`;
  document.getElementById('linhas-honorarios').appendChild(div);
}
(projeto.honorarios.linhas || []).forEach(addLinhaHonorario);

function addAmbiente(dados) {
  dados = dados || {nome:'', nota:'', imagem:null};
  const div = document.createElement('div');
  div.className = 'linha-tabela linha-ambiente';
  div.dataset.imagem = dados.imagem || '';
  div.innerHTML = `
    <button type="button" class="remover" onclick="this.parentElement.remove()">&times;</button>
    <div class="campo"><label>Nome</label><input type="text" class="amb-nome" value="${dados.nome.replace(/"/g,'&quot;')}"></div>
    <div class="campo"><label>Nota</label><textarea class="amb-nota">${dados.nota || ''}</textarea></div>
    <div class="campo"><label>Imagem</label>
      <div class="amb-preview">${dados.imagem?`<img class="miniatura" src="${dados.imagem}">`:''}</div>
      <input type="file" accept="image/*" class="amb-imagem-ficheiro">
    </div>`;
  div.querySelector('.amb-imagem-ficheiro').addEventListener('change', function(e){
    const ficheiro = e.target.files[0];
    if (!ficheiro) return;
    const leitor = new FileReader();
    leitor.onload = () => {
      div.dataset.imagem = leitor.result;
      div.querySelector('.amb-preview').innerHTML = `<img class="miniatura" src="${leitor.result}">`;
    };
    leitor.readAsDataURL(ficheiro);
  });
  document.getElementById('linhas-ambientes').appendChild(div);
}
(projeto.ambientes || []).forEach(addAmbiente);

const fasesEstadoAtual = {};
(projeto.fases || []).forEach(f => { fasesEstadoAtual[f.id] = {estado: f.estado, data: f.data}; });
const blocosFases = document.getElementById('fases-blocos');
FASES.forEach(f => {
  const atual = fasesEstadoAtual[f.id] || {estado: 'prevista', data: ''};
  const div = document.createElement('div');
  div.className = 'fase-bloco';
  div.dataset.faseId = f.id;
  div.innerHTML = `
    <h3>${f.titulo}</h3>
    <div class="linha2">
      <div><label>Estado</label>
        <select class="fase-estado">
          <option value="validada" ${atual.estado==='validada'?'selected':''}>Validada</option>
          <option value="aguarda" ${atual.estado==='aguarda'?'selected':''}>A aguardar validação</option>
          <option value="prevista" ${atual.estado==='prevista'?'selected':''}>Por abrir</option>
        </select>
      </div>
      <div class="fase-data-bloco"><label>Data da validação (por extenso, ex: "8 de janeiro de 2026")</label>
        <input type="text" class="fase-data" value="${atual.data || ''}"></div>
    </div>`;
  blocosFases.appendChild(div);
  const selo = div.querySelector('.fase-estado');
  const blocoData = div.querySelector('.fase-data-bloco');
  const atualizarVisibilidadeData = () => { blocoData.style.display = selo.value === 'validada' ? '' : 'none'; };
  selo.addEventListener('change', atualizarVisibilidadeData);
  atualizarVisibilidadeData();
});

function guardar() {
  const btn = document.getElementById('btn-guardar');
  const msg = document.getElementById('msg-estado');
  btn.disabled = true;
  msg.textContent = 'A gravar…';
  msg.className = 'estado-msg';

  const honorarios_linhas = [...document.querySelectorAll('.linha-honorario')].map(el => ({
    titulo: el.querySelector('.lh-titulo').value,
    descricao: el.querySelector('.lh-descricao').value,
    valor: parseFloat(el.querySelector('.lh-valor').value) || 0,
  }));
  const ambientes = [...document.querySelectorAll('.linha-ambiente')].map(el => ({
    nome: el.querySelector('.amb-nome').value,
    nota: el.querySelector('.amb-nota').value,
    imagem: el.dataset.imagem || null,
  }));
  const fases_estado = {};
  document.querySelectorAll('.fase-bloco').forEach(div => {
    const estado = div.querySelector('.fase-estado').value;
    const data = div.querySelector('.fase-data').value;
    fases_estado[div.dataset.faseId] = estado === 'validada' ? {estado, data} : {estado};
  });
  const valorProdutoTexto = document.getElementById('f-valor-produto').value;

  const campos = {
    cliente: document.getElementById('f-cliente').value,
    validade: document.getElementById('f-validade').value,
    honorarios_total: parseFloat(document.getElementById('f-honorarios-total').value) || 0,
    honorarios_total_com_iva: document.getElementById('f-honorarios-com-iva').checked,
    honorarios_linhas,
    conceito: {
      imagem: conceitoImagemAtual,
      leitura: document.getElementById('f-conceito-leitura').value || null,
      materiais: document.getElementById('f-conceito-materiais').value || null,
      documento: conceitoDocumento.valor,
    },
    documento_apresentacao: docApresentacao.valor,
    documento_orcamento: docOrcamento.valor,
    projeto_imagem: projetoImagemAtual,
    ambientes,
    valor_produto: valorProdutoTexto === '' ? null : parseFloat(valorProdutoTexto),
    valor_produto_com_iva: document.getElementById('f-valor-produto-com-iva').checked,
    fases_estado,
  };

  fetch(`/documentos-gerados/${idDocumento}/editar`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(campos)
  }).then(r => r.json().then(corpo => ({ok: r.ok, corpo})))
    .then(({ok, corpo}) => {
      btn.disabled = false;
      if (ok) {
        msg.className = 'estado-msg ok';
        msg.innerHTML = `Gravado. <a href="${corpo.url}" target="_blank">Ver portal</a>`;
      } else {
        msg.className = 'estado-msg err';
        msg.textContent = corpo.erro || 'Não consegui gravar.';
      }
    })
    .catch(() => {
      btn.disabled = false;
      msg.className = 'estado-msg err';
      msg.textContent = 'Falha de rede — tenta outra vez.';
    });
}
</script>
</body>
</html>
"""


def pagina_lista(portais: list) -> str:
    """Página interna (nunca linkada à cliente) onde a equipa consulta
    todos os portais de projeto já gerados, com pesquisa — ver
    db.listar_portais_projeto. Cada portal aponta para o seu link público
    (só leitura) e para a sua página de edição, exatamente como já
    existem hoje."""
    portais_json = json.dumps(portais, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE_LISTA.replace("__PORTAIS_JSON__", portais_json)


_TEMPLATE_LISTA = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="utf-8">
<title>Portais de projeto — uso interno</title>
<meta name="robots" content="noindex, nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root{--paper:#fdfaee; --ink:#1C1A17; --stone:#8E877C; --line:#E5E0D7; --clay:#B96D4E; --ok:#5A7D5A; --err:#B94E4E; --sienna:#A43A23;}
  *{box-sizing:border-box}
  body{background:var(--paper);color:var(--ink);font-family:'Jost',system-ui,sans-serif;
      max-width:1240px;margin:0 auto;padding:36px 24px 100px}
  h1{font-size:24px;font-weight:500;margin:0 0 4px}
  .sub{font-size:13.5px;color:var(--stone);margin:0 0 28px}
  .barra{display:flex;gap:12px;align-items:center;margin-bottom:26px;position:sticky;top:0;background:var(--paper);
        padding:10px 0;border-bottom:1px solid var(--line);z-index:1}
  .barra input{flex:1;max-width:420px;padding:11px 14px;border:1px solid var(--line);border-radius:6px;background:#fff;
              font-family:inherit;font-size:14.5px;color:var(--ink)}
  .barra input:focus{outline:2px solid var(--clay);outline-offset:1px}
  .contagem{font-size:12.5px;color:var(--stone);white-space:nowrap}
  .vazio{padding:60px 0;text-align:center;color:var(--stone);font-size:14px}
  .grelha{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}
  .tile{border:1px solid var(--line);border-radius:10px;background:#fff;overflow:hidden;
       display:flex;flex-direction:column;transition:.15s}
  .tile:hover{border-color:var(--clay);box-shadow:0 6px 18px rgba(28,26,23,.06);transform:translateY(-1px)}
  .tile-img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:var(--line)}
  .tile-img-vazio{width:100%;aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;
                 background:linear-gradient(135deg,var(--line),var(--paper));color:var(--stone);font-size:11.5px}
  .tile-corpo{padding:18px 20px 20px;display:flex;flex-direction:column;gap:16px;flex:1}
  .tile-topo .cliente{font-size:16.5px;font-weight:500;color:var(--ink);margin:0 0 3px}
  .tile-topo .ref{font-size:11.5px;color:var(--stone);font-weight:600;letter-spacing:.03em;text-transform:uppercase}
  .passos{display:flex;gap:4px}
  .passo{flex:1;height:5px;border-radius:3px;background:var(--line)}
  .passo.validada{background:var(--sienna)}
  .passo.aguarda{background:var(--clay)}
  .fases{display:flex;flex-wrap:wrap;gap:6px}
  .fase-chip{font-size:10.5px;padding:3px 9px;border-radius:20px;border:1px solid #E8A05F;
            background:#F8B681;color:#000;white-space:nowrap}
  .tile-rodape{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:auto;
              padding-top:14px;border-top:1px solid var(--line)}
  .tile-data{font-size:11px;color:var(--stone)}
  .acoes{display:flex;gap:6px}
  .acoes a{display:inline-block;padding:7px 13px;border-radius:5px;font-size:12.5px;text-decoration:none;
          border:1px solid var(--ink);color:var(--ink);transition:.12s}
  .acoes a.editar{background:var(--ink);color:var(--paper)}
  .acoes a:hover{opacity:.75}
  .acoes button.del{display:inline-block;padding:9px 13px;border-radius:5px;font-size:12.5px;
                    border:1px solid var(--line);background:none;color:var(--stone);cursor:pointer;
                    font-family:inherit;transition:.12s}
  .acoes button.del:hover{border-color:var(--err);color:var(--err)}
  .acoes button.del:disabled{opacity:.5;cursor:default}
</style>
</head>
<body>
  <h1>Portais de projeto</h1>
  <p class="sub">Todos os portais de acompanhamento gerados para clientes — o link de "Ver" é o que a cliente recebe.</p>
  <div class="barra">
    <input type="text" id="pesquisa" placeholder="Procurar por cliente, referência ou nº do card…" autofocus>
    <span class="contagem" id="contagem"></span>
  </div>
  <div id="lista"></div>

<script>
let portais = __PORTAIS_JSON__;

const rotuloFase = f => f.estado === 'validada' ? `${f.titulo} ✓` :
                        f.estado === 'aguarda'  ? `${f.titulo} — a aguardar` : `${f.titulo}`;

function formatarData(iso){
  return new Date(iso).toLocaleDateString('pt-PT', {day: 'numeric', month: 'long', year: 'numeric'});
}

function desenhar(filtro){
  const alvo = (filtro || '').trim().toLowerCase();
  const visiveis = portais.filter(p => !alvo ||
    (p.cliente || '').toLowerCase().includes(alvo) ||
    (p.ref || '').toLowerCase().includes(alvo) ||
    String(p.card_id).includes(alvo));

  document.getElementById('contagem').textContent =
    visiveis.length === portais.length ? `${portais.length} portais` : `${visiveis.length} de ${portais.length}`;

  document.getElementById('lista').innerHTML = !visiveis.length ? `<p class="vazio">Nenhum portal encontrado para "${filtro}".</p>` : `
    <div class="grelha">
      ${visiveis.map(p => `
        <div class="tile">
          ${p.imagem
            ? `<img class="tile-img" src="${p.imagem}" alt="Imagem de conceito de ${p.cliente || 'projeto'}">`
            : `<div class="tile-img-vazio">Sem imagem de conceito</div>`}
          <div class="tile-corpo">
            <div class="tile-topo">
              <p class="cliente">${p.cliente || '(sem nome)'}</p>
              <p class="ref">${p.ref || ''} · card ${p.card_id}</p>
            </div>
            <div class="passos">
              ${p.fases.map(f => `<span class="passo ${f.estado}"></span>`).join('')}
            </div>
            <div class="fases">
              ${p.fases.map(f => `<span class="fase-chip ${f.estado}">${rotuloFase(f)}</span>`).join('')}
            </div>
            <div class="tile-rodape">
              <span class="tile-data">atualizado a ${formatarData(p.criado_em)}</span>
              <div class="acoes">
                <a href="/documentos-gerados/${p.id}" target="_blank" rel="noopener">Ver</a>
                <a class="editar" href="/documentos-gerados/${p.id}/editar" target="_blank" rel="noopener">Editar</a>
                <button type="button" class="del" onclick="eliminarPortal(${p.id}, this)">Eliminar</button>
              </div>
            </div>
          </div>
        </div>`).join('')}
    </div>`;
}

document.getElementById('pesquisa').addEventListener('input', e => desenhar(e.target.value));
desenhar('');

async function eliminarPortal(id, botao){
  const portal = portais.find(p => p.id === id);
  const nome = portal ? (portal.cliente || `card ${portal.card_id}`) : 'este portal';
  if (!confirm(`Eliminar o portal de ${nome}? Esta ação é irreversível — o link deixa de funcionar para a cliente.`)) return;
  if (!confirm(`Tens mesmo a certeza? Vais eliminar definitivamente o portal de ${nome} — não há como desfazer.`)) return;
  botao.disabled = true;
  botao.textContent = 'A eliminar…';
  try {
    const r = await fetch(`/documentos-gerados/${id}`, {method: 'DELETE'});
    if (!r.ok) throw new Error('não foi possível eliminar');
    portais = portais.filter(p => p.id !== id);
    desenhar(document.getElementById('pesquisa').value);
  } catch (e) {
    botao.disabled = false;
    botao.textContent = 'Eliminar';
    alert('Falha ao eliminar — tenta outra vez.');
  }
}
</script>
</body>
</html>
"""
