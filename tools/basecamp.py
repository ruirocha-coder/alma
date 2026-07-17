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
TTL_CONCLUIDOS_RECENTES = 900

# colunas do Kanban que representam um estado terminal/fechado do fluxo (não
# trabalho esquecido) — um card parado aqui é esperado, não é sinal de nada.
COLUNAS_TERMINAIS = {"perdido", "perdidos", "vendido", "vendidos", "done",
                     "concluído", "concluido", "arquivo", "arquivado", "cancelado"}

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

def _itens_ativos() -> list[dict]:
    """Todas as tarefas (to-dos) e cards ativos e não concluídos, de todos os
    projetos — cacheado, porque várias funções (atrasos, cards parados, estado
    de projeto) partem dos mesmos dados e a conta tem milhares de itens em
    aberto (percorrê-los pode demorar minutos)."""
    if "itens_ativos" in _cache:
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
    return {
        "id": item["id"],
        "tipo": "tarefa" if item.get("type") == "Todo" else "card",
        "titulo": item.get("title") or item.get("content") or "(sem título)",
        "notas": _texto_simples(item.get("description", "")),
        # coluna do Kanban (estado do card) ou lista de tarefas (para Todos)
        # — dá contexto de onde o item está no fluxo de trabalho
        "estado": (item.get("parent") or {}).get("title"),
        "responsaveis": [p["name"] for p in item.get("assignees", [])],
        "projeto": (item.get("bucket") or {}).get("name"),
        "prazo": item.get("due_on"),
        "url": item.get("app_url"),
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

def _concluidos_recentemente(dias: int = 7) -> list[dict]:
    """Tarefas (to-dos) concluídas nos últimos `dias` dias, de todos os
    projetos — usado para saber "o que fez" alguém numa reunião 1:1. Só
    Todos (cards do Kanban não têm um estado "concluído" fiável neste fluxo
    — representam posição numa coluna, não conclusão de trabalho).

    A conta tem milhares de tarefas já concluídas ao longo dos anos, e a API
    do Basecamp não filtra por data no servidor — por isso pede-se ordenado
    por atualização mais recente e para de percorrer páginas assim que
    aparece uma tarefa mais antiga que o corte (tudo o resto, a seguir,
    também seria). O limite de páginas é só uma rede de segurança caso essa
    ordenação não se verifique."""
    chave = f"concluidos_{dias}"
    if chave in _cache:
        ts, itens = _cache[chave]
        if time.time() - ts < TTL_CONCLUIDOS_RECENTES:
            return itens

    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    itens = []
    url = f"{_base_url()}/projects/recordings.json"
    params = {"type": "Todo", "status": "active", "completed": "true",
              "sort": "updated_at", "direction": "desc"}
    for _ in range(50):
        if not url:
            break
        r = httpx.get(url, headers=_headers(), params=params, timeout=30)
        r.raise_for_status()
        parar = False
        for item in r.json():
            atualizado_em = item.get("updated_at")
            if not atualizado_em:
                continue
            if datetime.fromisoformat(atualizado_em.replace("Z", "+00:00")) < corte:
                parar = True
                break
            itens.append(item)
        if parar:
            break
        url = r.links.get("next", {}).get("url")
        params = None  # já incluído no url de "next"

    _cache[chave] = (time.time(), itens)
    return itens

def resumo_pessoa_basecamp(nome: str, dias: int = 7) -> dict:
    """Panorama de uma pessoa da equipa, pensado para preparar uma reunião
    1:1: o que concluiu nos últimos `dias` dias, o que tem em aberto agora
    (e o que está atrasado), e como a quantidade de trabalho ativo que tem
    compara com a média de quem mais tem itens atribuídos — para ajudar a
    perceber se a carga está ajustada. Ignora tudo o que já está numa
    coluna/lista de estado terminal (Perdido, Vendido, Done, ...) — não
    conta como trabalho em aberto nem entra na carga de trabalho, mesmo que
    a Basecamp não o marque como "completed". `nome` é um termo de pesquisa
    (não precisa de ser o nome completo)."""
    termo = _normalizar(nome)

    def _e_da_pessoa(item: dict) -> bool:
        return any(termo in _normalizar(p["name"]) for p in item.get("assignees", []))

    ativos = [i for i in _itens_ativos() if not _em_coluna_terminal(i)]
    itens_pessoa = [i for i in ativos if _e_da_pessoa(i)]
    concluidos_pessoa = [_formatar_item(i) for i in _concluidos_recentemente(dias) if _e_da_pessoa(i)]

    if not itens_pessoa and not concluidos_pessoa:
        return {"erro": f"não encontrei nenhum item (em aberto ou concluído recentemente) "
                        f"atribuído a alguém que corresponda a {nome!r}"}

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
        "concluido_ultimos_dias": {
            "dias": dias,
            "total": len(concluidos_pessoa),
            "itens": concluidos_pessoa[:40],
        },
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
    """Lê os comentários já existentes numa tarefa/card (comments_url vem de tarefas_e_cards_atrasados)."""
    comentarios = _get_paginado(comments_url)
    return [{"autor": (c.get("creator") or {}).get("name"), "conteudo": c.get("content"),
             "criado_em": c.get("created_at")} for c in comentarios]

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

def comentar(recording_id: int, texto: str):
    """Publica um comentário numa tarefa/card."""
    r = httpx.post(f"{_base_url()}/recordings/{recording_id}/comments.json",
                   headers=_headers(), json={"content": _markdown_para_basecamp(texto)}, timeout=30)
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
    pelo nome — usado para publicar no mural de projetos que não sejam a
    Gestão (ex: o mural próprio da Ecos Largos, só visível à equipa deles)."""
    termo = projeto.lower().strip()
    for p in listar_projetos():
        if termo not in p["name"].lower():
            continue
        for ferramenta in p.get("dock", []):
            if ferramenta.get("name") == "message_board" and ferramenta.get("enabled"):
                return p["id"], ferramenta["id"]
        raise ValueError(f"o projeto {p['name']!r} não tem Mural (message board) ativado")
    raise ValueError(f"nenhum projeto encontrado para {projeto!r}")

def publicar_mural(assunto: str, mensagem: str, projeto: str = "Gestão"):
    """Publica uma mensagem no Mural de um projeto (visível a quem tem
    acesso a esse projeto). Por omissão, o mural da Gestão (toda a equipa da
    Interior Guider); passa `projeto` para publicar no mural de outro
    projeto (ex: "Ecos Largos")."""
    if projeto.strip().lower() == "gestão":
        bucket_id, board_id = MURAL_BUCKET_ID, MURAL_BOARD_ID
    else:
        bucket_id, board_id = _resolver_mural(projeto)
    r = httpx.post(f"{_base_url()}/buckets/{bucket_id}/message_boards/{board_id}/messages.json",
                   headers=_headers(),
                   json={"subject": assunto, "content": _markdown_para_basecamp(mensagem), "status": "active"},
                   timeout=30)
    r.raise_for_status()
    return r.json()

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

TTL_PESSOAS_PROJETO = 3600  # 1h — a equipa de um projeto não muda de hora a hora

def pessoas_projeto(projeto: str) -> list[dict]:
    """Pessoas com acesso a um projeto específico do Basecamp (pelo nome) —
    usado para a Alma saber automaticamente quem pertence a que equipa (ex:
    Ecos Largos, uma equipa parceira gerida no mesmo Basecamp mas à parte da
    Interior Guider), sem precisar de uma lista de nomes fixa no código."""
    chave = f"pessoas_{projeto.lower().strip()}"
    if chave in _cache:
        ts, pessoas = _cache[chave]
        if time.time() - ts < TTL_PESSOAS_PROJETO:
            return pessoas
    termo = projeto.lower().strip()
    encontrados = [p for p in listar_projetos() if termo in p["name"].lower()]
    pessoas = _get_paginado(f"{_base_url()}/buckets/{encontrados[0]['id']}/people.json") if encontrados else []
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
        "description": "Dá um panorama de uma pessoa da equipa no Basecamp, pensado para preparar uma reunião individual (1:1): o que concluiu nos últimos dias (por omissão, 7 — a última semana), o que tem em aberto agora e o que está atrasado, e como a quantidade de trabalho ativo que tem compara com a média de quem tem itens atribuídos (para ajudar a avaliar se a carga está ajustada). Usa isto quando pedirem um resumo de uma pessoa específica antes de uma reunião com ela. `nome` é um termo de pesquisa (não precisa de ser o nome completo).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string"},
                "dias": {"type": "integer", "description": "Quantos dias para trás considerar como \"semana anterior\" — por omissão 7"}
            },
            "required": ["nome"]
        }
    }
]
