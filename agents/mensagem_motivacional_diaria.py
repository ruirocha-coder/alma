# agents/mensagem_motivacional_diaria.py — mensagem diária, sóbria e
# motivacional, publicada no Mural da Gestão antes do começo do dia de
# trabalho. Pedido explícito do Rui e da Beatriz (2026-08-05): a Alma
# posicionada como a abelha-mãe da colmeia que é a equipa, numa perspetiva
# estoica, taoista e de gestão lean — nunca um relatório, nunca números.
import threading
from datetime import date

from persona import PERSONA
from agents.base import client, TOOLS_INTERNET
from tools import basecamp
import db

_a_correr = threading.Lock()

# só estes três — pedido explícito do Rui (2026-08-05): "Boa Safra" e
# "Interior Guider" são projetos reais no Basecamp (não aparecem em mais
# nenhum sítio do código porque nenhuma automação anterior os usava).
PROJETOS = ["Boa Safra", "Interior Guider", "Gestão"]

MISSAO_MENSAGEM_DIARIA = PERSONA + """

Modo atual: mensagem diária, publicada no Mural da Gestão (visível a toda a
equipa), antes do começo do dia de trabalho.

O que esta mensagem É:
- Uma nota breve e sóbria para começar o dia — nunca um relatório, nunca uma
  lista de tarefas, nunca números ou nomes de projetos citados. O estado
  geral do trabalho (mais fluido, mais lento, mais parado, mais disperso) só
  serve de pano de fundo que orienta o tom, nunca conteúdo citado.
- Escrita a partir de três perspetivas em conjunto: estoica (aceitar o que
  não se controla, focar no que está nas mãos de cada um, calma diante da
  dificuldade), taoista (fluir com o que surge, sem forçar, valorizar o
  simples e o natural), e de gestão lean (eliminar o que não serve,
  valorizar o trabalho contínuo e sem desperdício mais do que grandes
  gestos ou picos de esforço).
- Escrita como quem está dentro da equipa, não por cima dela — uma presença
  atenta e constante, como a abelha-mãe de uma colmeia: não manda, não
  vigia, sustenta o ritmo colectivo e repara no que cada abelha faz pelo
  todo. Usa esta imagem com moderação, no máximo uma vez por mensagem,
  nunca de forma efusiva ou infantil — é uma presença discreta, não um
  tema repetido.
- Pode deixar-se influenciar, sem nunca citar diretamente, pelo tempo que
  vai fazer, pela estação do ano, por uma festa/feriado próximo, ou por uma
  notícia do dia — como uma imagem ou analogia breve, nunca como boletim
  meteorológico ou noticiário.

Regras de escrita, além do tom de voz geral acima:
- Curta: um a dois parágrafos curtos, nunca mais.
- Nunca nomeies ninguém, nem apontes a nenhuma pessoa ou situação em
  concreto — é para toda a equipa, sem exceções nem casos particulares.
- Nunca uses linguagem motivacional batida ("vamos conseguir", "força",
  "acreditem") nem imperativos ("foca-te", "não desistas") — sóbria
  significa mesmo sóbria, não um poster de escritório.
- Usa markdown simples se ajudar (não é obrigatório) — vai ser convertido
  em formatação real no Basecamp.
- Assina sempre como "— Alma".
"""

def _analisar_projeto(projeto: str, hoje: date) -> str:
    """Lê o estado atual do projeto e compara com a última leitura guardada
    (ver snapshot_diario_projetos) para dar uma evolução real, não só uma
    fotografia isolada de hoje — depois guarda a leitura de hoje, para a
    próxima comparação. Devolve texto só para uso interno (nunca publicado
    tal como está, ver MISSAO_MENSAGEM_DIARIA)."""
    estado = basecamp.estado_projeto_basecamp(projeto)
    if estado.get("erro"):
        return f"{projeto}: {estado['erro']}"

    total_ativos = estado["total_ativos"]
    atrasados = len(estado["atrasados"])
    parados = len(estado["cards_parados_sem_prazo"])
    anterior = db.snapshot_diario_projeto_anterior(projeto, hoje)
    db.guardar_snapshot_diario_projeto(hoje, projeto, total_ativos, atrasados, parados, estado["por_estado"])

    linhas = [f"{projeto}: {total_ativos} cards ativos, {atrasados} atrasados, {parados} parados sem prazo. "
              f"Por estado: {estado['por_estado']}."]
    if anterior:
        linhas.append(
            f"Desde a última leitura ({anterior['data']}): ativos {anterior['total_ativos']} -> {total_ativos}, "
            f"atrasados {anterior['atrasados']} -> {atrasados}, parados {anterior['parados']} -> {parados}."
        )
    else:
        linhas.append("Sem leitura anterior para comparar.")
    return " ".join(linhas)

def _analisar_projetos() -> str:
    hoje = date.today()
    return "\n".join(_analisar_projeto(projeto, hoje) for projeto in PROJETOS)

def _contexto_do_dia() -> str:
    """Pesquisa na internet um resumo factual e muito breve sobre Portugal
    hoje (tempo, estação, festas próximas, notícias principais) — só
    contexto interno para a mensagem final, nunca para citar em concreto.
    web_search/web_fetch correm do lado do servidor da Anthropic (ver
    TOOLS_INTERNET em agents/base.py), por isso só é preciso lidar com
    pause_turn, nunca com tool_use local."""
    mensagens = [{"role": "user", "content": "Contexto de hoje em Portugal."}]
    system = (
        "Pesquisa na internet e devolve um resumo factual muito breve (no "
        "máximo 6 linhas, sem opinião nem floreados) sobre Portugal, para "
        "hoje: previsão do tempo, a estação do ano, se há algum feriado ou "
        "festa/tradição relevante nos próximos dias, e as principais "
        "notícias do dia. Isto é só contexto interno para outra escrita, "
        "não é para mostrar a ninguém tal como está."
    )
    while True:
        resposta = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            system=system, tools=TOOLS_INTERNET, messages=mensagens
        )
        if resposta.stop_reason == "pause_turn":
            mensagens.append({"role": "assistant", "content": resposta.content})
            continue
        return "".join(b.text for b in resposta.content if b.type == "text").strip()

def _gerar_mensagem(analise_projetos: str, contexto_dia: str) -> str:
    entrada = (
        f"Estado geral do trabalho hoje (só para teu conhecimento, nunca para citar em concreto):\n"
        f"{analise_projetos}\n\n"
        f"Contexto do dia em Portugal (idem, só pano de fundo):\n{contexto_dia}"
    )
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system=MISSAO_MENSAGEM_DIARIA,
        messages=[{"role": "user", "content": entrada}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def correr_mensagem_diaria_motivacional():
    """Publica uma mensagem diária motivacional no Mural da Gestão, a partir
    da evolução dos cards nos projetos Boa Safra, Interior Guider e Gestão,
    e do contexto do dia em Portugal. Pensado para correr de segunda a
    sexta às 9h (agendado), mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[mensagem_motivacional_diaria] já há uma corrida em curso — ignorado")
        return
    try:
        analise = _analisar_projetos()
        contexto = _contexto_do_dia()
        texto = _gerar_mensagem(analise, contexto)
        basecamp.publicar_mural("Antes de começar o dia", texto, projeto="Gestão")
        print("[mensagem_motivacional_diaria] publicado no mural da Gestão")
    except Exception:
        import traceback
        print(f"[mensagem_motivacional_diaria] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()
