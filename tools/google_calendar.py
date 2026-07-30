# tools/google_calendar.py — API do Google Calendar via conta de serviço
# (service account), para a sincronização unidirecional Basecamp -> Google
# Calendar (ver agents/sincronizacao_calendario.py), pedido do Rui
# (2026-07-29).
#
# Autenticação feita à mão (JWT assinado com a chave privada da conta de
# serviço, trocado por um access_token) em vez de google-api-python-client
# ou google-auth: essas bibliotecas trazem o seu próprio transporte HTTP
# (requests), que esta aplicação não usa em mais lado nenhum (tudo o resto
# usa httpx) — mesmo padrão de troca de token com cache em memória já usado
# em tools/basecamp.py::_access_token.
import json, os, time
import httpx
import jwt as pyjwt

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTOS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendario_id}/events"
SCOPE = "https://www.googleapis.com/auth/calendar"

# colorId dos eventos do Google Calendar (paleta fixa da API, 1-11 — sem
# suporte a cor arbitrária/hex): "Peacock" é o azul, pedido do Rui
# (2026-07-29) para os eventos de deslocação ("Viagem: X -> Y") se
# distinguirem visualmente dos de entrega no calendário.
COR_AZUL_VIAGEM = "7"

_cache = {}


def _credenciais() -> dict:
    return json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])


def _calendario_id() -> str:
    return os.environ["GOOGLE_CALENDAR_ID_ENTREGAS"]


def _access_token() -> str:
    if "access_token" in _cache:
        token, expira_em = _cache["access_token"]
        if time.time() < expira_em - 60:
            return token
    cred = _credenciais()
    agora = int(time.time())
    assertion = pyjwt.encode(
        {
            "iss": cred["client_email"],
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": agora,
            "exp": agora + 3600,
        },
        cred["private_key"],
        algorithm="RS256",
    )
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }, timeout=30)
    r.raise_for_status()
    dados = r.json()
    token = dados["access_token"]
    _cache["access_token"] = (token, time.time() + dados.get("expires_in", 3600))
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}


def _corpo_evento(titulo: str, inicio_iso: str, fim_iso: str, descricao: str, cor_id: str = None,
                  localizacao: str = None) -> dict:
    # inicio_iso/fim_iso já vêm com o fuso horário incluído (ver
    # tools.agendamento_logistica.horario_para_iso) — o Calendar API aceita
    # "dateTime" em RFC3339 com offset diretamente, sem "timeZone" à parte.
    corpo = {
        "summary": titulo,
        "description": descricao or "",
        "start": {"dateTime": inicio_iso},
        "end": {"dateTime": fim_iso},
    }
    if cor_id:
        corpo["colorId"] = cor_id
    if localizacao:
        corpo["location"] = localizacao
    return corpo


def criar_evento(titulo: str, inicio_iso: str, fim_iso: str, descricao: str = "", cor_id: str = None,
                 localizacao: str = None) -> dict:
    r = httpx.post(
        EVENTOS_URL.format(calendario_id=_calendario_id()),
        headers=_headers(),
        json=_corpo_evento(titulo, inicio_iso, fim_iso, descricao, cor_id, localizacao), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def atualizar_evento(google_event_id: str, titulo: str, inicio_iso: str, fim_iso: str,
                     descricao: str = "", cor_id: str = None, localizacao: str = None) -> dict:
    r = httpx.patch(
        f"{EVENTOS_URL.format(calendario_id=_calendario_id())}/{google_event_id}",
        headers=_headers(),
        json=_corpo_evento(titulo, inicio_iso, fim_iso, descricao, cor_id, localizacao), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def eliminar_evento(google_event_id: str) -> None:
    r = httpx.delete(
        f"{EVENTOS_URL.format(calendario_id=_calendario_id())}/{google_event_id}",
        headers=_headers(), timeout=30,
    )
    # 404/410: já não existe do lado do Google (ex: apagado manualmente por
    # alguém) — não é um erro para uma sincronização que só quer garantir
    # que o evento deixa de lá estar.
    if r.status_code not in (204, 404, 410):
        r.raise_for_status()
