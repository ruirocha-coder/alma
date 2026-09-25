# tools/planeamento_serracao.py — quadro de planeamento de produção da
# Ecos Largos: agenda visual (arrastar e largar) de que OF vai para que
# linha de produção e em que dia, cruzando os cards reais do Basecamp
# (projeto "Ecos Largos") com o agendamento local (linha/início/duração —
# só aqui, o Basecamp não tem nenhum campo para isto).
#
# Regra combinada com o Rui (2026-09): SEM sincronização contínua. Ao criar
# uma encomenda nova aqui, cria-se o card correspondente no Basecamp (coluna
# Triagem — ver basecamp.criar_card). A partir daí, quadro e Basecamp
# trabalham de forma independente: arrastar/reagendar/redimensionar uma OF
# já existente nunca escreve nada de volta no Basecamp, e mudanças feitas
# no Basecamp nunca "empurram" sozinhas para aqui — a página relê o
# Basecamp sempre que é aberta ou atualizada manualmente.
#
# Única exceção (pedido explícito do Rui, 2026-09-30): sempre que o dia de
# início da produção é definido/alterado (ver agendar), esse dia é também
# escrito no campo "Due on" do card real no Basecamp — para quem só olha
# para lá, sem abrir este quadro, também ver quando a OF deve começar a
# ser produzida.
import math
import time
import unicodedata
from datetime import date, timedelta
import db
from tools import basecamp

PROJETO = "Ecos Largos"

# colunas do card table real do Ecos Largos que representam OFs em fluxo de
# fabrico (confirmado ao vivo, 2026-09, contra a API real; "secagem"
# acrescentada a pedido explícito do Rui, 2026-09-25 — coluna real do
# Basecamp, entra na cor automática de fundo tal como Produzido/Em
# Produção/Vendido). As colunas "Linha 1" a "Linha 6" / Charriots /
# Empilhadores do mesmo quadro guardam cards de ALOCAÇÃO DE PESSOAL, não
# OFs — ficam de fora deste quadro, por pedido explícito do Rui.
COLUNAS_OF_FLUXO = {"triagem", "programacao", "em producao", "produzido", "secagem"}
# "Vendido" inclui-se para uma OF já agendada não desaparecer do quadro
# quando a venda fecha no Basecamp — fica visível (cor automática roxa, ver
# template) em vez de desaparecer; nunca entra na fila (só cards em Triagem
# entram lá, ver estado_planeamento_serracao). Fica FORA de COLUNAS_OF_FLUXO
# (o conjunto pedido em bloco a cada leitura, ver _cards_of_ativos) de
# propósito: "Vendido" acumula TODO o histórico de vendas fechadas da conta
# (medido ao vivo, 2026-09-29: 1000+ cards, 6+ segundos só para a listar) —
# só interessam aqui as poucas OFs desta lista que já estejam agendadas
# localmente, por isso são pedidas uma a uma (ver basecamp.obter_cards),
# nunca a coluna inteira.
COLUNAS_OF = COLUNAS_OF_FLUXO | {"vendido"}

# linhas de produção reais da serração (mesmos nomes vistos nas colunas de
# pessoal do Basecamp, confirmado ao vivo) — usadas aqui só como categorias
# do agendamento local; não têm nenhuma ligação às colunas do Basecamp com
# o mesmo nome.
LINHAS = [
    "Linha 1 - Quad",
    "Linha 2 Reguas/Barrotes",
    "Linha 3 Bartly",
    "Linha 4 Mult.Troncos",
    "Linha 5 Tabuinha",
    "Linha 6 Alinhadeira",
]

# lista fixa e pequena de cores manuais (pedido explícito do Rui, 2026-09)
# — além destas, uma OF tem uma cor automática de fundo, de acordo com a
# coluna real no Basecamp (ver ESTADOS_COR/cores_estado no template) —
# também editável pela equipa, não só a manual.
# paleta alargada (pedido explícito do Rui, 2026-09: "as mesmas cores que
# o Google Calendar/Gmail têm" — uma grelha de 4 tons por matiz, como no
# seletor de cores deles). Os 6 nomes base (sem sufixo) continuam válidos
# tal e qual — são a mesma cor que já podia estar guardada em cards e nos
# estados automáticos (ver CORES_ESTADO/db.SEED_CORES_ESTADO_ECOS_LARGOS)
# antes desta expansão, só com o tom (hex) atualizado no frontend.
CORES_VALIDAS = {
    "cinza",
    "vermelho_escuro", "vermelho", "vermelho_medio", "vermelho_claro",
    "laranja_escuro", "laranja", "laranja_medio", "laranja_claro",
    "amarelo_escuro", "amarelo", "amarelo_medio", "amarelo_claro",
    "verde_escuro", "verde", "verde_medio", "verde_claro",
    "azul_escuro", "azul", "azul_medio", "azul_claro",
    "roxo_escuro", "roxo", "roxo_medio", "roxo_claro",
}

# estados/colunas do Basecamp que têm uma cor automática de fundo no
# quadro, editável pela equipa (ver atualizar_cor_estado) — chave interna
# -> título exato da coluna no Basecamp.
ESTADOS_COR = {"produzido": "Produzido", "em_producao": "Em Produção", "vendido": "Vendido", "secagem": "Secagem"}

# colunas (normalizadas, ver _normalizar) anteriores ao início da
# produção em si — usadas só para confirmar em definitivo o atraso de
# início de uma OF (ver estado_planeamento_serracao/
# db.marcar_atrasado_confirmado): se a OF ainda estiver numa destas
# colunas já depois do dia de início local, começou atrasada.
COLUNAS_ANTES_DA_PRODUCAO = {"triagem", "programacao"}

def _normalizar(texto: str) -> str:
    sem_acentos = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acentos.lower().strip()

_CACHE_CARDS_ATIVOS = {}  # {"itens": (timestamp, lista)}
TTL_CARDS_ATIVOS = 15  # segundos

def _cards_of_ativos(forcar: bool = False) -> list[dict]:
    """Todos os cards de OF ativos do quadro Kanban do Ecos Largos, só das
    colunas de fluxo de fabrico (ver COLUNAS_OF). Passa "" como nome do
    quadro porque o Ecos Largos tem um único card table ativo — não é
    preciso adivinhar o título exato dele (ver basecamp.cards_de_card_table:
    "" é substring de qualquer título).

    Bug real (Rui, 2026-09-29): "está a demorar muito a atualizar e a
    abrir a página" — cada leitura ia sempre buscar isto ao Basecamp sem
    cache nenhuma (10-12s reais, medidos ao vivo: um pedido para listar o
    card table + um pedido por cada coluna dele, à espera um do outro).
    Como esta página relê /dados a cada abertura, a cada 2 minutos, a
    seguir a qualquer gravação (várias, desde a correção do "problema dos
    dias") e sempre que chega um aviso de tempo real (SSE) — para esta
    mesma sessão e para qualquer outra pessoa com a página aberta —
    juntar tudo isto sem cache nenhuma tornava praticamente cada clique
    numa espera de 10s. Uma cache curta (15s) reduz isto a quase zero na
    esmagadora maioria dos pedidos, sem a informação alguma vez parecer
    desatualizada de forma notada (a produção real não muda card a cada
    poucos segundos). `forcar=True` ignora a cache (usado logo a seguir a
    esta própria página criar/apagar/renomear um card — ver
    _invalidar_cache_cards_ativos — nunca é preciso chamar com forcar=True
    a partir daqui, a invalidação já trata disso).

    Bug real de performance #2 (Rui, 2026-09-29): mesmo com cache e as
    colunas em paralelo, a leitura fria continuava a demorar ~8-11s —
    diagnosticado ao vivo: a coluna "Vendido" sozinha tinha 1000+ cards
    (todo o histórico de vendas fechadas da conta) e 6+ segundos só para
    paginar por ela. Só interessam aqui as OFs de "Vendido" que já estão
    agendadas localmente (ver COLUNAS_OF) — normalmente umas dezenas, não
    mil — por isso pede-se primeiro só as colunas de fluxo normais
    (COLUNAS_OF_FLUXO, rápidas, poucas dezenas de cards ao todo) e depois,
    só para as OFs já agendadas que não apareceram aí, pede-se cada card
    individualmente e em paralelo (ver basecamp.obter_cards) — muito mais
    barato do que listar a coluna inteira."""
    if not forcar and "itens" in _CACHE_CARDS_ATIVOS:
        ts, itens = _CACHE_CARDS_ATIVOS["itens"]
        if time.time() - ts < TTL_CARDS_ATIVOS:
            return itens
    cards = basecamp.cards_de_card_table("", projeto=PROJETO, colunas=COLUNAS_OF_FLUXO)
    itens = [c for c in cards if _normalizar(c.get("estado")) in COLUNAS_OF_FLUXO]
    ids_no_fluxo = {c["id"] for c in itens}
    ids_agendados = {a["basecamp_card_id"] for a in db.agendamentos_producao_ecos_largos()
                     if a["linha"] and a["dia_inicio"]}
    faltam = ids_agendados - ids_no_fluxo
    encontrados = basecamp.obter_cards(faltam, projeto=PROJETO)
    ids_encontrados = set()
    for card in encontrados:
        ids_encontrados.add(card["id"])
        if _normalizar(card.get("estado")) in COLUNAS_OF:
            itens.append(card)
    # uma OF agendada aqui cujo card já não existe mesmo no Basecamp
    # (apagado/mandado para o lixo diretamente lá, não por este quadro) —
    # limpa o agendamento local e o duplicado de logística, tal como já
    # acontece ao apagar por aqui (ver _remover_agendamento_local; pedido
    # explícito do Rui, 2026-09-25): antes ficava "preso" no quadro para
    # sempre, mesmo já não existindo do outro lado.
    if faltam - ids_encontrados:
        ids_ativos_atual = {c["id"] for c in itens}
        for card_id in (faltam - ids_encontrados):
            _remover_agendamento_local(card_id, ids_ativos=ids_ativos_atual)
    _CACHE_CARDS_ATIVOS["itens"] = (time.time(), itens)
    return itens

def _invalidar_cache_cards_ativos():
    """Limpa a cache de _cards_of_ativos (ver TTL_CARDS_ATIVOS) — chamado
    sempre que esta página escreve algo no Basecamp que muda a lista de
    cards ativos ou o seu título (criar, apagar, renomear), para a
    próxima leitura — normalmente o carregar() que o frontend dispara
    logo a seguir a qualquer gravação — nunca mostrar dados de antes
    dessa escrita só por ainda estar dentro da janela da cache."""
    _CACHE_CARDS_ATIVOS.pop("itens", None)

def estado_planeamento_serracao() -> dict:
    """Junta os cards de OF reais do Basecamp com o agendamento local
    (linha/dia/duração/volume/madeira) — devolve as capacidades atuais de
    cada linha, a bolsa (OFs sem linha/dia atribuídos, mais recentes
    criadas primeiro), as OFs já agendadas, prontas a desenhar no quadro,
    e os duplicados de logística/carregamento (ver
    _talvez_duplicar_logistica)."""
    cards_ativos = _cards_of_ativos()
    cards_por_id = {c["id"]: c for c in cards_ativos}
    agendamentos = {a["basecamp_card_id"]: a for a in db.agendamentos_producao_ecos_largos()}
    bolsa, agendadas = [], []
    # OFs que já avançaram para lá de Triagem/Programação e ainda nunca
    # foram verificadas (nem confirmadas atrasadas, nem confirmadas a
    # tempo) — a verificação ao vivo (Triagem/Programação já depois do dia
    # de início) só as apanha enquanto ainda lá estão; uma vez avançadas,
    # só o histórico real de eventos do Basecamp sabe dizer se entraram em
    # "Em Produção" no dia certo (pedido explícito do Rui, 2026-09-25,
    # exemplo real: "Girona", que já tinha avançado sem nunca ter sido
    # apanhado pela verificação ao vivo).
    pendentes_verificacao = []
    for c in cards_ativos:
        agendamento = agendamentos.get(c["id"])
        tem_agendamento = bool(agendamento and agendamento["linha"] and agendamento["dia_inicio"])
        # a fila (bolsa por agendar) só mostra OFs ainda em Triagem — uma OF
        # que já avançou no Basecamp (Programação/Em Produção/Produzido) sem
        # nunca ter sido agendada aqui já não é "por agendar", é trabalho já
        # em curso fora deste quadro, por isso fica de fora por completo
        # (pedido explícito do Rui, 2026-09). Só continua a aparecer, na
        # grelha, se já tiver um agendamento local guardado de antes.
        if not tem_agendamento and _normalizar(c.get("estado")) != "triagem":
            continue
        info = {
            "basecamp_card_id": c["id"],
            "titulo": c["titulo"],
            "coluna_basecamp": c["estado"],
            "prazo": c["prazo"],
            "criado_em": c.get("criado_em"),
            "url": c["url"],
            "volume_m3": agendamento["volume_m3"] if agendamento else None,
            "cor": agendamento["cor"] if agendamento else None,
            "cor_fundo": agendamento["cor_fundo"] if agendamento else None,
            "tipo_madeira": agendamento["tipo_madeira"] if agendamento else None,
        }
        if tem_agendamento:
            info["linha"] = agendamento["linha"]
            info["dia_inicio"] = agendamento["dia_inicio"]
            info["duracao_dias"] = agendamento["duracao_dias"]
            info["duracao_manual"] = agendamento["duracao_manual"]
            info["ordem"] = agendamento["ordem"]
            # borda vermelha de atraso (pedido explícito do Rui,
            # 2026-09-25): é só sobre o INÍCIO — se a OF ainda está em
            # Triagem/Programação já depois do dia de início local, ficou
            # atrasada, e fica assim para sempre (mesmo que "Em Produção"
            # chegue no dia seguinte, já não foi no dia certo). Uma OF que
            # chegue a "Em Produção" (ou mais além) antes disso alguma vez
            # ser detetado nunca fica marcada — não é sobre o fim previsto
            # nem sobre quanto tempo a produção em si está a demorar.
            atrasado_confirmado = agendamento["atrasado_confirmado"]
            ainda_antes_da_producao = _normalizar(c.get("estado")) in COLUNAS_ANTES_DA_PRODUCAO
            if (not atrasado_confirmado
                    and ainda_antes_da_producao
                    and date.fromisoformat(agendamento["dia_inicio"]) < date.today()):
                db.marcar_atrasado_confirmado(c["id"])
                atrasado_confirmado = True
            elif (not atrasado_confirmado
                    and not agendamento.get("inicio_verificado")
                    and not ainda_antes_da_producao):
                pendentes_verificacao.append((c["id"], agendamento["dia_inicio"]))
            info["atrasado_confirmado"] = atrasado_confirmado
            agendadas.append(info)
        else:
            bolsa.append(info)
    if pendentes_verificacao:
        datas_entrada = basecamp.datas_entrada_em_coluna(
            [card_id for card_id, _ in pendentes_verificacao], "Em Produção", projeto=PROJETO
        )
        info_por_id = {info["basecamp_card_id"]: info for info in agendadas}
        for card_id, dia_inicio in pendentes_verificacao:
            data_entrada = datas_entrada.get(card_id)
            if data_entrada and data_entrada > dia_inicio:
                db.marcar_atrasado_confirmado(card_id)
                info_por_id[card_id]["atrasado_confirmado"] = True
            else:
                # sem prova de atraso (entrou a tempo, ou o histórico de
                # eventos não deu para confirmar nada com confiança) —
                # fecha o caso sem marcar atraso, para não voltar a
                # consultar o Basecamp para esta OF (pedido explícito do
                # Rui, 2026-09-25).
                db.marcar_inicio_a_tempo(card_id)
    # encomendas mais recentes primeiro (pedido explícito do Rui, 2026-09)
    # — não por prazo, para uma encomenda nova (normalmente ainda sem
    # prazo definido) não ficar escondida ao fundo da fila.
    bolsa.sort(key=lambda c: c.get("criado_em") or "", reverse=True)
    capacidades = db.capacidades_linhas_producao_ecos_largos()

    logistica = []
    for lg in db.logistica_carregamentos_ecos_largos():
        c = cards_por_id.get(lg["basecamp_card_id"])
        if not c:
            continue  # OF já saiu do fluxo ativo no Basecamp — deixa de aparecer
        agendamento = agendamentos.get(lg["basecamp_card_id"])
        if not (agendamento and agendamento["linha"] and agendamento["dia_inicio"]):
            # só aparece na logística enquanto a OF continuar agendada na
            # grelha de produção (a tabela "de cima") — se for devolvida à
            # fila, o duplicado fica guardado mas escondido; volta a
            # aparecer sozinho se for agendada outra vez (pedido explícito
            # do Rui, 2026-09).
            continue
        logistica.append({
            "basecamp_card_id": lg["basecamp_card_id"],
            "titulo": c["titulo"],
            "coluna_basecamp": c["estado"],
            "url": c["url"],
            "dia_carregamento": lg["dia_carregamento"],
            # cor e cor_fundo são da OF, uma só, partilhadas com a
            # produção (não campos à parte na logística) — pedido
            # explícito do Rui, 2026-09: mudar qualquer uma tem de se ver
            # nas duas tabelas.
            "cor": agendamento["cor"],
            "cor_fundo": agendamento["cor_fundo"],
            "quem_carrega": lg["quem_carrega"],
        })

    return {
        "linhas": LINHAS,
        "capacidades": {linha: capacidades.get(linha) for linha in LINHAS},
        "cores_estado": db.cores_estado_producao(),
        "bolsa": bolsa,
        "agendadas": agendadas,
        "logistica": logistica,
    }

LIMITE_DIAS_REPARTIR = 60  # nunca tentar expandir a duração indefinidamente à procura de espaço

def _repartir_greedy(capacidade: float, ocupacao: dict, dia_inicio: str,
                     volume_m3: float, limite: int = LIMITE_DIAS_REPARTIR):
    """Núcleo da repartição greedy: simula encaixar `volume_m3` numa linha
    a partir de `dia_inicio`, usando sempre primeiro o que ainda sobra de
    capacidade em cada dia (ver `ocupacao`, de _ocupacao_diaria) — só o
    que não couber passa para o dia seguinte. Pedido explícito do Rui
    (2026-09): aproveitar ao máximo a capacidade de cada dia, em vez de
    exigir um ritmo diário igual em todos os dias que a encomenda ocupa
    (isso desperdiçava dias inteiros quase vazios só porque o primeiro
    dia já tinha pouca folga — ex: 35 m³ numa linha de 33 m³/dia, com 22
    já ocupados no 1º dia e nada no 2º, cabe em 2 dias — 11 no primeiro,
    24 no segundo — e não em 4 como uma repartição uniforme exigiria).

    Sábado e domingo nunca dão capacidade nenhuma (pedido explícito do
    Rui, 2026-09): uma encomenda demasiado grande para acabar até
    sexta-feira não passa para sábado — salta o fim de semana inteiro
    (conta como dias de calendário ocupados, sem produção nenhuma neles)
    e só continua a ser produzida na segunda-feira seguinte.

    Devolve (dias, alocacao): `dias` é quantos dias de calendário isso
    ocupa (mesmo que algum desses dias não tenha contribuído nada — um
    fim de semana, ou um dia reservado por completo por outra OF sem
    volume definido, não dão espaço nenhum, mas continuam a contar como
    dias ocupados do calendário); `alocacao` é {dia_iso: m³ que esta
    encomenda ficou mesmo a ocupar nesse dia} — os m³ REAIS usados, nunca
    uma média, para quem chamar poder somar a `ocupacao` sem repetir o
    erro do ritmo uniforme (ver _ocupacao_diaria). Devolve (None, {}) se
    não couber dentro de `limite` dias — e nesse caso não aloca nada."""
    inicio = date.fromisoformat(dia_inicio)
    restante = float(volume_m3)
    alocacao = {}
    for dias in range(1, limite + 1):
        data = inicio + timedelta(days=dias - 1)
        dia = data.isoformat()
        usado = ocupacao.get(dia, 0)
        # fim de semana não é dia de produção — nunca dá capacidade
        # nenhuma, esteja o que estiver ocupado nesse dia (pedido
        # explícito do Rui, 2026-09): uma encomenda grande demais para
        # acabar até sexta-feira não passa para sábado, salta o fim de
        # semana inteiro e continua na segunda-feira seguinte.
        if data.weekday() >= 5:  # sábado=5, domingo=6
            livre = 0
        else:
            livre = 0 if usado == float("inf") else max(0, capacidade - usado)
        tomado = min(livre, restante)
        if tomado > 1e-9:
            alocacao[dia] = tomado
        restante -= livre
        if restante <= 1e-9:
            return dias, alocacao
    return None, {}

def _dias_necessarios_greedy(capacidade: float, ocupacao: dict, dia_inicio: str,
                             volume_m3: float, limite: int = LIMITE_DIAS_REPARTIR):
    """Só o número de dias de _repartir_greedy — usado por quem só quer
    validar/decidir a duração de uma OF nova, sem precisar da alocação
    dia a dia (ver _duracao_por_volume, _validar_capacidade)."""
    dias, _ = _repartir_greedy(capacidade, ocupacao, dia_inicio, volume_m3, limite)
    return dias

def _ocupacao_diaria(linha: str, antes_de: tuple = None, excluir_id: int = None,
                     ids_ativos: set = None) -> dict:
    """Quanto de m³/dia está mesmo ocupado, dia a dia, numa linha —
    replica a fila de OFs já agendadas nessa linha pela mesma repartição
    greedy usada para encaixar uma OF nova (_repartir_greedy), em vez de
    assumir que cada OF produz sempre a mesma média (volume ÷ duração)
    todos os dias que ocupa.

    Bug real #1 (Rui, 2026-09): a versão antiga somava essa média por OF,
    independente das outras — o que divergia da regra "aproveita ao
    máximo a capacidade de cada dia primeiro" usada para colocar OFs
    novas (ver _repartir_greedy), montando duas contas diferentes para a
    mesma linha. Ex: linha de 33 m³/dia com duas OFs de 26 m³ a começar
    no mesmo dia — a versão antiga via só 13+8,7=21,7 m³ ocupados nesse
    dia (médias das duas), escondendo que, aplicando a mesma regra greedy
    às duas seguidas, a primeira OF a chegar já usa os 26 m³ inteiros
    logo no 1º dia, sem sobrar nada para a segunda.

    `antes_de`, se dado, é (dia_inicio, basecamp_card_id) da OF que está a
    ser validada — só conta OFs com prioridade ESTRITAMENTE anterior a
    essa (dia_inicio mais cedo, ou o mesmo dia mas basecamp_card_id mais
    baixo = criada primeiro no Basecamp), nunca as que vêm depois.

    Bug real #2 (Rui, 2026-09): antes disto, esta função só excluía o
    próprio card (`excluir_id`) mas contava TODAS as outras, incluindo as
    que vêm depois na fila — o que criava um paradoxo quando duas OFs
    começavam no mesmo dia: cada uma, ao validar-se a si própria,
    excluía-se e via a OUTRA com prioridade total e sem concorrência
    nenhuma (26 m³ inteiros, coube-lhe tudo num só dia), concluindo por
    isso as duas que só sobravam 7 m³ livres nesse dia — as duas a
    "perder" a vez uma para a outra ao mesmo tempo. Com `antes_de`, só a
    OF com prioridade real (a mais cedo a chegar ao Basecamp, desempatada
    por basecamp_card_id) vê o dia livre por completo; a outra vê
    corretamente que a primeira já lá está.

    Uma OF sem volume definido não entra nesta simulação (não há volume
    para repartir): ocupa a linha por completo nos dias da sua própria
    duração guardada (`float("inf")`), tal como antes — conservador, para
    nunca sobre-comprometer uma linha sem dados. `excluir_id` ignora
    sempre o próprio card, independentemente da prioridade (necessário ao
    mover uma OF já colocada: a sua própria linha antiga, ainda na base
    de dados com o dia_inicio antigo, nunca deve contar contra si mesma).

    Bug real #3 (Rui, 2026-09): uma OF que já saiu do fluxo ativo no
    Basecamp (apagada/arquivada por lá diretamente, sem passar por
    apagar_encomenda) deixa o agendamento local órfão — sem isto, esse
    órfão continuava a "ocupar" a linha para sempre, invisível no quadro,
    forçando encomendas novas a repartir-se sem motivo nenhum visível.
    Por isso só conta OFs que ainda existem mesmo no Basecamp.

    `ids_ativos`, se dado, poupa ir buscar os cards ativos ao Basecamp de
    novo (chamada lenta) — usado por _recalcular_linha, que precisa desta
    função uma vez por OF da linha e não pode repetir esse pedido de cada
    vez (ver ali)."""
    if ids_ativos is None:
        ids_ativos = {c["id"] for c in _cards_of_ativos()}
    capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
    agendamentos = [a for a in db.agendamentos_producao_ecos_largos()
                    if a["linha"] == linha and a["dia_inicio"]
                    and a["basecamp_card_id"] in ids_ativos
                    and a["basecamp_card_id"] != excluir_id
                    and (antes_de is None or (a["dia_inicio"], a["basecamp_card_id"]) < antes_de)]
    agendamentos.sort(key=lambda a: (a["dia_inicio"], a["basecamp_card_id"]))
    ocupacao = {}
    for a in agendamentos:
        if not a["volume_m3"]:
            duracao = max(1, a["duracao_dias"] or 1)
            inicio = date.fromisoformat(a["dia_inicio"])
            for i in range(duracao):
                dia = (inicio + timedelta(days=i)).isoformat()
                ocupacao[dia] = float("inf")
            continue
        if capacidade <= 0:
            continue  # sem capacidade configurada: não há m³/dia nenhum para repartir
        if a.get("duracao_manual"):
            # pedido explícito do Rui (2026-09-28): o dia de fim de uma OF
            # pode ser definido à mão (ver redefinir_fim), substituindo o
            # cálculo automático — nesse caso já não faz sentido repartir
            # greedily a partir do volume (a duração já está decidida,
            # não é para calcular), reparte-se em vez disso um ritmo
            # uniforme pelos dias ÚTEIS do intervalo escolhido (fins de
            # semana continuam a não produzir nada, mesmo numa duração
            # manual).
            duracao = max(1, a["duracao_dias"] or 1)
            inicio_manual = date.fromisoformat(a["dia_inicio"])
            dias_uteis = [inicio_manual + timedelta(days=i) for i in range(duracao)
                         if (inicio_manual + timedelta(days=i)).weekday() < 5]
            ritmo = a["volume_m3"] / max(1, len(dias_uteis))
            for dia_data in dias_uteis:
                dia = dia_data.isoformat()
                ocupacao[dia] = ocupacao.get(dia, 0) + ritmo
            continue
        _, alocacao = _repartir_greedy(capacidade, ocupacao, a["dia_inicio"], a["volume_m3"])
        for dia, valor in alocacao.items():
            ocupacao[dia] = ocupacao.get(dia, 0) + valor
    return ocupacao

def _prioridade(dia_inicio: str, basecamp_card_id: int = None) -> tuple:
    """Chave de prioridade (dia_inicio, basecamp_card_id) para desempate de
    fila entre OFs a começar no mesmo dia — quem chegou primeiro ao
    Basecamp (basecamp_card_id mais baixo) tem sempre prioridade sobre a
    capacidade desse dia. Sem id ainda (encomenda a ser criada agora, ver
    criar_encomenda), usa +infinito: uma OF novíssima nunca "corta a
    fila" a nenhuma que já esteja no quadro para o mesmo dia."""
    return (dia_inicio, basecamp_card_id if basecamp_card_id is not None else float("inf"))

def _duracao_por_volume(linha: str, volume_m3: float, dia_inicio: str = None,
                        excluir_id: int = None, ids_ativos: set = None) -> int:
    """Quantos dias uma encomenda ocupa numa linha, a partir do seu volume
    (m³) e da capacidade diária dessa linha (editável, ver
    atualizar_capacidade_linha) — a mais curta possível que caiba,
    repartindo greedily pelo espaço livre de cada dia a partir de
    `dia_inicio` (ver _dias_necessarios_greedy), respeitando a prioridade
    de quem chegou primeiro ao Basecamp em dias partilhados com outras OFs
    (ver _ocupacao_diaria, `antes_de`). Sem `dia_inicio` (ainda na fila,
    sem dia definido) ou sem capacidade configurada para a linha, usa só
    volume ÷ capacidade plena, sem olhar a ocupação (não há ainda dia
    nenhum para verificar). `ids_ativos` só poupa uma chamada ao Basecamp
    (ver _ocupacao_diaria) — passa em branco em condições normais."""
    if not volume_m3:
        return 1
    capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
    if capacidade <= 0:
        return 1
    duracao_minima = max(1, math.ceil(volume_m3 / capacidade))
    if not dia_inicio:
        return duracao_minima
    ocupacao = _ocupacao_diaria(linha, antes_de=_prioridade(dia_inicio, excluir_id),
                                excluir_id=excluir_id, ids_ativos=ids_ativos)
    dias = _dias_necessarios_greedy(capacidade, ocupacao, dia_inicio, volume_m3)
    return dias if dias is not None else duracao_minima  # não coube em espaço nenhum razoável — deixa _validar_capacidade recusar com a mensagem certa

def _validar_capacidade(linha: str, dia_inicio: str, duracao_dias: int,
                        volume_m3: float, excluir_id: int = None, ids_ativos: set = None) -> str:
    """Confirma que colocar esta OF (com este volume, repartido greedily
    pelo espaço livre de cada dia — ver _dias_necessarios_greedy) nesta
    linha, a partir deste dia, cabe dentro de `duracao_dias` dias sem
    ultrapassar a capacidade da linha em dia nenhum. Devolve uma mensagem
    de erro, ou None se estiver tudo bem. `ids_ativos` só poupa uma
    chamada ao Basecamp (ver _ocupacao_diaria)."""
    capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
    ocupacao = _ocupacao_diaria(linha, antes_de=_prioridade(dia_inicio, excluir_id),
                                excluir_id=excluir_id, ids_ativos=ids_ativos)
    inicio = date.fromisoformat(dia_inicio)
    if not volume_m3:
        for i in range(duracao_dias):
            dia = (inicio + timedelta(days=i)).isoformat()
            usado = ocupacao.get(dia, 0)
            if usado == float("inf"):
                return f"a linha {linha!r} está reservada por completo em {dia} por outra encomenda sem volume definido"
            if usado > 0:
                return (f"a linha {linha!r} já tem outra encomenda em {dia} — sem volume "
                        "definido, esta encomenda precisaria da linha só para ela nesse dia")
        return None
    if capacidade <= 0:
        # sem capacidade configurada para a linha: só bloqueia se algum
        # dia já estiver reservado por completo por uma OF sem volume
        for i in range(duracao_dias):
            dia = (inicio + timedelta(days=i)).isoformat()
            if ocupacao.get(dia, 0) == float("inf"):
                return f"a linha {linha!r} está reservada por completo em {dia} por outra encomenda sem volume definido"
        return None
    if _dias_necessarios_greedy(capacidade, ocupacao, dia_inicio, volume_m3, limite=duracao_dias) is None:
        faltam = float(volume_m3)
        for i in range(duracao_dias):
            dia = (inicio + timedelta(days=i)).isoformat()
            usado = ocupacao.get(dia, 0)
            faltam -= 0 if usado == float("inf") else max(0, capacidade - usado)
        return (f"não há capacidade suficiente na linha {linha!r} a partir de {dia_inicio} "
                f"em {duracao_dias} dia(s) — faltariam {max(0, faltam):.1f} m³")
    return None

DIAS_CURA_MADEIRA = {"seca": 4, "verde": 1}

def _calcular_dia_carregamento(dia_inicio: str, duracao_dias: int, tipo_madeira: str) -> str:
    """Dia de carregamento de uma OF: conta a partir do FIM da produção
    (dia_inicio + duração - 1) mais 4 dias (madeira seca) ou 1 dia
    (madeira verde) — pedido explícito do Rui (2026-09). Se calhar a
    sábado ou domingo, avança para a segunda-feira seguinte."""
    fim_producao = date.fromisoformat(dia_inicio) + timedelta(days=max(1, duracao_dias or 1) - 1)
    dia = fim_producao + timedelta(days=DIAS_CURA_MADEIRA[tipo_madeira])
    if dia.weekday() == 5:  # sábado
        dia += timedelta(days=2)
    elif dia.weekday() == 6:  # domingo
        dia += timedelta(days=1)
    return dia.isoformat()

def _talvez_duplicar_logistica(basecamp_card_id: int, dia_inicio: str, duracao_dias: int, tipo_madeira: str):
    """Cria o duplicado de logística/carregamento de uma OF, quando já tem
    linha/dia E tipo de madeira definidos — pedido explícito do Rui
    (2026-09), para a equipa da logística saber em que dia a OF estará
    pronta a carregar, assim que ela sair da fila.

    Se o duplicado já existir, recalcula o dia de carregamento a partir
    dos dados atuais e move-o se tiver mudado — pedido explícito do Rui
    (2026-09): sempre que a produção desta OF muda (linha, dia de início,
    ou o volume — que muda a duração), o dia de carregamento na logística
    tem de acompanhar, mesmo depois de já ter sido calculado uma vez (ex:
    a OF passa para a semana seguinte na produção → o carregamento também
    avança). A independência da logística mantém-se só no outro sentido:
    mover ou apagar o duplicado ali nunca mexe na produção."""
    if not dia_inicio or not tipo_madeira or tipo_madeira not in DIAS_CURA_MADEIRA:
        return
    dia_carregamento = _calcular_dia_carregamento(dia_inicio, duracao_dias, tipo_madeira)
    existente = db.logistica_carregamento(basecamp_card_id)
    if existente:
        if existente["dia_carregamento"] != dia_carregamento:
            db.mover_logistica_carregamento(basecamp_card_id, dia_carregamento)
        return
    db.criar_logistica_carregamento(basecamp_card_id, dia_carregamento)

def _recalcular_linha(linha: str, excluir_id: int = None, ids_ativos: set = None):
    """Recalcula e grava de novo a duração de TODAS as OFs já agendadas
    numa linha (exceto `excluir_id`, se dado — a que o próprio chamador
    acabou de guardar) — chamado sempre que algo pode ter mudado a
    ocupação partilhada da linha para as outras, para nenhuma ficar com a
    duração desatualizada.

    Bug real (Rui, 2026-09-28): mudar os m³ de uma encomenda, ou a
    capacidade da própria linha, só recalculava a duração dessa encomenda
    (ou não recalculava nada nenhuma, no caso da capacidade) — as outras
    OFs já colocadas na mesma linha ficavam com a duração antiga, por
    vezes claramente errada (ex: o Navalon, sozinho e a caber MUITO bem
    numa linha, aparecia a precisar de vários dias só porque uma
    encomenda vizinha, entretanto mudada, já não justificava esse
    espalhamento). _ocupacao_diaria já ignora a duração guardada de
    qualquer OF com volume (deriva sempre a alocação real a partir do seu
    volume+dia_inicio, ver ali) — por isso recalcular aqui, em qualquer
    ordem, dá sempre o resultado global correto, sem precisar de tocar
    duas vezes na mesma OF.

    Nunca toca numa OF com `duracao_manual` (dia de fim definido à mão,
    ver redefinir_fim) — essa duração foi escolhida deliberadamente pela
    equipa, sobrepondo-se ao cálculo automático; recalculá-la aqui
    apagaria essa escolha sem ninguém pedir.

    `ids_ativos`, se dado, poupa repetir o pedido ao Basecamp uma vez por
    OF da linha (lento) — busca-se aqui UMA VEZ e passa-se para cada
    chamada de _duracao_por_volume."""
    if ids_ativos is None:
        ids_ativos = {c["id"] for c in _cards_of_ativos()}
    for a in db.agendamentos_producao_ecos_largos():
        if (a["linha"] != linha or not a["dia_inicio"] or not a["volume_m3"]
                or a["basecamp_card_id"] == excluir_id or a["basecamp_card_id"] not in ids_ativos
                or a.get("duracao_manual")):
            continue
        nova_duracao = _duracao_por_volume(linha, a["volume_m3"], a["dia_inicio"],
                                           excluir_id=a["basecamp_card_id"], ids_ativos=ids_ativos)
        if nova_duracao == a["duracao_dias"]:
            continue
        db.guardar_agendamento_producao(a["basecamp_card_id"], linha, a["dia_inicio"],
                                        nova_duracao, a["volume_m3"])
        _talvez_duplicar_logistica(a["basecamp_card_id"], a["dia_inicio"], nova_duracao, a["tipo_madeira"])

def agendar(basecamp_card_id: int, linha: str, dia_inicio: str, volume_m3: float = None) -> dict:
    """Agenda (ou reagenda) uma OF numa linha/dia — quase tudo fica só na
    base local (ver nota no topo do módulo), à exceção do próprio dia de
    início, que é também escrito no "Due on" do card real no Basecamp
    (pedido explícito do Rui, 2026-09-30) sempre que muda. A duração é
    sempre calculada aqui a partir do volume e da capacidade da linha (ver
    _duracao_por_volume), nunca escolhida à mão. Se `volume_m3` não for
    indicado, mantém o volume já guardado anteriormente para esta OF (não
    o apaga só por não vir neste pedido). Recusa o agendamento (ver
    _validar_capacidade) se ultrapassar a capacidade da linha nalgum dos
    dias ocupados, ou se `dia_inicio` cair num sábado ou domingo — pedido
    explícito do Rui (2026-09-30): a linha nunca produz ao fim de semana
    (ver _repartir_greedy), por isso não faz sentido nenhuma OF começar
    nesse dia; ao recusar, o arrastar no quadro reverte sozinho para onde
    a OF estava antes (fila ou linha anterior, ver guardarAgendamento no
    template). Se já tiver tipo de madeira definido, duplica para a
    logística (ver _talvez_duplicar_logistica).

    Depois de guardar, recalcula as outras OFs da(s) linha(s) afetada(s)
    (a nova, e a antiga se a OF mudou de linha — ver _recalcular_linha):
    mudar os m³ ou o dia desta OF pode libertar ou ocupar espaço que
    outras já lá colocadas estavam a usar/precisar, e essas nunca são
    tocadas por nenhum outro pedido a não ser este."""
    if linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    if not dia_inicio:
        return {"erro": "falta indicar o dia de início"}
    try:
        if date.fromisoformat(dia_inicio).weekday() >= 5:
            return {"erro": "não é possível começar a produção num fim de semana — escolhe um dia útil"}
    except (TypeError, ValueError):
        return {"erro": f"dia de início inválido: {dia_inicio!r}"}
    existente = db.agendamento_producao(basecamp_card_id)
    if volume_m3 is not None:
        try:
            volume_m3 = float(volume_m3)
        except (TypeError, ValueError):
            return {"erro": "volume inválido"}
        if volume_m3 <= 0:
            return {"erro": "volume tem de ser maior que 0"}
    else:
        volume_m3 = existente["volume_m3"] if existente else None
    ids_ativos = {c["id"] for c in _cards_of_ativos()}
    duracao_dias = _duracao_por_volume(linha, volume_m3, dia_inicio, excluir_id=basecamp_card_id, ids_ativos=ids_ativos)
    erro = _validar_capacidade(linha, dia_inicio, duracao_dias, volume_m3, excluir_id=basecamp_card_id, ids_ativos=ids_ativos)
    if erro:
        return {"erro": erro}
    db.guardar_agendamento_producao(basecamp_card_id, linha, dia_inicio, duracao_dias, volume_m3)
    if not existente or existente["dia_inicio"] != dia_inicio:
        # melhor esforço: uma falha aqui (ex: Basecamp em baixo) não pode
        # impedir o agendamento local, que é a fonte de verdade real do
        # plano — só o "Due on" no Basecamp fica por atualizar.
        try:
            basecamp.atualizar_prazo_card(basecamp_card_id, dia_inicio, projeto=PROJETO)
            _invalidar_cache_cards_ativos()
        except Exception as e:
            print(f"[planeamento_serracao] falhou a escrever o dia de início no \"Due on\" "
                  f"do Basecamp (card {basecamp_card_id}): {e}")
    tipo_madeira = existente["tipo_madeira"] if existente else None
    _talvez_duplicar_logistica(basecamp_card_id, dia_inicio, duracao_dias, tipo_madeira)
    linha_antiga = existente["linha"] if existente else None
    if linha_antiga and linha_antiga != linha:
        _recalcular_linha(linha_antiga, ids_ativos=ids_ativos)
    _recalcular_linha(linha, excluir_id=basecamp_card_id, ids_ativos=ids_ativos)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id,
            "duracao_dias": duracao_dias, "volume_m3": volume_m3}


def redefinir_fim(basecamp_card_id: int, dia_fim: str) -> dict:
    """Define à mão o dia de fim de produção de uma OF já agendada,
    sobrepondo-se ao cálculo automático a partir do volume (pedido
    explícito do Rui, 2026-09-28) — para quando a realidade da produção
    diverge do que o modelo previu. Marca a OF como `duracao_manual` (ver
    _ocupacao_diaria e _recalcular_linha): fica de fora do recálculo
    automático a partir daqui, até o dia de início, a linha ou o volume
    voltarem a ser alterados pelo fluxo normal (agendar), que limpa esta
    marca de novo.

    Avisa se isto ultrapassar a capacidade da linha (repartindo o volume
    em partes iguais pelos dias úteis do intervalo escolhido — fim de
    semana nunca produz, mesmo numa duração manual, ver _ocupacao_diaria)
    mas guarda sempre na mesma (pedido explícito do Rui, 2026-09-29): ao
    contrário do agendamento automático, aqui a equipa está deliberadamente
    a corrigir o modelo com a realidade da produção — se souberem que cabe
    na mesma (ex: um turno extra), não faz sentido o aviso bloquear a
    gravação. O aviso vem no campo `aviso` da resposta, não em `erro`."""
    existente = db.agendamento_producao(basecamp_card_id)
    if not existente or not existente["linha"] or not existente["dia_inicio"]:
        return {"erro": "esta OF ainda não está agendada numa linha/dia"}
    try:
        fim = date.fromisoformat(dia_fim)
    except (TypeError, ValueError):
        return {"erro": f"data de fim inválida: {dia_fim!r}"}
    inicio = date.fromisoformat(existente["dia_inicio"])
    if fim < inicio:
        return {"erro": "o dia de fim não pode ser antes do dia de início"}
    duracao_dias = (fim - inicio).days + 1
    linha = existente["linha"]
    volume_m3 = existente["volume_m3"]
    ids_ativos = {c["id"] for c in _cards_of_ativos()}
    aviso = None
    if volume_m3:
        capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
        if capacidade > 0:
            dias_uteis = [inicio + timedelta(days=i) for i in range(duracao_dias)
                         if (inicio + timedelta(days=i)).weekday() < 5]
            if not dias_uteis:
                aviso = "este intervalo não tem nenhum dia útil — fim de semana nunca produz, ficaria sem nenhum m³ atribuído"
            else:
                ritmo = volume_m3 / len(dias_uteis)
                ocupacao = _ocupacao_diaria(linha, antes_de=_prioridade(existente["dia_inicio"], basecamp_card_id),
                                            excluir_id=basecamp_card_id, ids_ativos=ids_ativos)
                for dia_data in dias_uteis:
                    usado = ocupacao.get(dia_data.isoformat(), 0)
                    if usado == float("inf") or usado + ritmo > capacidade + 1e-9:
                        aviso = (f"não cabe na linha {linha!r}: em {dia_data.isoformat()} já estaria(m) "
                                 f"ocupado(s) {('a linha toda' if usado == float('inf') else f'{usado:.1f} m³ de {capacidade:.1f}')}"
                                 f" — com este fim precisarias de mais {ritmo:.1f} m³ nesse dia")
                        break
    db.guardar_agendamento_producao(basecamp_card_id, linha, existente["dia_inicio"], duracao_dias,
                                    volume_m3, duracao_manual=True)
    _talvez_duplicar_logistica(basecamp_card_id, existente["dia_inicio"], duracao_dias, existente["tipo_madeira"])
    _recalcular_linha(linha, excluir_id=basecamp_card_id, ids_ativos=ids_ativos)
    resultado = {"guardado": True, "basecamp_card_id": basecamp_card_id, "duracao_dias": duracao_dias}
    if aviso:
        resultado["aviso"] = aviso
    return resultado

def atualizar_capacidade_linha(linha: str, capacidade_m3_dia: float) -> dict:
    """Atualiza a capacidade (m³/dia) de uma linha — editável pela equipa
    diretamente no quadro (pedido explícito do Rui, 2026-09) — e recalcula
    logo a seguir a duração de todas as OFs já agendadas nessa linha (ver
    _recalcular_linha), que passam a caber de forma diferente com a nova
    capacidade."""
    if linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    try:
        capacidade_m3_dia = float(capacidade_m3_dia)
    except (TypeError, ValueError):
        return {"erro": "capacidade inválida"}
    if capacidade_m3_dia <= 0:
        return {"erro": "capacidade tem de ser maior que 0"}
    resultado = db.atualizar_capacidade_linha_producao(linha, capacidade_m3_dia)
    _recalcular_linha(linha)
    return resultado

def atualizar_cor_estado(estado: str, cor: str) -> dict:
    """Atualiza a cor automática de fundo de um estado/coluna do Basecamp
    (ver ESTADOS_COR) — editável pela equipa, tal como a capacidade de
    cada linha (pedido explícito do Rui, 2026-09: as duas paletas de cor,
    a manual por OF e a automática por estado, devem poder ser mudadas)."""
    if estado not in ESTADOS_COR:
        return {"erro": f"estado desconhecido: {estado!r} — usa uma de {sorted(ESTADOS_COR)}"}
    cor = (cor or "").strip().lower()
    if cor not in CORES_VALIDAS:
        return {"erro": f"cor desconhecida: {cor!r} — usa uma de {sorted(CORES_VALIDAS)}"}
    return db.atualizar_cor_estado_producao(estado, cor)

def desagendar(basecamp_card_id: int) -> dict:
    """Devolve uma OF à bolsa por agendar — só na base local.

    Bug real (Rui, 2026-09-29): tal como apagar uma OF, devolvê-la à fila
    também liberta a capacidade que ela ocupava na linha, mas as outras
    OFs a seguir a ela na mesma linha ficavam com a duração desatualizada
    (ver _recalcular_linha) — ninguém a chamava aqui. Recalcula a linha
    de onde a OF saiu, tal como os outros pontos que libertam ou ocupam
    espaço partilhado numa linha já fazem."""
    existente = db.agendamento_producao(basecamp_card_id)
    resultado = db.desagendar_producao(basecamp_card_id)
    if existente and existente["linha"] and existente["dia_inicio"]:
        _recalcular_linha(existente["linha"])
    return resultado

def definir_volume(basecamp_card_id: int, volume_m3: float) -> dict:
    """Define/atualiza o volume (m³) de uma OF ainda na fila (sem linha/dia
    atribuídos) — para uma encomenda já agendada, usa antes agendar (que
    também recalcula a duração)."""
    try:
        volume_m3 = float(volume_m3)
    except (TypeError, ValueError):
        return {"erro": "volume inválido"}
    if volume_m3 <= 0:
        return {"erro": "volume tem de ser maior que 0"}
    return db.guardar_volume_producao(basecamp_card_id, volume_m3)

def definir_cor(basecamp_card_id: int, cor: str) -> dict:
    """Define ou limpa a cor manual de uma OF (lista fixa, ver
    CORES_VALIDAS) — funciona tanto na fila como já agendada. `cor` vazio
    ou None limpa a cor manual e volta à cor automática (ver nota junto de
    CORES_VALIDAS)."""
    cor = (cor or "").strip().lower() or None
    if cor and cor not in CORES_VALIDAS:
        return {"erro": f"cor desconhecida: {cor!r} — usa uma de {sorted(CORES_VALIDAS)}"}
    db.guardar_cor_producao(basecamp_card_id, cor)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "cor": cor}

def definir_cor_fundo(basecamp_card_id: int, cor: str) -> dict:
    """Define ou limpa a cor de fundo manual de uma OF (mesma lista fixa,
    ver CORES_VALIDAS) — campo independente da cor da barra lateral
    (definir_cor). `cor` vazio ou None limpa a cor de fundo manual e volta
    à cor automática por estado. É um campo único por OF (guardado só no
    agendamento de produção) — usado tanto no card de produção como no
    de logística, para uma mudança aqui se ver nas duas tabelas (pedido
    explícito do Rui, 2026-09)."""
    cor = (cor or "").strip().lower() or None
    if cor and cor not in CORES_VALIDAS:
        return {"erro": f"cor desconhecida: {cor!r} — usa uma de {sorted(CORES_VALIDAS)}"}
    db.guardar_cor_fundo_producao(basecamp_card_id, cor)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "cor_fundo": cor}

def reordenar(basecamp_card_id: int, direcao: str) -> dict:
    """Troca a posição de empilhamento de uma OF com a sua vizinha
    imediata, entre as OFs agendadas no mesmo dia/linha — pedido explícito
    do Rui (2026-09): poder escolher qual aparece primeiro quando há mais
    do que uma OF no mesmo dia/linha. `direcao` é "cima" ou "baixo"."""
    if direcao not in ("cima", "baixo"):
        return {"erro": "direção inválida — usa \"cima\" ou \"baixo\""}
    agendamentos = db.agendamentos_producao_ecos_largos()
    alvo = next((a for a in agendamentos if a["basecamp_card_id"] == basecamp_card_id), None)
    if not alvo or not alvo["linha"] or not alvo["dia_inicio"]:
        return {"erro": "esta OF não está agendada"}
    grupo = [a for a in agendamentos if a["linha"] == alvo["linha"] and a["dia_inicio"] == alvo["dia_inicio"]]
    grupo.sort(key=lambda a: (a["ordem"] or 0, a["basecamp_card_id"]))
    posicao = next(i for i, a in enumerate(grupo) if a["basecamp_card_id"] == basecamp_card_id)
    troca_com = posicao - 1 if direcao == "cima" else posicao + 1
    if troca_com < 0 or troca_com >= len(grupo):
        return {"erro": "já está no topo" if direcao == "cima" else "já está no fundo"}
    grupo[posicao], grupo[troca_com] = grupo[troca_com], grupo[posicao]
    for i, a in enumerate(grupo):
        db.atualizar_ordem_producao(a["basecamp_card_id"], i)
    return {"trocado": True}

def apagar_encomenda(basecamp_card_id: int) -> dict:
    """Apaga uma encomenda por completo: manda o card real para o lixo do
    Basecamp (ver basecamp.apagar_card — reversível lá, durante algum
    tempo, tal como apagar manualmente) e remove o agendamento local e o
    duplicado de logística, se existirem. Ação a usar só quando for mesmo
    preciso (ex: encomenda criada por engano) — pedido explícito do Rui,
    2026-09.

    Bug real (Rui, 2026-09-29): apagar uma OF já agendada libertava a
    capacidade que ela ocupava numa linha partilhada, mas as outras OFs
    já colocadas a seguir a ela ficavam com a duração antiga (calculada
    contando ainda com o espaço que esta ocupava) — o mesmo "problema dos
    dias" já corrigido para mudar m³/capacidade, só que aqui ninguém
    chamava _recalcular_linha nenhuma. Por isso recalcula a linha depois
    de apagar, tal como agendar/atualizar_capacidade_linha já fazem."""
    basecamp.apagar_card(basecamp_card_id, projeto=PROJETO)
    _invalidar_cache_cards_ativos()
    _remover_agendamento_local(basecamp_card_id)
    return {"apagado": True, "basecamp_card_id": basecamp_card_id}

def _remover_agendamento_local(basecamp_card_id: int, ids_ativos: set = None) -> None:
    """Remove o agendamento local e o duplicado de logística de uma OF, e
    recalcula a linha libertada — partilhado por apagar_encomenda (que
    também manda o card para o lixo do Basecamp) e por _cards_of_ativos
    (quando deteta um card já apagado diretamente no Basecamp, sem passar
    por aqui — pedido explícito do Rui, 2026-09-25: antes ficava "preso"
    no quadro de planeamento para sempre, mesmo já não existindo do outro
    lado). `ids_ativos`, se dado, poupa a _recalcular_linha ter de pedir os
    cards ativos outra vez (ver _cards_of_ativos, que já os tem à mão
    quando chama isto)."""
    existente = db.agendamento_producao(basecamp_card_id)
    db.remover_agendamento_producao(basecamp_card_id)
    db.remover_logistica_carregamento(basecamp_card_id)
    if existente and existente["linha"] and existente["dia_inicio"]:
        _recalcular_linha(existente["linha"], ids_ativos=ids_ativos)

def renomear_encomenda(basecamp_card_id: int, titulo: str) -> dict:
    """Muda o título do card real no Basecamp — pedido explícito do Rui
    (2026-09): poder corrigir/editar o nome de uma encomenda mesmo depois
    de já estar agendada na tabela (bolsa ou já numa linha), sem ter de
    ir ao Basecamp à parte. O título não é guardado localmente em lado
    nenhum — o Basecamp continua a ser a única fonte deste campo, tal
    como para a coluna/prazo."""
    titulo = (titulo or "").strip()
    if not titulo:
        return {"erro": "o nome não pode ficar vazio"}
    basecamp.atualizar_titulo_card(basecamp_card_id, titulo, projeto=PROJETO)
    _invalidar_cache_cards_ativos()
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "titulo": titulo}


def mover_logistica(basecamp_card_id: int, dia_carregamento: str) -> dict:
    """Muda manualmente o dia de carregamento de uma OF já duplicada —
    independente da produção a partir daí (ver nota da tabela em db.py)."""
    if not dia_carregamento:
        return {"erro": "falta indicar o dia de carregamento"}
    if not db.logistica_carregamento(basecamp_card_id):
        return {"erro": "esta OF ainda não tem duplicado de logística"}
    return db.mover_logistica_carregamento(basecamp_card_id, dia_carregamento)

def definir_quem_carrega(basecamp_card_id: int, quem_carrega: str) -> dict:
    """Define/limpa quem carrega uma OF — preenchido à mão pela equipa da
    logística (pedido explícito do Rui, 2026-09), para depois se saber
    quem fez o carregamento."""
    if not db.logistica_carregamento(basecamp_card_id):
        return {"erro": "esta OF ainda não tem duplicado de logística"}
    quem_carrega = (quem_carrega or "").strip() or None
    db.guardar_quem_carrega_logistica(basecamp_card_id, quem_carrega)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "quem_carrega": quem_carrega}

def apagar_logistica(basecamp_card_id: int) -> dict:
    """Remove só o duplicado de logística de uma OF — não toca no
    agendamento de produção nem no card real no Basecamp."""
    db.remover_logistica_carregamento(basecamp_card_id)
    return {"apagado": True, "basecamp_card_id": basecamp_card_id}

TIPOS_MADEIRA = {"seca": "Seca", "verde": "Verde"}

def _numero_curto(basecamp_card_id) -> str:
    """Últimos 4 dígitos do id do card — pedido explícito do Rui
    (2026-09-30): o id completo (ex: 10285829501) é comprido demais para
    mostrar em cada card do quadro; os últimos 4 dígitos (ex: 9501) já
    bastam para diferenciar as OFs visíveis ao mesmo tempo (ver numCurto
    no template). Usado também para o mesmo número aparecer nas notas do
    card real no Basecamp (ver _acrescentar_numero_notas), para dar para
    cruzar os dois."""
    return str(basecamp_card_id)[-4:]

def _acrescentar_numero_notas(basecamp_card_id: int, notas_atuais: str) -> bool:
    """Acrescenta 'Nº: <últimos 4 dígitos>' no topo das notas de um card
    no Basecamp, se ainda lá não estiver (idempotente — nunca duplica a
    linha se já lá estiver, por isso é seguro chamar outra vez sobre o
    mesmo card). Devolve True se escreveu mesmo algo no Basecamp, False
    se já estava lá e nada mudou."""
    marcador = f"Nº: {_numero_curto(basecamp_card_id)}"
    if marcador in (notas_atuais or ""):
        return False
    notas_novas = f"{marcador}\n\n{notas_atuais}" if notas_atuais else marcador
    basecamp.atualizar_notas_card(basecamp_card_id, notas_novas, projeto=PROJETO)
    return True

def criar_encomenda(titulo: str, cliente: str = "", volume_m3: float = None, tipo_madeira: str = None,
                    notas: str = "", linha: str = None, dia_inicio: str = None) -> dict:
    """Cria uma encomenda nova: um card real na coluna Triagem do Basecamp
    (ver basecamp.criar_card, título "Cliente — Peça" e notas com o
    cliente/volume/tipo de madeira) e, se já vier com linha/dia, o
    agendamento local logo a acompanhar (duração calculada a partir do
    volume e da capacidade da linha — ver agendar). A partir de criado,
    este card passa a ser totalmente independente — ver nota no topo do
    módulo. `tipo_madeira` é "seca" ou "verde" (opcional)."""
    titulo = (titulo or "").strip()
    cliente = (cliente or "").strip()
    tipo_madeira = (tipo_madeira or "").strip().lower() or None
    if not titulo:
        return {"erro": "indica a peça (título) da encomenda"}
    if linha and linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    if tipo_madeira and tipo_madeira not in TIPOS_MADEIRA:
        return {"erro": f"tipo de madeira desconhecido: {tipo_madeira!r} — usa \"seca\" ou \"verde\""}
    if volume_m3 is not None:
        try:
            volume_m3 = float(volume_m3)
        except (TypeError, ValueError):
            return {"erro": "volume inválido"}
        if volume_m3 <= 0:
            return {"erro": "volume tem de ser maior que 0"}
    if linha and dia_inicio:
        # valida a capacidade ANTES de criar o card no Basecamp — para uma
        # colocação recusada nunca deixar para trás um card órfão lá.
        duracao_dias = _duracao_por_volume(linha, volume_m3, dia_inicio)
        erro = _validar_capacidade(linha, dia_inicio, duracao_dias, volume_m3)
        if erro:
            return {"erro": erro}
    titulo_basecamp = f"{cliente} — {titulo}" if cliente else titulo
    partes_notas = []
    if cliente:
        partes_notas.append(f"Cliente: {cliente}")
    if volume_m3:
        partes_notas.append(f"Volume: {volume_m3} m³")
    if tipo_madeira:
        partes_notas.append(f"Madeira: {TIPOS_MADEIRA[tipo_madeira]}")
    if notas:
        partes_notas.append(notas)
    notas_iniciais = "\n".join(partes_notas)
    card = basecamp.criar_card("Triagem", titulo_basecamp, notas_iniciais, projeto=PROJETO)
    _acrescentar_numero_notas(card["id"], notas_iniciais)
    _invalidar_cache_cards_ativos()
    if tipo_madeira:
        db.guardar_tipo_madeira_producao(card["id"], tipo_madeira)
    resultado = {
        "basecamp_card_id": card["id"],
        "titulo": card["titulo"],
        "coluna_basecamp": card["estado"],
        "prazo": card["prazo"],
        "url": card["url"],
        "volume_m3": volume_m3,
        "tipo_madeira": tipo_madeira,
    }
    if linha and dia_inicio:
        # ver agendar(): lê o tipo de madeira já guardado acima e duplica
        # logo para a logística, se aplicável.
        agendamento = agendar(card["id"], linha, dia_inicio, volume_m3)
        resultado.update({"linha": linha, "dia_inicio": dia_inicio,
                          "duracao_dias": agendamento.get("duracao_dias", 1)})
    elif volume_m3:
        # ainda fica na fila (sem linha/dia), mas guarda já o volume para
        # não se perder quando for agendada mais tarde.
        db.guardar_volume_producao(card["id"], volume_m3)
    return resultado

def duplicar_encomenda(basecamp_card_id: int) -> dict:
    """Duplica uma encomenda: cria um card novo no Basecamp (coluna
    Triagem, mesmo título com " (cópia)" a seguir), copiando o volume, o
    tipo de madeira e as cores desta OF — mas sempre para a fila por
    agendar, nunca já numa linha/dia (mesmo que o original já estivesse
    agendado) — pedido explícito do Rui (2026-09-29), como o "duplicar" do
    Google Calendar: a cópia fica pronta a arrastar para onde for preciso,
    sem herdar o lugar do original. Só para cards de produção — os de
    logística não têm esta opção (são sempre derivados automaticamente de
    uma OF de produção, ver _talvez_duplicar_logistica). O título não vive
    localmente em lado nenhum (ver renomear_encomenda) — por isso vai
    buscá-lo de novo ao Basecamp, aos cards ativos."""
    original = next((c for c in _cards_of_ativos() if c["id"] == basecamp_card_id), None)
    if not original:
        return {"erro": "encomenda não encontrada"}
    existente = db.agendamento_producao(basecamp_card_id)
    volume_m3 = existente["volume_m3"] if existente else None
    tipo_madeira = existente["tipo_madeira"] if existente else None
    cor = existente["cor"] if existente else None
    cor_fundo = existente["cor_fundo"] if existente else None
    card = basecamp.criar_card("Triagem", f"{original['titulo']} (cópia)", "", projeto=PROJETO)
    _acrescentar_numero_notas(card["id"], "")
    _invalidar_cache_cards_ativos()
    if volume_m3:
        db.guardar_volume_producao(card["id"], volume_m3)
    if tipo_madeira:
        db.guardar_tipo_madeira_producao(card["id"], tipo_madeira)
    if cor:
        db.guardar_cor_producao(card["id"], cor)
    if cor_fundo:
        db.guardar_cor_fundo_producao(card["id"], cor_fundo)
    return {"duplicado": True, "basecamp_card_id": card["id"], "titulo": card["titulo"],
            "coluna_basecamp": card["estado"], "url": card["url"], "volume_m3": volume_m3,
            "tipo_madeira": tipo_madeira, "cor": cor, "cor_fundo": cor_fundo}

def definir_tipo_madeira(basecamp_card_id: int, tipo_madeira: str) -> dict:
    """Define o tipo de madeira (seca/verde) de uma OF — se ela já
    estiver agendada e ainda não tiver duplicado de logística, cria-o
    agora (ver _talvez_duplicar_logistica): cobre o caso de uma OF que foi
    agendada antes de se saber o tipo de madeira."""
    tipo_madeira = (tipo_madeira or "").strip().lower()
    if tipo_madeira not in TIPOS_MADEIRA:
        return {"erro": "tipo de madeira desconhecido — usa \"seca\" ou \"verde\""}
    db.guardar_tipo_madeira_producao(basecamp_card_id, tipo_madeira)
    existente = db.agendamento_producao(basecamp_card_id)
    if existente and existente["linha"] and existente["dia_inicio"]:
        _talvez_duplicar_logistica(basecamp_card_id, existente["dia_inicio"],
                                   existente["duracao_dias"], tipo_madeira)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "tipo_madeira": tipo_madeira}

def pagina_planeamento() -> str:
    return _TEMPLATE

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Planeamento da serração — Ecos Largos</title>
<style>
  :root{
    --paper:#FFFFFF; --canvas:#F5F5F3; --raise:#FBFBF9;
    --ink:#1A1C1E; --dim:#75797D;
    --line:#E8E8E3; --edge:#D9D9D2;
    --blue:#1B6AC9; --gold:#E0A02C; --red:#C4452E; --grey:#9AA0A6; --hoje:#FFF6D6;
    --day:92px; --lane:78px; --laneLog:78px; --label:180px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--canvas);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
    font-size:15px;line-height:1.4;-webkit-font-smoothing:antialiased}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
  button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
  .wrap{max-width:1200px;margin:0 auto;padding:22px 14px 120px}

  header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px}
  h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0}
  .sub{color:var(--dim);font-size:14px}

  .nav{display:flex;align-items:center;gap:8px;margin:16px 0 10px;flex-wrap:wrap}
  .seg{display:flex;background:#EAEAE6;border-radius:9px;padding:3px}
  .seg button{padding:5px 13px;font-size:14px;color:var(--dim);border-radius:7px;font-weight:500}
  .seg button:hover{color:var(--ink)}
  .seg button.on{background:var(--paper);color:var(--ink);font-weight:600;
    box-shadow:0 1px 2px rgba(0,0,0,.10)}
  .step{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    width:32px;height:32px;color:var(--dim);font-size:17px;line-height:1}
  .step:hover{color:var(--ink);border-color:var(--dim)}
  .range{font-size:16px;font-weight:700;min-width:150px}
  .search{margin-left:auto;position:relative;display:flex;align-items:center}
  .search input{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:6px 12px 6px 30px;font-size:14px;width:220px;color:var(--ink)}
  .search input:focus{outline:2px solid var(--blue);outline-offset:0;border-color:var(--blue)}
  .search::before{content:"";position:absolute;left:10px;top:50%;width:13px;height:13px;
    transform:translateY(-50%);border:2px solid var(--dim);border-radius:50%;pointer-events:none}
  .search::after{content:"";position:absolute;left:20px;top:50%;width:7px;height:2px;
    transform:translateY(6px) rotate(45deg);background:var(--dim);pointer-events:none}
  .busca-dim{opacity:.22}
  .busca-match{box-shadow:0 0 0 2px var(--blue),0 4px 14px rgba(25,118,210,.35);z-index:15}

  .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 16px}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--paper);
    border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13.5px;color:var(--dim)}
  .pill b{color:var(--ink);font-weight:700}
  .legenda{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:10px 0 18px;font-size:11.5px;color:var(--dim)}
  .legendaItem{display:inline-flex;align-items:center;gap:6px}
  .legendaSwatch{width:13px;height:13px;border-radius:3px;flex-shrink:0}
  .legendaBorda{background:#fff;box-shadow:0 0 0 2px var(--red) inset}
  .dot{width:8px;height:8px;border-radius:50%;background:#4E9A51}
  .dot.busy{background:var(--gold);animation:blink .7s infinite alternate}
  @keyframes blink{to{opacity:.25}}
  .btn{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:6px 12px;font-size:13.5px;color:var(--blue);font-weight:500}
  .btn:hover{border-color:var(--dim)}
  .btn:focus-visible,.step:focus-visible,.seg button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
  .warn{color:var(--red)}

  .fila{background:var(--paper);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .filaHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
  .filaHead h2{margin:0;font-size:15px;font-weight:700;color:var(--ink)}
  .filaRow{display:flex;gap:10px;overflow-x:auto;padding:2px 2px 4px}
  .qcard{position:relative;flex:0 0 auto;min-width:190px;max-width:230px;background:var(--paper);border:1px solid var(--line);
    border-left:10px solid var(--grey);border-radius:9px;padding:9px 11px;touch-action:none;cursor:grab;
    box-shadow:0 1px 2px rgba(0,0,0,.07)}
  .qcard:hover{box-shadow:0 2px 6px rgba(0,0,0,.10)}
  .qcard .tt{font-size:14.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .qcard .of{font-size:12px;color:var(--dim);margin-top:2px}
  .empty{color:var(--dim);font-size:13.5px;padding:6px 0}

  .secTit{font-size:15px;font-weight:700;color:var(--ink);margin:22px 0 10px}
  .lblLog{cursor:default}
  .lblLog:hover{background:transparent}

  .board{display:flex;background:var(--paper);border:1px solid var(--line);
    border-radius:12px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .labels{flex:0 0 var(--label);border-right:1px solid var(--edge);background:var(--raise)}
  .labels .head{height:48px;border-bottom:1px solid var(--edge)}
  .lbl{height:var(--lane);border-bottom:1px solid var(--line);padding:10px 12px;
    display:flex;flex-direction:column;justify-content:center;cursor:pointer}
  .lbl:hover{background:var(--canvas)}
  .lbl:last-child{border-bottom:none}
  .lbl .n{font-size:14px;font-weight:700}
  .lbl .c{font-size:11.5px;color:var(--dim);margin-top:2px}
  .lbl .c:hover{color:var(--blue);text-decoration:underline}

  .scroll{flex:1;overflow-x:auto;overflow-y:hidden}
  .track{position:relative}
  .days{display:flex;height:48px;border-bottom:1px solid var(--edge)}
  .day{flex:0 0 var(--day);border-right:1px solid var(--line);padding:8px 10px;overflow:hidden}
  .day.wk{border-right:1px solid var(--edge)}
  .day .dn{font-size:13.5px;font-weight:700;white-space:nowrap}
  .day .dm{font-size:11.5px;color:var(--dim);white-space:nowrap}
  .day.sab .dn{color:var(--dim)}
  .day.hoje{background:var(--hoje);box-shadow:inset 0 -3px 0 var(--gold)}
  .dense .day{padding:8px 4px;text-align:center}
  .dense .day .dn{font-size:12.5px}
  .dense .day .dm{font-size:10px}

  .lanes{position:relative}
  .row{display:flex;height:var(--lane);border-bottom:1px solid var(--line)}
  #labelsLog .lbl,#lanesLog .row{height:var(--laneLog)}
  .row:last-child{border-bottom:none}
  .cell{flex:0 0 var(--day);border-right:1px solid var(--line);position:relative;background:var(--paper)}
  .cell.wk{border-right:1px solid var(--edge)}
  .cell.hoje{background:#FFFDF4}

  .blocks{position:absolute;inset:0;pointer-events:none}
  .blk{position:absolute;box-sizing:border-box;
    background:var(--paper);border:1px solid var(--line);border-left:10px solid var(--grey);
    border-radius:9px;padding:5px 9px;overflow:hidden;pointer-events:auto;cursor:grab;
    touch-action:none;user-select:none;box-shadow:0 1px 2px rgba(0,0,0,.09)}
  .blk:hover{box-shadow:0 2px 7px rgba(0,0,0,.13)}
  .blk:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
  .blk .tt{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .blk .of{font-size:11.5px;color:var(--dim);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dense .blk{padding:3px 6px;border-radius:7px}
  .dense .blk .of{font-size:10px}
  .dense .blk .tt{font-size:12px}
  .blk.drag{cursor:grabbing;box-shadow:0 8px 22px rgba(0,0,0,.22);z-index:9;border-color:var(--blue)}
  .blk.flash,.qcard.flash{animation:flash 1s ease-out}
  @keyframes flash{0%{background:var(--hoje)}100%{background:var(--paper)}}
  .blk.clipL{border-top-left-radius:0;border-bottom-left-radius:0;border-left-style:dashed}
  .blk.clipR{border-top-right-radius:0;border-bottom-right-radius:0;border-right:1px dashed var(--edge)}
  .ghost{position:fixed;z-index:99;pointer-events:none;box-shadow:0 8px 22px rgba(0,0,0,.22);opacity:.95}

  .blk.selecionado,.qcard.selecionado{outline:2px solid var(--blue);outline-offset:1px;z-index:8}
  .blk.atrasado,.qcard.atrasado{box-shadow:0 0 0 2px var(--red) inset}
  .editBtn{position:absolute;top:2px;right:2px;width:19px;height:19px;line-height:19px;
    text-align:center;border-radius:5px;background:rgba(255,255,255,.85);color:var(--dim);
    font-size:11px;cursor:pointer;pointer-events:auto}
  .editBtn:hover{background:#fff;color:var(--ink)}
  .qcard .editBtn{background:var(--canvas)}

  /* modo só consulta (pedido explícito do Rui, 2026-09-29): a página abre
     sempre assim, sem nenhum controlo de edição visível — só o botão
     "Editar" liga tudo isto de novo. Continua a ser possível clicar num
     card para abrir a ficha e ver a informação (pedido explícito do Rui,
     2026-09-25) — só que aí todos os campos ficam desativados e os
     botões de guardar/apagar/duplicar não aparecem (ver o parâmetro
     "somenteLeitura" de openSheet/openSheetLogistica). Esconder por CSS
     não basta sozinho (ver os "if(!modoEdicao)return" nos handlers de
     arrastar) mas garante que nada disto aparece clicável por engano. */
  body.viewonly .editBtn,body.viewonly #novo,body.viewonly #undo,
  body.viewonly #painelCoresEstado{display:none}
  body.viewonly .lbl{pointer-events:none}
  body.viewonly .blk,body.viewonly .qcard{cursor:pointer}

  .log{margin-top:14px;background:var(--paper);border:1px solid var(--line);
    border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .log summary{padding:12px 16px;cursor:pointer;font-size:14px;font-weight:600;
    color:var(--ink);list-style:none}
  .log summary::-webkit-details-marker{display:none}
  .log ul{margin:0;padding:0 16px 14px;list-style:none;max-height:215px;overflow:auto}
  .log li{border-top:1px solid var(--line);padding:8px 0;font-size:12px;display:flex;gap:9px;color:#4C5054}
  .arrow{flex:0 0 auto;color:var(--blue);font-weight:700}
  .arrow.local{color:#B4B8BB}
  .lt{word-break:break-word}
  .lt em{font-style:normal;color:var(--dim)}

  .veil{position:fixed;inset:0;background:rgba(26,28,30,.35);display:none;z-index:100}
  .veil.on{display:block}
  .sheet{position:fixed;top:50%;left:50%;background:var(--paper);z-index:101;
    border-radius:16px;padding:20px 20px 28px;width:min(560px,calc(100vw - 32px));
    box-sizing:border-box;box-shadow:0 12px 40px rgba(0,0,0,.25);
    max-height:calc(100vh - 64px);overflow-y:auto;overscroll-behavior:contain;
    transform:translate(-50%,-50%) scale(.96);opacity:0;pointer-events:none;
    transition:transform .18s ease,opacity .18s ease}
  .sheet.on{transform:translate(-50%,-50%) scale(1);opacity:1;pointer-events:auto}
  @media (prefers-reduced-motion:reduce){.sheet{transition:none}}
  .sheet h3{margin:0 0 2px;font-size:20px;font-weight:700;letter-spacing:-.01em}
  .sheet .of{font-size:12.5px;color:var(--dim);margin-bottom:10px}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
    border-top:1px solid var(--line);font-size:14px}
  .kv span{color:var(--dim)}
  .owner{font-size:12.5px;color:var(--dim);margin-top:12px;background:var(--canvas);
    border-radius:8px;padding:9px 11px}
  .acts{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
  .cores{display:grid;grid-template-columns:repeat(6,24px);gap:8px;margin-top:8px}
  .swatch{width:24px;height:24px;border-radius:50%;cursor:pointer;border:2px solid transparent;padding:0}
  .swatch.sel{border-color:var(--ink)}
  .swatch:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
  select,input,textarea{background:var(--paper);color:var(--ink);border:1px solid var(--edge);
    border-radius:8px;padding:6px 9px;font:inherit;font-size:14px}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:0;border-color:var(--blue)}
  .frow{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:9px 0;border-top:1px solid var(--line)}
  .frow label{color:var(--dim);font-size:14px;flex:0 0 auto}
  .frow input,.frow select,.frow textarea{flex:1 1 auto;min-width:0;max-width:62%}
  .err{color:var(--red);font-size:13px;min-height:16px;padding-top:8px}
  a.btn{display:inline-block;text-decoration:none}
  .btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
  .btn.primary:hover{background:#175CAF;border-color:#175CAF}
  .add{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:5px 12px;font-size:13.5px;color:var(--blue);font-weight:600}
  .add:hover{border-color:var(--blue)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Planeamento da serração</h1>
    <div class="sub">Ecos Largos · fins de semana assinalados</div>
  </header>

  <div class="nav">
    <div class="seg" id="seg">
      <button data-m="semana">Semana</button>
      <button data-m="duas">Duas semanas</button>
      <button data-m="mes">Mês</button>
    </div>
    <button class="step" id="prev" aria-label="Período anterior">‹</button>
    <button class="step" id="next" aria-label="Período seguinte">›</button>
    <div class="range" id="range"></div>
    <button class="btn" id="hoje">Hoje</button>
    <div class="search">
      <input type="search" id="busca" placeholder="Pesquisar encomendas…" autocomplete="off">
    </div>
  </div>

  <div class="bar">
    <div class="pill"><span class="dot" id="syncDot"></span><span id="syncTxt">Ligado ao Basecamp</span></div>
    <div class="pill"><b id="statBolsa">—</b> por agendar</div>
    <div class="pill"><b id="statAgendadas">—</b> agendadas</div>
    <button class="btn" id="undo">Anular</button>
    <button class="btn" id="atualizar">Atualizar do Basecamp</button>
    <button class="btn primary" id="modoEdicaoBtn">Editar</button>
  </div>

  <section class="fila">
    <div class="filaHead">
      <h2>Fila — por agendar (coluna Triagem/Programação no Basecamp)</h2>
      <button class="add" id="novo">+ Nova encomenda</button>
    </div>
    <div class="filaRow" id="fila"></div>
  </section>

  <div class="board" id="board">
    <div class="labels" id="labels"><div class="head"></div></div>
    <div class="scroll" id="scroll">
      <div class="track">
        <div class="days" id="days"></div>
        <div class="lanes" id="lanes"></div>
      </div>
    </div>
  </div>

  <div class="legenda" id="legenda"></div>

  <h2 class="secTit">Logística — dia de carregamento</h2>
  <div class="board boardLog" id="boardLog">
    <div class="labels" id="labelsLog"><div class="head"></div><div class="lbl lblLog">Carregar</div></div>
    <div class="scroll" id="scrollLog">
      <div class="track">
        <div class="days" id="daysLog"></div>
        <div class="lanes" id="lanesLog"></div>
      </div>
    </div>
  </div>

  <details class="log" id="painelCoresEstado">
    <summary>Cores por estado (fundo automático dos cards)</summary>
    <div id="coresEstadoLista" style="padding:2px 16px 14px"></div>
  </details>

  <details class="log" open>
    <summary>Registo — o que foi gravado</summary>
    <ul id="log"></ul>
  </details>
</div>

<div class="veil" id="veil"></div>
<div class="sheet" id="sheet"></div>

<script>
/* ---------- calendário: todos os dias, fins de semana assinalados ---------- */
const DOW=["dom","seg","ter","qua","qui","sex","sáb"];
const MES=["janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"];
const MESC=["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"];
const MASTER=[];
{
  const inicio=new Date(); inicio.setDate(inicio.getDate()-21);
  const fim=new Date(); fim.setDate(fim.getDate()+150);
  for(let d=new Date(inicio); d<=fim; d.setDate(d.getDate()+1)){
    MASTER.push({y:d.getFullYear(),mo:d.getMonth(),dd:d.getDate(),dow:d.getDay(),
      iso:`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`});
  }
}
const FDS=d=>d.dow===6||d.dow===0;
const clamp=(v,a,b)=>Math.max(a,Math.min(v,b));
const hojeISO=(()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`})();
const HOJE=MASTER.findIndex(d=>d.iso===hojeISO);
const idxOf=iso=>MASTER.findIndex(d=>d.iso===iso);
/* pedido explícito do Rui (2026-09-30): agendar uma OF para um dia fora
   da semana/período visível gravava bem (confirmado nos dados), mas
   parecia "não aparecer no calendário" — a vista simplesmente não
   estava naquele período. Sempre que a ficha agenda algo para uma data
   nova, a vista salta sozinha para lá, tal como a pesquisa já fazia
   para o primeiro resultado (ver corresponde/buscaClasse). */
function garantirNaVista(gs){
  if(gs===null||gs===undefined||gs<0) return;
  if(gs<view.start||gs>=view.start+view.len){
    view.start=clamp(gs-Math.floor(view.len/2),0,Math.max(MASTER.length-view.len,0));
  }
}
/* índice da segunda-feira da semana (calendário, não a de hoje) que
   contém o índice dado — pedido explícito do Rui (2026-09): a vista
   "semana" tem de mostrar a semana inteira (seg-dom, como o Google
   Calendar), com o dia de hoje só destacado por cor, nunca forçado a ser
   a primeira coluna. */
const segundaDe=idx=>{ const dow=MASTER[clamp(idx,0,MASTER.length-1)].dow; return idx-((dow+6)%7); };

let LINHAS=[];
let CAPACIDADES={};
let cards=[];
let cardsLog=[];
let selecionadoId=null;
let view={mode:"semana",start:Math.max(segundaDe(HOJE),0),len:7};
let undoStack=[], logs=[], DAY=92, LANE=78;
/* modo só consulta por omissão (pedido explícito do Rui, 2026-09-29): a
   página abre sempre assim, mesmo que já se tenha ativado a edição antes
   nesta sessão/browser — só o botão "Editar" liga a edição, e só até à
   próxima vez que a página abrir. */
let modoEdicao=false;

const $=s=>document.querySelector(s);
const card=id=>cards.find(c=>c.id===id);
const cardLog=id=>cardsLog.find(c=>c.id===id);
/* pedido explícito do Rui (2026-09-25): a borda vermelha ("atrasado") não
   é sobre o prazo do Basecamp, nem sobre quanto tempo a produção em si
   está a demorar — é só sobre o INÍCIO: se a OF ainda estava em
   Triagem/Programação já depois do dia de início local, começou
   atrasada, e fica assim marcada para sempre (mesmo que "Em Produção"
   chegue no dia seguinte). Calculado e gravado no servidor (ver
   estado_planeamento_serracao/db.marcar_atrasado_confirmado) — aqui só
   se lê a marca (`atrasadoConfirmado`), nunca se recalcula nada a partir
   de datas. */
const atrasado=c=>!!c.atrasadoConfirmado;
/* pedido explícito do Rui (2026-09-30): o id completo do card do
   Basecamp (ex: 10285829501) é comprido demais para caber no espaço do
   card — mostra-se só os últimos 4 dígitos (ex: 9501), que já bastam
   para diferenciar as OFs visíveis ao mesmo tempo no quadro. O id
   completo continua a viver no Basecamp e no URL do card (ver
   bcOpen) — isto é só de apresentação. */
const numCurto=id=>String(id).slice(-4);

/* pesquisar cards (pedido explícito do Rui, 2026-09-30): "tal como no
   Google Calendar" — uma caixa de texto que destaca os cards cujo
   título ou número (curto ou completo) batem certo com o que se
   escreve, esbate os restantes, e o Enter salta para a data do primeiro
   resultado agendado, mesmo que esteja fora do período visível. Nunca
   filtra de vez os cards (continuam todos lá, só visualmente
   esbatidos) — sair da pesquisa (caixa vazia) devolve tudo ao normal. */
let buscaQuery="";
const normalizarTexto=s=>(s||"").normalize("NFD").replace(/[̀-ͯ]/g,"").toLowerCase();
const corresponde=(c,q)=>normalizarTexto(c.titulo).includes(q) || numCurto(c.id).includes(q) || String(c.id).includes(q);
const buscaClasse=c=>!buscaQuery ? "" : (corresponde(c,buscaQuery) ? " busca-match" : " busca-dim");

/* clicar num card (fila, produção ou logística) destaca-o a ele e ao seu
   par na outra tabela (mesma OF, mesmo basecamp_card_id) — em vez de abrir
   logo a ficha de edição (pedido explícito do Rui, 2026-09: um clique
   simples só alinha visualmente os dois; a edição passa a ter um botão
   próprio, "✎", em cada card). Quando a OF tem os dois lados (produção e
   logística), a tabela de logística passa a mostrar os dias desviados
   (ver desvioLog) até a coluna do seu dia ficar exatamente alinhada com a
   da produção — cada tabela continua a mostrar as datas certas no
   cabeçalho (só desviadas, não erradas); não se usa scroll para isto
   porque a largura real do conteúdo é insuficiente para desvios grandes
   (o scroll fica sempre limitado pelo próprio tamanho da tabela). */
function selecionar(id){
  selecionadoId = (selecionadoId===id) ? null : id;
  if(selecionadoId===null){ desvioLog=0; renderDays(); renderLogistica(); return; }
  const cP=card(id), cL=cardLog(id);
  const prodColocado = cP && cP.linha!==null && cP.gs!==null;
  desvioLog = (prodColocado && cL) ? (cL.gs-cP.gs) : 0;
  renderDays(); renderLogistica(); aplicarSelecao();
}
function aplicarSelecao(){
  document.querySelectorAll(".blk,.qcard").forEach(el=>{
    el.classList.toggle("selecionado", selecionadoId!==null && +el.dataset.id===selecionadoId);
  });
}

/* lista fixa de cores — pedido explícito do Rui (2026-09), em todos os
   cards (produção, fila e logística):
   - Barra lateral: sempre manual (tipo de produto, ex: quadradilho) —
     ver corProduto (mesma função para produção, fila e logística). É da
     OF, uma só (não um campo à parte na logística) — mudar numa tabela
     tem de se ver na outra.
   - Fundo: manual, se a pessoa escolher uma cor de fundo no card (ver
     fundoManual) — senão automático, de acordo com a coluna do Basecamp
     (ver corEstadoFundo, uma cor por estado, editável no painel "Cores
     por estado"). Ver fundoCard, que junta as duas com esta prioridade,
     usado por produção, fila e logística por igual.
   - Atrasada (prazo do Basecamp ultrapassado): não usa cor nenhuma das
     duas — passa a um contorno vermelho próprio (ver .atrasado no CSS),
     para nunca competir com a cor de produto nem a de estado. */
// grelha de 4 tons x 6 matizes (pedido explícito do Rui, 2026-09: "as
// mesmas cores que o Google Calendar/Gmail têm") — ordem de inserção
// propositada (uma linha por tom, da mais escura à mais clara) para a
// grelha CSS de 6 colunas (ver .cores) desenhar visualmente as mesmas
// linhas por tom que o seletor deles tem. Os 6 nomes sem sufixo mantêm o
// significado de sempre (cor "normal" de cada matiz) — só o hex mudou.
const CORES={
  vermelho_escuro:{hex:"#D32F2F",label:"Vermelho escuro"},
  laranja_escuro:{hex:"#F57C00",label:"Laranja escuro"},
  amarelo_escuro:{hex:"#FFA000",label:"Amarelo escuro"},
  verde_escuro:{hex:"#388E3C",label:"Verde escuro"},
  azul_escuro:{hex:"#1976D2",label:"Azul escuro"},
  roxo_escuro:{hex:"#7B1FA2",label:"Roxo escuro"},
  vermelho:{hex:"#F44336",label:"Vermelho"},
  laranja:{hex:"#FF9800",label:"Laranja"},
  amarelo:{hex:"#FFC107",label:"Amarelo"},
  verde:{hex:"#4CAF50",label:"Verde"},
  azul:{hex:"#2196F3",label:"Azul"},
  roxo:{hex:"#9C27B0",label:"Roxo"},
  vermelho_medio:{hex:"#E57373",label:"Vermelho médio"},
  laranja_medio:{hex:"#FFB74D",label:"Laranja médio"},
  amarelo_medio:{hex:"#FFD54F",label:"Amarelo médio"},
  verde_medio:{hex:"#81C784",label:"Verde médio"},
  azul_medio:{hex:"#64B5F6",label:"Azul médio"},
  roxo_medio:{hex:"#BA68C8",label:"Roxo médio"},
  vermelho_claro:{hex:"#FFCDD2",label:"Vermelho claro"},
  laranja_claro:{hex:"#FFE0B2",label:"Laranja claro"},
  amarelo_claro:{hex:"#FFECB3",label:"Amarelo claro"},
  verde_claro:{hex:"#C8E6C9",label:"Verde claro"},
  azul_claro:{hex:"#BBDEFB",label:"Azul claro"},
  roxo_claro:{hex:"#E1BEE7",label:"Roxo claro"},
  cinza:{hex:"#9AA0A6",label:"Automática"},
};
// estado/coluna Basecamp -> chave interna (ver tools/planeamento_serracao.ESTADOS_COR)
const ESTADOS_COR={"Produzido":"produzido","Em Produção":"em_producao","Vendido":"vendido","Secagem":"secagem"};
let CORES_ESTADO={}; // chave interna -> chave de CORES, carregado em carregar()
function tintRgba(hex,alpha){
  const r=parseInt(hex.slice(1,3),16), g=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha})`;
}
function corEstadoFundo(c){
  const chave=ESTADOS_COR[c.coluna];
  const cor=chave&&CORES_ESTADO[chave];
  return (cor&&CORES[cor]) ? tintRgba(CORES[cor].hex,.22) : null;
}
function corProduto(c){
  return (c.cor && CORES[c.cor]) ? CORES[c.cor].hex : CORES.cinza.hex;
}
function fundoManual(c){
  return (c.corFundo && CORES[c.corFundo]) ? tintRgba(CORES[c.corFundo].hex,.22) : null;
}
function fundoCard(c){
  return fundoManual(c) || corEstadoFundo(c);
}

/* ---------- carregar dados reais do Basecamp + agendamento local ---------- */
async function carregar(){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A ler o Basecamp…";
  try{
    const r=await fetch("/planeamento-ecos-largos/dados");
    const d=await r.json();
    LINHAS=d.linhas; CAPACIDADES=d.capacidades||{}; CORES_ESTADO=d.cores_estado||{};
    cards=[
      ...d.bolsa.map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
        prazo:c.prazo,url:c.url,volume:c.volume_m3,cor:c.cor,corFundo:c.cor_fundo,madeira:c.tipo_madeira,linha:null,gs:null,dur:1,ordem:0})),
      ...d.agendadas.map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
        prazo:c.prazo,url:c.url,volume:c.volume_m3,cor:c.cor,corFundo:c.cor_fundo,madeira:c.tipo_madeira,linha:LINHAS.indexOf(c.linha),
        gs:idxOf(c.dia_inicio),dur:c.duracao_dias,duracaoManual:!!c.duracao_manual,ordem:c.ordem||0,
        atrasadoConfirmado:!!c.atrasado_confirmado})).filter(c=>c.linha>=0&&c.gs>=0)
    ];
    cardsLog=(d.logistica||[]).map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
      url:c.url,cor:c.cor,corFundo:c.cor_fundo,quemCarrega:c.quem_carrega,gs:idxOf(c.dia_carregamento)})).filter(c=>c.gs>=0);
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    render(); renderCoresEstado(); renderLegenda();
    log("local",`lido do Basecamp: ${d.bolsa.length} por agendar, ${d.agendadas.length} agendadas, ${cardsLog.length} na logística`);
  }catch(e){
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Falhou a ligação ao Basecamp";
    log("local",`erro a ler o Basecamp: ${e}`);
  }
}

/* releitura mais leve, só da logística — usada depois de agendar/definir
   madeira, para apanhar um duplicado novo que possa ter sido criado do
   lado do servidor, sem perturbar o resto do ecrã. */
async function atualizarLogistica(){
  try{
    const r=await fetch("/planeamento-ecos-largos/dados");
    const d=await r.json();
    cardsLog=(d.logistica||[]).map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
      url:c.url,cor:c.cor,corFundo:c.cor_fundo,quemCarrega:c.quem_carrega,gs:idxOf(c.dia_carregamento)})).filter(c=>c.gs>=0);
    renderLogistica();
  }catch(e){ /* falha silenciosa — não é uma ação que a pessoa pediu diretamente */ }
}

/* ---------- capacidade por linha (m³/dia), editável ---------- */
async function editarCapacidade(linha){
  const atual=CAPACIDADES[linha];
  const valor=prompt(`Capacidade de "${linha}" (m³/dia):`, atual!=null?atual:40);
  if(valor===null) return;
  const num=+valor;
  if(!num||num<=0){ alert("Indica um número maior que 0."); return; }
  try{
    const r=await fetch("/planeamento-ecos-largos/capacidade",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({linha,capacidade_m3_dia:num})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); return; }
    log("local",`capacidade de "${linha}" atualizada para ${num} m³/dia`);
    /* recarrega tudo: mudar a capacidade recalcula logo a duração de
       todas as OFs já agendadas nessa linha (ver _recalcular_linha) —
       uma simples atualização do rótulo não bastava. */
    await carregar();
  }catch(e){ alert("Falhou a guardar: "+e); }
}

/* legenda das cores, pequena e sempre visível (mesmo em modo só consulta),
   depois do quadro de produção — pedido explícito do Rui (2026-09-25):
   algo direto e simples a lembrar o que cada cor de fundo/borda
   significa, sem precisar de abrir o painel "Cores por estado" (esse é
   só para editar, este é só para consultar); as cores dos quadradinhos
   são tal e qual as que aparecem no fundo dos cards (mesmo tom diluído,
   ver corEstadoFundo/tintRgba), para a legenda corresponder exatamente
   ao que se vê no quadro. Gerada a partir dos mesmos dados
   (ESTADOS_COR/CORES_ESTADO) para nunca ficar desatualizada se a equipa
   mudar uma cor no painel. */
const ROTULOS_ESTADO={"Em Produção":"Em produção","Secagem":"No secador","Produzido":"Produzido","Vendido":"Vendido"};
function renderLegenda(){
  const ordem=["Em Produção","Secagem","Produzido","Vendido"];
  $("#legenda").innerHTML=
    ordem.filter(coluna=>ESTADOS_COR[coluna]!==undefined).map(coluna=>{
      const corNome=CORES_ESTADO[ESTADOS_COR[coluna]];
      const hex=(corNome&&CORES[corNome])?CORES[corNome].hex:CORES.cinza.hex;
      return `<span class="legendaItem"><span class="legendaSwatch" style="background:${tintRgba(hex,.22)};box-shadow:0 0 0 1px ${hex} inset"></span>${ROTULOS_ESTADO[coluna]}</span>`;
    }).join("")
    + `<span class="legendaItem"><span class="legendaSwatch legendaBorda"></span>Borda vermelha: atrasado</span>`;
}

/* painel "Cores por estado" — pedido explícito do Rui (2026-09): tal como
   a cor manual de cada OF já era editável, a cor automática de cada
   estado/coluna do Basecamp também passa a ser, com a mesma paleta. */
function renderCoresEstado(){
  $("#coresEstadoLista").innerHTML=Object.entries(ESTADOS_COR).map(([coluna,chave])=>`
    <div style="margin:10px 0">
      <div style="font-size:13px;color:var(--dim);margin-bottom:6px">${coluna}</div>
      <div class="cores">${Object.entries(CORES).filter(([k])=>k!=="cinza").map(([k,v])=>
        `<button class="swatch${CORES_ESTADO[chave]===k?" sel":""}" data-estado="${chave}" data-cor="${k}"
          style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    </div>`).join("");
  $("#coresEstadoLista").querySelectorAll(".swatch").forEach(sw=>{
    sw.onclick=async()=>{
      const estado=sw.dataset.estado, cor=sw.dataset.cor;
      try{
        const r=await fetch("/planeamento-ecos-largos/cor-estado",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({estado,cor})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); return; }
        CORES_ESTADO[estado]=cor;
        renderCoresEstado(); renderLegenda();
        renderLanes(); renderFila(); renderLogistica();
        log("local",`cor de "${sw.dataset.estado}" atualizada`);
      }catch(e){ alert("Falhou a guardar: "+e); }
    };
  });
}

/* ---------- período ---------- */
function setMode(m){
  view.mode=m;
  const anchor=view.start;
  if(m==="semana"||m==="duas"){
    view.len = m==="semana"?7:14;
    view.start = clamp(segundaDe(anchor), 0, Math.max(MASTER.length-view.len,0));
  }else{
    const d=MASTER[clamp(anchor,0,MASTER.length-1)];
    view.start=MASTER.findIndex(x=>x.mo===d.mo && x.y===d.y);
    view.len=MASTER.filter(x=>x.mo===d.mo && x.y===d.y).length;
  }
  limparAlinhamento(); render();
}
function step(dir){
  if(view.mode==="mes"){
    const cur=MASTER[view.start];
    const i=dir>0 ? MASTER.findIndex(x=>x.y>cur.y||(x.y===cur.y&&x.mo>cur.mo))
                  : MASTER.map(x=>x.y*12+x.mo).lastIndexOf(cur.y*12+cur.mo-1);
    if(i<0)return;
    const d=MASTER[i];
    view.start=MASTER.findIndex(x=>x.mo===d.mo && x.y===d.y);
    view.len=MASTER.filter(x=>x.mo===d.mo && x.y===d.y).length;
  }else{
    view.start=clamp(view.start+dir*view.len,0,Math.max(MASTER.length-view.len,0));
  }
  limparAlinhamento(); render();
}
/* as duas tabelas mostram sempre as mesmas datas por omissão — ao mudar
   de período (semana/mês, ‹ ›, Hoje) limpa a seleção e o desvio da
   logística (ver selecionar/desvioLog), para nunca navegar já desalinhado. */
function limparAlinhamento(){ selecionadoId=null; desvioLog=0; }
function rangeLabel(){
  const a=MASTER[view.start], b=MASTER[Math.min(view.start+view.len-1,MASTER.length-1)];
  if(!a||!b) return "";
  if(view.mode==="mes") return MES[a.mo][0].toUpperCase()+MES[a.mo].slice(1)+" "+a.y;
  if(a.mo===b.mo) return `${a.dd}–${b.dd} ${MESC[a.mo]}`;
  return `${a.dd} ${MESC[a.mo]} – ${b.dd} ${MESC[b.mo]}`;
}

/* ---------- render ---------- */
function metrics(){
  const avail=$("#scroll").clientWidth||600;
  if(view.mode==="mes"){ DAY=44; LANE=64; }
  else if(view.mode==="duas"){ DAY=Math.max(72,Math.floor(avail/14)); LANE=78; }
  else { DAY=Math.max(84,Math.floor(avail/7)); LANE=78; }
  document.documentElement.style.setProperty("--day",DAY+"px");
  document.documentElement.style.setProperty("--lane",LANE+"px");
  $("#board").classList.toggle("dense",DAY<70);
  $("#boardLog").classList.toggle("dense",DAY<70);
}
function days(){ return MASTER.slice(view.start,view.start+view.len); }
/* desvio (em dias) só da tabela de logística, aplicado enquanto um par de
   cards está destacado (ver selecionar) — em vez de scroll (limitado pela
   largura real do conteúdo, insuficiente para desvios grandes), muda-se
   literalmente que dias a tabela de logística mostra, para a coluna do
   par ficar exatamente alinhada com a da produção. Repõe-se a 0 sempre
   que a seleção é limpa ou o período muda. */
let desvioLog=0;
function daysLog(){ return MASTER.slice(view.start+desvioLog,view.start+desvioLog+view.len); }
function renderLabels(){
  calcularAlturasLinhas();
  $("#labels").innerHTML='<div class="head"></div>'+LINHAS.map((n,li)=>
    `<div class="lbl" data-linha="${n}" style="height:${ALTURAS_LINHA[li]||LANE}px"><div class="n">${n}</div>
     <div class="c">${CAPACIDADES[n]!=null?CAPACIDADES[n]+" m³/dia":"definir capacidade"} · editar</div></div>`).join("");
  $("#labels").querySelectorAll(".lbl").forEach(el=>{
    el.onclick=()=>{ if(modoEdicao) editarCapacidade(el.dataset.linha); };
  });
}
function diaHtml(d,hoje){
  const wk=FDS(d);
  return `<div class="day${wk?" wk":""}${wk?" sab":""}${hoje?" hoje":""}">
    <div class="dn">${view.mode==="mes"?d.dd:DOW[d.dow]+" "+d.dd}</div>
    <div class="dm">${view.mode==="mes"?DOW[d.dow][0]:MESC[d.mo]}</div></div>`;
}
function renderDays(){
  const D=days();
  $("#days").innerHTML=D.map((d,i)=>diaHtml(d,view.start+i===HOJE)).join("");
  $("#days").style.width=(D.length*DAY)+"px";
  const DL=daysLog();
  $("#daysLog").innerHTML=DL.map((d,i)=>diaHtml(d,view.start+desvioLog+i===HOJE)).join("");
  $("#daysLog").style.width=(DL.length*DAY)+"px";
}
/* várias OFs cabem na mesma linha/dia enquanto a soma dos seus ritmos
   diários (volume ÷ duração) não ultrapassar a capacidade dessa linha —
   pedido explícito do Rui (2026-09). Para nunca ficarem sobrepostas nem
   apertadas ao ponto de esconder informação (todos os cards têm de
   mostrar sempre os m³, pedido explícito do Rui), cada card ocupa um
   "slot" inteiro (0, 1, 2...) de altura fixa — o primeiro ainda livre em
   todos os dias que atravessa — e a LINHA cresce sozinha (altura
   própria, independente das outras linhas) até caber o dia mais cheio
   dela, em vez de espremer os cards numa altura fixa (ver
   calcularAlturasLinhas). Calculado sobre TODOS os cards da linha (não
   só os visíveis), para a posição de cada um não saltar ao navegar entre
   semanas. */
const ITEM_PROD_H=44, ITEM_PROD_GAP=4, ITEM_PROD_PAD=6;
/* fim de semana não é dia de produção (pedido explícito do Rui, 2026-09):
   um card que atravessa sábado/domingo não deve aparecer a "ocupar"
   esses dias no quadro — só os dias em que se produz mesmo (ex: uma
   encomenda de sexta a segunda mostra-se só na sexta e na segunda,
   nunca um bloco contínuo a cobrir o fim de semana também). `c.dur`
   continua a ser a duração em dias de calendário (usada pelo backend
   para calcular capacidade/carregamento, ver _dias_necessarios_greedy),
   mas a apresentação usa só os dias úteis dentro desse intervalo. */
function diasUteisCard(c){
  const dias=[];
  for(let k=0;k<c.dur;k++){
    const idx=c.gs+k, d=MASTER[idx];
    if(d && !FDS(d)) dias.push(idx);
  }
  return dias;
}
/* agrupa os dias úteis de um card em blocos contíguos (segmentos) — um
   card de sexta a segunda tem 2 segmentos (sexta sozinha, segunda
   sozinha), separados pelo fim de semana que fica em branco entre eles. */
function segmentosCard(c){
  const segmentos=[];
  diasUteisCard(c).forEach(idx=>{
    const atual=segmentos[segmentos.length-1];
    if(atual && idx===atual.fim+1) atual.fim=idx;
    else segmentos.push({inicio:idx,fim:idx});
  });
  return segmentos;
}
function encaixarCardsLinha(cardsLinha){
  const ocupado={}; // índice do dia -> Set de slots já usados nesse dia
  let maxSlots=1;
  [...cardsLinha].sort((a,b)=>a.gs-b.gs||(a.ordem||0)-(b.ordem||0)||a.id-b.id).forEach(c=>{
    const dias=diasUteisCard(c);
    let slot=0;
    for(;;slot++){
      let livre=true;
      for(const idx of dias){
        if((ocupado[idx]||new Set()).has(slot)){ livre=false; break; }
      }
      if(livre) break;
    }
    c._slot=slot;
    for(const idx of dias){
      if(!ocupado[idx]) ocupado[idx]=new Set();
      ocupado[idx].add(slot);
    }
    maxSlots=Math.max(maxSlots,slot+1);
  });
  return maxSlots;
}
let ALTURAS_LINHA=[], OFFSETS_LINHA=[];
function calcularAlturasLinhas(){
  ALTURAS_LINHA=LINHAS.map((nome,li)=>{
    const maxSlots=encaixarCardsLinha(cards.filter(c=>c.linha===li));
    return Math.max(LANE, maxSlots*ITEM_PROD_H+(maxSlots-1)*ITEM_PROD_GAP+ITEM_PROD_PAD*2);
  });
  let acumulado=0;
  OFFSETS_LINHA=ALTURAS_LINHA.map(alt=>{ const topo=acumulado; acumulado+=alt; return topo; });
}
/* a que linha corresponde uma posição vertical (em px, relativa ao topo
   de #lanes) — usado ao arrastar, já que as linhas já não têm todas a
   mesma altura (ver calcularAlturasLinhas). */
function linhaDeY(y){
  if(y<0) return 0;
  let acumulado=0;
  for(let i=0;i<LINHAS.length;i++){
    acumulado+=ALTURAS_LINHA[i];
    if(y<acumulado) return i;
  }
  return LINHAS.length-1;
}
function renderLanes(){
  calcularAlturasLinhas();
  const D=days();
  let h="";
  LINHAS.forEach((nome,li)=>{ h+=`<div class="row" style="height:${ALTURAS_LINHA[li]}px">`+D.map(d=>{
    const hoje=MASTER.indexOf(d)===HOJE;
    return `<div class="cell${FDS(d)?" wk":""}${hoje?" hoje":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  cards.filter(c=>c.linha!==null).forEach(c=>{
    // um card que atravessa um fim de semana desenha-se em vários
    // segmentos (um por cada grupo de dias úteis seguidos), com um
    // espaço em branco por cima do sábado/domingo — nunca um bloco
    // contínuo a cobrir dias em que não há produção (ver segmentosCard).
    const segmentos=segmentosCard(c);
    segmentos.forEach((seg,i)=>{
      const a=seg.inicio-view.start, b=seg.fim-view.start+1;
      if(b<=0||a>=view.len) return;
      const l=Math.max(a,0), r=Math.min(b,view.len);
      const clipL=a<0||i>0, clipR=b>view.len||i<segmentos.length-1;
      const el=document.createElement("div");
      el.className="blk"+(clipL?" clipL":"")+(clipR?" clipR":"")+(atrasado(c)?" atrasado":"")+buscaClasse(c);
      el.tabIndex=0; el.dataset.id=c.id;
      el.style.borderLeftColor=corProduto(c);
      const fundo=fundoCard(c); if(fundo) el.style.background=fundo;
      el.style.left=(l*DAY+3)+"px";
      el.style.top=(OFFSETS_LINHA[c.linha]+ITEM_PROD_PAD+c._slot*(ITEM_PROD_H+ITEM_PROD_GAP))+"px";
      el.style.width=((r-l)*DAY-8)+"px";
      el.style.height=ITEM_PROD_H+"px";
      el.innerHTML = i===0
        ? `<div class="editBtn" data-edit="${c.id}" title="Editar">✎</div><div class="tt">${c.titulo}</div>
           <div class="of">${c.volume?(c.volume+" m³ · "):""}#${numCurto(c.id)}</div>`
        : `<div class="tt">${c.titulo}</div>`;
      bl.appendChild(el);
    });
  });
  stats(); aplicarSelecao();
}
function renderFila(){
  const q=cards.filter(c=>c.linha===null);
  $("#fila").innerHTML = q.length ? q.map(c=>{
    const fundo=fundoCard(c);
    return `<div class="qcard${atrasado(c)?" atrasado":""}${buscaClasse(c)}" data-id="${c.id}"
       style="border-left-color:${corProduto(c)}${fundo?(";background:"+fundo):""}">
     <div class="editBtn" data-edit="${c.id}" title="Editar">✎</div>
     <div class="tt">${c.titulo}</div>
     <div class="of">${c.volume?(c.volume+" m³ · "):""}${c.coluna||""}${c.prazo?(" · prazo "+c.prazo):""}</div></div>`;
  }).join("")
    : '<div class="empty">Fila vazia.</div>';
  aplicarSelecao();
}
/* logística: uma única "linha" (sem divisão por linhas de produção); os
   cards de um mesmo dia empilham-se numa lista vertical (como no
   calendário do Google), todos com a mesma altura compacta — a altura da
   linha cresce sozinha até caber o dia mais cheio da vista atual, para
   nunca esconder um card por falta de espaço (pedido explícito do Rui,
   2026-09). */
const ITEM_LOG_H=44, ITEM_LOG_GAP=4, ITEM_LOG_PAD=6;
function renderLogistica(){
  const D=daysLog();
  const inicio=view.start+desvioLog;
  const hoje=(d)=>MASTER.indexOf(d)===HOJE;
  const porDia={};
  cardsLog.forEach(c=>{
    const a=c.gs-inicio;
    if(a<0||a>=view.len) return;
    (porDia[a]=porDia[a]||[]).push(c);
  });
  Object.values(porDia).forEach(lista=>lista.sort((x,y)=>x.id-y.id));
  const maxN=Math.max(1, ...Object.values(porDia).map(l=>l.length));
  const laneLog=Math.max(LANE, maxN*ITEM_LOG_H+(maxN-1)*ITEM_LOG_GAP+ITEM_LOG_PAD*2);
  document.documentElement.style.setProperty("--laneLog", laneLog+"px");
  let h='<div class="row">'+D.map(d=>
    `<div class="cell${FDS(d)?" wk":""}${hoje(d)?" hoje":""}"></div>`).join("")+'</div>';
  h+='<div class="blocks" id="blocksLog"></div>';
  const lanes=$("#lanesLog"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocksLog");
  Object.entries(porDia).forEach(([a,lista])=>{
    lista.forEach((c,i)=>{
      const el=document.createElement("div");
      el.className="blk"+buscaClasse(c);
      el.tabIndex=0; el.dataset.id=c.id;
      el.style.borderLeftColor=corProduto(c);
      const fundo=fundoCard(c); if(fundo) el.style.background=fundo;
      el.style.left=(a*DAY+3)+"px";
      el.style.top=(ITEM_LOG_PAD+i*(ITEM_LOG_H+ITEM_LOG_GAP))+"px";
      el.style.width=(DAY-8)+"px";
      el.style.height=ITEM_LOG_H+"px";
      el.innerHTML=`<div class="editBtn" data-editlog="${c.id}" title="Editar">✎</div><div class="tt">${c.titulo}</div>
        <div class="of">${c.quemCarrega?("carrega: "+c.quemCarrega):(c.coluna||"")}</div>`;
      bl.appendChild(el);
    });
  });
  aplicarSelecao();
}
function stats(){
  $("#statBolsa").textContent=cards.filter(c=>c.linha===null).length;
  $("#statAgendadas").textContent=cards.filter(c=>c.linha!==null).length;
}
function render(){
  metrics(); renderLabels(); renderDays(); renderLanes(); renderFila(); renderLogistica();
  $("#range").textContent=rangeLabel();
  document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.m===view.mode));
}
function renderLog(){
  $("#log").innerHTML=logs.slice(0,40).map(l=>
    `<li><span class="arrow ${l.dir}">${l.dir==="local"?"·":"→"}</span>
     <span class="lt mono">${l.t}</span></li>`).join("");
}
function log(dir,t){ logs.unshift({dir,t}); renderLog(); }

/* ---------- gravar agendamento local (nunca escreve no Basecamp) ---------- */
/* a duração é sempre calculada no servidor a partir do volume (m³) e da
   capacidade da linha (pedido explícito do Rui, 2026-09) — nunca escolhida
   à mão aqui; por isso todo o agendamento espera pela resposta do servidor
   antes de desenhar o bloco, para mostrar sempre a duração certa. */
/* a capacidade da linha pode recusar um agendamento (ver
   tools/planeamento_serracao._validar_capacidade) — quando isso acontece,
   a alteração já feita no ecrã (arrastar, largar da fila, ...) é revertida
   para `anterior`, para o quadro nunca ficar a mostrar algo que o servidor
   não aceitou. */
async function guardarAgendamento(c, anterior){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A gravar…";
  try{
    const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[c.linha],
        dia_inicio:MASTER[c.gs].iso,volume_m3:c.volume||null})});
    const d=await r.json();
    if(d.erro){
      log("local",`recusado: ${d.erro}`);
      if(anterior){ c.linha=anterior.linha; c.gs=anterior.gs; c.dur=anterior.dur; c.volume=anterior.volume; }
      alert(d.erro);
      $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
      renderFila(); renderLanes();
    }else{
      log("local",`guardado: ${c.titulo} → ${LINHAS[c.linha]}, ${MASTER[c.gs].iso}, ${d.duracao_dias}d`);
      /* recarrega tudo, não só esta OF: mover uma OF já agendada
         recalcula a duração de outras OFs já na mesma linha (ver
         _recalcular_linha) — sem isto ficavam com a duração antiga no
         ecrã até ao próximo refresh, bug real do Rui (2026-09-29). */
      await carregar();
      $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    }
  }catch(e){
    log("local",`erro ao guardar: ${e}`);
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    renderFila(); renderLanes();
  }
}
async function desagendarServidor(c){
  try{
    await fetch("/planeamento-ecos-largos/desagendar",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id})});
    log("local",`devolvido à fila: ${c.titulo}`);
    /* a linha de onde a OF saiu pode ter outras OFs recalculadas (ver
       _recalcular_linha, chamado agora também por desagendar). */
    await carregar();
  }catch(e){ log("local",`erro ao devolver à fila: ${e}`); }
}
function sync(c, anterior){ if(c.linha!==null && c.gs!==null) guardarAgendamento(c, anterior); else desagendarServidor(c); }

/* ---------- arrastar dentro da grelha ---------- */
let drag=null;
$("#lanes").addEventListener("pointerdown",e=>{
  if(!modoEdicao)return;
  if(e.target.closest(".editBtn"))return;
  const b=e.target.closest(".blk"); if(!b)return;
  const c=card(+b.dataset.id);
  // um card com fim de semana no meio tem vários segmentos (ver
  // segmentosCard) — agarrar em qualquer um deles tem de arrastar todos
  // juntos, senão só o segmento tocado se move durante o gesto e o
  // outro fica visualmente para trás até ao próximo render.
  const els=[...$("#blocks").querySelectorAll(`.blk[data-id="${c.id}"]`)];
  drag={els,c,x0:e.clientX,y0:e.clientY,gs0:c.gs,lin0:c.linha,dur0:c.dur,dx:0,dy:0};
  b.setPointerCapture(e.pointerId); els.forEach(el=>el.classList.add("drag")); e.preventDefault();
});
$("#lanes").addEventListener("pointermove",e=>{
  if(!drag)return;
  let dd=Math.round((e.clientX-drag.x0)/DAY);
  dd=clamp(dd, -drag.gs0, MASTER.length-drag.dur0-drag.gs0);
  const r=$("#lanes").getBoundingClientRect();
  const linhaAtual=linhaDeY(e.clientY-r.top);
  const dl=clamp(linhaAtual-drag.lin0, -drag.lin0, LINHAS.length-1-drag.lin0);
  drag.dx=dd; drag.dy=dl;
  const desvioY=OFFSETS_LINHA[drag.lin0+dl]-OFFSETS_LINHA[drag.lin0];
  drag.els.forEach(el=>{ el.style.transform=`translate(${dd*DAY}px,${desvioY}px)`; });
});
$("#lanes").addEventListener("pointerup",()=>{
  if(!drag)return; const c=drag.c;
  const moved=drag.dx||drag.dy;
  drag.els.forEach(el=>el.classList.remove("drag"));
  if(!moved){ drag.els.forEach(el=>el.style.transform=""); drag=null; return; }
  // só re-desenha quando algo realmente mudou de posição — voltar a
  // desenhar sempre (mesmo num simples clique sem arrastar) destruía o
  // próprio bloco clicado a meio do gesto, e o "click" que abre a ficha
  // nunca chegava a disparar (o alvo original já não existia no DOM).
  const anterior={id:c.id,linha:c.linha,gs:c.gs,dur:c.dur,volume:c.volume};
  undoStack.push(anterior);
  c.gs+=drag.dx; c.linha+=drag.dy;
  drag=null;
  renderLanes(); sync(c, anterior);
});

/* fila → grelha */
let qdrag=null;
$("#fila").addEventListener("pointerdown",e=>{
  if(!modoEdicao)return;
  if(e.target.closest(".editBtn"))return;
  const q=e.target.closest(".qcard"); if(!q)return;
  const g=q.cloneNode(true); g.className="qcard ghost";
  g.style.width=q.offsetWidth+"px"; document.body.appendChild(g);
  const move=ev=>{ g.style.left=(ev.clientX-q.offsetWidth/2)+"px"; g.style.top=(ev.clientY-22)+"px"; };
  qdrag={id:+q.dataset.id,g,move}; move(e);
  q.setPointerCapture(e.pointerId); e.preventDefault();
});
$("#fila").addEventListener("pointermove",e=>{ if(qdrag) qdrag.move(e); });
$("#fila").addEventListener("pointerup",e=>{
  if(!qdrag)return; const {id,g}=qdrag; g.remove();
  const r=$("#lanes").getBoundingClientRect();
  const x=e.clientX-r.left, y=e.clientY-r.top; qdrag=null;
  if(x<0||y<0||y>r.height||x>view.len*DAY) return;
  const c=card(id);
  const anterior={id:c.id,linha:c.linha,gs:c.gs,dur:c.dur,volume:c.volume};
  undoStack.push(anterior);
  c.linha=linhaDeY(y);
  c.gs=view.start+clamp(Math.floor(x/DAY),0,view.len-c.dur);
  renderFila(); renderLanes(); sync(c, anterior);
});

/* ---------- arrastar na logística (só o dia muda, não há linhas) ---------- */
let dragLog=null;
$("#lanesLog").addEventListener("pointerdown",e=>{
  if(!modoEdicao)return;
  if(e.target.closest(".editBtn"))return;
  const b=e.target.closest(".blk"); if(!b)return;
  const c=cardLog(+b.dataset.id);
  dragLog={el:b,c,x0:e.clientX,gs0:c.gs,dx:0};
  b.setPointerCapture(e.pointerId); b.classList.add("drag"); e.preventDefault();
});
$("#lanesLog").addEventListener("pointermove",e=>{
  if(!dragLog)return;
  let dd=Math.round((e.clientX-dragLog.x0)/DAY);
  dd=clamp(dd, -dragLog.gs0, MASTER.length-1-dragLog.gs0);
  dragLog.dx=dd;
  dragLog.el.style.transform=`translate(${dd*DAY}px,0)`;
});
$("#lanesLog").addEventListener("pointerup",()=>{
  if(!dragLog)return; const c=dragLog.c;
  const moved=dragLog.dx;
  dragLog.el.classList.remove("drag");
  if(!moved){ dragLog.el.style.transform=""; dragLog=null; return; }
  c.gs+=dragLog.dx;
  dragLog=null;
  renderLogistica(); moverLogisticaServidor(c);
});
async function moverLogisticaServidor(c){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A gravar…";
  try{
    const r=await fetch("/planeamento-ecos-largos/logistica/mover",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id,dia_carregamento:MASTER[c.gs].iso})});
    const d=await r.json();
    if(d.erro){ log("local",`erro ao mover carregamento: ${d.erro}`); }
    else log("local",`carregamento de "${c.titulo}" movido para ${MASTER[c.gs].iso}`);
  }catch(e){ log("local",`erro ao mover carregamento: ${e}`); }
  $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
}

/* ---------- ficha ---------- */
/* um clique simples num card (fila, produção ou logística) só destaca-o
   a ele e ao seu par na outra tabela (ver selecionar) — isto vale nos
   dois modos (pedido explícito do Rui, 2026-10-01: repor exatamente o
   comportamento de antes, também no modo só consulta). Para ver a
   informação: em modo de edição continua a ser o botão "✎" de cada card;
   em modo só consulta, onde o "✎" nem aparece, é um DUPLO clique (ou
   duplo toque, em telemóvel/tablet — evento nativo "dblclick", que os
   browsers já sabem gerar a partir de dois toques rápidos) que abre a
   ficha, em somenteLeitura — sem nenhum campo editável nem botão de
   guardar/apagar/duplicar.

   (Tentativas anteriores neste mesmo pedido — trocar o simples/duplo
   clique entre si, ou usar pressão longa — foram abandonadas a pedido do
   Rui: o simples clique tem de continuar a alinhar, tal como sempre foi,
   e o duplo clique passa a ser só um extra para abrir a ficha em modo só
   consulta, sem interferir nem atrasar o alinhamento.) */
$("#lanes").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ if(modoEdicao) openSheet(+e.target.closest(".editBtn").dataset.edit); return; }
  const b=e.target.closest(".blk"); if(!b) return;
  selecionar(+b.dataset.id,"producao");
});
$("#lanes").addEventListener("dblclick",e=>{
  if(modoEdicao) return; // em edição a ficha abre-se pelo botão "✎"
  const b=e.target.closest(".blk"); if(!b) return;
  openSheet(+b.dataset.id,true);
});
$("#fila").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ if(modoEdicao) openSheet(+e.target.closest(".editBtn").dataset.edit); return; }
  const q=e.target.closest(".qcard"); if(!q) return;
  selecionar(+q.dataset.id,"fila");
});
$("#fila").addEventListener("dblclick",e=>{
  if(modoEdicao) return;
  const q=e.target.closest(".qcard"); if(!q) return;
  openSheet(+q.dataset.id,true);
});
$("#lanesLog").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ if(modoEdicao) openSheetLogistica(+e.target.closest(".editBtn").dataset.editlog); return; }
  const b=e.target.closest(".blk"); if(!b) return;
  selecionar(+b.dataset.id,"logistica");
});
$("#lanesLog").addEventListener("dblclick",e=>{
  if(modoEdicao) return;
  const b=e.target.closest(".blk"); if(!b) return;
  openSheetLogistica(+b.dataset.id,true);
});
function openSheet(id,somenteLeitura){
  somenteLeitura=!!somenteLeitura;
  const dis=somenteLeitura?"disabled":"";
  const c=card(id);
  const agendado = c.linha!==null && c.gs!==null;
  let corpo = `
    <div class="frow"><label>Linha</label><select id="fLinha" ${dis}>
      <option value="-1" selected>— Por agendar (fila) —</option>
      ${LINHAS.map((n,i)=>`<option value="${i}">${n}</option>`).join("")}</select></div>
    <div class="frow"><label>Início</label><input id="fInicio" type="date" value="${c.prazo||hojeISO}" ${dis}></div>
    <div class="owner">Escolhe uma linha e um dia de início para agendar esta encomenda — ao guardar, aparece logo nesse dia no quadro, e esse dia substitui o "Due on" no Basecamp (ver Prazo abaixo). O card no Basecamp continua em Triagem até seres tu a movê-lo lá.</div>`;
  if(agendado){
    const s=MASTER[c.gs], f=MASTER[clamp(c.gs+c.dur-1,0,MASTER.length-1)];
    corpo = `
    <div class="frow"><label>Linha</label><select id="fLinha" ${dis}>${LINHAS.map((n,i)=>
      `<option value="${i}"${i===c.linha?" selected":""}>${n}</option>`).join("")}
      <option value="-1">— Devolver à fila (Triagem) —</option></select></div>
    <div class="frow"><label>Início</label><input id="fInicio" type="date" value="${s.iso}" data-original="${s.iso}" ${dis}></div>
    <div class="frow"><label>Fim</label><input id="fFim" type="date" value="${f.iso}" data-original="${f.iso}" ${dis}></div>
    <div class="owner">${c.duracaoManual
      ? "O fim desta OF foi definido à mão — deixou de ser recalculado automaticamente. Muda a linha, o início ou o volume para voltar ao cálculo automático."
      : `Duração calculada: ${c.dur} dias. Mudar o fim aqui passa a ser uma escolha manual — deixa de ser recalculado automaticamente.`}</div>`;
  }
  $("#sheet").innerHTML=`
    <div class="of mono">card #${numCurto(c.id)}</div>
    <div class="frow"><label>Nome</label><input id="fTitulo" type="text" value="${String(c.titulo).replace(/"/g,"&quot;")}" ${dis}></div>
    ${corpo}
    <div class="kv"><span>Coluna no Basecamp</span><b>${c.coluna||"—"}</b></div>
    <div class="kv"><span>Prazo no Basecamp</span><b>${c.prazo||"sem prazo"}</b></div>
    <div class="frow"><label>Volume (m³)</label><input id="fVol" type="number" min="0.1" step="0.1" value="${c.volume||""}" placeholder="ex: 30" ${dis}></div>
    <div class="frow"><label>Madeira</label><select id="fMad" ${dis}>
      <option value="">Não especificado</option>
      <option value="seca"${c.madeira==="seca"?" selected":""}>Seca</option>
      <option value="verde"${c.madeira==="verde"?" selected":""}>Verde</option>
    </select></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor da barra lateral (tipo de produto)</label>
    <div class="cores" id="coresBarra">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.cor||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}" ${dis}
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor de fundo</label>
    <div class="cores" id="coresFundo">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.corFundo||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}" ${dis}
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    ${somenteLeitura?"":`
    <div class="acts">
      <button class="btn" id="duplicar">Duplicar</button>
      <button class="btn warn" id="apagar">Apagar encomenda</button>
    </div>`}
    <div class="acts">
      ${c.url?`<a class="btn" id="bcOpen" target="_blank" rel="noopener" href="${c.url}">Abrir card no Basecamp</a>`:""}
      ${somenteLeitura?"":`<button class="btn primary" id="guardarTudo">Guardar tudo</button>`}
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
  if(somenteLeitura) return;
  $("#coresBarra").querySelectorAll(".swatch").forEach(sw=>{
    sw.onclick=async()=>{
      const cor=sw.dataset.cor;
      try{
        const r=await fetch("/planeamento-ecos-largos/cor",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,cor})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); return; }
        c.cor=cor||null;
        const gemeo=cardsLog.find(x=>x.id===c.id); if(gemeo) gemeo.cor=c.cor;
        $("#coresBarra").querySelectorAll(".swatch").forEach(x=>x.classList.remove("sel"));
        sw.classList.add("sel");
        log("local",`cor de "${c.titulo}" atualizada`);
        render();
      }catch(e){ alert("Falhou a guardar: "+e); }
    };
  });
  $("#coresFundo").querySelectorAll(".swatch").forEach(sw=>{
    sw.onclick=async()=>{
      const cor=sw.dataset.cor;
      try{
        const r=await fetch("/planeamento-ecos-largos/cor-fundo",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,cor})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); return; }
        c.corFundo=cor||null;
        const gemeo=cardsLog.find(x=>x.id===c.id); if(gemeo) gemeo.corFundo=c.corFundo;
        $("#coresFundo").querySelectorAll(".swatch").forEach(x=>x.classList.remove("sel"));
        sw.classList.add("sel");
        log("local",`cor de fundo de "${c.titulo}" atualizada`);
        render();
      }catch(e){ alert("Falhou a guardar: "+e); }
    };
  });
  $("#apagar").onclick=async()=>{
    if(!confirm(`Apagar definitivamente "${c.titulo}"?\n\nIsto manda o card para o lixo no Basecamp (fica lá recuperável durante algum tempo, tal como apagar manualmente).`)) return;
    $("#apagar").textContent="A apagar…"; $("#apagar").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/apagar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; return; }
      log("local",`apagado: ${c.titulo}`);
      /* recarrega tudo: apagar uma OF já agendada liberta espaço e
         recalcula a duração de outras OFs na mesma linha (ver
         _recalcular_linha) — uma simples remoção local não bastava. */
      await carregar(); closeSheet();
    }catch(e){ alert("Falhou a apagar: "+e); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; }
  };
  $("#duplicar").onclick=async()=>{
    $("#duplicar").textContent="A duplicar…"; $("#duplicar").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/duplicar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); $("#duplicar").textContent="Duplicar"; $("#duplicar").disabled=false; return; }
      log("local",`duplicado: "${d.titulo}" — entrou na fila por agendar`);
      /* a cópia entra sempre na fila por agendar (pedido explícito do
         Rui, 2026-09-29), mesmo que o original já estivesse numa linha —
         por isso um carregar() normal já basta, não recalcula nenhuma
         linha (a cópia ainda não ocupa nenhuma). Pedido explícito do Rui
         (2026-09-30): abre logo a ficha da cópia, tal como o Google
         Calendar abre já os detalhes de um evento duplicado, para
         escolher ali mesmo a linha/dia — sem isso, "Guardar tudo" na
         cópia deixa-a na fila (comportamento normal de um card novo). */
      await carregar(); openSheet(d.basecamp_card_id);
    }catch(e){ alert("Falhou a duplicar: "+e); $("#duplicar").textContent="Duplicar"; $("#duplicar").disabled=false; }
  };
  $("#guardarTudo").onclick=async()=>{
    /* pedido explícito do Rui (2026-09-28): um botão único que guarda
       qualquer campo esquecido, sem duplo-clicar em cada "Guardar" — reusa
       os mesmos endpoints dos botões individuais. Linha/início/volume só
       são reenviados se realmente mudaram desde que a ficha abriu (senão
       este botão, clicado sem tocar em nada, apagaria silenciosamente uma
       duração definida à mão no campo Fim, que o /agendar reporia para
       automática); Nome e Madeira são reenviados sempre que válidos, já
       que reenviar o mesmo valor é inofensivo (endpoints idempotentes). */
    const btn=$("#guardarTudo");
    btn.textContent="A guardar…"; btn.disabled=true;
    const erros=[], avisos=[];
    const titulo=$("#fTitulo").value.trim();
    if(titulo){
      try{
        const r=await fetch("/planeamento-ecos-largos/renomear",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,titulo})});
        const d=await r.json();
        if(d.erro) erros.push(`nome: ${d.erro}`);
        else{ c.titulo=titulo; const gemeo=cardsLog.find(x=>x.id===c.id); if(gemeo) gemeo.titulo=titulo; }
      }catch(e){ erros.push(`nome: ${e}`); }
    }else erros.push("nome: não pode ficar vazio");
    if(agendado && +$("#fLinha").value===-1){
      /* pedido explícito do Rui (2026-09-30): tem de continuar a ser
         possível devolver à fila uma OF já numa linha — a opção
         "Devolver à fila" no próprio select de Linha faz isso pelo
         "Guardar tudo", sem precisar de um botão à parte. */
      try{
        const r=await fetch("/planeamento-ecos-largos/desagendar",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id})});
        const d=await r.json();
        if(d.erro) erros.push(`devolver à fila: ${d.erro}`);
        else{ c.linha=null; c.gs=null; }
      }catch(e){ erros.push(`devolver à fila: ${e}`); }
    }else if(agendado){
      const linhaIdx=+$("#fLinha").value;
      const iso=$("#fInicio").value;
      const isoOriginal=$("#fInicio").dataset.original;
      const vol=+$("#fVol").value;
      const linhaMudou=linhaIdx!==c.linha, inicioMudou=iso&&iso!==isoOriginal, volMudou=vol>0&&vol!==c.volume;
      if(linhaMudou||inicioMudou||volMudou){
        try{
          const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[linhaIdx],
              dia_inicio:iso||MASTER[c.gs].iso,volume_m3:vol>0?vol:(c.volume||null)})});
          const d=await r.json();
          if(d.erro) erros.push(`linha/início/volume: ${d.erro}`);
          else{ c.linha=linhaIdx; c.gs=idxOf(iso||MASTER[c.gs].iso); c.dur=d.duracao_dias; c.volume=d.volume_m3; c.duracaoManual=false; garantirNaVista(c.gs); }
        }catch(e){ erros.push(`linha/início/volume: ${e}`); }
      }
      const fimVal=$("#fFim").value, fimOriginal=$("#fFim").dataset.original;
      if(fimVal && fimVal!==fimOriginal){
        try{
          const r=await fetch("/planeamento-ecos-largos/fim",{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({basecamp_card_id:c.id,dia_fim:fimVal})});
          const d=await r.json();
          if(d.erro) erros.push(`fim: ${d.erro}`);
          else{
            c.dur=d.duracao_dias; c.duracaoManual=true;
            /* pedido explícito do Rui (2026-09-30): o aviso de capacidade
               continua a aparecer, mas já não bloqueia a gravação — quem
               está a corrigir o fim à mão sabe melhor do que o modelo se
               cabe mesmo ou não (ver redefinir_fim). */
            if(d.aviso) avisos.push(`fim: ${d.aviso}`);
          }
        }catch(e){ erros.push(`fim: ${e}`); }
      }
    }else{
      /* pedido explícito do Rui (2026-09-30): "duplicar" abre logo a ficha
         da cópia (ver #duplicar) — escolher aqui uma linha + início agenda
         a OF diretamente, tal como o Google Calendar abre já os detalhes
         de um evento duplicado para escolheres a data. O card no Basecamp
         fica sempre em Triagem (nunca se mexe sozinho, ver nota no topo
         do módulo) até alguém o mover lá manualmente. */
      const linhaIdx=+$("#fLinha").value;
      const iso=$("#fInicio").value;
      const vol=+$("#fVol").value;
      if(linhaIdx>=0){
        if(!iso) erros.push("linha/início: escolhe uma data de início");
        else{
          try{
            const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
              headers:{"Content-Type":"application/json"},
              body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[linhaIdx],
                dia_inicio:iso,volume_m3:vol>0?vol:(c.volume||null)})});
            const d=await r.json();
            if(d.erro) erros.push(`linha/início: ${d.erro}`);
            else{ c.linha=linhaIdx; c.gs=idxOf(iso); c.dur=d.duracao_dias; c.volume=d.volume_m3; garantirNaVista(c.gs); }
          }catch(e){ erros.push(`linha/início: ${e}`); }
        }
      }else if(vol>0){
        try{
          const r=await fetch("/planeamento-ecos-largos/volume",{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({basecamp_card_id:c.id,volume_m3:vol})});
          const d=await r.json();
          if(d.erro) erros.push(`volume: ${d.erro}`);
          else c.volume=d.volume_m3;
        }catch(e){ erros.push(`volume: ${e}`); }
      }
    }
    const tipo=$("#fMad").value;
    if(tipo){
      try{
        const r=await fetch("/planeamento-ecos-largos/madeira",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,tipo_madeira:tipo})});
        const d=await r.json();
        if(d.erro) erros.push(`madeira: ${d.erro}`);
        else c.madeira=tipo;
      }catch(e){ erros.push(`madeira: ${e}`); }
    }
    log("local",`"${c.titulo}" — guardar tudo`);
    /* recarrega tudo, não só esta OF: linha/início/volume/fim podem ter
       recalculado a duração de outras OFs já na mesma linha (ver
       _recalcular_linha) — sem isto ficavam com a duração antiga no
       ecrã até ao próximo refresh. */
    await carregar();
    btn.textContent="Guardar tudo"; btn.disabled=false;
    if(avisos.length) alert("Guardado, mas atenção:\n"+avisos.join("\n"));
    if(erros.length) alert("Alguns campos falharam:\n"+erros.join("\n"));
    else closeSheet();
  };
}
function closeSheet(){ $("#veil").classList.remove("on"); $("#sheet").classList.remove("on"); }

/* ---------- ficha de logística ---------- */
function openSheetLogistica(id,somenteLeitura){
  somenteLeitura=!!somenteLeitura;
  const dis=somenteLeitura?"disabled":"";
  const c=cardLog(id);
  const d=MASTER[c.gs];
  $("#sheet").innerHTML=`
    <div class="of mono">card #${numCurto(c.id)} · logística</div>
    <div class="frow"><label>Nome</label><input id="fTitulo" type="text" value="${String(c.titulo).replace(/"/g,"&quot;")}" ${dis}></div>
    ${somenteLeitura?"":`<div class="acts"><button class="btn" id="guardarTitulo">Guardar nome</button></div>`}
    <div class="kv"><span>Coluna no Basecamp</span><b>${c.coluna||"—"}</b></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Dia de carregamento</label>
    <div class="frow"><input id="logDia" type="date" value="${d.iso}" ${dis}>${somenteLeitura?"":`<button class="btn" id="guardarDia">Guardar</button>`}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Quem carrega</label>
    <div class="frow"><input id="logQuem" placeholder="Ex: João" value="${c.quemCarrega?String(c.quemCarrega).replace(/"/g,"&quot;"):""}" ${dis}>${somenteLeitura?"":`<button class="btn" id="guardarQuem">Guardar</button>`}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor da barra lateral</label>
    <div class="cores" id="coresBarra">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.cor||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}" ${dis}
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor de fundo</label>
    <div class="cores" id="coresFundo">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.corFundo||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}" ${dis}
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <div class="owner">Isto é o duplicado de logística desta OF — mover ou apagar aqui não altera a produção nem o Basecamp. As duas cores são exceção: são da OF, uma só, por isso mudam também no card de produção.</div>
    ${somenteLeitura?"":`
    <div class="acts">
      <button class="btn warn" id="apagarLog">Apagar duplicado</button>
    </div>`}
    <div class="acts">
      ${c.url?`<a class="btn" id="bcOpen" target="_blank" rel="noopener" href="${c.url}">Abrir card no Basecamp</a>`:""}
      ${somenteLeitura?"":`<button class="btn primary" id="guardarTudoLog">Guardar tudo</button>`}
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
  if(somenteLeitura) return;
  $("#guardarTitulo").onclick=async()=>{
    const titulo=$("#fTitulo").value.trim();
    if(!titulo){ alert("O nome não pode ficar vazio."); return; }
    $("#guardarTitulo").textContent="A guardar…"; $("#guardarTitulo").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/renomear",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id,titulo})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); return; }
      c.titulo=titulo;
      const gemeo=cards.find(x=>x.id===c.id); if(gemeo) gemeo.titulo=titulo;
      log("local",`nome atualizado para "${titulo}"`);
      render();
    }catch(e){ alert("Falhou a guardar: "+e); }
    finally{ $("#guardarTitulo").textContent="Guardar nome"; $("#guardarTitulo").disabled=false; }
  };
  $("#guardarDia").onclick=async()=>{
    const iso=$("#logDia").value;
    if(!iso){ alert("Escolhe uma data."); return; }
    $("#guardarDia").textContent="A guardar…"; $("#guardarDia").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/logistica/mover",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id,dia_carregamento:iso})});
      const dd=await r.json();
      if(dd.erro){ alert(dd.erro); $("#guardarDia").textContent="Guardar"; $("#guardarDia").disabled=false; return; }
      c.gs=idxOf(iso);
      log("local",`dia de carregamento de "${c.titulo}" alterado para ${iso}`);
      renderLogistica(); closeSheet();
    }catch(e){ alert("Falhou a guardar: "+e); $("#guardarDia").textContent="Guardar"; $("#guardarDia").disabled=false; }
  };
  $("#guardarQuem").onclick=async()=>{
    const quem_carrega=$("#logQuem").value.trim();
    $("#guardarQuem").textContent="A guardar…"; $("#guardarQuem").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/logistica/quem-carrega",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id,quem_carrega})});
      const dd=await r.json();
      if(dd.erro){ alert(dd.erro); return; }
      c.quemCarrega=dd.quem_carrega||null;
      log("local",`quem carrega "${c.titulo}" atualizado`);
      renderLogistica();
    }catch(e){ alert("Falhou a guardar: "+e); }
    finally{ $("#guardarQuem").textContent="Guardar"; $("#guardarQuem").disabled=false; }
  };
  $("#coresBarra").querySelectorAll(".swatch").forEach(sw=>{
    sw.onclick=async()=>{
      const cor=sw.dataset.cor;
      try{
        const r=await fetch("/planeamento-ecos-largos/cor",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,cor})});
        const dd=await r.json();
        if(dd.erro){ alert(dd.erro); return; }
        c.cor=cor||null;
        const gemeo=cards.find(x=>x.id===c.id); if(gemeo) gemeo.cor=c.cor;
        $("#coresBarra").querySelectorAll(".swatch").forEach(x=>x.classList.remove("sel"));
        sw.classList.add("sel");
        log("local",`cor de "${c.titulo}" atualizada (é a mesma OF da produção)`);
        render();
      }catch(e){ alert("Falhou a guardar: "+e); }
    };
  });
  $("#coresFundo").querySelectorAll(".swatch").forEach(sw=>{
    sw.onclick=async()=>{
      const cor=sw.dataset.cor;
      try{
        const r=await fetch("/planeamento-ecos-largos/cor-fundo",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,cor})});
        const dd=await r.json();
        if(dd.erro){ alert(dd.erro); return; }
        c.corFundo=cor||null;
        const gemeo=cards.find(x=>x.id===c.id); if(gemeo) gemeo.corFundo=c.corFundo;
        $("#coresFundo").querySelectorAll(".swatch").forEach(x=>x.classList.remove("sel"));
        sw.classList.add("sel");
        log("local",`cor de fundo de "${c.titulo}" atualizada (é a mesma OF da produção)`);
        render();
      }catch(e){ alert("Falhou a guardar: "+e); }
    };
  });
  $("#apagarLog").onclick=async()=>{
    if(!confirm(`Apagar o duplicado de logística de "${c.titulo}"?\n\nNão afeta a produção nem o Basecamp.`)) return;
    $("#apagarLog").textContent="A apagar…"; $("#apagarLog").disabled=true;
    try{
      await fetch("/planeamento-ecos-largos/logistica/apagar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id})});
      cardsLog=cardsLog.filter(x=>x.id!==c.id);
      log("local",`duplicado de logística apagado: ${c.titulo}`);
      renderLogistica(); closeSheet();
    }catch(e){ alert("Falhou a apagar: "+e); $("#apagarLog").textContent="Apagar duplicado"; $("#apagarLog").disabled=false; }
  };
  $("#guardarTudoLog").onclick=async()=>{
    const btn=$("#guardarTudoLog");
    btn.textContent="A guardar…"; btn.disabled=true;
    const erros=[];
    const titulo=$("#fTitulo").value.trim();
    if(titulo){
      try{
        const r=await fetch("/planeamento-ecos-largos/renomear",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,titulo})});
        const dd=await r.json();
        if(dd.erro) erros.push(`nome: ${dd.erro}`);
        else{ c.titulo=titulo; const gemeo=cards.find(x=>x.id===c.id); if(gemeo) gemeo.titulo=titulo; }
      }catch(e){ erros.push(`nome: ${e}`); }
    }else erros.push("nome: não pode ficar vazio");
    const iso=$("#logDia").value;
    if(iso && iso!==d.iso){
      try{
        const r=await fetch("/planeamento-ecos-largos/logistica/mover",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,dia_carregamento:iso})});
        const dd=await r.json();
        if(dd.erro) erros.push(`dia de carregamento: ${dd.erro}`);
        else{ c.gs=idxOf(iso); garantirNaVista(c.gs); }
      }catch(e){ erros.push(`dia de carregamento: ${e}`); }
    }
    const quem_carrega=$("#logQuem").value.trim();
    try{
      const r=await fetch("/planeamento-ecos-largos/logistica/quem-carrega",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id,quem_carrega})});
      const dd=await r.json();
      if(dd.erro) erros.push(`quem carrega: ${dd.erro}`);
      else c.quemCarrega=dd.quem_carrega||null;
    }catch(e){ erros.push(`quem carrega: ${e}`); }
    log("local",`"${c.titulo}" — guardar tudo (logística)`);
    renderLogistica(); render();
    btn.textContent="Guardar tudo"; btn.disabled=false;
    if(erros.length) alert("Alguns campos falharam:\n"+erros.join("\n"));
    else closeSheet();
  };
}

/* ---------- nova encomenda ---------- */
function openForm(){
  $("#sheet").innerHTML=`
    <h3>Criar encomenda</h3>
    <div class="frow"><label>Cliente</label><input id="fCli" placeholder="Ex: Casa Cerne"></div>
    <div class="frow"><label>Peça</label><input id="fTt" placeholder="Ex: Soalho carvalho 22mm"></div>
    <div class="frow"><label>Volume (m³)</label><input id="fVol" type="number" min="0.1" step="0.1" placeholder="Ex: 30"></div>
    <div class="frow"><label>Madeira</label><select id="fMadeira">
      <option value="">Não especificado</option>
      <option value="seca">Seca</option>
      <option value="verde">Verde</option>
    </select></div>
    <div class="frow"><label>Notas</label><textarea id="fNotas" rows="2" placeholder="opcional"></textarea></div>
    <div class="err" id="fErr"></div>
    <div class="owner">O card nasce sempre na coluna Triagem do Basecamp (projeto Ecos Largos), com o título "Peça — Cliente", e fica sempre na fila por agendar — arrasta-o depois para uma linha/dia.</div>
    <div class="acts">
      <button class="btn primary" id="fSave">Criar encomenda</button>
      <button class="btn" id="close">Cancelar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
  $("#fCli").focus();
  $("#fSave").onclick=async()=>{
    const titulo=$("#fTt").value.trim(), cliente=$("#fCli").value.trim(), notas=$("#fNotas").value.trim();
    const volume=$("#fVol").value?+$("#fVol").value:null;
    if(!titulo){ $("#fErr").textContent="Escreve a peça — é o título do card no Basecamp."; $("#fTt").focus(); return; }
    $("#fSave").textContent="A criar…"; $("#fSave").disabled=true;
    try{
      const body={titulo,cliente,volume_m3:volume,tipo_madeira:$("#fMadeira").value||null,notas,
        linha:null,dia_inicio:null};
      const r=await fetch("/planeamento-ecos-largos/nova-encomenda",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json();
      if(d.erro){ $("#fErr").textContent=d.erro; $("#fSave").textContent="Criar encomenda"; $("#fSave").disabled=false; return; }
      cards.unshift({id:d.basecamp_card_id,titulo:d.titulo,coluna:d.coluna_basecamp,prazo:d.prazo,url:d.url,
        volume:d.volume_m3,madeira:d.tipo_madeira||null,cor:null,ordem:0,linha:null,gs:null,dur:1});
      log("local",`criado no Basecamp (Triagem): ${d.titulo}`);
      render(); closeSheet();
      setTimeout(()=>{ const b=document.querySelector(`.qcard[data-id="${d.basecamp_card_id}"]`); if(b)b.classList.add("flash"); },30);
    }catch(e){ $("#fErr").textContent="Falhou a criar no Basecamp: "+e; $("#fSave").textContent="Criar encomenda"; $("#fSave").disabled=false; }
  };
}
$("#novo").onclick=()=>{ if(modoEdicao) openForm(); };

/* ---------- controlos ---------- */
$("#seg").onclick=e=>{ const b=e.target.closest("button"); if(b) setMode(b.dataset.m); };
$("#prev").onclick=()=>step(-1);
$("#next").onclick=()=>step(1);
$("#hoje").onclick=()=>{ view.start=Math.max(HOJE,0); setMode(view.mode); };
/* pesquisar (ver buscaClasse/corresponde): destaca ao escrever (título
   ou número, curto ou completo), esbate o resto. Enter salta para a
   data do primeiro resultado agendado que não esteja já visível, tal
   como no Google Calendar — se já estiver visível, só o seleciona. */
$("#busca").addEventListener("input",()=>{
  buscaQuery=normalizarTexto($("#busca").value.trim());
  render();
});
$("#busca").addEventListener("keydown",e=>{
  if(e.key!=="Enter") return;
  e.preventDefault();
  if(!buscaQuery) return;
  const candidatos=[...cards,...cardsLog].filter(c=>c.gs!==null && corresponde(c,buscaQuery));
  if(!candidatos.length) return;
  candidatos.sort((a,b)=>a.gs-b.gs);
  const naVista=candidatos.find(c=>c.gs>=view.start && c.gs<view.start+view.len);
  const alvo=naVista||candidatos[0];
  if(!naVista) view.start=clamp(alvo.gs-Math.floor(view.len/2),0,Math.max(MASTER.length-view.len,0));
  selecionadoId=alvo.id;
  render();
});
$("#atualizar").onclick=()=>carregar();
$("#undo").onclick=()=>{
  if(!modoEdicao||!undoStack.length)return;
  const prev=undoStack.pop(); const c=card(prev.id);
  if(!c)return;
  c.linha=prev.linha; c.gs=prev.gs; c.dur=prev.dur;
  render(); sync(c); log("local","anulado");
};
/* modo só consulta por omissão (pedido explícito do Rui, 2026-09-29): a
   página abre sempre em modo consulta, mesmo que já se tenha ligado a
   edição antes nesta sessão — só o botão "Editar" liga tudo (arrastar,
   fichas, capacidade, cores por estado, nova encomenda), até fechar ou
   recarregar a página. */
function aplicarModoEdicao(){
  document.body.classList.toggle("viewonly",!modoEdicao);
  $("#modoEdicaoBtn").textContent=modoEdicao?"Terminar edição":"Editar";
}
$("#modoEdicaoBtn").onclick=()=>{ modoEdicao=!modoEdicao; aplicarModoEdicao(); };
aplicarModoEdicao();
document.addEventListener("keydown",e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==="z"){e.preventDefault();$("#undo").click();}
  if(e.key==="ArrowLeft"&&!e.target.closest("select,input,textarea"))step(-1);
  if(e.key==="ArrowRight"&&!e.target.closest("select,input,textarea"))step(1);
});
let rt; addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(render,120)});

carregar();
/* relê o Basecamp sozinho de vez em quando (ex: para apanhar uma OF que
   mudou de coluna lá, sem ser preciso recarregar a página à mão) — nunca
   enquanto a ficha estiver aberta ou a arrastar algo, para não perder o
   que a pessoa está a fazer. */
setInterval(()=>{ if(!drag && !dragLog && !qdrag && !$("#veil").classList.contains("on")) carregar(); }, 120000);
/* tempo real (pedido explícito do Rui, 2026-09): sempre que alguém muda
   algo no quadro — nesta sessão ou noutra pessoa noutro separador — todas
   as páginas abertas atualizam sozinhas, sem precisar de refresh nem de
   esperar pelo polling acima. Ver main.py: /planeamento-ecos-largos/eventos
   (Server-Sent Events) + _notificar_planeamento_ecos_largos, chamado por
   todos os endpoints que escrevem. O browser religa sozinho o EventSource
   se a ligação cair — não é preciso lógica de reconexão aqui; o polling
   de 2 minutos acima fica só como rede de segurança. */
if(typeof EventSource!=="undefined"){
  const eventos=new EventSource("/planeamento-ecos-largos/eventos");
  eventos.onmessage=()=>{
    if(!drag && !dragLog && !qdrag && !$("#veil").classList.contains("on")) carregar();
  };
}
</script>
</body>
</html>
"""
