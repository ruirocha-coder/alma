# agents/resumo_semanal_basecamp.py — resumo semanal de atividade, publicado
# no Mural (visível a toda a equipa), com sugestões de melhoria.
#
# A Ecos Largos é uma equipa parceira à parte (projetos e mural próprios) —
# os atrasos dela não devem aparecer misturados no resumo da Boa Safra/
# Interior Guider, nem vice-versa. Por isso há duas corridas independentes,
# cada uma filtrada aos projetos da sua equipa e publicada no mural certo.
import threading
from datetime import date, timedelta
from persona import PERSONA
from agents.base import client
from tools import basecamp, ecos_largos

_a_correr_interior_guider = threading.Lock()
_a_correr_ecos_largos = threading.Lock()

def _semana_passada() -> tuple:
    """Segunda a sexta da semana que acabou de terminar — calculado aqui,
    nunca pelo modelo (a mesma razão de sempre: aritmética de datas não
    se confia à IA, ver tools/ecos_largos._semana_de). Bug real (Rui,
    2026-07-27): o resumo semanal nunca recebia a data de hoje no
    contexto, e o modelo escreveu por iniciativa própria um cabeçalho
    "Semana de ___", sem saber a data — como não sabia, e a persona pede
    honestidade epistémica, escreveu literalmente "(data atual não
    disponível no contexto — preencher antes de publicar)" e isso foi
    publicado a sério no Mural, visível a toda a equipa.

    Bug real (Beatriz, 2026-08-04): esta função chamava-se _semana_atual
    e devolvia a semana CORRENTE (segunda a sexta a partir de hoje) — como
    o resumo corre à segunda-feira de manhã, isso é a semana que está
    mesmo agora a começar, não a que passou. O cabeçalho do resumo
    (ex: "Semana de 03/08 a 07/08", publicado a 3/08) ficava a descrever
    dias ainda por acontecer, e incoerente com a secção de produção logo
    a seguir, que mostra corretamente a semana anterior (ver
    tools/ecos_largos._semana_de, usada com "semana_passada"). Agora usa o
    mesmo recuo de 7 dias que essa função já usava."""
    hoje = date.today()
    inicio_semana_corrente = hoje - timedelta(days=hoje.weekday())
    inicio = inicio_semana_corrente - timedelta(days=7)
    return inicio, inicio + timedelta(days=4)

def _e_projeto_ecos_largos(nome_projeto: str) -> bool:
    return "ecos largos" in (nome_projeto or "").lower()

# pedido explícito do Rui (2026-07-27): o resumo semanal da Ecos Largos
# tinha só dados do Basecamp (tarefas/cards em atraso) — nunca incluía
# nada do dashboard de produção, apesar de ser a mesma equipa e o mesmo
# Mural. Junta-se aqui o total de produção da semana passada (input/
# output, já calculado em Python — nunca deixar o modelo somar isto, ver
# tools/ecos_largos._totais_intervalo) como mais uma secção do mesmo
# resumo, em vez de ficar espalhado por dois posts sem ligação entre si.
def _texto_producao_semana_passada() -> str:
    resultado = ecos_largos.ler_dashboard_producao_intervalo(periodo="semana_passada")
    if "erro" in resultado:
        return f"(não foi possível obter os dados de produção da semana passada: {resultado['erro']})"
    totais = resultado.get("totais") or {}
    if not totais:
        return "(sem dados de produção disponíveis para a semana passada)"
    linhas = [f"Semana de {resultado['inicio']} a {resultado['fim']}:"]
    if "input_m3" in totais:
        linhas.append(f"- Entrada de madeira: {totais['input_m3']:.2f} m³".replace(".", ","))
    if "output_m3" in totais:
        linhas.append(f"- Saída/produção: {totais['output_m3']:.2f} m³".replace(".", ","))
    return "\n".join(linhas)

MISSAO_RESUMO_SEMANAL = PERSONA + """

Modo atual: resumo semanal de atividade para toda a equipa, publicado no
Mural do Basecamp. Vais escrever UMA mensagem com base no estado atual das
tarefas e cards em atraso (dados abaixo). Quando o contexto também tiver
uma secção "Produção da semana passada", NÃO a repitas nem escrevas tu
mesma uma secção com esses números — isso é acrescentado à tua mensagem
depois, por código, sempre com os valores exatos (ver histórico: confiar
no modelo para reproduzir esses números à letra já falhou, umas vezes
saía, outras não). Usa esses dados só como contexto para as sugestões
finais, se fizer sentido (ex: referir que a produção ficou abaixo do
esperado), sem citares os números em si.

Regras desta mensagem:
- Começa sempre com um cabeçalho "Semana de {data_inicio} a {data_fim}",
  usando EXATAMENTE o intervalo de datas dado no contexto abaixo — nunca
  tentes calcular ou adivinhar a data tu mesma (não sabes a data de hoje
  com fiabilidade), e nunca escrevas um aviso a dizer que a data não está
  disponível: ela está sempre no contexto, junto aos dados de atrasos.
- Tom calmo, direto e construtivo — nunca acusatório, isto é lido por toda
  a equipa.
- Resume o panorama geral (quantos itens em atraso, quais os projetos mais
  afetados) sem listar cada um exaustivamente.
- Termina com 2 a 3 sugestões concretas de melhoria, baseadas em padrões que
  vires nos dados (ex: um projeto acumula muitos atrasos, um tipo de tarefa
  repete-se, ou a produção ficou abaixo do esperado).
- Usa markdown (títulos, negrito, listas) — vai ser convertido em
  formatação real no Basecamp.
- Assina sempre como "— Alma"."""

def _gerar_resumo(atrasados: list[dict], producao_texto: str = None) -> str:
    por_projeto = {}
    for item in atrasados:
        por_projeto.setdefault(item["projeto"], []).append(item)
    resumo_projetos = "\n".join(
        f"- {projeto}: {len(itens)} em atraso (o mais antigo tem {max(i['dias_atraso'] for i in itens)} dias)"
        for projeto, itens in sorted(por_projeto.items(), key=lambda x: -len(x[1]))
    ) or "(nenhum item em atraso)"

    inicio_semana, fim_semana = _semana_passada()
    contexto = f"""Semana de {inicio_semana.strftime('%d/%m/%Y')} a {fim_semana.strftime('%d/%m/%Y')}

Total de tarefas/cards em atraso: {len(atrasados)}

Por projeto:
{resumo_projetos}"""
    if producao_texto:
        contexto += f"\n\nProdução da semana passada:\n{producao_texto}"

    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system=MISSAO_RESUMO_SEMANAL,
        messages=[{"role": "user", "content": contexto}]
    )
    texto = "".join(b.text for b in resposta.content if b.type == "text").strip()

    # a secção de produção é sempre acrescentada aqui, por código — pedido
    # da Beatriz (2026-08-04), depois do resumo de 3/agosto ter saído sem
    # ela: pedir ao modelo para "incluir uma secção própria com esses
    # números" (ver histórico deste ficheiro) não garantia que saísse
    # sempre; os valores em si nunca dependeram do modelo (calculados em
    # tools.ecos_largos._totais_intervalo), só faltava parar de confiar
    # nele para os reproduzir na mensagem final.
    if producao_texto:
        texto += f"\n\n## Produção da semana passada\n{producao_texto}"
    return texto

def _correr(lock: threading.Lock, etiqueta: str, filtro, projeto_mural: str, incluir_producao: bool = False):
    """Núcleo partilhado pelas duas corridas: só muda o filtro dos atrasados,
    o lock (para não sobrepor duas corridas da mesma equipa), o mural onde
    fica publicado o resumo, e se inclui também a produção da semana
    passada (só faz sentido para a Ecos Largos — a Interior Guider não tem
    dashboard de produção)."""
    if not lock.acquire(blocking=False):
        print(f"[resumo_semanal:{etiqueta}] já há uma corrida em curso — ignorado")
        return

    try:
        try:
            atrasados = [i for i in basecamp.tarefas_e_cards_atrasados() if filtro(i["projeto"])]
        except Exception as e:
            print(f"[resumo_semanal:{etiqueta}] não foi possível obter tarefas do Basecamp: {e!r}")
            return

        producao_texto = None
        if incluir_producao:
            try:
                producao_texto = _texto_producao_semana_passada()
            except Exception as e:
                print(f"[resumo_semanal:{etiqueta}] não consegui obter os dados de produção: {e!r}")

        texto = _gerar_resumo(atrasados, producao_texto)
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
    tarefas/cards da Ecos Largos em atraso, e (pedido do Rui, 2026-07-27) a
    produção da semana passada, no mesmo resumo — separado do resumo da
    Interior Guider."""
    _correr(_a_correr_ecos_largos, "ecos_largos", _e_projeto_ecos_largos, "Ecos Largos",
           incluir_producao=True)
