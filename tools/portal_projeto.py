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
    ref = f"IG-{card_id}"
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
    --paper:#FBFAF8; --ink:#1C1A17; --stone:#8E877C; --line:#E5E0D7;
    --clay:#B96D4E; --err:#B94E4E;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{background:var(--paper);color:var(--ink);font-family:'Jost',system-ui,sans-serif;
       font-size:16px;line-height:1.75;font-weight:300;-webkit-font-smoothing:antialiased}
  .wrap{max-width:720px;margin:0 auto;padding:0 28px}
  a{color:inherit}

  header{padding:44px 0 0;display:flex;justify-content:space-between;align-items:center;gap:16px}
  header img{height:22px;width:auto;opacity:.9}
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
    <img src="data:image/png;base64,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" alt="Interior Guider">
    <span class="ref" id="ref"></span>
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
  :root{--paper:#FBFAF8; --ink:#1C1A17; --stone:#8E877C; --line:#E5E0D7; --clay:#B96D4E; --ok:#5A7D5A; --err:#B94E4E;}
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
  .passo.validada{background:var(--ok)}
  .passo.aguarda{background:var(--clay)}
  .fases{display:flex;flex-wrap:wrap;gap:6px}
  .fase-chip{font-size:10.5px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);color:var(--stone);
            white-space:nowrap}
  .fase-chip.validada{background:#EDF2EA;border-color:#CFDECB;color:var(--ok)}
  .fase-chip.aguarda{background:#FBEFE8;border-color:#E9D3C3;color:var(--clay)}
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
