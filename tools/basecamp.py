# tools/basecamp.py — API do Basecamp (OAuth2 com refresh automático).
#
# Ao contrário do BigCommerce, o Basecamp não usa um token fixo: o access_token
# expira ao fim de ~2 semanas. Guardamos aqui só o refresh_token (não expira) e
# trocamo-lo por um access_token novo sempre que necessário, em memória.
import os, re, time, unicodedata
from datetime import date, datetime, timedelta, timezone
import httpx
from bs4 import BeautifulSoup

def _normalizar(texto: str) -> str:
    """Baixa para minúsculas e remove acentos — para comparar nomes de forma
    tolerante a diferenças de acentuação entre como alguém escreve o seu
    nome na consola e como está registado no Basecamp (ex: "Eugénia" vs
    "Eugenia" têm de contar como a mesma pessoa)."""
    sem_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acentos.lower().strip()

_cache = {}  # {chave: (timestamp, valor)}
TOKEN_URL = "https://launchpad.37signals.com/authorization/token"
TIPOS_MONITORIZADOS = ("Todo", "Kanban::Card")
TTL_ITENS_ATIVOS = 900  # segundos — 15 min chega para pedidos em cadeia (ex: resumo de projeto)

# colunas do Kanban (estado de um card) ou nomes de todolist (para um Todo)
# que não representam trabalho em aberto real — ou porque já fecharam o
# fluxo (Perdido, Vendido, Done, Concluído, Arquivo, Cancelado, Not Now), ou
# porque nunca foram trabalho a fazer, só lembretes automáticos do funil de
# vendas (a lista "Avisos") — um item parado aqui é esperado, não é sinal de
# nada, e não deve contar como tarefa/card em aberto nem atrasado.
COLUNAS_TERMINAIS = {"perdido", "perdidos", "vendido", "vendidos", "done",
                     "concluído", "concluido", "arquivo", "arquivado", "cancelado",
                     "not now", "avisos"}

def _base_url():
    return f"https://3.basecampapi.com/{os.environ['BASECAMP_ACCOUNT_ID']}"

def _access_token():
    if "access_token" in _cache:
        token, expira_em = _cache["access_token"]
        if time.time() < expira_em - 60:
            return token
    r = httpx.post(TOKEN_URL, data={
        "type": "refresh",
        "refresh_token": os.environ["BASECAMP_REFRESH_TOKEN"],
        "client_id": os.environ["BASECAMP_CLIENT_ID"],
        "client_secret": os.environ["BASECAMP_CLIENT_SECRET"],
    }, timeout=30)
    r.raise_for_status()
    dados = r.json()
    token = dados["access_token"]
    _cache["access_token"] = (token, time.time() + dados.get("expires_in", 1209600))
    return token

def _headers():
    return {
        "Authorization": f"Bearer {_access_token()}",
        "User-Agent": "Alma (Interior Guider) - alma@interiorguider.com",
        "Content-Type": "application/json",
    }

def _get_paginado(url: str, params: dict = None, etiqueta: str = "") -> list:
    """O Basecamp pagina via header Link: <url>; rel="next".

    Contas com muito histórico podem ter milhares de itens em aberto (o
    Basecamp não permite filtrar por prazo no servidor) — isto pode demorar
    minutos, por isso tem retry ligeiro e imprime progresso para os logs não
    parecerem "presos" durante uma corrida agendada."""
    itens = []
    pagina = 0
    while url:
        for tentativa in range(3):
            try:
                r = httpx.get(url, headers=_headers(), params=params, timeout=30)
                r.raise_for_status()
                break
            except httpx.HTTPError as e:
                if tentativa == 2:
                    raise
                print(f"[basecamp] pedido falhou ({e!r}), tentativa {tentativa + 1}/3")
                time.sleep(2 * (tentativa + 1))
        itens.extend(r.json())
        pagina += 1
        if etiqueta and pagina % 20 == 0:
            print(f"[basecamp] {etiqueta}: página {pagina}, {len(itens)} itens acumulados")
        url = r.links.get("next", {}).get("url")
        params = None  # já incluído no url de "next"
    return itens

def _texto_simples(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

def _itens_ativos(forcar: bool = False) -> list[dict]:
    """Todas as tarefas (to-dos) e cards ativos e não concluídos, de todos os
    projetos — cacheado, porque várias funções (atrasos, cards parados, estado
    de projeto) partem dos mesmos dados e a conta tem milhares de itens em
    aberto (percorrê-los pode demorar minutos). `forcar=True` ignora a cache e
    vai sempre buscar os dados atuais — usado pela sugestão semanal de
    logística (bug real reportado, 2026-07-28: a equipa corrigia uma morada
    nas notas de um card e a sugestão continuava a mostrar a morada antiga,
    porque calhava de correr dentro da janela de 15 min da cache; esta
    operação é rara e importante o suficiente para nunca valer a pena
    arriscar dados desatualizados)."""
    if not forcar and "itens_ativos" in _cache:
        ts, itens = _cache["itens_ativos"]
        if time.time() - ts < TTL_ITENS_ATIVOS:
            return itens
    itens = []
    for tipo in TIPOS_MONITORIZADOS:
        # completed=false evita percorrer todo o histórico de tarefas já
        # concluídas — só traz o que ainda está em aberto.
        encontrados = _get_paginado(f"{_base_url()}/projects/recordings.json",
                                    params={"type": tipo, "status": "active", "completed": "false"},
                                    etiqueta=tipo)
        print(f"[basecamp] {tipo}: {len(encontrados)} em aberto")
        itens.extend(encontrados)
    _cache["itens_ativos"] = (time.time(), itens)
    return itens

def _formatar_item(item: dict) -> dict:
    if item.get("type") == "Todo":
        tipo = "tarefa"
    elif item.get("type") == "Kanban::Board":
        tipo = "card table"
    else:
        tipo = "card"
    return {
        "id": item["id"],
        "tipo": tipo,
        "titulo": item.get("title") or item.get("content") or "(sem título)",
        "notas": _texto_simples(item.get("description", "")),
        # coluna do Kanban (estado do card) ou lista de tarefas (para Todos)
        # — dá contexto de onde o item está no fluxo de trabalho
        "estado": (item.get("parent") or {}).get("title"),
        "responsaveis": [p["name"] for p in item.get("assignees", [])],
        "projeto": (item.get("bucket") or {}).get("name"),
        "prazo": item.get("due_on"),
        "url": item.get("app_url"),
        # url da própria API deste registo (distinto do "url" acima, que é
        # o link para abrir no browser) — pedido do Rui (2026-07-24): sem
        # isto, ler_anexos_registo_basecamp não tinha forma de saber que
        # url pedir para ler os PDFs (fatura/orçamento) anexados a um card
        # encontrado por procurar_cards_basecamp.
        "url_api": item.get("url"),
        "comments_count": item.get("comments_count", 0),
        "comments_url": item.get("comments_url"),
    }

def _em_coluna_terminal(item: dict) -> bool:
    """O item está numa coluna/lista de estado terminal (Perdido, Vendido,
    Done, Concluído, Arquivo, Cancelado). A API do Basecamp só marca
    'completed' quando alguém fecha a checkbox de uma tarefa — um card do
    Kanban fica "fechado" ao mudar de coluna, não por isso; por isso não
    basta olhar para completed=false para saber se algo ainda está mesmo em
    aberto. Sempre que a Alma reporta o que está ativo/atrasado, tem de
    ignorar o que já está aqui — não é trabalho esquecido, é trabalho já
    fechado (ganho, perdido, ou concluído de outra forma)."""
    estado = ((item.get("parent") or {}).get("title") or "").strip().lower()
    return estado in COLUNAS_TERMINAIS

def tarefas_e_cards_atrasados() -> list[dict]:
    """Tarefas (to-dos) e cards, de todos os projetos, com prazo ultrapassado
    e não concluídos — ignora o que já está numa coluna/lista de estado
    terminal (Perdido, Vendido, Done, ...), que não é atraso, é trabalho já
    fechado."""
    hoje = date.today()
    atrasados = []
    for item in _itens_ativos():
        prazo = item.get("due_on")
        if not prazo or item.get("completed") or _em_coluna_terminal(item):
            continue
        if date.fromisoformat(prazo) >= hoje:
            continue
        formatado = _formatar_item(item)
        formatado["dias_atraso"] = (hoje - date.fromisoformat(prazo)).days
        atrasados.append(formatado)
    return atrasados

def cards_parados_sem_prazo(dias_sem_atividade: int = 14) -> list[dict]:
    """Cards do Kanban sem prazo definido e sem atividade há mais de X dias —
    não aparecem em tarefas_e_cards_atrasados (não têm due_on), mas podem
    estar igualmente esquecidos. Ignora colunas de estado terminal/fechado
    (ex: Perdido, Vendido, Done) onde um card parado é esperado, não um
    sinal de negligência."""
    agora = datetime.now(timezone.utc)
    parados = []
    for item in _itens_ativos():
        if item.get("type") != "Kanban::Card" or item.get("due_on") or item.get("completed"):
            continue
        if _em_coluna_terminal(item):
            continue
        atualizado_em = item.get("updated_at")
        if not atualizado_em:
            continue
        dias = (agora - datetime.fromisoformat(atualizado_em.replace("Z", "+00:00"))).days
        if dias < dias_sem_atividade:
            continue
        formatado = _formatar_item(item)
        formatado["dias_parado"] = dias
        parados.append(formatado)
    return parados

TTL_CARD_TABLES = 900  # mesmo TTL de _itens_ativos — o conjunto de quadros muda tão pouco quanto o de cards

def _card_tables_ativos(forcar: bool = False) -> list[dict]:
    """Os quadros Kanban (Card Tables) em si — não os cards que têm dentro.
    Um Card Table é o seu próprio tipo de registo na API do Basecamp
    (`Kanban::Board`), separado de `Kanban::Card`; por isso fica de fora de
    _itens_ativos (usada para contagens de tarefas/cards em aberto por
    estado — um quadro não tem "estado" nem prazo, e misturá-lo ali
    inflacionava essas contagens). Bug real (Rui, 2026-08-04): a Alma nunca
    encontrava um quadro pelo nome (ex: "Programa Redes Sociais") porque
    TIPOS_MONITORIZADOS só via tarefas e cards — o próprio quadro, se for
    isso que alguém está a procurar, era invisível para qualquer busca.

    Sem filtro "completed" (um quadro não tem essa noção, ao contrário de
    tarefas/cards) — só "status": "active", para excluir quadros
    arquivados/na lixeira."""
    if not forcar and "card_tables_ativos" in _cache:
        ts, itens = _cache["card_tables_ativos"]
        if time.time() - ts < TTL_CARD_TABLES:
            return itens
    itens = _get_paginado(f"{_base_url()}/projects/recordings.json",
                          params={"type": "Kanban::Board", "status": "active"},
                          etiqueta="Kanban::Board")
    print(f"[basecamp] Kanban::Board: {len(itens)} em aberto")
    _cache["card_tables_ativos"] = (time.time(), itens)
    return itens

def procurar_cards_basecamp(termo: str, projeto: str = None) -> list[dict]:
    """Procura tarefas, cards ou card tables (de todos os projetos, ou só
    de um em concreto) cujo título ou notas contenham `termo` — pedido
    explícito do Rui (2026-07-24): as notas de um card guardam
    frequentemente informação crítica (morada de entrega, dados do
    cliente, datas acordadas) que só aparecia noutras ferramentas quando o
    card estava atrasado ou parado; isto permite consultar as notas de
    QUALQUER card em aberto, a qualquer momento, mesmo dentro do prazo. Só
    considera itens ativos e não concluídos (ver _itens_ativos) — não
    encontra cards já arquivados/na lixeira. `termo` procura tanto no
    título como no texto das notas, tolerante a acentos.

    Inclui também os próprios card tables (quadros Kanban) cujo título
    contenha `termo` — ver _card_tables_ativos: um card table é um objeto
    à parte de um card, sem notas, por isso só o título é comparado."""
    alvo = _normalizar(termo)
    projeto_normalizado = _normalizar(projeto) if projeto else None
    encontrados = []
    for item in _itens_ativos():
        if projeto_normalizado and projeto_normalizado not in _normalizar((item.get("bucket") or {}).get("name") or ""):
            continue
        titulo = item.get("title") or item.get("content") or ""
        notas = _texto_simples(item.get("description", ""))
        if alvo not in _normalizar(titulo) and alvo not in _normalizar(notas):
            continue
        encontrados.append(_formatar_item(item))
    for item in _card_tables_ativos():
        if projeto_normalizado and projeto_normalizado not in _normalizar((item.get("bucket") or {}).get("name") or ""):
            continue
        titulo = item.get("title") or ""
        if alvo not in _normalizar(titulo):
            continue
        encontrados.append(_formatar_item(item))
    return encontrados

def estado_projeto_basecamp(projeto: str) -> dict:
    """Panorama de um projeto do Basecamp: tarefas/cards genuinamente em
    aberto agrupados por estado/coluna, com contagens de atraso e cards
    parados sem prazo. Ignora tudo o que já está numa coluna/lista de estado
    terminal (Perdido, Vendido, Done, ...) — é trabalho já fechado, não
    trabalho ativo. `projeto` é um termo de pesquisa pelo nome (não precisa
    de ser exato)."""
    termo = projeto.lower().strip()
    itens = [i for i in _itens_ativos()
             if termo in ((i.get("bucket") or {}).get("name") or "").lower()
             and not _em_coluna_terminal(i)]
    if not itens:
        return {"erro": f"nenhum item em aberto encontrado para um projeto que corresponda a {projeto!r}"}

    hoje = date.today()
    por_estado = {}
    atrasados = []
    for item in itens:
        estado = (item.get("parent") or {}).get("title") or "(sem estado)"
        por_estado[estado] = por_estado.get(estado, 0) + 1
        prazo = item.get("due_on")
        if prazo and not item.get("completed") and date.fromisoformat(prazo) < hoje:
            formatado = _formatar_item(item)
            formatado["dias_atraso"] = (hoje - date.fromisoformat(prazo)).days
            atrasados.append(formatado)

    parados = [p for p in cards_parados_sem_prazo() if p["projeto"] == itens[0]["bucket"]["name"]]

    return {
        "projeto": itens[0]["bucket"]["name"],
        "total_ativos": len(itens),
        "por_estado": por_estado,
        "atrasados": sorted(atrasados, key=lambda i: -i["dias_atraso"])[:30],
        "cards_parados_sem_prazo": sorted(parados, key=lambda i: -i["dias_parado"])[:30],
    }

def resumo_pessoa_basecamp(nome: str) -> dict:
    """Panorama de uma pessoa da equipa, pensado para preparar uma reunião
    1:1: o que tem em aberto agora (e o que está atrasado), e como a
    quantidade de trabalho ativo que tem compara com a média de quem mais
    tem itens atribuídos — para ajudar a perceber se a carga está ajustada.
    Ignora completamente os to-dos (Todo) — só considera cards do Kanban,
    que são o que representa trabalho atribuível de forma fiável neste
    panorama. Ignora também tudo o que já está numa coluna de estado
    terminal (Perdido, Vendido, Done, ...) — não conta como trabalho em
    aberto nem entra na carga de trabalho, mesmo que a Basecamp não o
    marque como "completed". `nome` é um termo de pesquisa (não precisa de
    ser o nome completo)."""
    termo = _normalizar(nome)

    def _e_da_pessoa(item: dict) -> bool:
        return any(termo in _normalizar(p["name"]) for p in item.get("assignees", []))

    ativos = [i for i in _itens_ativos() if i.get("type") == "Kanban::Card" and not _em_coluna_terminal(i)]
    itens_pessoa = [i for i in ativos if _e_da_pessoa(i)]

    if not itens_pessoa:
        return {"erro": f"não encontrei nenhum card em aberto atribuído a alguém que corresponda a {nome!r}"}

    hoje = date.today()
    atrasados = []
    for item in itens_pessoa:
        prazo = item.get("due_on")
        if prazo and not item.get("completed") and date.fromisoformat(prazo) < hoje:
            formatado = _formatar_item(item)
            formatado["dias_atraso"] = (hoje - date.fromisoformat(prazo)).days
            atrasados.append(formatado)

    # carga de trabalho: quantos itens genuinamente em aberto cada pessoa
    # tem neste momento, para comparar esta pessoa com a média
    contagem_por_pessoa = {}
    for item in ativos:
        for p in item.get("assignees", []):
            contagem_por_pessoa[p["name"]] = contagem_por_pessoa.get(p["name"], 0) + 1
    media_equipa = (sum(contagem_por_pessoa.values()) / len(contagem_por_pessoa)) if contagem_por_pessoa else 0

    return {
        "pessoa": nome,
        "em_aberto_agora": {
            "total": len(itens_pessoa),
            "atrasados": sorted(atrasados, key=lambda i: -i["dias_atraso"])[:30],
            "itens": [_formatar_item(i) for i in itens_pessoa][:40],
        },
        "carga_de_trabalho": {
            "itens_ativos_desta_pessoa": len(itens_pessoa),
            "media_da_equipa_com_itens_atribuidos": round(media_equipa, 1),
        },
    }

def ler_comentarios(comments_url: str) -> list[dict]:
    """Lê os comentários já existentes numa tarefa/card (comments_url vem de tarefas_e_cards_atrasados).
    Inclui o url (da própria API, nunca reconstruído à mão — o Basecamp
    aninha os recordings sob o bucket do projeto, um /recordings/{id}.json
    solto na raiz dá sempre 404) e os nomes dos ficheiros que o comentário
    tenha anexados diretamente (ex: um PDF partilhado num comentário, não
    na descrição da tarefa/card) — para se poder ler esses anexos depois
    com ler_anexos_registo_basecamp(url do comentário), não só os da
    tarefa/card."""
    comentarios = _get_paginado(comments_url)
    resultado = []
    for c in comentarios:
        anexos = [a.get("filename") or a.get("name") or "(sem nome)"
                 for a in (c.get("content_attachments") or [])]
        resultado.append({
            "id": c.get("id"),
            "url": c.get("url"),
            "autor": (c.get("creator") or {}).get("name"),
            "conteudo": c.get("content"),
            "criado_em": c.get("created_at"),
            "anexos": anexos,
        })
    return resultado

def procurar_anexo_em_comentarios(comments_url: str, termo: str) -> list[dict]:
    """Procura, nos comentários de uma tarefa/card, aqueles que têm um
    ficheiro anexado cujo nome contenha `termo` (ex: um nome de ficheiro
    mencionado nas notas do card, como "OR 2026_13.pdf") — pedido real
    (Beatriz, 2026-07-27): as notas de um card mencionavam nomes de
    PDFs (fatura/orçamento), mas os ficheiros em si estavam anexados a
    UM comentário entre 145, e não havia forma de encontrar qual sem os
    percorrer manualmente. Devolve cada comentário correspondente, com o
    seu próprio "url" pronto a passar a ler_anexos_registo_basecamp."""
    termo_normalizado = _normalizar(termo)
    comentarios = ler_comentarios(comments_url)
    return [c for c in comentarios
            if any(termo_normalizado in _normalizar(nome) for nome in (c.get("anexos") or []))]

def _escapar_html(texto: str) -> str:
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _dividir_linha_tabela(linha: str) -> list:
    l = linha.strip()
    if l.startswith("|"):
        l = l[1:]
    if l.endswith("|"):
        l = l[:-1]
    return [c.strip() for c in l.split("|")]

def _e_separador_tabela(linha: str) -> bool:
    celulas = _dividir_linha_tabela(linha)
    return bool(celulas) and all(re.match(r"^:?-+:?$", c) for c in celulas)

def _tabela_para_html(cabecalho: list, linhas: list) -> str:
    partes = ["<table><thead><tr>"]
    for h in cabecalho:
        partes.append(f"<th>{h}</th>")
    partes.append("</tr></thead><tbody>")
    for linha in linhas:
        partes.append("<tr>")
        for i in range(len(cabecalho)):
            partes.append(f"<td>{linha[i] if i < len(linha) else ''}</td>")
        partes.append("</tr>")
    partes.append("</tbody></table>")
    return "".join(partes)

def _markdown_para_basecamp(bruto: str) -> str:
    """Converte o markdown simples que a Alma escreve (negrito, itálico, títulos,
    listas, links, código, tabelas, linhas horizontais) para HTML — os
    comentários do Basecamp são HTML puro, por isso markdown sem converter
    aparece tal e qual (asteriscos, cardinais, barras verticais, ...) em vez
    de formatado."""
    blocos_codigo = []

    def _guardar_bloco(m):
        blocos_codigo.append(f"<pre>{_escapar_html(m.group(1).rstrip(chr(10)))}</pre>")
        return f"@@CODEBLOCK{len(blocos_codigo) - 1}@@"

    texto = re.sub(r"```[a-zA-Z0-9]*\n?([\s\S]*?)```", _guardar_bloco, bruto)
    texto = _escapar_html(texto)

    texto = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", texto)
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', texto)
    texto = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", texto)
    texto = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", texto)
    texto = re.sub(r"(^|[^*])\*([^*\n]+)\*(?!\*)", r"\1<em>\2</em>", texto)
    texto = re.sub(r"(^|[^_])_([^_\n]+)_(?!_)", r"\1<em>\2</em>", texto)

    partes = []
    paragrafo = []
    em_ul = em_ol = False

    def fechar_paragrafo():
        nonlocal paragrafo
        if paragrafo:
            partes.append(f"<p>{'<br>'.join(paragrafo)}</p>")
            paragrafo = []

    def fechar_listas():
        nonlocal em_ul, em_ol
        if em_ul:
            partes.append("</ul>")
            em_ul = False
        if em_ol:
            partes.append("</ol>")
            em_ol = False

    linhas = texto.split("\n")
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        aparada = linha.strip()
        bloco_codigo = re.match(r"^@@CODEBLOCK(\d+)@@$", aparada)
        titulo = re.match(r"^(#{1,3})\s+(.*)", aparada)
        item_ul = re.match(r"^[-*]\s+(.*)", aparada)
        item_ol = re.match(r"^\d+\.\s+(.*)", aparada)
        # tabela: esta linha tem pipes e a seguinte é a linha de separação
        # (---|---|---) — só aí é que vale a pena tratar como tabela, para
        # não confundir uma frase qualquer que tenha um "|" à mistura
        e_tabela = "|" in aparada and i + 1 < len(linhas) and _e_separador_tabela(linhas[i + 1])
        e_linha_horizontal = re.match(r"^-{3,}$", aparada) is not None

        if e_tabela:
            fechar_paragrafo()
            fechar_listas()
            cabecalho = _dividir_linha_tabela(aparada)
            i += 2  # salta a linha de separação
            linhas_tabela = []
            while i < len(linhas) and "|" in linhas[i]:
                linhas_tabela.append(_dividir_linha_tabela(linhas[i]))
                i += 1
            partes.append(_tabela_para_html(cabecalho, linhas_tabela))
            continue
        elif not aparada:
            fechar_paragrafo()
            fechar_listas()
        elif e_linha_horizontal:
            fechar_paragrafo()
            fechar_listas()
            partes.append("<hr>")
        elif bloco_codigo:
            fechar_paragrafo()
            fechar_listas()
            partes.append(blocos_codigo[int(bloco_codigo.group(1))])
        elif titulo:
            fechar_paragrafo()
            fechar_listas()
            # o editor do Basecamp só tem um nível de título — todos os
            # níveis de markdown (#, ##, ###) mapeiam para o mesmo <h1>
            partes.append(f"<h1>{titulo.group(2)}</h1>")
        elif item_ul:
            fechar_paragrafo()
            if em_ol:
                partes.append("</ol>")
                em_ol = False
            if not em_ul:
                partes.append("<ul>")
                em_ul = True
            partes.append(f"<li>{item_ul.group(1)}</li>")
        elif item_ol:
            fechar_paragrafo()
            if em_ul:
                partes.append("</ul>")
                em_ul = False
            if not em_ol:
                partes.append("<ol>")
                em_ol = True
            partes.append(f"<li>{item_ol.group(1)}</li>")
        else:
            fechar_listas()
            paragrafo.append(linha)
        i += 1

    fechar_paragrafo()
    fechar_listas()
    return "".join(partes)

# "@Nome da Pessoa" no texto que a Alma escreve vira uma menção real do
# Basecamp (que notifica a pessoa), não só o nome em texto simples — desde
# que corresponda a alguém com acesso ao projeto em questão. O Basecamp
# representa uma menção como uma tag <bc-attachment sgid="..."> no HTML do
# conteúdo, onde o sgid vem do próprio registo da pessoa (attachable_sgid).
_PADRAO_MENCAO = re.compile(r"@([A-ZÀ-ÖØ-Þ][^\s@,.!?;:()]*(?:\s+[A-ZÀ-ÖØ-Þ][^\s@,.!?;:()]*){0,3})")

def _adicionar_arroba_em_nomes_conhecidos(texto: str, pessoas: list) -> str:
    """Antes de resolver "@Nome" para uma menção real, garante que o nome
    de QUALQUER pessoa com acesso a este projeto que apareça no texto
    SEM "@" à frente passa a ter — pedido explícito do Rui (2026-07-29):
    "sempre que nomeia um elemento da equipa ele é sempre tagado, em
    qualquer parte do texto". Nunca depende de o LLM se lembrar de
    escrever o "@" sozinho (o mesmo princípio de sempre nesta aplicação:
    o que tem de ser sempre certo fica garantido em código, não confiado
    à IA) — aqui a garantia cobre TODAS as formas de texto que a Alma
    publica no Basecamp (comentários, mural), pois todas passam por
    _markdown_para_basecamp_com_mencoes.

    Substitui todas as ocorrências (não só a primeira) do nome completo
    de cada pessoa, tal como está registado no Basecamp — comparação sem
    distinguir maiúsculas, mas preservando a escrita exata usada no
    texto. Nomes mais longos são tentados primeiro, para "Rui Rocha" não
    ficar só parcialmente tagado por causa de outra pessoa chamada só
    "Rui". Uma ocorrência já precedida de "@" nunca é tocada (evita
    "@@Nome")."""
    nomes = sorted({p["name"] for p in pessoas if p.get("name")}, key=len, reverse=True)
    for nome in nomes:
        padrao = re.compile(r"(?<!@)\b" + re.escape(nome) + r"\b", re.IGNORECASE)
        texto = padrao.sub(lambda m: "@" + m.group(0), texto)
    return texto

def _resolver_mencoes(texto: str, projeto: str) -> tuple:
    """Substitui cada "@Nome" por um marcador de posição, para cada pessoa
    encontrada que tenha acesso ao projeto indicado — devolve o texto com
    os marcadores e a lista de attachable_sgid pela mesma ordem, para
    trocar pela tag real da menção depois da conversão para HTML (tal como
    os blocos de código, para o "<" e ">" da tag não serem escapados).
    Um "@Nome" que não corresponda a ninguém fica só o nome, sem o "@", em
    vez de um símbolo pendurado sem menção nenhuma.

    Antes disso, qualquer nome de pessoa (com acesso a este projeto) que
    apareça no texto sem "@" já é corrigido por
    _adicionar_arroba_em_nomes_conhecidos — para nunca depender de a Alma
    se lembrar de escrever o "@" sozinha."""
    try:
        pessoas = pessoas_projeto(projeto) if projeto else []
    except Exception as e:
        print(f"[basecamp] não consegui obter pessoas do projeto para resolver menções: {e!r}")
        pessoas = []

    texto = _adicionar_arroba_em_nomes_conhecidos(texto, pessoas)

    sgids = []

    def _substituir(m):
        nome = m.group(1)
        termo = _normalizar(nome)
        for p in pessoas:
            if _normalizar(p["name"]) == termo and p.get("attachable_sgid"):
                sgids.append(p["attachable_sgid"])
                return f"@@MENCAO{len(sgids) - 1}@@"
        # bug real reportado em produção (2026-07-29): sem este log, uma
        # menção falhada (nome sem correspondência exata a ninguém com
        # acesso a este projeto, ou sem attachable_sgid) fica
        # INDISTINGUÍVEL de um "@Nome" que nunca chegou a ser escrito —
        # nos dois casos o resultado final é só o nome em texto simples,
        # sem "@" nenhum (ver comentário acima). Este log é a única forma
        # de confirmar, pelos logs do Railway, QUAL dos dois está mesmo a
        # acontecer da próxima vez, sem ter de partilhar credenciais para
        # investigar.
        print(f"[basecamp] menção \"@{nome}\" não corresponde a ninguém com acesso ao projeto "
             f"\"{projeto}\" (ou a pessoa não tem attachable_sgid) — publicada sem menção real. "
             f"Pessoas encontradas nesse projeto: {[p.get('name') for p in pessoas]}")
        return nome

    return _PADRAO_MENCAO.sub(_substituir, texto), sgids

def _markdown_para_basecamp_com_mencoes(texto: str, projeto: str = None) -> str:
    """Como _markdown_para_basecamp, mas primeiro resolve "@Nome" para
    menções reais do Basecamp quando `projeto` for indicado."""
    if not projeto:
        return _markdown_para_basecamp(texto)
    texto_com_marcadores, sgids = _resolver_mencoes(texto, projeto)
    html = _markdown_para_basecamp(texto_com_marcadores)
    for i, sgid in enumerate(sgids):
        html = html.replace(f"@@MENCAO{i}@@", f'<bc-attachment sgid="{sgid}"></bc-attachment>')
    return html

def comentar(recording_id: int, texto: str, projeto: str = None):
    """Publica um comentário numa tarefa/card. Se `projeto` for indicado,
    resolve "@Nome" no texto para uma menção real do Basecamp (notifica a
    pessoa) sempre que corresponder a alguém com acesso a esse projeto."""
    r = httpx.post(f"{_base_url()}/recordings/{recording_id}/comments.json",
                   headers=_headers(), json={"content": _markdown_para_basecamp_com_mencoes(texto, projeto)},
                   timeout=30)
    r.raise_for_status()
    return r.json()

# Mural (Message Board) do projeto "Gestão" — toda a equipa da Interior
# Guider está lá, por isso serve como mural por omissão. Outros projetos
# (ex: Ecos Largos, uma equipa parceira à parte) têm o seu próprio Mural,
# resolvido dinamicamente pelo nome em vez de hardcoded, já que só a Gestão
# é usada com frequência suficiente para valer a pena poupar esse pedido.
MURAL_BUCKET_ID = 603157
MURAL_BOARD_ID = 85747247

def _resolver_mural(projeto: str) -> tuple:
    """Descobre o bucket_id e o id do Mural (message_board) de um projeto
    pelo nome (ver _encontrar_projeto) — usado para publicar no mural de
    projetos que não sejam a Gestão (ex: o mural próprio da Ecos Largos,
    só visível à equipa deles)."""
    p = _encontrar_projeto(projeto)
    if not p:
        raise ValueError(f"nenhum projeto encontrado para {projeto!r}")
    for ferramenta in p.get("dock", []):
        if ferramenta.get("name") == "message_board" and ferramenta.get("enabled"):
            return p["id"], ferramenta["id"]
    raise ValueError(f"o projeto {p['name']!r} não tem Mural (message board) ativado")

def _ids_pessoas(projeto: str, nomes: list) -> list:
    """Resolve `nomes` (lista de nomes completos) para os seus ids de
    pessoa, de entre quem tem acesso a `projeto` — nomes sem
    correspondência são ignorados (nunca rebenta por causa disto)."""
    pessoas = pessoas_projeto(projeto)
    termos = {_normalizar(nome) for nome in nomes}
    return [p["id"] for p in pessoas if _normalizar(p["name"]) in termos]

def _restringir_subscritores_existentes(bucket_id: int, recording_id: int, projeto: str, ids_a_manter: list) -> None:
    """Garante que a lista de subscritores DAQUI PARA A FRENTE (ex:
    notificações de comentários futuros numa mensagem do Mural) fica
    restrita exatamente a `ids_a_manter` — remove explicitamente toda a
    gente com acesso ao projeto que não esteja nessa lista (incluindo a
    própria Alma, que o Basecamp subscreve automaticamente como autora).

    Confirmado ao vivo contra a API real do Basecamp: o PUT
    ".../subscription.json" só remove mesmo alguém através do campo
    "unsubscriptions" — o campo "subscriptions" sozinho (só com quem
    manter) NÃO substitui a lista existente (testado e confirmado: com
    só "subscriptions", a lista continuou com todas as pessoas já
    subscritas). É preciso mandar sempre os dois campos.

    IMPORTANTE: isto só afeta subscrições FUTURAS (comentários depois de
    publicado) — a notificação do PRÓPRIO ato de publicar já foi enviada
    antes disto correr, decidida pelo parâmetro "subscriptions" passado
    na CRIAÇÃO da mensagem (ver publicar_mural) — bug real reportado em
    produção (2026-07-29): sem esse parâmetro na criação, a notificação
    inicial ia sempre para toda a gente com acesso ao projeto (11-12
    pessoas), e só a partir daí é que esta função conseguia limitar
    quem seria notificado por comentários seguintes — tarde demais para
    a notificação que já tinha saído."""
    pessoas = pessoas_projeto(projeto)
    remover = [p["id"] for p in pessoas if p["id"] not in ids_a_manter]
    url = f"{_base_url()}/buckets/{bucket_id}/recordings/{recording_id}/subscription.json"
    r = httpx.put(url, headers=_headers(),
                 json={"subscriptions": ids_a_manter, "unsubscriptions": remover}, timeout=30)
    r.raise_for_status()

def publicar_mural(assunto: str, mensagem: str, projeto: str = "Gestão", notificar_apenas: list = None):
    """Publica uma mensagem no Mural de um projeto (visível a quem tem
    acesso a esse projeto). Por omissão, o mural da Gestão (toda a equipa da
    Interior Guider); passa `projeto` para publicar no mural de outro
    projeto (ex: "Ecos Largos"). "@Nome" na mensagem vira uma menção real
    (notifica a pessoa) se corresponder a alguém com acesso a este projeto.

    `notificar_apenas`, quando indicado (lista de nomes completos),
    restringe quem é NOTIFICADO sobre esta mensagem exatamente a essas
    pessoas — por omissão do Basecamp, toda a gente com acesso ao
    projeto seria notificada, logo na própria publicação (bug real
    reportado em produção, 2026-07-29: um post desta app notificou 11
    pessoas mesmo com uma tentativa de restringir feita SÓ DEPOIS de
    criar a mensagem — tarde demais, o Basecamp já tinha enviado a
    notificação inicial com base na lista por omissão). A forma certa,
    confirmada ao vivo, é passar "subscriptions" já no pedido de CRIAÇÃO
    da mensagem — só assim a notificação inicial já sai só para essas
    pessoas. De seguida, restringe-se também a subscrição daqui para a
    frente (comentários futuros), para nem a própria Alma (autora,
    subscrita automaticamente) ficar na lista.

    Uma falha ao restringir a subscrição futura nunca impede a
    publicação da mensagem em si (a notificação inicial, essa, já saiu
    correta de qualquer forma — só a subscrição a longo prazo é que
    pode ficar por afinar, e o erro fica registado nos logs)."""
    if projeto.strip().lower() == "gestão":
        bucket_id, board_id = MURAL_BUCKET_ID, MURAL_BOARD_ID
    else:
        bucket_id, board_id = _resolver_mural(projeto)

    corpo = {"subject": assunto, "content": _markdown_para_basecamp_com_mencoes(mensagem, projeto),
            "status": "active"}
    if notificar_apenas:
        corpo["subscriptions"] = _ids_pessoas(projeto, notificar_apenas)

    r = httpx.post(f"{_base_url()}/buckets/{bucket_id}/message_boards/{board_id}/messages.json",
                   headers=_headers(), json=corpo, timeout=30)
    r.raise_for_status()
    resultado = r.json()
    if notificar_apenas:
        try:
            _restringir_subscritores_existentes(bucket_id, resultado["id"], projeto, corpo["subscriptions"])
        except Exception as e:
            print(f"[basecamp] não consegui afinar a subscrição futura de \"{assunto}\" "
                 f"a {notificar_apenas}: {e!r}")
    return resultado

def listar_mural(projeto: str = "Gestão", limite: int = 20) -> list[dict]:
    """Lista as mensagens mais recentes do Mural de um projeto (assunto,
    autor, data, quantos comentários tem, e o url para ler o conteúdo
    completo com ler_mensagem_mural) — usa isto para encontrar um post
    anterior (ex: um resumo semanal/diário antigo, para comparar com o
    atual) antes de o leres na íntegra. Por omissão, o mural da Gestão."""
    if projeto.strip().lower() == "gestão":
        bucket_id, board_id = MURAL_BUCKET_ID, MURAL_BOARD_ID
    else:
        bucket_id, board_id = _resolver_mural(projeto)
    mensagens = _get_paginado(f"{_base_url()}/buckets/{bucket_id}/message_boards/{board_id}/messages.json")
    mensagens.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return [{
        "id": m["id"],
        "assunto": m.get("subject") or m.get("title") or "(sem assunto)",
        "autor": (m.get("creator") or {}).get("name"),
        "criado_em": m.get("created_at"),
        "comments_count": m.get("comments_count", 0),
        "url": m.get("url"),
        "app_url": m.get("app_url"),
    } for m in mensagens[:limite]]

def ler_mensagem_mural(url: str) -> dict:
    """Lê o conteúdo completo e os comentários de uma mensagem do Mural —
    usa o campo `url` devolvido por listar_mural."""
    try:
        mensagem = obter_recording(url)
    except Exception as e:
        return {"erro": f"não consegui ler esta mensagem do mural: {e}"}
    comentarios = ler_comentarios(mensagem["comments_url"]) if mensagem.get("comments_url") else []
    return {
        "assunto": mensagem.get("subject") or mensagem.get("title") or "(sem assunto)",
        "autor": (mensagem.get("creator") or {}).get("name"),
        "criado_em": mensagem.get("created_at"),
        "conteudo": _texto_simples(mensagem.get("content", "")),
        "comentarios": comentarios,
    }

def _resolver_vault(projeto: str) -> tuple:
    """Descobre o bucket_id e o id do Vault (Docs & Files) de um projeto
    pelo nome (ver _encontrar_projeto) — tal como _resolver_mural, mas
    para o Vault em vez do Mural. Usado para criar um documento novo e
    permanente num projeto (ex: o resumo anual de avaliações de cargas
    de toros da Ecos Largos)."""
    p = _encontrar_projeto(projeto)
    if not p:
        raise ValueError(f"nenhum projeto encontrado para {projeto!r}")
    for ferramenta in p.get("dock", []):
        if ferramenta.get("name") == "vault" and ferramenta.get("enabled"):
            return p["id"], ferramenta["id"]
    raise ValueError(f"o projeto {p['name']!r} não tem Docs & Files (vault) ativado")

def criar_documento(titulo: str, conteudo: str, projeto: str) -> dict:
    """Cria um novo Documento no Vault (Docs & Files) de um projeto do
    Basecamp — para um registo permanente e organizado (ex: o resumo
    anual de avaliações de cargas de toros), não para comentar numa
    tarefa/card existente (usa comentar) nem para publicar no Mural (usa
    publicar_mural)."""
    bucket_id, vault_id = _resolver_vault(projeto)
    r = httpx.post(f"{_base_url()}/buckets/{bucket_id}/vaults/{vault_id}/documents.json",
                   headers=_headers(),
                   json={"title": titulo, "content": _markdown_para_basecamp(conteudo), "status": "active"},
                   timeout=30)
    r.raise_for_status()
    return r.json()

def _resolver_schedule(projeto: str) -> tuple:
    """Descobre o bucket_id e o id da Agenda (Schedule) de um projeto pelo
    nome (ver _encontrar_projeto) — tal como _resolver_mural/
    _resolver_vault. Confirmado ao vivo (2026-07-28, contra a API real do
    Basecamp) que o projeto "Entregas" já tem a Agenda ativada (dock
    "schedule")."""
    p = _encontrar_projeto(projeto)
    if not p:
        raise ValueError(f"nenhum projeto encontrado para {projeto!r}")
    for ferramenta in p.get("dock", []):
        if ferramenta.get("name") == "schedule" and ferramenta.get("enabled"):
            return p["id"], ferramenta["id"]
    raise ValueError(f"o projeto {p['name']!r} não tem Agenda (Schedule) ativada — "
                    "ativa-a nas definições do projeto no Basecamp antes de criar eventos")

def criar_evento_calendario(titulo: str, inicio_iso: str, fim_iso: str,
                            descricao: str = "", projeto: str = "Entregas") -> dict:
    """Cria um evento na Agenda (Schedule) de um projeto do Basecamp —
    confirmado ao vivo (2026-07-28) contra o projeto "Entregas" real,
    criado e depois apagado como teste. `inicio_iso`/`fim_iso` têm de ser
    datetimes ISO8601 já com o fuso horário incluído (ver
    tools.agendamento_logistica.horario_para_iso — nunca construídos aqui
    sem fuso horário, para nunca assumir por engano a diferença errada
    para UTC)."""
    bucket_id, schedule_id = _resolver_schedule(projeto)
    r = httpx.post(f"{_base_url()}/buckets/{bucket_id}/schedules/{schedule_id}/entries.json",
                   headers=_headers(),
                   json={"summary": titulo, "starts_at": inicio_iso, "ends_at": fim_iso,
                        "description": _markdown_para_basecamp(descricao), "all_day": False, "notify": False},
                   timeout=30)
    r.raise_for_status()
    return r.json()

def entradas_agenda(projeto: str = "Entregas") -> list[dict]:
    """Lista as entradas atualmente ativas na Agenda (Schedule) de um
    projeto — usado pela sincronização unidirecional para o Google
    Calendar (ver agents/sincronizacao_calendario.py) para saber o que
    existe agora no Basecamp. O Basecamp só devolve aqui entradas ativas
    (uma entrada apagada/trashed deixa simplesmente de aparecer nesta
    lista) — é assim que a sincronização deteta eliminações, comparando
    esta lista contra o mapeamento guardado na base de dados."""
    bucket_id, schedule_id = _resolver_schedule(projeto)
    return _get_paginado(f"{_base_url()}/buckets/{bucket_id}/schedules/{schedule_id}/entries.json")


def _get_bytes(url: str) -> bytes:
    """Descarrega um ficheiro anexado (Upload) — usa a mesma autenticação da API."""
    r = httpx.get(url, headers=_headers(), timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.content

def obter_recording(url: str) -> dict:
    """Vai buscar a representação completa e atual de um registo (comentário,
    tarefa, card, ...) pelo seu próprio URL da API — útil quando o payload de
    um webhook vem mais resumido do que o pedido direto."""
    r = httpx.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def meu_perfil() -> dict:
    """A própria conta Alma no Basecamp (id, nome) — para nunca reagir aos seus próprios comentários."""
    if "meu_perfil" in _cache:
        return _cache["meu_perfil"]
    r = httpx.get(f"{_base_url()}/my/profile.json", headers=_headers(), timeout=30)
    r.raise_for_status()
    perfil = r.json()
    _cache["meu_perfil"] = perfil
    return perfil

def listar_projetos() -> list[dict]:
    return _get_paginado(f"{_base_url()}/projects.json")

def _encontrar_projeto(nome: str) -> dict:
    """Encontra um projeto do Basecamp pelo nome — usado por
    pessoas_projeto/_resolver_mural/_resolver_vault/_resolver_schedule,
    para nunca haver duas versões desta lógica a divergir.

    Bug real reportado em produção (2026-07-29): a correspondência era só
    por substring ("termo in p['name'].lower()"), sem preferência
    nenhuma por uma correspondência exata — se houver mais do que um
    projeto cujo nome contenha o termo (ex: um projeto arquivado ou
    duplicado com nome parecido), o primeiro da lista (ordem arbitrária
    da API) era escolhido sempre, mesmo sendo o projeto errado.
    Confirmado ao vivo: a busca por "Marketing Interior Guider" resolveu
    para um bucket_id que devolvia 404 em /people.json — sinal claro de
    que não era o projeto certo, e por isso NENHUMA menção (nem
    "@Beatriz Barbosa", nem "@Rui Rocha", ambas escritas corretamente com
    "@") conseguia resolver-se.

    Tenta sempre primeiro uma correspondência EXATA (sem acentuação/
    maiúsculas); só cai para substring se não houver nenhuma exata, para
    continuar a tolerar pedidos parciais legítimos. Devolve None se não
    encontrar nenhum projeto."""
    termo = nome.lower().strip()
    projetos = listar_projetos()
    for p in projetos:
        if p["name"].strip().lower() == termo:
            return p
    for p in projetos:
        if termo in p["name"].lower():
            return p
    return None

TTL_PESSOAS_PROJETO = 3600  # 1h — a equipa de um projeto não muda de hora a hora

def pessoas_projeto(projeto: str) -> list[dict]:
    """Pessoas com acesso a um projeto específico do Basecamp (pelo nome,
    ver _encontrar_projeto) — usado para a Alma saber automaticamente
    quem pertence a que equipa (ex: Ecos Largos, uma equipa parceira
    gerida no mesmo Basecamp mas à parte da Interior Guider), sem
    precisar de uma lista de nomes fixa no código, e para resolver
    menções "@Nome" (ver _resolver_mencoes).

    Bug real reportado em produção (2026-07-29), confirmado ao vivo contra
    a API real: "/buckets/{id}/people.json" NÃO é um endpoint válido do
    Basecamp — devolve 404 para QUALQUER projeto (testado e confirmado
    tanto em "Entregas" como em "Marketing Interior Guider", o mesmo 404
    nos dois). Isto significa que NENHUMA menção alguma vez resolveu de
    facto (não era só um projeto específico) — o endpoint certo é
    "/projects/{id}/people.json" (confirmado ao vivo: devolve as pessoas
    certas, com attachable_sgid presente)."""
    chave = f"pessoas_{projeto.lower().strip()}"
    if chave in _cache:
        ts, pessoas = _cache[chave]
        if time.time() - ts < TTL_PESSOAS_PROJETO:
            return pessoas
    p = _encontrar_projeto(projeto)
    pessoas = _get_paginado(f"{_base_url()}/projects/{p['id']}/people.json") if p else []
    _cache[chave] = (time.time(), pessoas)
    return pessoas

def pertence_a_projeto(nome: str, projeto: str) -> bool:
    """Se alguém (pelo nome) tem acesso a um projeto específico do Basecamp."""
    termo = _normalizar(nome)
    return any(termo in _normalizar(p["name"]) for p in pessoas_projeto(projeto))

def pertence_a_ecos_largos(nome: str) -> bool:
    return pertence_a_projeto(nome, "Ecos Largos")

def listar_webhooks(bucket_id: int) -> list[dict]:
    return _get_paginado(f"{_base_url()}/buckets/{bucket_id}/webhooks.json")

def criar_webhook(bucket_id: int, payload_url: str, tipos: list[str] = None):
    corpo = {"payload_url": payload_url}
    if tipos:
        corpo["types"] = tipos
    r = httpx.post(f"{_base_url()}/buckets/{bucket_id}/webhooks.json",
                   headers=_headers(), json=corpo, timeout=30)
    r.raise_for_status()
    return r.json()

TOOLS_ESTADO_PROJETO = [
    {
        "name": "estado_projeto_basecamp",
        "description": "Dá um panorama de um projeto do Basecamp: quantas tarefas/cards ativos existem por estado/coluna, quais estão atrasados e quais são cards do Kanban sem prazo parados há semanas (ignorando colunas de estado fechado como Perdido/Vendido/Done). Usa isto quando alguém perguntar como está um projeto ou pedir um resumo de atividade. `projeto` é um termo de pesquisa pelo nome (não precisa de ser exato).",
        "input_schema": {
            "type": "object",
            "properties": {"projeto": {"type": "string"}},
            "required": ["projeto"]
        }
    },
    {
        "name": "resumo_pessoa_basecamp",
        "description": "Dá um panorama de uma pessoa da equipa no Basecamp, pensado para preparar uma reunião individual (1:1): os cards do Kanban que tem em aberto agora e o que está atrasado (ignora completamente to-dos — só cards contam como trabalho real neste panorama), e como a quantidade de trabalho ativo que tem compara com a média de quem tem itens atribuídos (para ajudar a avaliar se a carga está ajustada). Usa isto quando pedirem um resumo de uma pessoa específica antes de uma reunião com ela. `nome` é um termo de pesquisa (não precisa de ser o nome completo).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string"}
            },
            "required": ["nome"]
        }
    },
    {
        "name": "procurar_cards_basecamp",
        "description": "Procura tarefas, cards OU card tables (quadros Kanban) do Basecamp por um termo — ex: o nome de um cliente, um número de encomenda, uma morada, um fornecedor, ou o próprio nome de um quadro. Compara título e notas de tarefas/cards; para card tables (que não têm notas) compara só o título. Devolve os itens encontrados com o campo \"tipo\" a indicar qual é qual (\"tarefa\", \"card\" ou \"card table\") e, para tarefas/cards, as notas (campo \"notas\"), onde costuma estar informação crítica como morada de entrega, dados do cliente, e datas acordadas. Usa isto sempre que precisares de consultar as notas de um card específico, mesmo que ele não esteja atrasado nem parado, OU para confirmar se existe um quadro com um certo nome — não é preciso esperar por um resumo geral do projeto, esta ferramenta encontra o item certo diretamente. `projeto` (opcional) filtra por um projeto em concreto, se souberes qual é. Se precisares de ler um PDF anexado ao card (ex: a fatura ou o orçamento, para identificar os produtos), usa depois ler_anexos_registo_basecamp com o campo \"url_api\" do card encontrado (nunca o campo \"url\", que é só o link para abrir no browser). Se o PDF estiver mencionado nas notas mas ler_anexos_registo_basecamp não encontrar nada anexado ao card, o ficheiro pode estar anexado a um COMENTÁRIO em vez da descrição — usa então procurar_anexo_em_comentarios com o campo \"comments_url\" deste card. IMPORTANTE: a morada de ENTREGA só pode vir do texto das notas (campo \"notas\") devolvido por esta ferramenta — nunca de um PDF anexado nem de um comentário; o PDF do orçamento/fatura tem a sua própria morada, mas é a morada fiscal/de faturação do cliente, que pode ser um sítio bem diferente do local real de entrega. Se as notas não tiverem morada nenhuma, diz isso claramente em vez de a ires buscar a outro sítio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string", "description": "termo a procurar no título ou nas notas do card (ex: nome do cliente, número de encomenda, morada)"},
                "projeto": {"type": "string", "description": "opcional — filtra por um projeto específico"}
            },
            "required": ["termo"]
        }
    },
    {
        "name": "procurar_anexo_em_comentarios",
        "description": "Procura, nos comentários de uma tarefa/card, aqueles que têm um ficheiro anexado cujo nome contenha `termo` — usa isto quando um nome de ficheiro (ex: um PDF de fatura/orçamento mencionado nas notas do card, como \"OR 2026_13.pdf\") não aparece anexado diretamente ao card (ler_anexos_registo_basecamp devolve vazio), já que o Basecamp às vezes só permite anexar ficheiros a comentários, não à descrição do card. Nunca percorras os comentários um a um à procura disto — esta ferramenta encontra o comentário certo diretamente, mesmo havendo uma centena ou mais. Devolve cada comentário correspondente com o seu próprio \"url\", pronto a passar a ler_anexos_registo_basecamp para ler o ficheiro.",
        "input_schema": {
            "type": "object",
            "properties": {
                "comments_url": {"type": "string", "description": "o campo \"comments_url\" do card (devolvido por procurar_cards_basecamp, estado_projeto_basecamp, etc.) — nunca inventado"},
                "termo": {"type": "string", "description": "nome (ou parte do nome) do ficheiro a encontrar, ex: \"OR 2026_13\""}
            },
            "required": ["comments_url", "termo"]
        }
    }
]
