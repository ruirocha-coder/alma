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

  header{padding:44px 0 0;display:flex;justify-content:space-between;align-items:center;gap:16px}
  header img{height:38px;width:auto;opacity:.9}
  header .ref{font-size:12px;color:var(--stone)}

  .nome{padding:60px 0 40px}
  .nome h1{font-weight:300;font-size:clamp(30px,6vw,44px);line-height:1.15;letter-spacing:-.01em}
  .nome p{margin-top:14px;color:var(--stone);font-size:15px;max-width:46ch}

  .tiles{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
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
  .fase-topo{display:flex;justify-content:space-between;align-items:baseline;gap:16px}
  .fase-topo h2{font-weight:400;font-size:20px;display:flex;align-items:baseline;gap:12px}
  .fase-topo h2 .n{font-size:12px;color:var(--stone);font-weight:300}
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
  .l .d{display:block;font-size:12.5px;color:var(--stone);margin-top:1px}
  .l .v{white-space:nowrap;font-weight:400}
  .l.credito{color:var(--clay)}
  .l.destaque{border-bottom:none;padding-top:16px;border-top:1px solid var(--ink);margin-top:4px}
  .l.destaque .v{font-size:24px;font-weight:300}

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
  .validado{margin-top:30px;padding-top:22px;border-top:1px solid var(--line);
            font-size:13px;color:var(--clay);display:flex;align-items:center;gap:9px}
  .validado::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--clay)}
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
    <span class="ref" id="ref"></span>
    <img src="data:image/png;base64,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" alt="Interior Guider">
  </header>

  <div class="nome">
    <h1 id="cliente"></h1>
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
      <div class="validado">Validado por si a ${f.data}</div>
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
