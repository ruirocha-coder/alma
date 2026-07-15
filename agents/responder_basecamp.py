import re, traceback
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
    try:
        _processar(payload)
    except Exception:
        # nunca falhar em silêncio: isto corre numa thread em segundo plano,
        # uma exceção aqui não aparece em lado nenhum a não ser que a apanhemos.
        print(f"[responder_basecamp] ERRO inesperado a processar webhook: {traceback.format_exc()}")

def _processar(payload: dict):
    recording = payload.get("recording") or {}
    kind = (payload.get("kind") or "").lower()
    criador = recording.get("creator") or payload.get("creator") or {}
    print(f"[responder_basecamp] evento recebido: kind={kind!r} "
          f"recording_id={recording.get('id')} type={recording.get('type')} "
          f"criador={criador.get('name')!r} tem_content={'content' in recording} "
          f"tem_parent={'parent' in recording}")

    o_meu_id = basecamp.meu_perfil()["id"]
    if criador.get("id") == o_meu_id:
        print("[responder_basecamp] ignorado: é a própria Alma (evita ciclos)")
        return

    # o conteúdo embutido no payload do webhook vem sem a expansão da menção
    # (falta a legenda com o nome da pessoa mencionada, ex: "Alma") — só a API
    # devolve a representação completa e atual do registo, por isso vai
    # sempre lá buscar antes de decidir se há menção, mesmo que o payload já
    # pareça ter conteúdo e alvo.
    if recording.get("url"):
        try:
            recording = basecamp.obter_recording(recording["url"])
        except Exception as e:
            print(f"[responder_basecamp] não consegui reobter o registo completo: {e!r}")

    if "comment" in kind:
        evento_id = recording.get("id")
        texto_bruto = recording.get("content") or ""
        alvo = recording.get("parent") or {}
    else:
        # menção dentro do próprio card/tarefa (título ou descrição), não numa resposta
        evento_id = recording.get("id")
        texto_bruto = f"{recording.get('content', '')} {recording.get('title', '')}"
        alvo = recording

    if not evento_id or not _menciona_alma(_texto_simples(texto_bruto)):
        print(f"[responder_basecamp] ignorado: sem menção à Alma no texto ({_texto_simples(texto_bruto)[:120]!r})")
        return
    alvo_id = alvo.get("id")
    if not alvo_id:
        print(f"[responder_basecamp] ignorado: menção sem alvo claro (kind={kind})")
        return
    if db.evento_ja_processado(evento_id):
        print(f"[responder_basecamp] ignorado: evento {evento_id} já processado")
        return

    # quando a menção vem de um comentário, "alvo" é só a referência resumida
    # ao pai (id/título/tipo/url) — não tem as notas da tarefa/card. Vai
    # sempre buscar o registo completo do alvo antes de escrever o contexto,
    # exceto quando já o temos (menção dentro do próprio card/tarefa).
    alvo_completo = alvo
    if "description" not in alvo and alvo.get("url"):
        try:
            alvo_completo = basecamp.obter_recording(alvo["url"])
        except Exception as e:
            print(f"[responder_basecamp] não consegui reobter as notas do alvo: {e!r}")

    comentarios = basecamp.ler_comentarios(f"{basecamp._base_url()}/recordings/{alvo_id}/comments.json")
    titulo = alvo_completo.get("title") or alvo.get("title") or "(sem título)"
    notas = _texto_simples(alvo_completo.get("description", ""))
    historico = "\n".join(f"- {c['autor']}: {c['conteudo']}" for c in comentarios) or "(sem comentários ainda)"
    contexto = f"""Foste mencionada nesta tarefa/card do Basecamp: {titulo}

Notas da tarefa/card:
{notas or "(sem notas)"}

Conversa/comentários existentes:
{historico}"""

    utilizador_basecamp = f"{criador.get('name', 'alguém')} (Basecamp)"
    resposta = responder(utilizador_basecamp, [{"role": "user", "content": contexto}])
    basecamp.comentar(alvo_id, resposta)
    db.registar_evento_processado(evento_id, resposta)
    print(f"[responder_basecamp] respondido a {criador.get('name')} em '{titulo}'")
