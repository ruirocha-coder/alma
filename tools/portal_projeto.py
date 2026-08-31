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
    --clay:#A43A23; --err:#B94E4E;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{background:var(--paper);color:var(--ink);font-family:'Jost',system-ui,sans-serif;
       font-size:16px;line-height:1.75;font-weight:300;-webkit-font-smoothing:antialiased}
  .wrap{max-width:720px;margin:0 auto;padding:0 28px}
  a{color:inherit}

  header{background:var(--clay);width:100%}
  .topo{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:20px 28px}
  .logo{height:44px;width:auto;display:block;flex-shrink:0}
  .projeto-id{color:#fff;font-size:13px;text-align:right;white-space:nowrap;opacity:.92}
  .projeto-id .ref{font-size:12px;color:#fff;opacity:.8;margin-left:8px}
  .faixa{width:100%;height:8px;background:#91A4A7}

  .boas-vindas{padding:48px 0 8px}
  .boas-vindas h1{font-weight:400;font-size:32px;line-height:1.3}
  .boas-vindas h1 .destaque{color:#F8B681}
  .boas-vindas p{margin-top:14px;color:var(--stone);font-size:15px}

  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:64px}
  .tile{position:relative;padding:18px 16px 16px;background:rgba(145,164,167,.15);
       text-decoration:none;color:inherit;display:block;transition:background-color .15s}
  .tile:not(:first-child)::before{content:"";position:absolute;left:-4px;top:0;bottom:0;
       width:1px;background:var(--line)}
  .tile:hover{background:rgba(145,164,167,.25)}
  .tile .t{font-size:14.5px;font-weight:400}
  .tile .e{font-size:11px;margin-top:10px;color:var(--stone);line-height:1.4}
  .tile.validada .e{color:var(--clay)}
  .tile.aguarda{background:#F5F2EC}
  .tile.aguarda .t{font-weight:500}
  .tile.prevista{opacity:.34;pointer-events:none}

  .fase{border-top:1px solid var(--line);padding:64px 0;scroll-margin-top:16px}
  .fase.prevista{opacity:.32}
  .demo{pointer-events:none}
  .fase-topo{display:flex;justify-content:space-between;align-items:flex-end;gap:16px}
  .fase-topo h2{font-weight:400;font-size:20px;padding-left:27px;
       background:url(data:image/png;base64,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) no-repeat 0 10px;background-size:18px 16px}
  .estado{font-size:12px;color:var(--stone);white-space:nowrap}
  .estado.ok{color:var(--clay)}
  .corpo{margin-top:0}
  .fase-topo+.corpo{margin-top:26px}

  .imagem{aspect-ratio:16/10;background:linear-gradient(135deg,#EDE8DF,#C9BEAC);margin-top:18px;margin-bottom:22px}
  .imagem img{width:100%;height:100%;object-fit:cover;display:block}
  .leitura{font-size:15px;line-height:1.7;text-align:justify;color:var(--stone);padding:0 10px}
  .leitura+.leitura{margin-top:14px}
  .materiais{margin-top:16px;font-size:13px;color:var(--stone);padding:0 10px}

  .amb{margin-top:34px}
  .docs+.amb{margin-top:18px}
  .amb:first-of-type{margin-top:0}
  .amb .img{aspect-ratio:16/9;background:linear-gradient(135deg,#E9E2D5,#CBBBA3)}
  .amb .img img{width:100%;height:100%;object-fit:cover;display:block}
  .amb-texto{text-align:right;margin-top:6px;padding:0 10px}
  .amb h3{font-weight:300;font-size:15px}
  .amb p{font-size:15px;color:var(--stone);margin-top:3px}

  .linhas{width:100%}
  .l{display:flex;justify-content:space-between;gap:20px;padding:12px 0;border-bottom:1px solid var(--line);font-size:15px}
  .l:last-child{border-bottom:none}
  .l:has(+ .l.destaque){border-bottom:none}
  .l .d{display:block;font-size:12.5px;color:var(--stone);margin-top:1px}
  .l .v{white-space:nowrap}
  .l.credito{color:var(--clay)}
  .l.destaque{border-bottom:none;padding-top:16px;border-top:1px solid var(--ink);margin-top:4px}
  .linhas-caixa{border:1px solid var(--line);padding:0 16px}
  .linhas-caixa .l.destaque{margin:4px -16px 0;padding:16px 16px 14px;background:#F1ECDA}
  .linhas-caixa .l.destaque .v{font-size:19px;font-weight:500}

  .docs{margin-top:28px}
  .corpo>.docs:first-child{margin-top:0}
  .doc{padding:14px 0 14px 26px;font-size:14.5px;font-weight:400;text-decoration:none;
       transition:color .15s;display:block;position:relative;
       background:url(data:image/png;base64,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) no-repeat 0 19px;background-size:16px 16px}
  .doc:hover{color:var(--clay)}
  .doc-txt{position:relative;border-bottom:2px solid #F8B681;padding-bottom:1px}
  .doc-txt::after{content:"";position:absolute;left:-19px;top:100%;width:0;border-left:2px solid #F8B681;height:34px}
  .doc .ext{color:var(--stone);font-size:12.5px}
  .doc:hover .ext{color:var(--clay)}
  .doc.off{opacity:.4;pointer-events:none}

  .credito-bloco{margin-top:40px}
  .credito-bloco h3{font-weight:400;font-size:14.5px;line-height:1.35;padding-left:27px;
       background:url(data:image/png;base64,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) no-repeat 0 2px;background-size:16px 16px}
  .credito-bloco h3 em{font-style:normal;color:var(--clay)}
  .credito-bloco p{margin-top:10px;font-size:12.5px;color:var(--stone)}
  .credito-escuro{background:#91A4A7;padding:22px 24px}
  .credito-escuro h3{color:#fff}
  .credito-escuro h3 em{color:#F8B681}
  .credito-escuro p{color:rgba(255,255,255,.78)}

  .nota{margin-top:40px;font-size:13px;color:var(--stone);padding:0 10px}
  .nota b{font-weight:500;color:var(--ink)}

  .exemplo{margin-top:26px;padding:18px 20px;border:1px dashed var(--line);border-radius:8px;background:rgba(0,0,0,.015)}
  .painel-resumo{margin-top:18px;padding:2px 14px}
  .painel-resumo:has(.pag){padding:0}
  .exemplo-selo{display:inline-block;font-size:12px;color:var(--clay);font-weight:500;letter-spacing:.01em}

  .pag-tit{margin-top:40px;font-size:15px;font-weight:500;color:var(--ink);padding-left:27px;
       background:url(data:image/png;base64,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) no-repeat 0 5px;background-size:16px 16px}
  .pag{margin-top:16px;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
  .painel-resumo .pag{margin-top:0}
  .pf{padding:12px 18px;background:rgba(248,182,129,.15)}
  .pf-topo{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
  .pf-topo .pct{font-size:22px;font-weight:300;color:var(--stone)}
  .pf-topo .val{font-size:14.5px;font-weight:400;white-space:nowrap;color:var(--clay)}
  .pf-q{margin-top:10px;font-size:12.5px;font-weight:500}
  .pf p{margin-top:5px;font-size:12.5px;color:var(--stone);line-height:1.55}

  .validar{margin-top:34px;padding-top:26px;text-align:right}
  .validar .conv{font-size:13px;color:var(--stone);margin-top:14px}
  .btn{display:inline-block;margin-top:14px;background:#91A4A7;border:1px solid #91A4A7;color:#000;
       text-decoration:none;font-size:13px;font-weight:400;padding:6.9px 32px;transition:.15s;
       font-family:inherit;cursor:pointer}
  .btn:hover{background:transparent;color:var(--ink)}
  .btn:focus-visible{outline:2px solid var(--clay);outline-offset:3px}
  .btn:disabled{opacity:.5;cursor:default}
  .btn:disabled:hover{background:#91A4A7;color:#000}
  .validar-msg{margin-top:10px;font-size:12.5px;color:var(--err)}
  .validar-caixa{margin-top:34px;padding:26px 24px;background:#91A4A7;display:flex;
       justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap}
  .validar-texto{flex:1;min-width:220px}
  .validar-texto h3{color:#fff;font-weight:400;font-size:20px;line-height:1.3}
  .validar-texto p{color:rgba(255,255,255,.85);font-size:13.5px;margin-top:8px;max-width:420px}
  .btn-adjudicar{background:#fff;color:var(--ink);border:none;padding:16px 28px;font-size:14px;
       font-weight:500;font-family:inherit;cursor:pointer;white-space:nowrap;transition:.15s}
  .btn-adjudicar:hover{background:#F5F2EC}
  .btn-adjudicar:disabled{opacity:.6;cursor:default}
  .espera{margin-top:24px;font-size:13.5px;color:var(--stone)}

  footer{border-top:1px solid var(--line);margin-top:20px;padding:26px 0 60px;
         font-size:12px;color:var(--stone);display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
  footer a{text-decoration:none;border-bottom:1px solid var(--line)}

  @media(max-width:560px){
    .tiles{grid-template-columns:repeat(2,1fr)}
    .tile:nth-child(3)::before{display:none}
    .tile:nth-child(3)::after,.tile:nth-child(4)::after{content:"";position:absolute;
         top:-4px;left:0;right:0;height:1px;background:var(--line)}
    .fase-topo{flex-direction:column;gap:4px}
    .pag{grid-template-columns:1fr}
  }
</style>
</head>
<body>
<header>
  <div class="topo wrap">
    <img class="logo" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAvMAAAGCCAYAAABtk0wuAAAv2ElEQVR4nO3dX3LjOJbv8YMO2X4veRfdezBzCRNxu+cupjuSjunFzO2ZPSS9h+pdWPVuWRG4DyTStFKy/hHnHADfz8tMVVeKSJsCfjw4JEUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALhVsB4AULP3t833EKTPeYzV/ZrvMVCo3HME8wNQv9USH7LbbuISn6MhRunvHtbP1uMAAAAAbvUn6wEAAAAAuM4ilXkAAHC5EOIQY+itxwGgXM2F+RDiYD0GAABERFb3jy8i8mI9DgDlaq7NZpo4AQAAgOI1F+YBAACAWhDmAQAAgEIR5gEAAIBCEeYBAACAQhHmAQAAgEIR5gEAAIBCEeYBAACAQhHmAQAAgEIR5gEAAIBCEeYBAACAQhHmAQAAgEIR5gEAAIBCEeYBAACAQhHmAQAAgEIR5gEAAIBCrZb4kBilX+JzAAAAAJxvkTB/97B+XuJzAAAAAJyPNhsAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFCEeQAAAKBQhHkAAACgUIR5AAAAoFAr6wGgHO9vm+/H/rcQ4rC6f3zRHA+wlK/ObRHOb6AlzAcoDWEen+y2r08xhk5EJATpz/+TQXbbzad/E+PHn797WD8vMDzgYnsLcxeCdJd/yq/ntwjnOFAarfmAwA9NwXoAsJcmt8vC+3XSZNdK8Hl/23zP/XNd3a/5Hk9mF6NXLtK3i1EGERlE2jnPa3OgMvvL+RSj9Px+fbu+OLWs1ta9Gs3PpYmrOYEQkMmpbbprLHmSpBPTeIIbQoi95+rFAr/H7KFyXg26RukLjObF6DVSuK+9UndgsbuZxs/s2gvApRbuHD+3udK/35diPvBl6SzUwpxwDcJ8JrvtJi79mUtUYKeTtLeqWh7jtcqV4/foTYmVfa/n8Tm8nuu3yrELletnNQWMmy60lxpb7t27Er/flyp9Pqg52C+9hrYwJ1yDp9k0Yrd9fXp/2/wQCYPHCS8E6XfbTcyxo4F6vL9tvo+Lg8/z+Byzc/3Hbvv6ZD2eVry/bb6PP/NNDEGKDH74rJb5QCQM72+bH6x/ulIuqmFO4AbYBowhXrpQQH1mCjq9l+rlrS0sUkCbjXclV92+Mv59wrDbbqqt1nugcd8K9NQ9H0jnaf2r1XxOKCEXnYMwX7Gx6hcG63FcI4V6kdhZbj/eOqG+v21EJO+iU+uknxZtKeRC9BbeLmJrQIivC/MBblXznECbTaVSS431OG43bj9ajwK6xu3mcrfOr0W72e1S60Wti3aLPLeI5sR8sIwW5oRFKvMaJxpXp+epcQsyBOne3zY/vD/5BreruXJyiRCkf3/bdJzz5yt5JxKHMR+MmA+uU2MeOmaRMK/0ZSPMn5AWsxq3ID/6i19N226QR0uT7rnSOf/+thnuHtbfrMfjWbovyHocWAbzwa+YDy4zFZn7GvPQIbTZVKKdqlQY2HKsS6stNecKQbrddhN58s2vdtvXp2n7vLMeC5bBfPA15oOvpSfUtLajQ5ivQD398eeZthzpo69Ai5Pu9bh/ZC6FPutxYBmthrDrMR/sm3UndNZj0UaYL9w0+XXW49CW+uitx4HrUFG9TjrvW6/KEfrq0nIIuwXzwYfWipr7CPMFazXIJwT6MrXTEpbH7P6R5hbwWfW2sx4LltF6CLtVg/NBt/8vmBMI88Xi5B0R6MtCa8SSmlrAqd5WiHVsSW3MB/PzhR3eD4T5Au22r0+cvB/GG4Lqn8RKR2tEDm3cEM5uTn0I8jm0MR+IMCfsI8wXhhP4mDaqEqVi4c6n9hvCmfPqw3yQT+3zgQhzwiGE+YJwAp/Cz8YjFu78Km436/he14X5IL+K54MJc8I+wnxBYgy99Ri8q3sCKw8Lt54a2804d+rCfKCn/kCPOcJ8QZgET6sx0JSKezss0G4Gnwjy+lgP27GyHgCwvDCISCMvcfapxJawGI/enNuVFULCsNu+dqv7xxfrkQAiBHlbzActIMyjSu9vmx93D+tv1uNokecgH6MMIjKIiNw9rJ/P/GOf/rvd9vUpxtCJjDebLTa4RbGAw4epMtxZj6NtzAe1I8zjF/MK5anAM3sMlqvqZdpeZPLSF2Pog6N9kXQ+XxDevzSdU+m8ehb5+T1w9R2Y7rHhghZmPF/YJ/MLfJHz54n5Rb2I5wv7EfNB3Qjz+DmZXRN2Zn/GXahh8tLnZTv9lnP6GtNxfn4HPCzs6QY4dqhgx1+QT3NDCHG4pdizd1Ev8nkNdBfumQ/qRphvWIzSLx12UqhJVQvLCY3qvC4P2+kxyhBC7O8e7H7n6TvgIdTzHbjNftV2LoR48N9j5O1JKjnWu0PmBa5pHew9FDhEmA+Wcuz+Kss5gTDfII1JLVUtdtvXwXIyozqvyboKFzvLEL/PT6jnhvBzae/o1MrDhb3Ix8W9VXidjvtNxM+OHfPBZZbaycmNR1M2JEYZVvfroLlQre4fX8ZtvdhpHXOOR3PpsKzCxSj96n4dvE60dw/r59X9OkyLgglvVVJPxt9L7Ka58RtBfgm2F/bpd3r3sP7mZV6YzQO99ViYD74WowxpXUlzgpfz6BjCfDPGic3q6OMXIXYWgWZ+kxKWZ/s8+diVEr64qPVlvlh7X6hLYh8UfYX4fR4u7pkPDhsvtMbzp5R1JSHMV+6j6mQ/saUqvfYk5mNrs14WbyZOu0wezutLrO4fX6wWct4gPZqF+KIW6xJYXtiXNidYXtyPrNsi/Zjt5LivwB9DmK9YjDJ4rFBYBPrZIzSxoKkPtNM8ZjqvNY+5NKOL2qarcfMF23ostbK7YLTdeb6W5cW9COuix3asaxHmK+U98BiEmU7xWM3Q3/Uoc9E+xCLQt1qNm276L37B9syiKu9p5/kWVlX6lneta5sTCPMV8h7kE80xenk0WE30qzrlL9r72KXKi2q8Hu2qvNed52ul+8q0j9vSfPChvjmBMF+ZUoL8B73Jq81JKx/Nqs7Y51zHor1PO9C3Uo2rLex5pl2VL2+dO49FoG9lPhAp776KSxDmK1PaBLe6f3xRDDKd0nGqp3lhNC3cVVVR9mkH+tp752sNe15pVuVr/91aBPoWCl21nzeE+apY3hl/Pa0vGK02y9Gq5tQ+Ac9pBvqan2zT0jnjgWZVvpXfrXagr70638J5Q5ivROltCFov0qi9IqlB82dY+wS8L4TY6xynzifbtLBoe6N5YdjS71Y70NdanW9lTiDMV6CGNgSt8fMCqdvpLd5l7jTdYmo76zWOVdt3oZVF2xu9Hc9m54NB6XCd0nHUtDQnEOYroFXNy00pxHQKx6iaxuI93qhU7k7TLe4e1s8aC3hNW+stLdqeaFVzS995voVmG2ptu3UtzQmE+cLVFHo0qvP0zd9Ga/FuaRI+ROsCvZbFu5aCRoG63AeoYef5djq7EnXt1rW1k0OYL1xti5hWiwGu1uU+AOeAXrtNHYt3fe8fKIVGcaS2Ne4aWu02tezWtbiTQ5gvWE1V+SSEOOQ+Rq03+mjQWLypwo2Udqr63MfIqcY5sBQa8yi/3w9au5Wl79a1upNDmC9YjRULJm6/lBbvPvcxSqLx8yh58a5xDixIl/sA/H4/Y7futFbPGcJ8wWoNvtqvt4cfLVZUvqLx8yh18W5xK92T3Lt0VOV/pTQ/dgrHyKLlc4YwX6jKK5hD5s/vMn9+lXK3ZHARd5jCd73L/PlZcOFnR2M3p9UK6ym554OSHxLR8jlDmC+URm+5ldx/t5Inq8oN1gPwiO/DryovZriXezen5QrrKRoXsSW23rV+zhDmC1XzSVvz361UGv3yVFoP0/g+lLZ4c66Y6zJ//pD584uWexezxNa7lqvyIoT5IlGVQm1osfla7u98SYs354q93Ls5XKydNGT+/C7z5y+q9aq8CGEewHm6zJ8/ZP58VKL1ClztuFg7LffFToGtd4P1AKwR5gvUQtWC3QdfCpzcq6Lwne8yf/5iWq/AWVNouRsyf34VuOj50EImOoUwD8Ack7GtUi7WuMivH3PB2YacH17KfTRc1IwI84XhxIW2Uib12vHdB5DkfspVQffRDNYD8IAwX57BegBoS0GTeu0G6wFYo2rrQpfrg7lgPR/tZqOaH9N9CcI8AFO0TvjADgzOkbkla8j42dXh4oeLmoQwDwAFaH1bneAC/GKwHgB8IMwDQAGoQBFcakfLhB8h+N8x5QL/A2EeAACY44IVFxqsB+AFYR7Al0qo0AAA0CrCPAAAQGF4uhMSwjwAAABQqJX1AAA0r3t/21iPAQCAIhHmAZianlvdGQ8DAIAi0WYDAAAAFIowDwAAABSKNhsApmKUnqcyAMBldtvXJ+sxwAcq8wC+FCPPmQcAb2IMnfUY4ANhHgAAACgUYR4AAJh7f9t8tx4DRjHKYD0GnI8wDwAAUJ4u42cPGT8bCyPMA7DWWQ8AAEozvaMDIMwD+FoIccj7+SxIQCly3hAfAjfbA9cgzAP40ur+8cV6DACAD7nvL8hdxMGyCPMAzPG8ZAAi3AR7gS7nh1PEKQthHoA5npcMlEHhBW9d5s+vAu2JmCPMAziJF0cB0EBIPS337gWPpSwPYR6AOW58A8qRO+zRanNSl/nzh8yfj4UR5gGcpHEzFH3zQDGGzJ/fZf78orF7gX2EeQAnadwMRd88AJExrHJxf5jGroXCfRFYGGEewFlyb63TagOUQSPsxRj63McoUe55kn75MhHmAZxryH0AqnFAGRQu7qnO71G6l2BQOAYWRpgHcBaqcQBmhtwHYD74TGP3kpdFlYkwD+BsVOMAiOhc3DMffHh/2/zQOA4viyoTYR7AJYbcB6AaB5RBp786KBzDt9329UnjCTa8T6RchHkAZ6MaB2Bm0DiIVlXaL50LGlpsykWYB3ARqnEARPQeYdjyBb7WhUyMMtBiUy7CPIBLDRoHoRoH+Kf3KMMwtBbo39823xVfEDUoHQcZEOYBXIRqHIAkhNhrHaul+2mmPvle63i8KKpshHkAF9O7Uaq9ahxQktX944tWdT4E6VrYsRvnPL1WQ258LR9hHsDFdKs4BHrAM83qfO2BXjvIi1CVrwFhHsBVNKs5LW2vA6XRrM6L1BvoLYI8Vfk6rKwHAKBMdw/r591202scKy3eIcS+1icuKL2qXUSoxGF5Y3VeL4jWNidM3/9e+7jMBXUgzAO4WozSa92kNT7VIQy77WtXw+I9N4YSnadWTBVUFnAsanX/+PL+thkUn75SzZyg+f2foypfD9psAFzNpqpTTw/9bvv6pL2Qa/Y3oy1251YYSmy72W1fn3bbTbQI8iJU5WtCmAdwo9jpH7PMxXsu9cdqLuS8GAY5affOz42Pst3EUi70x/nL8uV4FvM2ciHMA7iJ1QKeemZLWbznrBbyu4f1N+1joi3259h4oe91Xnh/23y3rMaLcFFfI8I8gJtZba+nnlnNm0dvYbutTiUOWmzPtdm84CbUz0J8bz0W+wsuLI0wD+BmU3W+tzp+CNLvtpvoNdSn3nirbXUqcdBk2W4zNw/1FnPD9L13E+JHXNTXiDAPYBF3D+tn6wU8BOmtFu5D5iHecludShy0eTrnQpBudsGfdX6YB/jpe9/nOtaluKivF4+mBLCYu4f1t3ERszOF5m633fQxSm/xxIb3t833tIiHoH30fVTiYCV2tjd5/mo+P4h8fjzjpXPFbvv6FGPopn/s0gW7/Xf+VzHK4OkCC8sizANYmJ8FfKrG9dOOwZAr2M8W9c6yAr9vvJihEgcb07Pn1d5FcY352K55CZ7H4H4Ij6StG2EewKJW948vu+1r5yXQixysxg0iY0vQNQF/tk3vtho3VeJ4jjRM3T2sn9/fNq4uctsTi36pFk4jzANYnMXbIC+Rwr3IddU479hShyd3D+tvVm85BUG+BdwACyCLu4f1N+sbYltEkIdHzAf6uOG1HYR5ANmwgOsiyMMz5gM9zAVtIcwDyIoFXAeLN0rAfJAfc0F7CPMAsmMBz4vFGyVhPsiHuaBNhHkAKljA82DxRomYD3KIHXNBmwjzANSMCw0vMVoKQR4lI9AviafWtIwwD0DVuOAQ6G9HFQ7l4wL/NuPFEEG+dYR5AOpW948vq/t1oCp3ORZv1IYL/OuknTnmAhDmAZiZttl763GUIkbpWbxRIy7wL8XOHD4Q5gGYuntYP4vEjkX8uFSNH39WQL1ou/na+CKodeCCHnOEeQDmVvePL1TpD6Maj9bMqvS99Vh8oRqPwwjzANy4e1g/s9U+ShU4qvFoFbt2oxilpxqPrxDmAbiTttpbXMRnLTVU4NC8tGvX4nyQQjwX9DiFMA/ApdYW8XmIpwIHfNbSfECIx6UI8wBcS4t4rT2049+JEA+co+ZQT4jHtQjzAIox66nvS17IY5RhvnAT4oHL7F/klzoffLw3ghCP662sBwAAl5oWvWcRkfe3zXcR6UKQznRQJ0xhY2DBBpZV2nyQdhiZC7CUYD0AAFjKbvv6FGPoxMFinsJ7CHGg8g7om4K9hGDbnsdcgNwI8wCqNQv3IhkD/ryXn2ob4JPGfMBcAAuEeQDNSpW7S1BdA+qzF/TPRmAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgCrEP/715/jH//yX9TjOUdJYAQC3+ZP1AADAu/jHv/4sEv4pIn/xHpJLGisA4HaEeQD4wiwcJ25DckljBQAsgzAPAEccCMeJu5Bc0lgBAMshzAPAAV+E48RNSC5prACAZRHmAWDPGeE4MQ/JJY0VALA8wjwAzFwQjhOzkFzSWAEAeRDmAWByRThO1ENySWMFAORDmAcAuSkcJ2ohuaSxAgDyCtYDqNX72+b7Of9dCHFY3T++5B4PgOMWCMdzv4ff/vqPhT7rF4uONYb/Duv/8/8W+SwAgAnC/IX2Q3oI0uc4TowyiMiQ/vnuYf2c4zgARlOl+i8LfVyWQF/SRQcAQAdh/ojd9vUpxtCJ5Avs14pxHA8BH1iW90C/4PgI8gBQCcL8ZBbeuxCksx3NZVIVn5Yd4HaLBvoF21gI8rjFua2f12DtAWw1Heanya248H5KjNIzuQLXW7ZCH/8efvvbv52MhyDfqN12E3N9dozSs1MM2GkuzNca4I8h2APX8RLoCfJYAmEeqFcTj6bcbV+f3t82P3bbTQxB+laCvEjq9w/DbruJObdZgdpMwff3hT7tn+PNq5chyAMATqk6zL+/bb6P1YgwtBTgjwlB+inU/9htX5+sxwN4Zxnol9wZIMgDQL2qC/NTFf57qsJbj8ej8cKGaj1wDotAv3SLzzKfAwDwqJown1pppip8bz2eUsyq9YR64AjNQO+lVx8AUIbiw/xeiO+sx1MqQj3wNY1AT5AHAFyq6DA/Bk9C/JJSqKenHvjVsr3nnwM9QR4AcI0iwzw98RrCwI2ywCFL9qCPgZ4gDwC4VlFhPrXUEOJ1pBtlxzYmACIiY1BeNtALQR4AcKViwjx98XZCkI7WG+DD8oF+CQR5AGiR+zC/274+TS01nfVYQJUeSHwFeoI8ALTKdZhP1XjrceADVXrgg49AT5AHgJa5DPOz3vjOeiw4Jgw8xhKwDvQEeQBonbswP1Z86Y0vQQjS03YDWAV6gjwAwFmYT8+Ntx4HzkfbDTDSDfQEeQDAyE2Y55GTpQsDgR6t0wn0BHkAwAcXYZ7++FoQ6IG8gZ4gDwD4zDTM89jJGvH4SiBToP+dIA8A2GcW5tONrlbHRz4hSEegBwAAyC9YHLT2IB/jxb3/XY27EzHKcPew/mY9DkBb/ONffxYJ/8zw0b+H3/76jwyfi8rttpuY67NjlP7uYf2c6/MBfE09zNcS5OeBfelJbLd9fYoxdNM/Fh30CfRoTcYgnxDocTHCPFAv1TBfapCPUQYRGUKIw+r+8cViDLOAX1y4J9CjFQpBPiHQ4yKEeaBeamG+tCCfArzXCSqF+1Ie50mgR+0Ug3xCoMfZCPNAvdRugI0x9FrHulaMMsQo/ep+He4e1t88T06r+8eXu4f18+p+HWKUfrr4cGu6Kfa79TiAHAyCvIjIX+If//NfyscEADijUpn3/hz5GKW3bKFZ0vvb5rvvan3savg5A4lRkJ+jQo+TqMwD9cpemfcc5GdV+OdaAua8Wm89lsN4sRTq4SDIi1ChB4CmZQ3zU5W4y3mMa8xDvPVYcvEd6su5dwI4xkmQTwj0ANCobGF+t3198tbuEaMMtYf4fbNQP1iPZY6XSqF8boJ88pe4+d//tB4EAEBXxsq8n+rrGGRj1/LTVMa/e+y8hHpuiEXJFqyC/x5+++t/iMjvi3xaiP933DEAALQiS5j3VHWdbsz5VktP/C2mJ+B889J6E4L09M+jNFOQ/8sCH/XzxtXp/y4T6CX8k0APAO1YPMz76pOPXUstNecafyZeqvR+dnCAU3IE+YRADwC4xqJh3kuffOqNpxp/3KxKP1iPxdNODnDMgkFejj1KkkAPALjUomHew4uhUluN9ThK4aHtJgTpaLeBZ0sGeZH496/+VwI9AOASi4V5H+01tNVcI7XdWI7Bw4UgcMjSQT789rd/n/qvCPQAgHMtFubt22t4s+gtxp+dXaDn6TbwyCLIJwR6AMA5Fgnz9j3PBPklOAj0vdWxgX2WQT4h0AMATrk5zE83vXYLjOVKBPklWQd6+wtDwEeQTwj0AICv3BzmbXudCfI5WAZ6boaFNU9BPiHQAwCOuSnM21blCfI5WQZ6boaFFY9BPiHQAwAOubEyb/XCH4K8htX944vFc+ipzsOC5yCfEOgBAPuuDvNWTx4ZXwhFkNdi9WIpqvPQFDf/+5/iPMjPPv+/l/uoPy30dwYAWAnX/sHddhOXHMg5YpSBF0LpG6vkFrsw7MBAzzKV+dxBfjrKH//6s0j4540f8/uxN9HitN329SnG0O3/e6/vOsm5Zk8va3T59/bu2HkUQqRwibNdFeanF0T1C4/lpNX9+uqLD9zG4nfOAgFttwV6nSD/82i3BXqC/Bf2dp67Je4Nm79p22JeI8zry3EeiXw+lwj9ELk+zP/Qv/GVKq01i987F3DQdl2g1w3yP496XaAnyM/MKqOLha1zpVCmEYQJ83lZnkeJ5vmEPA60sP9yPh3KRRcHJYuWC9prfDD63WdZJI5tbS6llck018/Rutp0WaC3CfI/j35ZoCfIy8d56+lFddO9SUOuuYMwv7wpeJmF91NilN56LtWQ4x5OjfM5jfvSeWiRME91tm3a7Ta5LuRy/z1aOWdz/Rw9hIPzAr1tkP85ivMCfdNB3mOAPyZHsCfML8N7gD+m5mCf49zOsYYvNQcdGtvFT7Ox2oaED9oTdmkTJupxxmMgf/cQ5EVExnHEv3/xnzQb5N/fNt/HN0uHoYQgLzLOeyFIv9tu4hJVR6unz9Vit319en/b/NhtNzEE6Utcl8ZzPwxLnVM43/vb5vt4wZFvDlpdOqAcgzhmqso2cbVfklaqzkD47a//OFKhdxeOw29/+3f8419/P1ChdzdWDVYPaljaFOr7lqrfXkyV1F7GSnw1OKfyS+eO1oXfRZV57YkxhKh6PADYd6BC7zYcH6jQux1rLqkKVkOQn1uyUo+v7bavT7NKamc9nlxm59QPXtS4jLSLo33unB3mtX/RvBwKgBezQO8+HM8CvfuxLikFsNpC/L4QpCd85TEPYtZj0TSGzjBwoXg9qxCfnN1mMzXtq6EqD8CTkoLx1MtfzHhvMW+FMB6Kmln44klvCxmDWF3tNJei/eY600VQb3nunF2Z136CCVV5AMBXxkW07laIr4Qg3W67iVTprzdry+qsx+IFuz/n8bQbeFaY1/6FUpUHAByTtrQ9LKI+0CJxDc6h42i9+Zq3dqyzwnzOl+v8eiyq8gCAw9LL66ikfpaqqdbjKMGsotpZj8U7qvSfzQoJnfVY5s4K88pXroPisQAAhfBWDfMmBOkIXl/jHLpcqtK3fl55LiRc/NKo3LjpAgCwz2M1zCOC13GcQ7cKQ6u7P+n+HOtxHHMyzGv2S/G2VwDAPkLYNQj0idfWiBKl3R/rcWgq4d4KV5X5EOJgPQYAgA+EsFsR6D23RpSqpUBfyvxzMsxrXo1w4ysAQIQQthy/rQG5pXPIehw1aiHQlxLkRRxV5mmxAQAk04ugsADvLQI5EOTzqznQlxTkRU6Eec1+eVpsAAAi5S2k8IUgr6fWQF/a/OOmMk+LDQCAII9bsaujq9ZAX5LVif+90xhEjDxbHgBaR5DHrUo8h6YMNMz/XWmtUSnQ3z2sv1mPpUVfhnnFL8SgdBwAgEPTU1c663GgXJ6D/Dywn/k+nU//zW77+hRj6ET8Bv0p0H/nfUH6TlXmVdAvDwDtKqXHeb+Cek5o2bv3rPMaNkvn8WIwRulDiMMSbcTTZ6TPeRb5eW65OqdCkH63fV3k74zzHQ3zms+m5ZcOAO2KMfQhWI/iV+kpa7dUGvf+7M//P4V8r1XWkni6GIxRhhBir5FrpnPLYbAPg4g4/EbX62iYjzF0HidXAEA9vLVGaIWxWch/Ti0UBPvreLgYjFF6y/aSFOync6m3/k7RP3+bQ/dRfMW8zYabXwGgTZ5aIyzD2KyF4vn9bfOdUH++6efVWR0/Rhk8hdbpXPpmHepDkG63fX2i8+I8t+4Ceng05WA9AACAPg+PEIxR+tX9Oni5ae/uYf28ul8HXqR42m77+mR74RM7T0F+bnX/+DKOLXZ2o/DR+uTV+B2PXZp/bpmDvgrz3bUfCgDAVzxUVD2F+H2E+tOsLgbTuVNC1Xl1//gynUeDxfF5/vxnMcowLyAsdQ4dDfPW/VYAgHpZVVTHUOO3orpvvNiIHS2pn01V+U7/yOWcO3N3D+tvFheGqd1G+7g+jedOjgKCeZuN16oIACAPq2pd6m8uoaI6l1omqNLPWbRwxK60c2cuXRhqH9dDO52lVInPee6Yh3kAQDuoqF7PKox5s/fs/uxKaqs5Zfw76J5DrVbnZ7uA2YvWhHkAgBqbKl3ZFdU5izDmjWaLlren1SzB4hxqrTo/PR1LbReQMA8AUGFTla8nyCfWNzVa0q7K1xbkE+1A31Z1XqcaP0eYBwCoiDF0ykesLsjPTX30g/U4NOneOF33Doh2oG+jOm8z55i/NAoAarPbbmKuz7Z+0+QttINYzUE+CSH2rTzPW7cq38b5s7p/fHl/2/Qa3836XyRld85QmQcAZKcZxManR9QaGD5rrIe+0zhIS+ePyHhjtdYOj/7unBbbiz8q805Mr17urMfhWQhxaGmCBWqiVZWfblgscufiWqv7x5fd9rWruUI/9Vt3uY/T4vkjMrZs5dxRTKZ5oLKfr/0uDmHeiRhDZ/taav+mfjvCPFAYzRvfar1h8ZSpXWKo9YWP0xqZXavnzyh2GheE72+b7/VcMNkHeRHabAAAmentOjbTbnJQzUFUo9jV+ku5VvePL0rtNp3CMbIb3z9gH+RFqMy7EUIcKrnTu6u1MgTgOkpBbLh78LGw2tKprmrS2tmpp1p8PY0bqmvICN7eP0CYd2K6uit+IXp/24hUctUN4HaKQczNwmqpxnYbjRab1qvyidbTbUpvtRkvevygzQYAkI1Giw1B7DNvQeNWGjs7JQfLpSn9LDqFY2Thqb0mIcwDAHLqch+AIPaZYu9zFbgY/FXun0nJO0cedwEJ8wCAbHIv2gSxw2qpzmu8nyCEOOQ+Rmk0fiaaT7laitf5hjAPAMhCY7GmKn+YtzYAz/hZ/Upjd6fEd+t4nW/Mw3yJV2YAgNNyL9a0knzNaxXxErn75Wv4GWU0WA/AE8/nytEwz6t9cQ1efAVgpsv8+UPmzy+a1yqiJ7TYHJf7Z1NaXvD8ffqqMj9oDQIAUJ/c/fKeF1cvSt690Ni5p8XmOH42H7x/j8zbbAAAuJT3xdWRwXoA12Ln3p7n1hJlg/UAvmIe5kvbZsFx3P8AIFGYD4bMn18Fdi+OI6ja03ha0RK8f4/MwzzqQRUFQJJ7PqDXuQmd9QBax/esDEfDvOZVCBVdAMAl6Oc9X6ktSSW/WKgWfM/K2MFxUZmnoluNznoAAIBfDNYD8Mh76wRwLhdhXgiBVaCKAkBDCZUyAGfprAdwSgmtRl+Gea0JkxBYPlqlAMzxcAM/Sggj8CtnFiwh/5XQauSlMk8YLBytUgDgUwlhZB+ZADjfl2Fe82qeMFi8znoAAIA6kAmA830Z5jWv5tmSLVsJW2UA6kDbCAB8ONlmo/lIK7bVylTKSx8A1KHEthH4UurjOoFDVmf8N4MotVBM22pM0uXprAcAAMAlKESdrbMeAL52MsyP25lBYSg/W2147mthaLEBAJRkWrc642EAizjZZqO9ncmVcln4fQEAANg569GUmi/o4EbY4nTWAwAAAGjVOT3z6nbb1yducPJvumG5sx4HgLa8v22+3z2sacnETVb3a50eYiCzsyrz2pNmjKHXPB6uw+8JAADA1tlvgNV8jFMI0vGYSv+48RXCzgyADNh5Ac53dpgX0X0mK1Vf37jxFSJc0OE4nuPtB/M1ULezw7z2VTLVed+4URnACUPGz+4yfjYAFOWSyrzqU21GYdA9Hs7x/rb5YT0GAO1iRwhLoGCIWlwU5scXSOlie9CX3fb1iYUUAMrBTuph01vngeJdFOZX948v2n2QIUjP1bMf3MtwnhYuQvle4iu5iz8tfMdap98NAJTpojA/GZYexCkESB+oymOOqha+ovCukC7z51eBix6gfheHeYvHRYUgHROSB9zDAMAHCgtn66wH4BXtR6jFNZV5k60v2m1scdMr9rEQ4pTcbZkUeU4r+aKHZ80D57kqzNt9wagMW3h/23wveUHA8riwxpmGzJ/fZf78onGxcxo/I9TgqjAvYndjChViXVOffG89jqXlvjmvxp/ZHP3y8IAiw0md9QBuxcvHgNOuDvNW1fmpf55Ar6bO3RCFm/Nq11kPAP5prBNUVg+r6IEFQ84Pr73wgjZcHeZF7KrzBHod/IxvU2srSkUhARUgjB1Wy1PgNN5vwwUhSndTmLe8OSUE6WoNSx68v21+ENhuU2srSq1/L+ShUfQhjH1W0wW30i5qp3AMIJubwvwodrd/xrXCQKBfXitBPncvZq0Vw1r/XshDo7LKOflZLVX5RGGupjiIot0c5i3eCvsZgX5JrQT5yZD7ALWdm1RAcSmt+1NoCxzVVJWfGXIfoLYLILRlgcq8SAixX+JzbhjBwER+u8aCvIraWlKogOIaGq02VFeT+h5aoNHSy/mDki0S5qfqfL/EZ12Lm2Jv02KQV1og+tzH0EJVHtfSaLWZjqR0HJ9qXgM1OgCozqNUi4R5kTEYWT8PlkB/nRaDvKYaQnCt7xuADs1Hwba6BlTaXvOTRgcA1XmUarEwL+Kh3SZ9GTeRL+Rpu+3rU+tBXuMCtIYQTMUKt9LavZ2KOsVfQF9iXO/q3pXQuyDkPjyUZ9Ew76Hd5kMYWpvQL5Em/5aD/GTQOEjJ5+L72+Y75wlupfko4xCkbyuQ1R3kE618QfECpVk0zIv4aLdJQpD+/W3zo61J/bRxG7qNyf8UrYBRarigvQZL0l0b2qiwttRWpDhf07KLoiwe5kV8tNskY0WRKr3IGMx2202kymqjtGpPC1v30KW/NtQd6Ftsk9Rs16r53EFdsoT5sbfN8mVSvxoro+320lONP45e3sNKu/iAfzbvJakz0LcY5EU0n4wkUuu5g/qssn3w/ePL+9um97dFH4b3t80QQuw1n7BgZep37q3H4dm4OASlY0m/274O3s+9VoMC8hur89qFhTDstq+d9+/dOVrfMZuyheL9XvWcO4fstq9PWu9DCSG6X/tKlS3Mi4z9be9vm85bKJi13lQb6j2E+BiliBtsV/ePL7vtRvGIfheHaWLvS/i9oUz6YSwZ5/y7h/U33eMup/Ugn9w9rL/ttpuod8QwvL9tes2buDXMHoShpVM7UmOytNnM3T2sv3m5IXbfLNRXc5Ps+9vm+9QX31uPpaRFU/8pTP62b3nCEbRY3VeVbmz09t07B62Sn2nP2emBGprHzGls+dQ7n2IUqvIZZQ/zIv5DXQr1u+0mjmG4rIl+el68mxA/8nXPxCk2FRc/gV57YkfbLB9jXNpDEXhwwWEWc3bJF4NzUxtlr3lMTw9GqZFKmB+VEe7GE3ys1nsO9vMAP1VTe+sxfYguW0hOsQkX47mmf9zR7MVhvdUY0CbrlgXvD0VI300usr+inytKuxics7owjFGqbGf2RC3Me3zCzVdCkC4F+1Sxt/7ypjH4DPCjkrfSdJ+SMD+uzVuLUzWeih/seFgTfLVazkM8382v2TwdaVTae2ysLgxjlMH6wr0FWW+A/eVg948vu+1rV2KlIQXn3XbTi3yu4uY4UecXDh5D+yHTl9Z1S9VX7G7MS3RuyvZwczQg4uE7N5q1WkqMYnKj4zTndyLSKd6QWDz9m2E/zKr0bm+OtZ7vaa/RoRrmRcoO9HPzL0cK+MmV7RrunvpzidKDfGLz2Lz58T+etCSyXEUjBYWSzzHUyTKMHTK13/RTxTdrVZHv5VJiZzxvp3PGTai3DvEiMl0Yl7lTXxqz638esVWX1f364Lm05CKtNVF6e8Z6ChWXPKN3trNTZFDwtCheI2c4Lf1nc0gJ68Gtu7Hz53lbh6yleTgnPc3bMUpv8Ux1T48W1izw5S4GHMs3nqhX5n8euJIKPUR89L0ux2GlsBORTiSI7vPwAR1+XzL44avdWJHPYf/Y34P2mXzuHtbfvAT66X47SW1bOYN9ukhM55yXc6yGnfqSmIV5kY9A7+VKEtco88k1p4wTsN9gAdTG60sGz8V8Yc9bIUbkl2A/iIw37N66uyOzXVcvAf5DXQW+EpiGeZH0lBtxc0WNS9QZ5EXKDxZAiTxVV1Eq2/75r3zssh7e3RH5tMNzcP3xF9z31ZsLPFN8zvzXPL8pFp+Nv6f6v7DchQ/oY3setyjtMdj7Qhh3hcu8oK0/F3jlJsyLpEm83C9hC9JNLS18YS3fUlmKGGXgZ4TlsQ7geqUH+jIR5C25CvMifAk9q+Xxk5e4e1g/s2N0XGvnA3SwDiyvtQtvziFNBHlr7sK8yPglXN2vAyHKj+nRY00GN1rAjmGhRD6EseW0WIgR4RzSQZD3wGWYT2i78SJ21s8Qtkb//D4mcOSXwhgX09drNcgnBPqcWAe8cB3mRajSW2rlRtdzsCDMcU5Az+r+8YXdseu0HuQTLgpzYB3wxH2YT6jS60ptNXxZPxDoRZjAYYVAf5mWWyMP4aJwGTHKsLpfB9YBX4oJ8yKfqvS99VhqlarxrbfVHNN2oCfIwxZFnXMxhx8zBfreehwl4gLRr6LCfDJOUmyZLY1q/HnaDPQEefhAy8Qpx7+rBPxRyhDW4ygLF4ieFRnmRT62zJjUb0c1/nJt3ctBkIcvs5aJ3nosXtD+cJm25vDrcV6VodgwnxDqbxU7qvHXq7kHk0kc3rFLO6L94Xrkh8NmRT7OqwIUH+YTQv1lYpSeoLaMGvt4CQcoRctVenZVl9HyOXQILbflqSbMJ3uhvrcejzcpxDP5L6uuLVvCAcpz97B+bukBCQSu5bV2Du0jH5SrujCfTKH+5xezjpB1nfQab76k+ZVcpaetBjWoPZAxl+dX+zm0j3OqfNWG+bm7h/Vzi9X68e869rzxJdVT2iNU6Y1EjWoLZAQufTUXBCny1aWJMJ/Mq/W1Bvv9LyhVVjvew8Q8xN9ynrAQwLO9OX+wHs+lCFz2aioIUuSr08p6AFam8PIiIs8iIu9vm+8iIiGU90VNkwtfTJ+m38vzbvv6FGPorM+xGKUPIQ53D1zooR3TnP9N5Od834UgnemgjkjfUYoxvsxzg5f5/BycT/UL1gPwKoV7cTjhp/DOl7Nc2gvBVJEcuODTsdtuYq7Pnm585Pe4kPRdFOO5nsBVLi/nUEKBrz2E+QvMAr6Iwpc2BbD0z3wx67X0xWM6dwgHNvbmikXxO81rFsxEMs3z87mdeb1OWgXBedsP51K7CPMLunUBZ5HGvkvOKc4fIK+9oH82vptIrj2HRDiPAAAAAAAAAAAAAAAAAAAAAABAe/4/arUX23MLwq4AAAAASUVORK5CYII=" alt="Interior Guider">
    <div class="projeto-id"><span id="cliente"></span><span class="ref" id="ref"></span></div>
  </div>
</header>
<div class="faixa"></div>
<div class="wrap">

  <div class="boas-vindas">
    <h1>Bem vindo.<br>Tudo o que vai ver aqui <span class="destaque">partiu de si.</span></h1>
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
    <div class="t">${f.titulo}</div>
    <div class="e">${rotuloTile(f)}</div>
  </a>`).join('');

const conteudo = {
  honorarios: () => `
    <div class="linhas-caixa">
      <div class="linhas">
        ${projeto.honorarios.linhas.map(l=>`
          <div class="l"><span>${l.t}<span class="d">${l.d}</span></span><span class="v">${eur(l.v)}</span></div>`).join('')}
        <div class="l destaque"><span>Honorários de projeto</span><span class="v">${eur(projeto.honorarios.total)}</span></div>
      </div>
    </div>
    <div class="credito-bloco credito-escuro">
      <h3>Crédito na compra Interior Guider</h3>
      <p>Os honorários cobrem o diagnóstico, o desenho e a especificação. Na compra de 100% da especificação com o Interior Guider, aplica-se um crédito de 1€ por cada 10€ do conjunto, que abate diretamente ao orçamento. O valor consta da fase de orçamento.</p>
    </div>`,

  conceito: () => `
    <div class="docs">
      <a class="doc ${projeto.documentos.conceito?'':'off'}" href="#" ${projeto.documentos.conceito?`onclick="return abrirDocumento(projeto.documentos.conceito, 'Conceito psicoestético - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span class="doc-txt">Conceito psicoestético <span class="ext">. PDF</span></span></a>
    </div>
    <div class="imagem">${projeto.conceito.imagem?`<img src="${projeto.conceito.imagem}" alt="Imagem guia">`:''}</div>
    ${projeto.conceito.leitura?projeto.conceito.leitura.split(/\n\s*\n/).map(p=>`<p class="leitura">${p.trim()}</p>`).join(''):''}
    ${projeto.conceito.materiais?`<p class="materiais">${projeto.conceito.materiais}</p>`:''}`,

  projeto: () => `
    <div class="docs">
      <a class="doc ${projeto.documentos.apresentacao?'':'off'}" href="#" ${projeto.documentos.apresentacao?`onclick="return abrirDocumento(projeto.documentos.apresentacao, 'Apresentação do projeto - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span class="doc-txt">Apresentação do projeto <span class="ext">. PDF</span></span></a>
    </div>
    ${projeto.ambientes.map(a=>`
      <div class="amb">
        <div class="img">${(projeto.documentos.apresentacao && a.imagem)?`<img src="${a.imagem}" alt="${a.nome}">`:''}</div>
        <div class="amb-texto">
          <h3>${a.nome}</h3>
          <p>${a.nota}</p>
        </div>
      </div>`).join('')}`,

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
    <div class="docs">
      <a class="doc ${projeto.documentos.orcamento?'':'off'}" href="#" ${projeto.documentos.orcamento?`onclick="return abrirDocumento(projeto.documentos.orcamento, 'Orçamento detalhado - ${projeto.cliente}.pdf')"`:'onclick="return false"'}>
        <span class="doc-txt">Orçamento detalhado <span class="ext">. PDF</span></span></a>
    </div>
    <div class="linhas-caixa">
      <div class="linhas">
        <div class="l"><span>Ambiente completo<span class="d">100% da especificação · inclui entrega, montagem e garantia única</span></span><span class="v">${eur(totalProduto)}</span></div>
        <div class="l credito"><span>Crédito na compra Interior Guider<span class="d">1€ por cada 10€ do conjunto</span></span><span class="v">− ${eur(credito)}</span></div>
        <div class="l destaque"><span>Valor a pagar</span><span class="v">${eur(totalAPagar)}</span></div>
      </div>
    </div>
    <div class="credito-bloco">
      <h3>Crédito na compra Interior Guider: Comprando o projeto completo, <em>${eur(credito)}</em> abatem ao seu orçamento.</h3>
      <p>Na compra de 100% da especificação com o Interior Guider, aplica-se um crédito de 1€ por cada 10€ do conjunto. A compra parcial não dá direito ao crédito e fica a preço de tabela.</p>
    </div>
    <div class="pag-tit">Como se paga</div>
    <div class="painel-resumo">
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
    </div>
    <p class="nota"><b>Condição do crédito:</b> aplica-se apenas à compra de 100% da especificação de fornecimento. Peças pré-existentes do cliente foram integradas na fase de desenho e não entram neste valor. A compra parcial fica a preço de tabela, sem crédito. ${projeto.validade}</p>`
};

$('fases').innerHTML = projeto.fases.map((f,i)=>{
  let estado, bloco;

  if(f.estado === "validada"){
    estado = `<span class="estado ok">validado a ${f.data}</span>`;
    bloco  = conteudo[f.id]() + `
      <div class="validar">
        <a class="btn" href="${projeto.acoes[f.id]}">${f.acao}</a>
        <p class="conv">${f.obs}</p>
      </div>`;
  } else if(f.estado === "aguarda"){
    estado = `<span class="estado">a aguardar a sua validação</span>`;
    if(f.id === 'orcamento' && temProduto){
      bloco = conteudo[f.id]() + `
        <div class="validar-caixa">
          <div class="validar-texto">
            <h3>Concretizar o ambiente completo</h3>
            <p>Ao adjudicar, recebe de imediato a confirmação e os dados de pagamento da primeira fase. O seu designer acompanha o resto.</p>
          </div>
          <button type="button" class="btn-adjudicar" onclick="validarFase('${f.id}', this)">Adjudicar — ${eur(p50)} (50%)</button>
        </div>
        <p class="validar-msg" id="msg-${f.id}"></p>`;
    } else {
      bloco  = conteudo[f.id]() + `
        <div class="validar">
          <button type="button" class="btn" onclick="validarFase('${f.id}', this)">${f.acao}</button>
          <p class="conv">${f.obs}</p>
          <p class="validar-msg" id="msg-${f.id}"></p>
        </div>`;
    }
  } else {
    const anterior = projeto.fases[i-1];
    estado = `<span class="estado">por abrir</span>`;
    bloco  = `<div class="demo">${conteudo[f.id]()}</div>
      <p class="espera">Esta secção abre para validação depois${anterior ? ` — ${anterior.titulo.toLowerCase()}` : ''}.</p>`;
  }

  return `<section class="fase ${f.estado==='prevista'?'prevista':''}" id="${f.id}">
    <div class="fase-topo"><h2>${f.titulo}</h2>${estado}</div>
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
