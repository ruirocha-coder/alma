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
# _coluna_real_on_hold, em agents/logistica_entregas.py). Pedido
# explícito do Rui (2026-07-28): a região de um card é sempre decidida
# pela coluna real do Basecamp, NUNCA pela Alma a adivinhar pela morada —
# mesmo um card cuja morada não pareça "à primeira vista" pertencer a
# essa região pode estar lá de propósito, por ficar no caminho da rota
# dessa região. Por isso, quando a coluna real não pode ser confirmada
# (ex: API indisponível, ou parent sem url), o card fica de fora de
# todas as rotas e é sinalizado para confirmação manual (ver
# _texto_nao_confirmados) — nunca se adivinha a região pela morada.
#
# Regras de significado do "On Hold" CONFIRMADAS pelo Rui (2026-07-27) —
# ver tools/logistica.py para o detalhe: por trás de "Produção" significa
# data confirmada com o fornecedor mas ainda não chegou ao armazém (NUNCA
# entra nesta sugestão); por trás de "Assistências" aguarda ser agendada
# (também não é uma entrega, fica de fora); só por trás de uma região
# (Lisboa/Porto/Outro) é que está mesmo pronta a ser entregue.
#
# Esta sugestão organiza a semana em três camadas: (1) o texto do modelo
# dá contexto geral (volume, notas relevantes) — nunca decide dias/horas;
# (2) um link de Google Maps por região, com todas as paragens prontas a
# entregar essa semana, para a Conceição validar/editar diretamente no
# Maps antes de sair (ver tools.logistica.gerar_link_google_maps — a
# ordem das paragens já vem otimizada via Google Directions API, pedido
# do Rui 2026-07-27; se essa otimização não estiver disponível, o link
# vem na ordem original e continua a poder ser otimizado/editado à mão
# dentro do próprio Maps); (3) uma PROPOSTA DE AGENDAMENTO determinística
# — dia útil + hora de chegada/saída de cada entrega, calculada a partir
# do trajeto real e do tempo de montagem (ver
# _construir_texto_proposta_agendamento e tools/agendamento_logistica.py)
# — pedido explícito do Rui (2026-07-28). É só uma proposta: a Alma nunca
# cria eventos no calendário do projeto Entregas sozinha a partir dela —
# só depois de a Conceição ou a Isa confirmarem (com ou sem ajustes) é
# que agents.agendamento_entregas.criar_eventos_calendario_entregas_restrito
# é chamada, com os dados finais tal como confirmados na conversa.
import threading
from datetime import date, timedelta
from agents.base import client
from agents import estimativa_montagem
from agents.logistica_entregas import (_extrair_dados_encomenda, _coluna_real_on_hold,
                                       _REGIAO_POR_COLUNA, _formatar_documentos_referencia)
from tools import basecamp, logistica, documentos_referencia, tempos_montagem, agendamento_logistica
import db

# limite defensivo contra uma corrida descontrolada — nunca deve ser o
# que decide quais entregas prontas entram (ver nota mais abaixo, no
# corte real de 2026-07-28): o projeto "Entregas" tem, na realidade,
# várias centenas de cards no total (confirmado contra a API real), por
# isso este valor tem de ter folga bem acima do volume normal de
# entregas prontas numa semana qualquer.
_a_correr = threading.Lock()
MAX_CARDS_POR_CORRIDA = 300

# nome completo tal como já usado nas menções existentes (ver
# tools/logistica.gerar_texto_condicao_fixa) — mantém-se o mesmo em toda
# a aplicação, para a menção ser sempre resolvida para a mesma pessoa.
RESPONSAVEL_MENCAO = "Conceição Costa"

_REGIOES = ("Lisboa", "Porto", "Outro")

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

Para cada entrega, inclui sempre o cliente, a morada, o que foi
encomendado, a data prevista de entrega, e o tempo de montagem estimado,
exatamente como aparecem acima — nunca inventes nem alteres estes dados.
Agrupa sempre por região (nunca misturar Lisboa e Porto no mesmo bloco).

NÃO proponhas tu mesma em que dia ou a que horas visitar cada entrega —
isso é calculado à parte, com dados reais de trajeto e tempo de
montagem (ver a "Proposta de agendamento" que vem a seguir ao teu
texto, com dia e hora exatos de cada paragem); menciona só que essa
proposta vem a seguir, para a Conceição/Isa validarem e pedirem à Alma
para criar os eventos no calendário quando estiver fechada. O teu texto
aqui serve para dar contexto geral da semana (quantas entregas,
volume/carga de trabalho por região, alguma nota relevante sobre os
produtos ou procedimentos) — nunca um calendário GPS otimizado.

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
    (cards_por_regiao, moradas_por_regiao, nao_confirmados, itens_prontos,
    entregas_por_regiao, textos_pdf_por_id): cards_por_regiao tem o texto
    formatado de cada entrega pronta (cliente/morada/produto/data, para o
    resumo em texto); moradas_por_regiao tem só as moradas em bruto de
    cada uma (para o trajeto do Google Maps) — extraídos na mesma
    passagem, para nunca ler os dados de cada card duas vezes. Só conta
    cards mesmo prontos a entregar (em "On Hold" por trás de uma região —
    nunca por trás de "Produção" ou "Assistências", ver
    tools.logistica.fase_encomenda); a região vem sempre da coluna real
    por trás da secção "On Hold" (ver _coluna_real_on_hold) — nunca da
    morada.

    nao_confirmados é a lista de títulos de cards cuja coluna real não
    pôde ser confirmada (ex: falha de rede, ou parent sem url) — pedido
    explícito do Rui (2026-07-28): a decisão de região nunca é da Alma,
    por isso estes cards ficam de fora de todas as rotas em vez de
    adivinhados pela morada, e são sinalizados para verificação manual.

    itens_prontos é a lista dos itens brutos do Basecamp (não só o texto
    já formatado) de cada card mesmo pronto a entregar — usado por
    agents.estimativa_montagem para calcular e publicar a estimativa de
    tempo de montagem por card (precisa do item bruto para ler o PDF da
    encomenda e o comments_url, não só do texto já resumido).

    entregas_por_regiao tem, por região, um dict por entrega
    ({"id", "titulo", "morada", "cliente", "produtos_encomendados"}) —
    pedido do Rui (2026-07-28), para a proposta de agendamento (ver
    _construir_texto_proposta_agendamento) poder alinhar o tempo de
    montagem e a morada de cada entrega à sua posição na rota otimizada,
    sem repetir a extração de dados do card.

    textos_pdf_por_id tem, por id de card, o texto do PDF/orçamento já
    lido (ver agents.estimativa_montagem._texto_pdf_encomenda) — pedido
    explícito do Rui (2026-07-29): a leitura do PDF passa a ser
    obrigatória sempre que esta sugestão corre (o orçamento tem quase
    sempre a lista de produtos completa, nunca a morada). Devolvido aqui
    e reaproveitado por _publicar_estimativas_montagem, para nunca ler e
    processar o mesmo PDF duas vezes por card."""
    # forcar=True (bug real reportado, 2026-07-28): a equipa corrigia uma
    # morada nas notas de um card e a sugestão continuava a mostrar a
    # morada antiga — porque _itens_ativos() tem uma cache de 15 min
    # partilhada com outras operações mais frequentes. A sugestão semanal
    # é rara e importante o suficiente para nunca arriscar dados
    # desatualizados, nem que seja só nesses 15 minutos.
    itens = [i for i in basecamp._itens_ativos(forcar=True)
            if i.get("type") == "Kanban::Card"
            and ((i.get("bucket") or {}).get("name") or "").strip().lower() == logistica.PROJETO_ENTREGAS.lower()]

    cards_por_regiao = {regiao: [] for regiao in _REGIOES}
    moradas_por_regiao = {regiao: [] for regiao in _REGIOES}
    entregas_por_regiao = {regiao: [] for regiao in _REGIOES}
    nao_confirmados = []
    itens_prontos = []
    textos_pdf_por_id = {}
    cortados_pelo_limite = 0

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
        # Por trás de "Assistências" também não é uma entrega.
        if fase in ("producao", "assistencia_aguarda_agendamento"):
            continue
        if fase == "outro":
            if coluna_real is None:
                # a coluna real não pôde ser confirmada de todo — nunca
                # adivinhar a região pela morada (pedido do Rui,
                # 2026-07-28), fica fora de todas as rotas, sinalizado
                # para verificação manual
                titulo = item.get("title") or item.get("content") or "(sem título)"
                nao_confirmados.append(titulo)
            continue

        # bug real corrigido (2026-07-28): o limite era aplicado ANTES
        # deste ponto, à lista bruta de TODOS os cards do projeto
        # Entregas (265 no total, contra a API real) — cortava a maioria
        # antes sequer de se saber quais estavam mesmo em "On Hold",
        # deixando de fora entregas prontas só por caírem depois da
        # posição 40 na lista. Agora o limite só conta cards que já se
        # confirmou estarem mesmo prontos a entregar.
        if len(itens_prontos) >= MAX_CARDS_POR_CORRIDA:
            cortados_pelo_limite += 1
            continue

        titulo = item.get("title") or item.get("content") or "(sem título)"
        notas = basecamp._texto_simples(item.get("description", ""))
        # leitura do PDF obrigatória (pedido do Rui, 2026-07-29) — o
        # orçamento/PDF tem quase sempre a lista de produtos completa;
        # nunca usado para "morada" (ver _MISSAO_EXTRACAO). Lido aqui uma
        # única vez por card e reaproveitado por
        # _publicar_estimativas_montagem (evita ler o mesmo PDF duas
        # vezes).
        try:
            texto_pdf = estimativa_montagem._texto_pdf_encomenda(item)
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não consegui ler o PDF de {item['id']}: {e!r}")
            texto_pdf = None
        textos_pdf_por_id[item["id"]] = texto_pdf
        try:
            dados = _extrair_dados_encomenda(titulo, notas, texto_pdf=texto_pdf)
        except Exception as e:
            print(f"[sugestao_logistica_semanal] falhou a extrair dados de {item['id']}: {e!r}")
            dados = {}

        regiao = _REGIAO_POR_COLUNA[logistica.normalizar_coluna(coluna_real)]
        cards_por_regiao[regiao].append(_formatar_card_pronto(titulo, dados))
        if dados.get("morada"):
            moradas_por_regiao[regiao].append(dados["morada"])
        itens_prontos.append(item)
        entregas_por_regiao[regiao].append({
            "id": item["id"], "titulo": titulo, "morada": dados.get("morada"),
            "cliente": dados.get("cliente"), "produtos_encomendados": dados.get("produtos_encomendados"),
        })

    if cortados_pelo_limite:
        print(f"[sugestao_logistica_semanal] ATENÇÃO: {cortados_pelo_limite} entrega(s) pronta(s) a mais "
             f"do que o limite de {MAX_CARDS_POR_CORRIDA} por corrida — ficaram de fora desta sugestão")

    return (cards_por_regiao, moradas_por_regiao, nao_confirmados, itens_prontos,
           entregas_por_regiao, textos_pdf_por_id)

def _texto_nao_confirmados(nao_confirmados: list) -> str:
    """Secção que sinaliza cards cuja coluna real por trás de "On Hold"
    não pôde ser confirmada — nunca adivinhados pela morada (pedido do
    Rui, 2026-07-28), por isso ficam de fora das rotas e têm de ser
    verificados manualmente no Basecamp. Devolve "" se não houver
    nenhum."""
    if not nao_confirmados:
        return ""
    linhas = "\n".join(f"- {titulo}" for titulo in nao_confirmados)
    return ("\n\n---\n\n### Cards por confirmar manualmente\n"
            "Não foi possível confirmar automaticamente a região destes cards "
            "(falha ao ler a coluna real por trás de \"On Hold\") — não entraram em "
            f"nenhuma rota, verifica-os diretamente no Basecamp:\n{linhas}")

def _separar_moradas_por_regiao(moradas_por_regiao: dict) -> tuple:
    """Separa, por região, as moradas reconhecidas pelo Google Maps das
    que não são (ver logistica.separar_moradas_por_reconhecimento) —
    calculado uma só vez, partilhado entre o trajeto/custo
    (_texto_trajetos_google_maps) e a proposta de agendamento
    (_construir_texto_proposta_agendamento), para nunca verificar a
    mesma morada duas vezes.

    Bug real reportado em produção (2026-07-28): a Google Directions API
    falha o pedido de trajeto com várias paragens por INTEIRO assim que
    uma só das moradas não é geocodificável — sem ordem otimizada, sem
    custo, sem proposta de horário para a região inteira, mesmo havendo
    outras moradas boas no grupo. Separar antes de pedir o trajeto
    (usando só as reconhecidas) evita perder tudo por causa de uma só
    morada mal escrita.

    Devolve (moradas_reconhecidas_por_regiao, moradas_excluidas_por_regiao)."""
    reconhecidas_por_regiao, excluidas_por_regiao = {}, {}
    for regiao, moradas in moradas_por_regiao.items():
        reconhecidas, excluidas = logistica.separar_moradas_por_reconhecimento(moradas)
        reconhecidas_por_regiao[regiao] = reconhecidas
        excluidas_por_regiao[regiao] = excluidas
    return reconhecidas_por_regiao, excluidas_por_regiao

def _texto_custo_deslocacao(regiao: str, moradas: list) -> str:
    """Custo de deslocação estimado para esta região (ver
    tools.tempos_montagem.custo_deslocacao), a partir de km/duração REAIS da
    mesma chamada da Directions API já feita para o trajeto (ver
    tools.logistica.metricas_trajeto) — nunca a tabela fixa de zonas do
    documento. Devolve "" se não houver km/duração disponíveis (sem chave
    configurada, ou falha do cálculo)."""
    metricas = logistica.metricas_trajeto(moradas)
    parametros = db.obter_parametros_estimativa()
    custo = tempos_montagem.custo_deslocacao(regiao, metricas["km"], metricas["duracao_min"], parametros)
    if not custo:
        return ""
    linha = (f"  Custo de deslocação estimado: combustível+manutenção "
            f"{custo['combustivel'] + custo['manutencao']:.0f} € + tempo de equipa "
            f"{custo['tempo_equipa']:.0f} €")
    if custo["total_estimado"] is not None:
        linha += f" + portagens {custo['portagens']:.0f} € = **{custo['total_estimado']:.0f} €**"
    else:
        linha += f" ≈ {custo['subtotal_sem_portagens']:.0f} € ({custo['portagens_nota']})"
    return "\n" + linha

def _texto_trajetos_google_maps(moradas_reconhecidas_por_regiao: dict, moradas_excluidas_por_regiao: dict) -> str:
    """Constrói, em Python (nunca pedido ao modelo — um url é demasiado
    fácil de corromper se reescrito por um LLM), a secção com os links do
    Google Maps de cada região que tenha entregas prontas esta semana, com
    o custo de deslocação estimado (ver _texto_custo_deslocacao). Devolve ""
    se não houver nenhuma morada disponível em região nenhuma.

    Recebe já os dois grupos separados (ver _separar_moradas_por_regiao)
    — o link e o custo usam só as moradas reconhecidas pelo Google Maps,
    nunca as excluídas (bug real reportado em produção, 2026-07-28: uma
    só morada mal geocodificada fazia falhar o trajeto/custo da região
    INTEIRA, mesmo havendo outras moradas boas). As excluídas ficam de
    fora do link e são listadas explicitamente, para adicionar à mão."""
    linhas = []
    for regiao, moradas in moradas_reconhecidas_por_regiao.items():
        excluidas = moradas_excluidas_por_regiao.get(regiao) or []
        if not moradas and not excluidas:
            continue
        link = logistica.gerar_link_google_maps(moradas) if moradas else None
        if link:
            linhas.append(f"- **{regiao}** ({len(moradas)} paragem/paragens): {link}")
            linhas.append(_texto_custo_deslocacao(regiao, moradas))
        elif excluidas:
            linhas.append(f"- **{regiao}**: nenhuma morada desta região foi reconhecida pelo Google Maps "
                          "— sem trajeto nem custo calculados")
        for morada_errada in excluidas:
            linhas.append(f"  - ⚠️ o Google Maps não reconhece esta morada: \"{morada_errada}\" — "
                          "EXCLUÍDA do trajeto/custo acima, confirma-a e adiciona-a manualmente no Maps")
    if not linhas:
        return ""
    return ("\n\n---\n\n### Trajetos no Google Maps (partida e regresso ao armazém)\n"
            + "\n".join(l for l in linhas if l)
            + "\n\nA ordem das paragens já vem otimizada — abre o link para validar/editar "
              "o trajeto antes de sair.")

def _publicar_estimativas_montagem(itens_prontos: list, textos_pdf_por_id: dict = None) -> dict:
    """Calcula e publica, como comentário em cada card, a estimativa de
    tempo de montagem (ver agents.estimativa_montagem) — pedido explícito
    do Rui (2026-07-28), seguindo o "Procedimento Tempos de Montagem para
    Logística". Best-effort por card: uma falha num card nunca impede a
    publicação da sugestão semanal em si, e _estimar_e_publicar_card já
    ignora sozinha cards que já têm estimativa (mas devolve os minutos na
    mesma, ver lá).

    `textos_pdf_por_id`, quando fornecido (ver _cards_prontos_a_entregar),
    reaproveita o PDF já lido para a extração de dados da encomenda, para
    nunca ler o mesmo PDF duas vezes por card.

    Devolve {recording_id: {"minutos":, "rendimento":}}, para a proposta
    de agendamento e a tabela preparatória (ver
    _construir_texto_proposta_agendamento e
    _construir_tabela_agendamento) — cards sem estimativa calculável
    (falha na extração ou ao publicar) ficam de fora deste dict, nunca
    com um valor inventado."""
    estimativas_por_id = {}
    for item in itens_prontos:
        texto_pdf = (textos_pdf_por_id or {}).get(item.get("id"))
        try:
            resultado = estimativa_montagem._estimar_e_publicar_card(
                item, logistica.PROJETO_ENTREGAS, texto_pdf=texto_pdf)
        except Exception as e:
            print(f"[sugestao_logistica_semanal] falhou a estimativa de montagem de {item.get('id')}: {e!r}")
            continue
        if resultado:
            estimativas_por_id[item["id"]] = {"minutos": resultado["minutos"],
                                              "rendimento": resultado.get("rendimento") or {"euros_hora": None, "banda": None}}
    return estimativas_por_id

def _calcular_agendamento_por_regiao(entregas_por_regiao: dict, estimativas_por_id: dict,
                                     moradas_reconhecidas_por_regiao: dict, inicio_semana: date) -> dict:
    """Calcula, uma única vez por região, tudo o que a proposta de
    agendamento em texto (_construir_texto_proposta_agendamento) e a
    tabela preparatória (_construir_tabela_agendamento) precisam —
    entregas agendáveis, trajeto real (Google Directions API, ver
    tools.logistica.plano_trajeto) e horário do dia (ver
    tools.agendamento_logistica.calcular_horario_dia) — para nunca pedir
    o mesmo trajeto à Directions API duas vezes pela mesma região.

    Entregas sem morada, cuja morada não é reconhecida pelo Google Maps
    (ver _separar_moradas_por_regiao — nunca entram no cálculo do
    trajeto, para uma só morada má não fazer perder a proposta da região
    inteira), ou cujo tempo de montagem ainda não foi calculado, ficam de
    fora e são devolvidas à parte, para serem sinalizadas para inclusão
    manual — nunca com um horário inventado.

    Devolve {regiao: {"dia": date|None, "horario": dict|None (ver
    calcular_horario_dia — None se não houver entregas agendáveis, ou se
    o trajeto real não puder ser calculado), "sem_morada": [titulos],
    "sem_morada_reconhecida": [titulos], "sem_estimativa": [titulos]}} —
    só para regiões com pelo menos uma entrega pronta a entregar."""
    resultado = {}
    indice_dia = 0
    for regiao, entregas in entregas_por_regiao.items():
        if not entregas:
            continue

        moradas_reconhecidas = moradas_reconhecidas_por_regiao.get(regiao) or []
        agendaveis = [e for e in entregas if e["morada"] in moradas_reconhecidas and e["id"] in estimativas_por_id]
        sem_morada = [e["titulo"] for e in entregas if not e["morada"]]
        sem_morada_reconhecida = [e["titulo"] for e in entregas
                                  if e["morada"] and e["morada"] not in moradas_reconhecidas]
        sem_estimativa = [e["titulo"] for e in entregas
                          if e["morada"] in moradas_reconhecidas and e["id"] not in estimativas_por_id]

        dia = None
        horario = None
        if agendaveis:
            moradas = [e["morada"] for e in agendaveis]
            plano = logistica.plano_trajeto(moradas)
            if plano["pernas_minutos"] is not None:
                entregas_ordenadas = [agendaveis[i] for i in plano["ordem_indices"]]
                paragens = [{**e, "minutos_montagem": estimativas_por_id[e["id"]]["minutos"],
                            "rendimento": estimativas_por_id[e["id"]]["rendimento"]}
                           for e in entregas_ordenadas]
                horario = agendamento_logistica.calcular_horario_dia(
                    paragens, plano["pernas_minutos"], plano["pernas_km"])
                dia = agendamento_logistica.proximo_dia_util(inicio_semana, indice_dia)
                indice_dia += 1

        resultado[regiao] = {"dia": dia, "horario": horario, "sem_morada": sem_morada,
                             "sem_morada_reconhecida": sem_morada_reconhecida,
                             "sem_estimativa": sem_estimativa}
    return resultado

def _construir_texto_proposta_agendamento(agendamento_por_regiao: dict) -> str:
    """Constrói, em Python (nunca pedido ao modelo — datas/horas não se
    confiam à IA), a proposta de agendamento: um dia útil consecutivo por
    região com entregas prontas, e dentro de cada dia, a hora de
    chegada/saída de cada entrega — a partir de
    _calcular_agendamento_por_regiao (trajeto real + horário do dia já
    calculados uma única vez).

    Pedido explícito do Rui (2026-07-28): esta é só a PROPOSTA — a Alma
    nunca cria eventos no calendário sozinha a partir disto; só depois de
    a Conceição ou a Isa confirmarem (com ou sem ajustes) é que
    criar_eventos_calendario_entregas (ver agents/agendamento_entregas.py)
    é chamada, com os valores finais tal como confirmados na conversa.

    Se uma região não couber dentro do turno normal, sinaliza isso
    claramente em vez de decidir sozinha como dividir por mais dias (ver
    tools/agendamento_logistica.py).

    Devolve "" se não houver nenhuma entrega agendável em região
    nenhuma."""
    blocos = []
    for regiao, info in agendamento_por_regiao.items():
        linhas = []
        horario = info["horario"]
        if horario is not None:
            linhas.append(f"**{info['dia'].strftime('%d/%m/%Y (%A)')}**")
            for paragem in horario["paragens"]:
                linhas.append(f"- {paragem['chegada']}–{paragem['saida']}: **{paragem['titulo']}** "
                              f"— {paragem['cliente'] or '(cliente não identificado)'}, "
                              f"{paragem['morada']}"
                              + (f" ({paragem['produtos_encomendados']})" if paragem.get("produtos_encomendados") else ""))
            linhas.append(f"- Regresso ao armazém estimado: {horario['regresso']}")
            if not horario["cabe_no_turno_normal"]:
                linhas.append(f"  - ⚠️ ultrapassa o turno normal (17:30) — a logística tem de decidir "
                              "manualmente que paragem(ns) passar para outro dia (as horas extra são a "
                              "margem para o imprevisto, não para planear)")

        if info["sem_morada"]:
            linhas.append("⚠️ Sem morada identificada, fora da proposta: " + ", ".join(info["sem_morada"]))
        if info["sem_morada_reconhecida"]:
            linhas.append("⚠️ Morada não reconhecida pelo Google Maps, fora da proposta (confirma e agenda à mão): "
                          + ", ".join(info["sem_morada_reconhecida"]))
        if info["sem_estimativa"]:
            linhas.append("⚠️ Sem tempo de montagem calculado ainda, fora da proposta: " + ", ".join(info["sem_estimativa"]))

        if linhas:
            blocos.append(f"#### {regiao}\n" + "\n".join(linhas))

    if not blocos:
        return ""
    return ("\n\n---\n\n### Proposta de agendamento\n\n" + "\n\n".join(blocos) +
            "\n\nEsta é só uma proposta — depois de validada/ajustada pela Conceição ou pela Isa, "
            "pede à Alma para criar os eventos no calendário do projeto Entregas com os dados finais.")

def _texto_tempo_montagem_resumido(rendimento: dict, minutos_conta_a: float) -> str:
    """Resume Conta A (minutos, a única estimativa de tempo que o
    procedimento produz) e Conta B (€/h + banda — ver
    tools.tempos_montagem.calcular_rendimento) na mesma célula da tabela.

    NOTA IMPORTANTE (pedido do Rui, 2026-07-29): o pedido original diz
    para escolher sempre "a de menor tempo" entre Conta A e Conta B — mas
    o "Procedimento Tempos de Montagem para Logística" define Conta B só
    como uma VERIFICAÇÃO do rendimento (€/h) da Conta A, nunca como uma
    estimativa de tempo independente e comparável (não há, no
    procedimento, uma fórmula para converter €/h de volta em minutos sem
    inventar um valor de referência que o documento não dá). Por isso,
    por agora, o tempo usado no agendamento é sempre o da Conta A (a
    única estimativa de tempo real disponível) — Conta B aparece
    resumida ao lado só como o seu papel real: uma verificação, para a
    Conceição/Isa confirmarem se o rendimento implícito faz sentido, não
    como um segundo tempo a escolher. Confirma com o Rui se isto não for
    o que se pretendia."""
    minutos_texto = f"{minutos_conta_a:.0f} min (Conta A)"
    if rendimento and rendimento.get("euros_hora") is not None:
        minutos_texto += f" · Conta B: {rendimento['euros_hora']:.0f} €/h, banda \"{rendimento['banda']}\""
    return minutos_texto

def _celula_tabela(texto: str) -> str:
    """Substitui "|" por " – " dentro de uma célula de tabela markdown —
    os títulos dos cards no Basecamp usam "|" como separador interno
    (ex: "II | Anália Vasconcelos 18052026 | €1 467"), e um "|" cru
    dentro de uma célula parte a linha da tabela em colunas a mais.

    Bug real reportado em produção (2026-07-29), em duas tentativas: a
    primeira versão escapava com "\\|" (a convenção do CommonMark/GFM) —
    mas o Basecamp NÃO respeita esse escape ao renderizar a tabela, só o
    ignora ao dividir as colunas (fica com o número certo de colunas),
    sem NUNCA reconstruir o "|" original (o "\\" fica visível como texto,
    e o "|" simplesmente desaparece) — como a linha continuava a ter mais
    campos separados por "|" do que colunas no cabeçalho, os campos a
    mais (o tempo/custo REAIS) eram descartados silenciosamente pelo
    renderizador, e só sobravam fragmentos do título nas primeiras
    colunas. A única forma fiável de nunca partir uma linha, com este
    renderizador, é nunca deixar passar um "|" cru para dentro de uma
    célula — escapado ou não — por isso substituímos o caráter em vez de
    o escapar."""
    return (texto or "").replace("|", " – ")

def _construir_tabela_agendamento(agendamento_por_regiao: dict) -> str:
    """Tabela preparatória de agendamento, pedida explicitamente pelo Rui
    (2026-07-29), para rever ANTES de pedir a criação dos eventos no
    calendário: uma lista de eventos (viagem/cliente/almoço), pela ordem
    real da rota, com o tempo estimado e o custo de cada um — a partir de
    _calcular_agendamento_por_regiao (mesmo trajeto/horário já calculados
    para a proposta em texto, nunca recalculado/repedido à API).

    Custo de viagem: tools.tempos_montagem.custo_viagem_perna (por
    perna). Custo de montagem: tools.tempos_montagem.custo_montagem_paragem
    (horas × custo_hora_pessoa_eur × 2 pessoas, mesma taxa da viagem).
    Ver _texto_tempo_montagem_resumido para a nota sobre Conta A/Conta B.

    Devolve "" se não houver nenhum dia com horário calculado em região
    nenhuma."""
    if not any(info["horario"] is not None for info in agendamento_por_regiao.values()):
        return ""

    parametros = db.obter_parametros_estimativa()
    blocos = []
    for regiao, info in agendamento_por_regiao.items():
        horario = info["horario"]
        if horario is None:
            continue

        linhas = ["| Evento | Tempo estimado | Custo |", "|---|---|---|"]
        total_viagem_min = total_montagem_min = 0.0
        total_custo = 0.0
        for evento in horario["eventos"]:
            if evento["tipo"] == "viagem":
                custo = tempos_montagem.custo_viagem_perna(evento["km"], evento["minutos"], parametros)
                total_viagem_min += evento["minutos"]
                if custo and custo["subtotal"] is not None:
                    total_custo += custo["subtotal"]
                    custo_texto = f"{custo['combustivel'] + custo['manutencao']:.0f} € combustível+manutenção + {custo['tempo_equipa']:.0f} € equipa = {custo['subtotal']:.0f} €"
                elif custo:
                    total_custo += custo["tempo_equipa"]
                    custo_texto = f"{custo['tempo_equipa']:.0f} € equipa (sem km — só duração disponível)"
                else:
                    custo_texto = "—"
                de_texto = _celula_tabela(evento["de"])
                para_texto = _celula_tabela(evento["para"])
                linhas.append(f"| Viagem: {de_texto} → {para_texto} | {evento['minutos']:.0f} min | {custo_texto} |")
            elif evento["tipo"] == "cliente":
                custo_montagem = tempos_montagem.custo_montagem_paragem(evento["minutos"], parametros)
                total_montagem_min += evento["minutos"]
                total_custo += custo_montagem or 0
                tempo_texto = _texto_tempo_montagem_resumido(evento.get("rendimento"), evento["minutos"])
                titulo_texto = _celula_tabela(evento["titulo"])
                cliente_texto = _celula_tabela(evento.get("cliente") or "(cliente não identificado)")
                linhas.append(f"| Cliente: {titulo_texto} — {cliente_texto} | {tempo_texto} | {custo_montagem:.0f} € |")
            else:  # almoço
                linhas.append(f"| Almoço | {evento['minutos']:.0f} min | — |")

        linhas.append(f"| **Total** | **{total_viagem_min:.0f} min viagem + {total_montagem_min:.0f} min montagem** | **{total_custo:.0f} €** |")
        titulo_bloco = f"#### {regiao}" + (f" — {info['dia'].strftime('%d/%m/%Y (%A)')}" if info["dia"] else "")
        blocos.append(titulo_bloco + "\n" + "\n".join(linhas))

    if not blocos:
        return ""
    return ("\n\n---\n\n### Tabela preparatória de agendamento\n\n" + "\n\n".join(blocos) +
            "\n\nRevê esta tabela antes de pedir a criação dos eventos no calendário do projeto Entregas.")

def correr_sugestao_semanal_logistica() -> dict:
    """Uma corrida da sugestão semanal de logística de entregas: lê os
    cards prontos a entregar (ver _cards_prontos_a_entregar), publica a
    estimativa de tempo de montagem em cada card novo (ver
    _publicar_estimativas_montagem — feito sempre, em toda corrida, seja
    agendada ou disparada manualmente), gera o texto de organização da
    semana, um trajeto de Google Maps por região (ver
    _texto_trajetos_google_maps) e uma proposta de agendamento (dia/hora
    por entrega, ver _construir_texto_proposta_agendamento), e publica
    tudo junto no Mural "Programação", dirigido à Conceição Costa.
    Pensado para correr às segundas de manhã (agendado), mas pode ser
    disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[sugestao_logistica_semanal] já há uma corrida em curso — ignorado")
        return {"erro": "já está a correr uma sugestão semanal"}

    try:
        try:
            (cards_por_regiao, moradas_por_regiao, nao_confirmados, itens_prontos,
             entregas_por_regiao, textos_pdf_por_id) = _cards_prontos_a_entregar()
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não foi possível obter os cards do Basecamp: {e!r}")
            return {"erro": str(e)}

        estimativas_por_id = _publicar_estimativas_montagem(itens_prontos, textos_pdf_por_id)

        try:
            documentos_texto = _formatar_documentos_referencia(documentos_referencia.documentos_referencia_empresa())
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não consegui ler os documentos de referência: {e!r}")
            documentos_texto = None

        moradas_reconhecidas_por_regiao, moradas_excluidas_por_regiao = _separar_moradas_por_regiao(moradas_por_regiao)

        inicio_semana, fim_semana = _semana_atual()
        agendamento_por_regiao = _calcular_agendamento_por_regiao(
            entregas_por_regiao, estimativas_por_id, moradas_reconhecidas_por_regiao, inicio_semana)
        texto = _gerar_texto_sugestao(cards_por_regiao, inicio_semana, fim_semana, documentos_texto)
        texto += _texto_trajetos_google_maps(moradas_reconhecidas_por_regiao, moradas_excluidas_por_regiao)
        texto += _construir_texto_proposta_agendamento(agendamento_por_regiao)
        texto += _construir_tabela_agendamento(agendamento_por_regiao)
        texto += _texto_nao_confirmados(nao_confirmados)
        basecamp.publicar_mural("Sugestão de logística semanal", texto, projeto=logistica.PROJETO_ENTREGAS)

        contagens = {regiao: len(cards) for regiao, cards in cards_por_regiao.items()}
        total_prontos = sum(contagens.values())
        print(f"[sugestao_logistica_semanal] publicado — {total_prontos} entrega(s) pronta(s): {contagens}")
        resultado = {"total_prontos": total_prontos, "por_regiao": contagens}
        if nao_confirmados:
            resultado["nao_confirmados"] = nao_confirmados
        return resultado
    finally:
        _a_correr.release()

def trajetos_logistica_entregas() -> dict:
    """Gera, a pedido (fora do ciclo semanal automático), um link de
    Google Maps por região com todas as entregas prontas a fazer agora —
    pedido explícito do Rui (2026-07-27): a mesma informação da sugestão
    semanal, mas disponível a qualquer momento na conversa, não só às
    segundas de manhã. Usa exatamente a mesma lógica de deteção de cards
    prontos a entregar (ver _cards_prontos_a_entregar), para nunca haver
    duas versões a divergir. Não publica estimativas de montagem (isso só
    acontece no ciclo semanal, ver _publicar_estimativas_montagem)."""
    (cards_por_regiao, moradas_por_regiao, nao_confirmados, _itens_prontos,
     _entregas_por_regiao, _textos_pdf_por_id) = _cards_prontos_a_entregar()
    moradas_reconhecidas_por_regiao, moradas_excluidas_por_regiao = _separar_moradas_por_regiao(moradas_por_regiao)
    trajetos = {}
    parametros = db.obter_parametros_estimativa()
    for regiao, moradas in moradas_reconhecidas_por_regiao.items():
        # usa só as moradas reconhecidas para o link/custo (ver
        # _separar_moradas_por_regiao) — uma só morada mal geocodificada
        # não pode fazer perder o trajeto/custo da região inteira
        link = logistica.gerar_link_google_maps(moradas) if moradas else None
        if link:
            trajetos[regiao] = {"paragens": len(moradas), "link": link}
            metricas = logistica.metricas_trajeto(moradas)
            custo = tempos_montagem.custo_deslocacao(regiao, metricas["km"], metricas["duracao_min"], parametros)
            if custo:
                trajetos[regiao]["custo_deslocacao_estimado"] = custo
        moradas_excluidas = moradas_excluidas_por_regiao.get(regiao) or []
        if moradas_excluidas:
            trajetos.setdefault(regiao, {"paragens": len(moradas), "link": link})["moradas_nao_reconhecidas"] = moradas_excluidas
    contagens = {regiao: len(cards) for regiao, cards in cards_por_regiao.items()}
    resultado = {"por_regiao": contagens, "trajetos_google_maps": trajetos}
    if nao_confirmados:
        resultado["nao_confirmados"] = nao_confirmados
    return resultado
