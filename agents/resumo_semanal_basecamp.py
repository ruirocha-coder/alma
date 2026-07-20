# agents/resumo_semanal_basecamp.py — resumo semanal de atividade, publicado
# no Mural (visível a toda a equipa), com sugestões de melhoria.
#
# A Ecos Largos é uma equipa parceira à parte (projetos e mural próprios) —
# os atrasos dela não devem aparecer misturados no resumo da Boa Safra/
# Interior Guider, nem vice-versa. Por isso há duas corridas independentes,
# cada uma filtrada aos projetos da sua equipa e publicada no mural certo.
import threading
from persona import PERSONA
from agents.base import client
from tools import basecamp

_a_correr_interior_guider = threading.Lock()
_a_correr_ecos_largos = threading.Lock()

def _e_projeto_ecos_largos(nome_projeto: str) -> bool:
    return "ecos largos" in (nome_projeto or "").lower()

MISSAO_RESUMO_SEMANAL = PERSONA + """

Modo atual: resumo semanal de atividade para toda a equipa, publicado no
Mural do Basecamp. Vais escrever UMA mensagem com base no estado atual das
tarefas e cards em atraso (dados abaixo).

Regras desta mensagem:
- Tom calmo, direto e construtivo — nunca acusatório, isto é lido por toda
  a equipa.
- Resume o panorama geral (quantos itens em atraso, quais os projetos mais
  afetados) sem listar cada um exaustivamente.
- Termina com 2 a 3 sugestões concretas de melhoria, baseadas em padrões que
  vires nos dados (ex: um projeto acumula muitos atrasos, um tipo de tarefa
  repete-se).
- Usa markdown (títulos, negrito, listas) — vai ser convertido em
  formatação real no Basecamp.
- Assina sempre como "— Alma"."""

def _gerar_resumo(atrasados: list[dict]) -> str:
    por_projeto = {}
    for item in atrasados:
        por_projeto.setdefault(item["projeto"], []).append(item)
    resumo_projetos = "\n".join(
        f"- {projeto}: {len(itens)} em atraso (o mais antigo tem {max(i['dias_atraso'] for i in itens)} dias)"
        for projeto, itens in sorted(por_projeto.items(), key=lambda x: -len(x[1]))
    ) or "(nenhum item em atraso esta semana)"

    contexto = f"""Total de tarefas/cards em atraso: {len(atrasados)}

Por projeto:
{resumo_projetos}"""

    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system=MISSAO_RESUMO_SEMANAL,
        messages=[{"role": "user", "content": contexto}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def _correr(lock: threading.Lock, etiqueta: str, filtro, projeto_mural: str):
    """Núcleo partilhado pelas duas corridas: só muda o filtro dos atrasados,
    o lock (para não sobrepor duas corridas da mesma equipa) e o mural onde
    fica publicado o resumo."""
    if not lock.acquire(blocking=False):
        print(f"[resumo_semanal:{etiqueta}] já há uma corrida em curso — ignorado")
        return

    try:
        try:
            atrasados = [i for i in basecamp.tarefas_e_cards_atrasados() if filtro(i["projeto"])]
        except Exception as e:
            print(f"[resumo_semanal:{etiqueta}] não foi possível obter tarefas do Basecamp: {e!r}")
            return

        texto = _gerar_resumo(atrasados)
        basecamp.publicar_mural("Resumo semanal de atividade", texto, projeto=projeto_mural)
        print(f"[resumo_semanal:{etiqueta}] publicado no mural")
    except Exception:
        import traceback
        print(f"[resumo_semanal:{etiqueta}] ERRO inesperado: {traceback.format_exc()}")
    finally:
        lock.release()

def correr_resumo_semanal():
    """Gera e publica no Mural da Gestão (Interior Guider) o resumo semanal de
    atividade — só dos projetos da Interior Guider, nunca da Ecos Largos, que
    tem a sua própria corrida e o seu próprio mural (ver
    correr_resumo_semanal_ecos_largos). Pensado para correr uma vez por
    semana (agendado), mas pode ser disparado manualmente."""
    _correr(_a_correr_interior_guider, "interior_guider",
            lambda projeto: not _e_projeto_ecos_largos(projeto), "Gestão")

def correr_resumo_semanal_ecos_largos():
    """Gera e publica no Mural da Ecos Largos o resumo semanal de atividade —
    só dos projetos da Ecos Largos, separado do resumo da Interior Guider."""
    _correr(_a_correr_ecos_largos, "ecos_largos", _e_projeto_ecos_largos, "Ecos Largos")
