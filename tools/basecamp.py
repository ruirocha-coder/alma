# tools/basecamp.py — API do Basecamp (OAuth2 com refresh automático).
#
# Ao contrário do BigCommerce, o Basecamp não usa um token fixo: o access_token
# expira ao fim de ~2 semanas. Guardamos aqui só o refresh_token (não expira) e
# trocamo-lo por um access_token novo sempre que necessário, em memória.
import os, time
from datetime import date
import httpx

_cache = {}  # {chave: (timestamp, valor)}
TOKEN_URL = "https://launchpad.37signals.com/authorization/token"
TIPOS_MONITORIZADOS = ("Todo", "Kanban::Card")

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

def tarefas_e_cards_atrasados() -> list[dict]:
    """Tarefas (to-dos) e cards, de todos os projetos, com prazo ultrapassado e não concluídos."""
    hoje = date.today()
    atrasados = []
    for tipo in TIPOS_MONITORIZADOS:
        # completed=false evita percorrer todo o histórico de tarefas já
        # concluídas — só traz o que ainda está em aberto.
        itens = _get_paginado(f"{_base_url()}/projects/recordings.json",
                              params={"type": tipo, "status": "active", "completed": "false"},
                              etiqueta=tipo)
        print(f"[basecamp] {tipo}: {len(itens)} em aberto, a filtrar por prazo...")
        for item in itens:
            prazo = item.get("due_on")
            if not prazo or item.get("completed"):
                continue
            if date.fromisoformat(prazo) >= hoje:
                continue
            atrasados.append({
                "id": item["id"],
                "tipo": "tarefa" if tipo == "Todo" else "card",
                "titulo": item.get("title") or item.get("content") or "(sem título)",
                "projeto": (item.get("bucket") or {}).get("name"),
                "prazo": prazo,
                "dias_atraso": (hoje - date.fromisoformat(prazo)).days,
                "url": item.get("app_url"),
                "comments_count": item.get("comments_count", 0),
                "comments_url": item.get("comments_url"),
            })
    return atrasados

def ler_comentarios(comments_url: str) -> list[dict]:
    """Lê os comentários já existentes numa tarefa/card (comments_url vem de tarefas_e_cards_atrasados)."""
    comentarios = _get_paginado(comments_url)
    return [{"autor": (c.get("creator") or {}).get("name"), "conteudo": c.get("content"),
             "criado_em": c.get("created_at")} for c in comentarios]

def comentar(recording_id: int, texto: str):
    """Publica um comentário numa tarefa/card. Única ação de escrita desta integração."""
    r = httpx.post(f"{_base_url()}/recordings/{recording_id}/comments.json",
                   headers=_headers(), json={"content": texto}, timeout=30)
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
