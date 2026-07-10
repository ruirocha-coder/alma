# agents/resumo_semanal_basecamp.py — resumo semanal de atividade, publicado
# no Mural (visível a toda a equipa), com sugestões de melhoria.
import threading
from persona import PERSONA
from agents.base import client
from tools import basecamp

_a_correr = threading.Lock()

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

def correr_resumo_semanal():
    """Gera e publica no Mural o resumo semanal de atividade. Pensado para
    correr uma vez por semana (agendado), mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[resumo_semanal] já há uma corrida em curso — ignorado")
        return

    try:
        try:
            atrasados = basecamp.tarefas_e_cards_atrasados()
        except Exception as e:
            print(f"[resumo_semanal] não foi possível obter tarefas do Basecamp: {e!r}")
            return

        texto = _gerar_resumo(atrasados)
        basecamp.publicar_mural("Resumo semanal de atividade", texto)
        print("[resumo_semanal] publicado no mural")
    except Exception:
        import traceback
        print(f"[resumo_semanal] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()
