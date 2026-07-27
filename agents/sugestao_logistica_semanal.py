# agents/sugestao_logistica_semanal.py — sugestão semanal de organização
# das entregas, pedida explicitamente pelo Rui (2026-07-23): toda
# segunda de manhã, publica no Mural "Programação" do projeto Entregas
# uma sugestão de como organizar a semana de entregas, dirigida à
# Conceição Costa (e só a ela).
#
# Modelo CONFIRMADO em 2026-07-23, contra a API real do Basecamp (com
# credenciais partilhadas pelo Rui para o efeito): o `parent` de um card
# em "On Hold" é do tipo "Kanban::OnHold" — um objeto próprio, à parte da
# coluna de região. O seu `title` é sempre genérico ("On hold"), mas o
# seu `url` aponta diretamente para a coluna de região REAL por trás
# dessa secção (confirmado: duas cartas diferentes, uma com url para a
# coluna "Porto", outra para "Produção"). Um diagnóstico anterior
# concluiu (erradamente) que a região não era recuperável — mas nesse
# diagnóstico foi-se um nível a mais na hierarquia (o `parent` do `parent`
# é sim o quadro geral "Logística", mas o PRÓPRIO objeto obtido a partir
# do `url` já É a coluna de região, sem precisar de mais nenhum passo).
# Por isso a região volta a ser lida da estrutura (ver
# _coluna_real_on_hold, em agents/logistica_entregas.py) — só cai na
# classificação por morada (ver _classificar_regiao) quando essa
# resolução falhar de verdade (ex: API indisponível), como rede de
# segurança.
#
# Regras de significado do "On Hold" CONFIRMADAS pelo Rui (2026-07-27) —
# ver tools/logistica.py para o detalhe: por trás de "Produção" significa
# data confirmada com o fornecedor mas ainda não chegou ao armazém (NUNCA
# entra nesta sugestão); por trás de "Assistências" aguarda ser agendada
# (também não é uma entrega, fica de fora); só por trás de uma região
# (Lisboa/Porto/Outro) é que está mesmo pronta a ser entregue.
#
# Esta sugestão organiza: que dia visitar cada região (pelo modelo, sem
# dados reais de distância/tempo — só uma organização sensata), e por
# que ordem dentro de cada dia. Além disso (pedido do Rui, 2026-07-27),
# gera um link de Google Maps por região, com todas as paragens prontas
# a entregar essa semana, para a Conceição validar/editar diretamente no
# Maps antes de sair — ver tools.logistica.gerar_link_google_maps. Não
# tenta replicar a otimização de rota do Google Maps (nunca temos dados
# reais de distância/tempo entre moradas), só constrói o link com as
# paragens certas; a otimização em si faz-se dentro do próprio Maps.
import threading
from datetime import date, timedelta
from agents.base import client
from agents.logistica_entregas import (_extrair_dados_encomenda, _coluna_real_on_hold,
                                       _REGIAO_POR_COLUNA, _formatar_documentos_referencia)
from tools import basecamp, logistica, documentos_referencia

_a_correr = threading.Lock()
MAX_CARDS_POR_CORRIDA = 40

# nome completo tal como já usado nas menções existentes (ver
# tools/logistica.gerar_texto_condicao_fixa) — mantém-se o mesmo em toda
# a aplicação, para a menção ser sempre resolvida para a mesma pessoa.
RESPONSAVEL_MENCAO = "Conceição Costa"

_REGIOES = ("Lisboa", "Porto", "Outro")

_MISSAO_CLASSIFICAR_REGIAO = """Classificas a morada de entrega abaixo
numa destas três regiões: "Lisboa", "Porto", ou "Outro" (qualquer
localização que não seja claramente da área de Lisboa ou do Porto,
incluindo arredores/área metropolitana). Responde só com uma destas três
palavras, nada mais."""

def _classificar_regiao(morada: str) -> str:
    """Rede de segurança para quando a coluna real por trás da secção
    "On Hold" não é uma coluna de região (ex: o card ainda não foi movido
    de "Produção" para a coluna certa, mas já foi marcado pronto) — a
    morada de entrega é o sinal mais fiável que resta nesse caso."""
    if not morada:
        return "Outro"
    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system=_MISSAO_CLASSIFICAR_REGIAO,
        messages=[{"role": "user", "content": morada}]
    )
    escolha = "".join(b.text for b in resposta.content if b.type == "text").strip()
    return escolha if escolha in _REGIOES else "Outro"

def _semana_atual() -> tuple:
    """Segunda a sexta da semana corrente — calculado aqui, nunca pelo
    modelo (a mesma razão de sempre: aritmética de datas não se confia à
    IA, ver tools/ecos_largos._semana_de para o mesmo padrão)."""
    hoje = date.today()
    inicio = hoje - timedelta(days=hoje.weekday())
    return inicio, inicio + timedelta(days=4)

def _formatar_card_pronto(titulo: str, dados: dict) -> str:
    data_entrega = dados.get("data_entrega_cliente")
    return (
        f"- **{titulo}**\n"
        f"  Cliente: {dados.get('cliente') or '(não identificado)'}\n"
        f"  Morada: {dados.get('morada') or '(não identificada — verificar notas do card)'}\n"
        f"  Encomendado: {dados.get('produtos_encomendados') or '(não identificado)'}\n"
        f"  Data prevista de entrega: {data_entrega.isoformat() if data_entrega else '(não identificada)'}"
    )

def _gerar_texto_sugestao(cards_por_regiao: dict, inicio_semana: date, fim_semana: date,
                          documentos_texto: str = None) -> str:
    blocos = [f"### {regiao} ({len(cards)} pronta(s) a entregar)\n" + "\n\n".join(cards)
             for regiao, cards in cards_por_regiao.items() if cards]
    contexto = "\n\n".join(blocos) if blocos else "(nenhum card pronto a entregar esta semana, em nenhuma região)"

    # pedido do Rui (2026-07-24): o documento "Procedimento Tempos de
    # Montagem para Logística" (projeto Alma Data) tem os tempos de
    # montagem por produto — relevante aqui porque afeta quantas entregas
    # cabem num dia e a ordem sensata de visitas, não só a distância entre
    # moradas. Passa-se o texto todo dos documentos de referência (não só
    # este), pela mesma razão de _gerar_texto_fg_h em logistica_entregas.py:
    # o produto encomendado ("produtos_encomendados") é só um resumo em
    # texto livre, por isso o modelo tem de procurar a correspondência ele
    # próprio, com a mesma cautela de nunca inventar um tempo que não
    # encontrar.
    bloco_documentos = f"""

Documentos de referência disponíveis (projeto Alma Data), incluindo o
"Procedimento Tempos de Montagem para Logística" — usa-o para teres em conta
o tempo de montagem previsto de cada entrega ao sugerires quantas cabem num
dia e a ordem de visitas (uma montagem demorada limita quantas mais entregas
cabem nesse dia). Só aplicas um tempo a um produto se conseguires
identificá-lo com confiança a partir do que está descrito em "Encomendado";
se não conseguires, ou o documento não cobrir esse produto, não inventes um
tempo — segue só pela morada/região como até agora:
{documentos_texto}""" if documentos_texto else ""

    missao = f"""Preparas, para a Conceição Costa (responsável pela logística de
entregas da Interior Guider / Boa Safra), uma sugestão semanal de
organização das entregas — a publicar no Mural "Programação" do projeto
Entregas no Basecamp. Semana de {inicio_semana.strftime('%d/%m/%Y')} a
{fim_semana.strftime('%d/%m/%Y')}.

Abaixo estão os cards já em "On Hold" — significa que a encomenda já foi
feita ao fornecedor e o produto já está em armazém, pronto a ser
entregue. Já vêm agrupados por região (Lisboa/Porto/Outro).

{contexto}
{bloco_documentos}

Organiza uma sugestão de calendário para a semana: que dia(s) visitar
cada região (agrupa sempre por região — nunca misturar Lisboa e Porto no
mesmo dia), e dentro de cada dia, sugere uma ordem sensata de visita
pelos endereços (usando o teu conhecimento geral da zona/ruas
mencionadas). Esta parte da tua resposta NÃO é uma rota GPS otimizada
com distâncias/tempos reais — é só uma organização sensata para não
andar às voltas; diz isso claramente se não tiveres informação
suficiente para ordenar com confiança. Para cada entrega, inclui sempre
o cliente, a morada, o que foi encomendado, e a data prevista de
entrega, exatamente como aparecem acima — nunca inventes nem alteres
estes dados.

Não incluas tu mesma nenhum link de Google Maps nem tentes gerar um url
— isso é acrescentado à parte, depois do teu texto, com o trajeto real
por região (ida e volta ao armazém, com todas as paragens); menciona
apenas que o trajeto de Google Maps vem a seguir, para validar/editar
antes de sair.

Se não houver nenhum card pronto nalguma região, ou nenhum de todo, diz
isso claramente em vez de inventar entregas.

Termina sempre a mensagem a mencionar "@Conceição Costa" (e só ela,
nenhuma outra pessoa) — é a destinatária desta sugestão.

Escreve só o texto final da mensagem do mural, sem comentário à parte."""
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        system=missao, messages=[{"role": "user", "content": "Gera a sugestão semanal."}]
    )
    texto = "".join(b.text for b in resposta.content if b.type == "text").strip()
    if RESPONSAVEL_MENCAO not in texto:
        texto += f"\n\n@{RESPONSAVEL_MENCAO}"
    return texto

def _cards_prontos_a_entregar() -> tuple:
    """Lê os cards ativos do projeto "Entregas" e devolve
    (cards_por_regiao, moradas_por_regiao): cards_por_regiao tem o texto
    formatado de cada entrega pronta (cliente/morada/produto/data, para o
    resumo em texto); moradas_por_regiao tem só as moradas em bruto de
    cada uma (para o trajeto do Google Maps) — extraídos na mesma
    passagem, para nunca ler os dados de cada card duas vezes. Só conta
    cards mesmo prontos a entregar (em "On Hold" por trás de uma região —
    nunca por trás de "Produção" ou "Assistências", ver
    tools.logistica.fase_encomenda); a região vem da coluna real por trás
    da secção "On Hold" (com a morada como rede de segurança só quando
    essa resolução falhar — ver _coluna_real_on_hold e
    _classificar_regiao)."""
    itens = [i for i in basecamp._itens_ativos()
            if i.get("type") == "Kanban::Card"
            and ((i.get("bucket") or {}).get("name") or "").strip().lower() == logistica.PROJETO_ENTREGAS.lower()]
    itens = itens[:MAX_CARDS_POR_CORRIDA]

    cards_por_regiao = {regiao: [] for regiao in _REGIOES}
    moradas_por_regiao = {regiao: [] for regiao in _REGIOES}

    for item in itens:
        estado = ((item.get("parent") or {}).get("title") or "").strip()
        if logistica.normalizar_coluna(estado) != logistica.COLUNA_PRONTO_ENTREGA:
            continue  # só cards em "On Hold" interessam a esta sugestão

        coluna_real = _coluna_real_on_hold(item)
        fase = logistica.fase_encomenda(estado, coluna_real)

        # pedido do Rui (2026-07-27): "On Hold" por trás de "Produção"
        # significa data confirmada com o fornecedor mas AINDA NÃO
        # chegou ao armazém — nunca incluir isto na sugestão de
        # entregas (bug real: entrava como se já estivesse pronto).
        # Por trás de "Assistências" também não é uma entrega. Só cai
        # na morada como rede de segurança quando a resolução da
        # coluna real falhar de verdade (ex: API indisponível) —
        # nunca quando ela resolveu com sucesso para algo que
        # confirmadamente não é uma região.
        if fase in ("producao", "assistencia_aguarda_agendamento"):
            continue
        if fase == "outro" and coluna_real is not None:
            continue

        titulo = item.get("title") or item.get("content") or "(sem título)"
        notas = basecamp._texto_simples(item.get("description", ""))
        try:
            dados = _extrair_dados_encomenda(titulo, notas)
        except Exception as e:
            print(f"[sugestao_logistica_semanal] falhou a extrair dados de {item['id']}: {e!r}")
            dados = {}

        if fase == "pronto_entrega":
            regiao = _REGIAO_POR_COLUNA[logistica.normalizar_coluna(coluna_real)]
        else:
            regiao = _classificar_regiao(dados.get("morada"))
        cards_por_regiao[regiao].append(_formatar_card_pronto(titulo, dados))
        if dados.get("morada"):
            moradas_por_regiao[regiao].append(dados["morada"])

    return cards_por_regiao, moradas_por_regiao

def _texto_trajetos_google_maps(moradas_por_regiao: dict) -> str:
    """Constrói, em Python (nunca pedido ao modelo — um url é demasiado
    fácil de corromper se reescrito por um LLM), a secção com os links do
    Google Maps de cada região que tenha entregas prontas esta semana.
    Devolve "" se não houver nenhuma morada disponível em região nenhuma."""
    linhas = []
    for regiao, moradas in moradas_por_regiao.items():
        link = logistica.gerar_link_google_maps(moradas)
        if link:
            linhas.append(f"- **{regiao}** ({len(moradas)} paragem/paragens): {link}")
    if not linhas:
        return ""
    return ("\n\n---\n\n### Trajetos no Google Maps (partida e regresso ao armazém)\n"
            + "\n".join(linhas)
            + "\n\nAbre o link e usa a opção de otimizar a ordem das paragens no Google Maps "
              "para validar/editar o trajeto antes de sair.")

def correr_sugestao_semanal_logistica() -> dict:
    """Uma corrida da sugestão semanal de logística de entregas: lê os
    cards prontos a entregar (ver _cards_prontos_a_entregar), gera o
    texto de organização da semana e um trajeto de Google Maps por
    região (ver _texto_trajetos_google_maps), e publica tudo junto no
    Mural "Programação", dirigido à Conceição Costa. Pensado para correr
    às segundas de manhã (agendado), mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[sugestao_logistica_semanal] já há uma corrida em curso — ignorado")
        return {"erro": "já está a correr uma sugestão semanal"}

    try:
        try:
            cards_por_regiao, moradas_por_regiao = _cards_prontos_a_entregar()
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não foi possível obter os cards do Basecamp: {e!r}")
            return {"erro": str(e)}

        try:
            documentos_texto = _formatar_documentos_referencia(documentos_referencia.documentos_referencia_empresa())
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não consegui ler os documentos de referência: {e!r}")
            documentos_texto = None

        inicio_semana, fim_semana = _semana_atual()
        texto = _gerar_texto_sugestao(cards_por_regiao, inicio_semana, fim_semana, documentos_texto)
        texto += _texto_trajetos_google_maps(moradas_por_regiao)
        basecamp.publicar_mural("Sugestão de logística semanal", texto, projeto=logistica.PROJETO_ENTREGAS)

        contagens = {regiao: len(cards) for regiao, cards in cards_por_regiao.items()}
        total_prontos = sum(contagens.values())
        print(f"[sugestao_logistica_semanal] publicado — {total_prontos} entrega(s) pronta(s): {contagens}")
        return {"total_prontos": total_prontos, "por_regiao": contagens}
    finally:
        _a_correr.release()

def trajetos_logistica_entregas() -> dict:
    """Gera, a pedido (fora do ciclo semanal automático), um link de
    Google Maps por região com todas as entregas prontas a fazer agora —
    pedido explícito do Rui (2026-07-27): a mesma informação da sugestão
    semanal, mas disponível a qualquer momento na conversa, não só às
    segundas de manhã. Usa exatamente a mesma lógica de deteção de cards
    prontos a entregar (ver _cards_prontos_a_entregar), para nunca haver
    duas versões a divergir."""
    cards_por_regiao, moradas_por_regiao = _cards_prontos_a_entregar()
    trajetos = {}
    for regiao, moradas in moradas_por_regiao.items():
        link = logistica.gerar_link_google_maps(moradas)
        if link:
            trajetos[regiao] = {"paragens": len(moradas), "link": link}
    contagens = {regiao: len(cards) for regiao, cards in cards_por_regiao.items()}
    return {"por_regiao": contagens, "trajetos_google_maps": trajetos}
