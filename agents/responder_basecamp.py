import re
from bs4 import BeautifulSoup
from persona import PERSONA
from agents.base import correr_agente, TOOLS_COMUNS
from tools import basecamp
import db

MISSAO_BASECAMP = PERSONA + """

Modo atual: foste mencionada diretamente num comentário ou numa tarefa/card
do Basecamp. Alguém da equipa dirigiu-se a ti — responde ao pedido dela,
usando o contexto da tarefa/card e da conversa fornecidos abaixo. Publicas
UM comentário de resposta, direto e útil; usa as ferramentas disponíveis
(catálogo, páginas do site, memória) sempre que ajudarem a responder melhor.

Reforço das regras: a única ação externa que executas é publicar este
comentário de resposta — nunca alteres prazos, responsáveis, conteúdo de
tarefas ou qualquer outro dado no Basecamp. Se o pedido implicar uma ação
que não seja responder com informação (ex: "atualiza o prazo", "fecha esta
tarefa", "manda um email"), explica que não podes fazer isso diretamente e
sugere o que a pessoa deve fazer a seguir.

Não escrevas saudações nem te apresentes — vai direto à resposta."""

def responder(utilizador: str, mensagens: list) -> str:
    return correr_agente(MISSAO_BASECAMP, TOOLS_COMUNS, mensagens, utilizador)

def _texto_simples(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

def _menciona_alma(texto: str) -> bool:
    return re.search(r"\balma\b", texto, re.IGNORECASE) is not None

def processar_evento_webhook(payload: dict):
    """Reage a um evento de webhook do Basecamp (comentário criado, ou tarefa/card
    criado/atualizado) que mencione a Alma pelo nome — lê o contexto e responde."""
    recording = payload.get("recording") or {}
    kind = (payload.get("kind") or "").lower()
    criador = recording.get("creator") or payload.get("creator") or {}

    try:
        o_meu_id = basecamp.meu_perfil()["id"]
    except Exception as e:
        print(f"[responder_basecamp] não foi possível confirmar a própria identidade: {e!r}")
        return
    if criador.get("id") == o_meu_id:
        return  # nunca reagir aos seus próprios comentários/tarefas — evita ciclos

    if "comment" in kind:
        evento_id = recording.get("id")
        texto_bruto = recording.get("content", "")
        alvo = recording.get("parent") or {}
    else:
        # menção dentro do próprio card/tarefa (título ou descrição), não numa resposta
        evento_id = recording.get("id")
        texto_bruto = f"{recording.get('content', '')} {recording.get('title', '')}"
        alvo = recording

    if not evento_id or not _menciona_alma(_texto_simples(texto_bruto)):
        return
    alvo_id = alvo.get("id")
    if not alvo_id:
        print(f"[responder_basecamp] menção sem alvo claro (kind={kind}), ignorado")
        return
    if db.evento_ja_processado(evento_id):
        return

    try:
        comentarios = basecamp.ler_comentarios(f"{basecamp._base_url()}/recordings/{alvo_id}/comments.json")
        titulo = alvo.get("title") or "(sem título)"
        historico = "\n".join(f"- {c['autor']}: {c['conteudo']}" for c in comentarios) or "(sem comentários ainda)"
        contexto = f"""Foste mencionada nesta tarefa/card do Basecamp: {titulo}

Conteúdo/descrição: {_texto_simples(alvo.get('content', ''))}

Conversa/comentários existentes:
{historico}"""

        utilizador_basecamp = f"{criador.get('name', 'alguém')} (Basecamp)"
        resposta = responder(utilizador_basecamp, [{"role": "user", "content": contexto}])
        basecamp.comentar(alvo_id, resposta)
        db.registar_evento_processado(evento_id, resposta)
        print(f"[responder_basecamp] respondido a {criador.get('name')} em '{titulo}'")
    except Exception as e:
        print(f"[responder_basecamp] falhou para evento {evento_id}: {e!r}")
