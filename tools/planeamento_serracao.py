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
import math
import unicodedata
from datetime import date, timedelta
import db
from tools import basecamp

PROJETO = "Ecos Largos"

# colunas do card table real do Ecos Largos que representam OFs em fluxo de
# fabrico (confirmado ao vivo, 2026-09, contra a API real). As colunas
# "Linha 1" a "Linha 6" / Charriots / Empilhadores do mesmo quadro guardam
# cards de ALOCAÇÃO DE PESSOAL, não OFs — ficam de fora deste quadro, por
# pedido explícito do Rui. "Vendido" inclui-se para uma OF já agendada não
# desaparecer do quadro quando a venda fecha no Basecamp — fica visível
# (cor automática roxa, ver template) em vez de desaparecer; nunca entra
# na fila (só cards em Triagem entram lá, ver estado_planeamento_serracao).
COLUNAS_OF = {"triagem", "programacao", "em producao", "produzido", "vendido"}

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
ESTADOS_COR = {"produzido": "Produzido", "em_producao": "Em Produção", "vendido": "Vendido"}

def _normalizar(texto: str) -> str:
    sem_acentos = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acentos.lower().strip()

def _cards_of_ativos() -> list[dict]:
    """Todos os cards de OF ativos do quadro Kanban do Ecos Largos, só das
    colunas de fluxo de fabrico (ver COLUNAS_OF) — vai sempre buscar dados
    atuais ao Basecamp (basecamp.cards_de_card_table não tem cache), nunca
    mantém cópia à parte. Passa "" como nome do quadro porque o Ecos Largos
    tem um único card table ativo — não é preciso adivinhar o título exato
    dele (ver basecamp.cards_de_card_table: "" é substring de qualquer
    título)."""
    cards = basecamp.cards_de_card_table("", projeto=PROJETO)
    return [c for c in cards if _normalizar(c.get("estado")) in COLUNAS_OF]

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
            info["ordem"] = agendamento["ordem"]
            agendadas.append(info)
        else:
            bolsa.append(info)
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
            "cor": lg["cor"],
            # cor de fundo é da OF, uma só, partilhada com a produção (não
            # um campo à parte na logística) — pedido explícito do Rui,
            # 2026-09: mudar o fundo tem de se ver nas duas tabelas.
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

def _duracao_por_volume(linha: str, volume_m3: float, dia_inicio: str = None,
                        excluir_id: int = None) -> int:
    """Quantos dias uma encomenda ocupa numa linha, a partir do seu volume
    (m³) e da capacidade diária dessa linha (editável, ver
    atualizar_capacidade_linha).

    Aproveita sempre ao máximo a capacidade de cada dia (pedido explícito
    do Rui, 2026-09): se `dia_inicio` já tiver outra(s) encomenda(s) a
    ocupar parte da capacidade da linha nesse dia, esta encomenda não é
    simplesmente recusada — a duração vai crescendo (1, 2, 3... dias) até
    encontrar a mais curta cujo ritmo diário (volume ÷ duração) caiba,
    todos os dias, no espaço ainda livre (ver _ocupacao_diaria); ou seja,
    reparte-se sozinha por esse dia e pelos seguintes em vez de ocupar só
    o primeiro. Sem `dia_inicio` (ainda na fila, sem dia definido) ou sem
    capacidade configurada para a linha, usa só volume ÷ capacidade
    plena, sem olhar a ocupação (não há ainda dia nenhum para verificar)."""
    if not volume_m3:
        return 1
    capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
    if capacidade <= 0:
        return 1
    duracao_minima = max(1, math.ceil(volume_m3 / capacidade))
    if not dia_inicio:
        return duracao_minima
    ocupacao = _ocupacao_diaria(linha, excluir_id=excluir_id)
    inicio = date.fromisoformat(dia_inicio)
    for duracao in range(duracao_minima, LIMITE_DIAS_REPARTIR + 1):
        ritmo = volume_m3 / duracao
        if all(
            ocupacao.get((inicio + timedelta(days=i)).isoformat(), 0) + ritmo <= capacidade + 1e-9
            for i in range(duracao)
        ):
            return duracao
    return duracao_minima  # não coube em espaço nenhum razoável — deixa _validar_capacidade recusar com a mensagem certa

def _ocupacao_diaria(linha: str, excluir_id: int = None) -> dict:
    """Quanto de m³/dia já está ocupado, dia a dia, numa linha — soma o
    "ritmo diário" (volume ÷ duração) de cada OF já agendada nessa linha
    que cubra esse dia. Uma OF sem volume definido não tem ritmo diário
    conhecido: ocupa a linha por completo nesses dias (`float("inf")`),
    tal como acontecia antes de existir volume/capacidade — conservador,
    para nunca sobre-comprometer uma linha sem dados. `excluir_id` ignora
    o próprio card (para permitir reagendar/mover uma OF já colocada)."""
    ocupacao = {}
    for a in db.agendamentos_producao_ecos_largos():
        if a["linha"] != linha or not a["dia_inicio"]:
            continue
        if excluir_id is not None and a["basecamp_card_id"] == excluir_id:
            continue
        duracao = max(1, a["duracao_dias"] or 1)
        ritmo = (a["volume_m3"] / duracao) if a["volume_m3"] else None
        inicio = date.fromisoformat(a["dia_inicio"])
        for i in range(duracao):
            dia = (inicio + timedelta(days=i)).isoformat()
            if ritmo is None:
                ocupacao[dia] = float("inf")
            else:
                ocupacao[dia] = ocupacao.get(dia, 0) + ritmo
    return ocupacao

def _validar_capacidade(linha: str, dia_inicio: str, duracao_dias: int,
                        volume_m3: float, excluir_id: int = None) -> str:
    """Confirma que colocar esta OF (com este volume/duração) nesta linha,
    a partir deste dia, não ultrapassa a capacidade (m³/dia) da linha em
    nenhum dos dias que ocupa — pedido explícito do Rui (2026-09): se já
    ultrapassar a capacidade nesse dia, não deve ser possível; se ainda
    houver folga, deve ser possível colocar outra encomenda no mesmo
    espaço. Devolve uma mensagem de erro, ou None se estiver tudo bem."""
    capacidade = db.capacidades_linhas_producao_ecos_largos().get(linha) or 0
    ritmo_card = (volume_m3 / duracao_dias) if volume_m3 else None
    ocupacao = _ocupacao_diaria(linha, excluir_id=excluir_id)
    inicio = date.fromisoformat(dia_inicio)
    for i in range(duracao_dias):
        dia = (inicio + timedelta(days=i)).isoformat()
        usado = ocupacao.get(dia, 0)
        if ritmo_card is None:
            if usado > 0:
                return (f"a linha {linha!r} já tem outra encomenda em {dia} — sem volume "
                        "definido, esta encomenda precisaria da linha só para ela nesse dia")
            continue
        if usado == float("inf"):
            return f"a linha {linha!r} está reservada por completo em {dia} por outra encomenda sem volume definido"
        if capacidade > 0 and usado + ritmo_card > capacidade + 1e-9:
            sobra = max(0, capacidade - usado)
            return (f"capacidade excedida em {dia} na linha {linha!r}: já há {usado:.1f} "
                    f"m³/dia ocupados de {capacidade:.1f} m³/dia (sobram só {sobra:.1f} m³/dia)")
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
    """Cria o duplicado de logística/carregamento de uma OF — só quando já
    tem linha/dia E tipo de madeira definidos, e só uma vez (ver
    db.criar_logistica_carregamento, ON CONFLICT DO NOTHING): pedido
    explícito do Rui (2026-09), para a equipa da logística saber em que
    dia a OF estará pronta a carregar, assim que ela sair da fila."""
    if not dia_inicio or not tipo_madeira or tipo_madeira not in DIAS_CURA_MADEIRA:
        return
    if db.logistica_carregamento(basecamp_card_id):
        return
    dia_carregamento = _calcular_dia_carregamento(dia_inicio, duracao_dias, tipo_madeira)
    db.criar_logistica_carregamento(basecamp_card_id, dia_carregamento)

def agendar(basecamp_card_id: int, linha: str, dia_inicio: str, volume_m3: float = None) -> dict:
    """Agenda (ou reagenda) uma OF numa linha/dia — só na base local, nunca
    escreve nada no Basecamp (ver nota no topo do módulo). A duração é
    sempre calculada aqui a partir do volume e da capacidade da linha (ver
    _duracao_por_volume), nunca escolhida à mão. Se `volume_m3` não for
    indicado, mantém o volume já guardado anteriormente para esta OF (não
    o apaga só por não vir neste pedido). Recusa o agendamento (ver
    _validar_capacidade) se ultrapassar a capacidade da linha nalgum dos
    dias ocupados. Se já tiver tipo de madeira definido, duplica para a
    logística (ver _talvez_duplicar_logistica)."""
    if linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    if not dia_inicio:
        return {"erro": "falta indicar o dia de início"}
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
    duracao_dias = _duracao_por_volume(linha, volume_m3, dia_inicio, excluir_id=basecamp_card_id)
    erro = _validar_capacidade(linha, dia_inicio, duracao_dias, volume_m3, excluir_id=basecamp_card_id)
    if erro:
        return {"erro": erro}
    db.guardar_agendamento_producao(basecamp_card_id, linha, dia_inicio, duracao_dias, volume_m3)
    tipo_madeira = existente["tipo_madeira"] if existente else None
    _talvez_duplicar_logistica(basecamp_card_id, dia_inicio, duracao_dias, tipo_madeira)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id,
            "duracao_dias": duracao_dias, "volume_m3": volume_m3}

def atualizar_capacidade_linha(linha: str, capacidade_m3_dia: float) -> dict:
    """Atualiza a capacidade (m³/dia) de uma linha — editável pela equipa
    diretamente no quadro (pedido explícito do Rui, 2026-09)."""
    if linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    try:
        capacidade_m3_dia = float(capacidade_m3_dia)
    except (TypeError, ValueError):
        return {"erro": "capacidade inválida"}
    if capacidade_m3_dia <= 0:
        return {"erro": "capacidade tem de ser maior que 0"}
    return db.atualizar_capacidade_linha_producao(linha, capacidade_m3_dia)

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
    """Devolve uma OF à bolsa por agendar — só na base local."""
    return db.desagendar_producao(basecamp_card_id)

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
    2026-09."""
    basecamp.apagar_card(basecamp_card_id, projeto=PROJETO)
    db.remover_agendamento_producao(basecamp_card_id)
    db.remover_logistica_carregamento(basecamp_card_id)
    return {"apagado": True, "basecamp_card_id": basecamp_card_id}

def mover_logistica(basecamp_card_id: int, dia_carregamento: str) -> dict:
    """Muda manualmente o dia de carregamento de uma OF já duplicada —
    independente da produção a partir daí (ver nota da tabela em db.py)."""
    if not dia_carregamento:
        return {"erro": "falta indicar o dia de carregamento"}
    if not db.logistica_carregamento(basecamp_card_id):
        return {"erro": "esta OF ainda não tem duplicado de logística"}
    return db.mover_logistica_carregamento(basecamp_card_id, dia_carregamento)

def definir_cor_logistica(basecamp_card_id: int, cor: str) -> dict:
    """Define ou limpa a cor manual de um card de logística (mesma lista
    fixa, ver CORES_VALIDAS)."""
    cor = (cor or "").strip().lower() or None
    if cor and cor not in CORES_VALIDAS:
        return {"erro": f"cor desconhecida: {cor!r} — usa uma de {sorted(CORES_VALIDAS)}"}
    if not db.logistica_carregamento(basecamp_card_id):
        return {"erro": "esta OF ainda não tem duplicado de logística"}
    db.guardar_cor_logistica(basecamp_card_id, cor)
    return {"guardado": True, "basecamp_card_id": basecamp_card_id, "cor": cor}

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
    card = basecamp.criar_card("Triagem", titulo_basecamp, "\n".join(partes_notas), projeto=PROJETO)
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

  .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 16px}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--paper);
    border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13.5px;color:var(--dim)}
  .pill b{color:var(--ink);font-weight:700}
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
  .blk .of{font-size:11.5px;color:var(--dim);margin-top:1px;white-space:nowrap}
  .dense .blk{padding:3px 6px;border-radius:7px}
  .dense .blk .of{display:none}
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

  .veil{position:fixed;inset:0;background:rgba(26,28,30,.35);display:none}
  .veil.on{display:block}
  .sheet{position:fixed;top:50%;left:50%;background:var(--paper);
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
  </div>

  <div class="bar">
    <div class="pill"><span class="dot" id="syncDot"></span><span id="syncTxt">Ligado ao Basecamp</span></div>
    <div class="pill"><b id="statBolsa">—</b> por agendar</div>
    <div class="pill"><b id="statAgendadas">—</b> agendadas</div>
    <button class="btn" id="undo">Anular</button>
    <button class="btn" id="atualizar">Atualizar do Basecamp</button>
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

  <details class="log">
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

const $=s=>document.querySelector(s);
const card=id=>cards.find(c=>c.id===id);
const cardLog=id=>cardsLog.find(c=>c.id===id);
const atrasado=c=>c.prazo && c.prazo<hojeISO;

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
     ver corProduto (mesma função para produção, fila e logística).
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
const ESTADOS_COR={"Produzido":"produzido","Em Produção":"em_producao","Vendido":"vendido"};
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
        gs:idxOf(c.dia_inicio),dur:c.duracao_dias,ordem:c.ordem||0})).filter(c=>c.linha>=0&&c.gs>=0)
    ];
    cardsLog=(d.logistica||[]).map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
      url:c.url,cor:c.cor,corFundo:c.cor_fundo,quemCarrega:c.quem_carrega,gs:idxOf(c.dia_carregamento)})).filter(c=>c.gs>=0);
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    render(); renderCoresEstado();
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
    CAPACIDADES[linha]=num;
    log("local",`capacidade de "${linha}" atualizada para ${num} m³/dia`);
    renderLabels();
  }catch(e){ alert("Falhou a guardar: "+e); }
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
        renderCoresEstado();
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
  $("#labels").innerHTML='<div class="head"></div>'+LINHAS.map(n=>
    `<div class="lbl" data-linha="${n}"><div class="n">${n}</div>
     <div class="c">${CAPACIDADES[n]!=null?CAPACIDADES[n]+" m³/dia":"definir capacidade"} · editar</div></div>`).join("");
  $("#labels").querySelectorAll(".lbl").forEach(el=>{
    el.onclick=()=>editarCapacidade(el.dataset.linha);
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
   pedido explícito do Rui (2026-09). Para nunca ficarem sobrepostas (o
   que as fazia "desaparecer" umas atrás das outras), cada card recebe
   aqui uma fatia vertical própria dentro da lane (uma acima, outra
   abaixo), com altura proporcional ao seu ritmo diário — quanto mais m³
   por dia ocupar, maior o card. Calculado sobre TODOS os cards da linha
   (não só os visíveis), para a posição de cada um não saltar ao navegar
   entre semanas.*/
const ALTURA_MIN_FRACAO=0.38;
function encaixarCardsLinha(cardsLinha, capacidade){
  const ocupado={};
  [...cardsLinha].sort((a,b)=>a.gs-b.gs||(a.ordem||0)-(b.ordem||0)||a.id-b.id).forEach(c=>{
    const ritmo=c.volume?c.volume/c.dur:null;
    const fracao=(ritmo&&capacidade>0)?clamp(ritmo/capacidade,ALTURA_MIN_FRACAO,1):1;
    let topo=0;
    for(let k=0;k<c.dur;k++) topo=Math.max(topo,ocupado[c.gs+k]||0);
    c._topoFracao=topo; c._alturaFracao=fracao;
    for(let k=0;k<c.dur;k++) ocupado[c.gs+k]=topo+fracao;
  });
}
function renderLanes(){
  const D=days();
  let h="";
  LINHAS.forEach(()=>{ h+='<div class="row">'+D.map(d=>{
    const hoje=MASTER.indexOf(d)===HOJE;
    return `<div class="cell${FDS(d)?" wk":""}${hoje?" hoje":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  LINHAS.forEach((nome,li)=>{
    encaixarCardsLinha(cards.filter(c=>c.linha===li), CAPACIDADES[nome]||0);
  });
  const PAD=4;
  cards.filter(c=>c.linha!==null).forEach(c=>{
    const a=c.gs-view.start, b=a+c.dur;
    if(b<=0||a>=view.len) return;
    const l=Math.max(a,0), r=Math.min(b,view.len);
    const usavel=LANE-PAD;
    const el=document.createElement("div");
    el.className="blk"+(a<0?" clipL":"")+(b>view.len?" clipR":"")+(atrasado(c)?" atrasado":"");
    el.tabIndex=0; el.dataset.id=c.id;
    el.style.borderLeftColor=corProduto(c);
    const fundo=fundoCard(c); if(fundo) el.style.background=fundo;
    el.style.left=(l*DAY+3)+"px";
    el.style.top=(c.linha*LANE+PAD+c._topoFracao*usavel)+"px";
    el.style.width=((r-l)*DAY-8)+"px";
    el.style.height=Math.max(16,c._alturaFracao*usavel-PAD)+"px";
    el.innerHTML=`<div class="editBtn" data-edit="${c.id}" title="Editar">✎</div><div class="tt">${c.titulo}</div>
      <div class="of">${c.volume?(c.volume+" m³ · "):""}${c.prazo?("prazo "+c.prazo):"sem prazo"}</div>`;
    bl.appendChild(el);
  });
  stats(); aplicarSelecao();
}
function renderFila(){
  const q=cards.filter(c=>c.linha===null);
  $("#fila").innerHTML = q.length ? q.map(c=>{
    const fundo=fundoCard(c);
    return `<div class="qcard${atrasado(c)?" atrasado":""}" data-id="${c.id}"
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
      el.className="blk";
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
    }else{
      c.dur=d.duracao_dias; c.volume=d.volume_m3;
      log("local",`guardado: ${c.titulo} → ${LINHAS[c.linha]}, ${MASTER[c.gs].iso}, ${c.dur}d`);
      atualizarLogistica(); // pode ter criado agora o duplicado de logística
    }
  }catch(e){ log("local",`erro ao guardar: ${e}`); }
  $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
  renderFila(); renderLanes();
}
async function desagendarServidor(c){
  try{
    await fetch("/planeamento-ecos-largos/desagendar",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id})});
    log("local",`devolvido à fila: ${c.titulo}`);
  }catch(e){ log("local",`erro ao devolver à fila: ${e}`); }
}
function sync(c, anterior){ if(c.linha!==null && c.gs!==null) guardarAgendamento(c, anterior); else desagendarServidor(c); }

/* ---------- arrastar dentro da grelha ---------- */
let drag=null;
$("#lanes").addEventListener("pointerdown",e=>{
  if(e.target.closest(".editBtn"))return;
  const b=e.target.closest(".blk"); if(!b)return;
  const c=card(+b.dataset.id);
  drag={el:b,c,x0:e.clientX,y0:e.clientY,gs0:c.gs,lin0:c.linha,dur0:c.dur,dx:0,dy:0};
  b.setPointerCapture(e.pointerId); b.classList.add("drag"); e.preventDefault();
});
$("#lanes").addEventListener("pointermove",e=>{
  if(!drag)return;
  let dd=Math.round((e.clientX-drag.x0)/DAY), dl=Math.round((e.clientY-drag.y0)/LANE);
  dd=clamp(dd, -drag.gs0, MASTER.length-drag.dur0-drag.gs0);
  dl=clamp(dl, -drag.lin0, LINHAS.length-1-drag.lin0);
  drag.dx=dd; drag.dy=dl;
  drag.el.style.transform=`translate(${dd*DAY}px,${dl*LANE}px)`;
});
$("#lanes").addEventListener("pointerup",()=>{
  if(!drag)return; const c=drag.c;
  const moved=drag.dx||drag.dy;
  drag.el.classList.remove("drag");
  if(!moved){ drag.el.style.transform=""; drag=null; return; }
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
  c.linha=clamp(Math.floor(y/LANE),0,LINHAS.length-1);
  c.gs=view.start+clamp(Math.floor(x/DAY),0,view.len-c.dur);
  renderFila(); renderLanes(); sync(c, anterior);
});

/* ---------- arrastar na logística (só o dia muda, não há linhas) ---------- */
let dragLog=null;
$("#lanesLog").addEventListener("pointerdown",e=>{
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
/* um clique simples num card (fila, produção ou logística) só destaca-o a
   ele e ao seu par na outra tabela — a edição fica no botão "✎" de cada
   card (pedido explícito do Rui, 2026-09). */
$("#lanes").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ openSheet(+e.target.closest(".editBtn").dataset.edit); return; }
  const b=e.target.closest(".blk"); if(b) selecionar(+b.dataset.id,"producao");
});
$("#fila").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ openSheet(+e.target.closest(".editBtn").dataset.edit); return; }
  const q=e.target.closest(".qcard"); if(q) selecionar(+q.dataset.id,"fila");
});
$("#lanesLog").addEventListener("click",e=>{
  if(e.target.closest(".editBtn")){ openSheetLogistica(+e.target.closest(".editBtn").dataset.editlog); return; }
  const b=e.target.closest(".blk"); if(b) selecionar(+b.dataset.id,"logistica");
});
function openSheet(id){
  const c=card(id);
  const agendado = c.linha!==null && c.gs!==null;
  let corpo = `<div class="kv"><span>Estado</span><b>Por agendar</b></div>`;
  if(agendado){
    const s=MASTER[c.gs], f=MASTER[clamp(c.gs+c.dur-1,0,MASTER.length-1)];
    corpo = `
    <div class="frow"><label>Linha</label><select id="fLinha">${LINHAS.map((n,i)=>
      `<option value="${i}"${i===c.linha?" selected":""}>${n}</option>`).join("")}</select></div>
    <div class="frow"><label>Início</label><input id="fInicio" type="date" value="${s.iso}"></div>
    <div class="acts"><button class="btn" id="guardarLinha">Guardar linha/início</button></div>
    <div class="kv"><span>Fim</span><b>${DOW[f.dow]} ${f.dd} ${MESC[f.mo]} · ${c.dur} dias</b></div>`;
  }
  $("#sheet").innerHTML=`
    <div class="of mono">card ${c.id}</div>
    <h3>${c.titulo}</h3>
    ${corpo}
    <div class="kv"><span>Coluna no Basecamp</span><b>${c.coluna||"—"}</b></div>
    <div class="kv"><span>Prazo no Basecamp</span><b>${c.prazo||"sem prazo"}</b></div>
    <div class="frow"><label>Volume (m³)</label><input id="fVol" type="number" min="0.1" step="0.1" value="${c.volume||""}" placeholder="ex: 30"></div>
    <div class="acts"><button class="btn" id="guardarVol">Guardar volume</button></div>
    <div class="frow"><label>Madeira</label><select id="fMad">
      <option value="">Não especificado</option>
      <option value="seca"${c.madeira==="seca"?" selected":""}>Seca</option>
      <option value="verde"${c.madeira==="verde"?" selected":""}>Verde</option>
    </select></div>
    <div class="acts"><button class="btn" id="guardarMad">Guardar madeira</button></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor da barra lateral (tipo de produto)</label>
    <div class="cores" id="coresBarra">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.cor||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}"
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor de fundo</label>
    <div class="cores" id="coresFundo">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.corFundo||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}"
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <div class="owner">A linha, o início, a duração, o volume e a madeira vivem só aqui — o Basecamp não tem onde os guardar. A duração é sempre calculada a partir do volume e da capacidade da linha. A barra lateral é a cor do tipo de produto; o fundo é automático por estado (amarelo em Produzido, laranja em Em Produção, roxo em Vendido) a não ser que escolhas uma cor de fundo aqui — nesse caso essa cor sobrepõe-se à automática. Mudar isto aqui não altera nada no Basecamp.</div>
    <div class="acts">
      ${agendado?'<button class="btn" id="cima">Mover para cima</button><button class="btn" id="baixo">Mover para baixo</button>':""}
    </div>
    <div class="acts">
      ${agendado?'<button class="btn" id="toFila">Devolver à fila</button>':""}
      ${c.url?`<a class="btn" id="bcOpen" target="_blank" rel="noopener" href="${c.url}">Abrir card no Basecamp</a>`:""}
      <button class="btn warn" id="apagar">Apagar encomenda</button>
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
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
  if(agendado){
    $("#guardarLinha").onclick=async()=>{
      const linhaIdx=+$("#fLinha").value;
      const iso=$("#fInicio").value;
      if(!iso){ alert("Escolhe uma data de início."); return; }
      $("#guardarLinha").textContent="A guardar…"; $("#guardarLinha").disabled=true;
      try{
        const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[linhaIdx],dia_inicio:iso,volume_m3:c.volume||null})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); $("#guardarLinha").textContent="Guardar linha/início"; $("#guardarLinha").disabled=false; return; }
        c.linha=linhaIdx; c.gs=idxOf(iso); c.dur=d.duracao_dias; c.volume=d.volume_m3;
        log("local",`"${c.titulo}" movido para ${LINHAS[linhaIdx]}, ${iso}`);
        await atualizarLogistica();
        render(); closeSheet();
      }catch(e){ alert("Falhou a guardar: "+e); $("#guardarLinha").textContent="Guardar linha/início"; $("#guardarLinha").disabled=false; }
    };
    $("#toFila").onclick=()=>{
      undoStack.push({id:c.id,linha:c.linha,gs:c.gs,dur:c.dur,volume:c.volume});
      c.linha=null; c.gs=null;
      renderFila(); renderLanes(); sync(c); closeSheet();
    };
    const moverOrdem=direcao=>async()=>{
      try{
        const r=await fetch("/planeamento-ecos-largos/reordenar",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,direcao})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); return; }
        log("local",`"${c.titulo}" movido para ${direcao}`);
        await carregar(); closeSheet();
      }catch(e){ alert("Falhou a reordenar: "+e); }
    };
    $("#cima").onclick=moverOrdem("cima");
    $("#baixo").onclick=moverOrdem("baixo");
  }
  $("#guardarVol").onclick=async()=>{
    const vol=+$("#fVol").value;
    if(!vol||vol<=0){ alert("Indica um volume maior que 0."); return; }
    $("#guardarVol").textContent="A guardar…"; $("#guardarVol").disabled=true;
    try{
      if(agendado){
        const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[c.linha],
            dia_inicio:MASTER[c.gs].iso,volume_m3:vol})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); $("#guardarVol").textContent="Guardar volume"; $("#guardarVol").disabled=false; return; }
        c.dur=d.duracao_dias; c.volume=d.volume_m3;
      }else{
        const r=await fetch("/planeamento-ecos-largos/volume",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,volume_m3:vol})});
        const d=await r.json();
        if(d.erro){ alert(d.erro); $("#guardarVol").textContent="Guardar volume"; $("#guardarVol").disabled=false; return; }
        c.volume=d.volume_m3;
      }
      log("local",`volume de "${c.titulo}" atualizado: ${c.volume} m³`);
      render(); closeSheet();
    }catch(e){ alert("Falhou a guardar: "+e); $("#guardarVol").textContent="Guardar volume"; $("#guardarVol").disabled=false; }
  };
  $("#guardarMad").onclick=async()=>{
    const tipo=$("#fMad").value;
    if(!tipo){ alert("Escolhe Seca ou Verde."); return; }
    $("#guardarMad").textContent="A guardar…"; $("#guardarMad").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/madeira",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id,tipo_madeira:tipo})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); $("#guardarMad").textContent="Guardar madeira"; $("#guardarMad").disabled=false; return; }
      c.madeira=tipo;
      log("local",`madeira de "${c.titulo}" atualizada: ${tipo}`);
      await atualizarLogistica();
      render(); closeSheet();
    }catch(e){ alert("Falhou a guardar: "+e); $("#guardarMad").textContent="Guardar madeira"; $("#guardarMad").disabled=false; }
  };
  $("#apagar").onclick=async()=>{
    if(!confirm(`Apagar definitivamente "${c.titulo}"?\n\nIsto manda o card para o lixo no Basecamp (fica lá recuperável durante algum tempo, tal como apagar manualmente).`)) return;
    $("#apagar").textContent="A apagar…"; $("#apagar").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/apagar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; return; }
      cards=cards.filter(x=>x.id!==c.id);
      cardsLog=cardsLog.filter(x=>x.id!==c.id);
      log("local",`apagado: ${c.titulo}`);
      render(); closeSheet();
    }catch(e){ alert("Falhou a apagar: "+e); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; }
  };
}
function closeSheet(){ $("#veil").classList.remove("on"); $("#sheet").classList.remove("on"); }

/* ---------- ficha de logística ---------- */
function openSheetLogistica(id){
  const c=cardLog(id);
  const d=MASTER[c.gs];
  $("#sheet").innerHTML=`
    <div class="of mono">card ${c.id} · logística</div>
    <h3>${c.titulo}</h3>
    <div class="kv"><span>Coluna no Basecamp</span><b>${c.coluna||"—"}</b></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Dia de carregamento</label>
    <div class="frow"><input id="logDia" type="date" value="${d.iso}"><button class="btn" id="guardarDia">Guardar</button></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Quem carrega</label>
    <div class="frow"><input id="logQuem" placeholder="Ex: João" value="${c.quemCarrega?String(c.quemCarrega).replace(/"/g,"&quot;"):""}"><button class="btn" id="guardarQuem">Guardar</button></div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor da barra lateral</label>
    <div class="cores" id="coresBarra">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.cor||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}"
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <label style="font-size:14px;color:var(--dim);display:block;margin-top:12px">Cor de fundo</label>
    <div class="cores" id="coresFundo">${Object.entries(CORES).map(([chave,v])=>
      `<button class="swatch${(c.corFundo||"cinza")===chave?" sel":""}" data-cor="${chave==="cinza"?"":chave}"
        style="background:${v.hex}" title="${v.label}" aria-label="${v.label}"></button>`).join("")}</div>
    <div class="owner">Isto é o duplicado de logística desta OF — mover, apagar ou mudar a cor da barra lateral aqui não altera a produção nem o Basecamp. A cor de fundo é exceção: é da OF, uma só, por isso muda também no card de produção.</div>
    <div class="acts">
      ${c.url?`<a class="btn" id="bcOpen" target="_blank" rel="noopener" href="${c.url}">Abrir card no Basecamp</a>`:""}
      <button class="btn warn" id="apagarLog">Apagar duplicado</button>
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
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
        const r=await fetch("/planeamento-ecos-largos/logistica/cor",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:c.id,cor})});
        const dd=await r.json();
        if(dd.erro){ alert(dd.erro); return; }
        c.cor=cor||null;
        $("#coresBarra").querySelectorAll(".swatch").forEach(x=>x.classList.remove("sel"));
        sw.classList.add("sel");
        log("local",`cor do carregamento de "${c.titulo}" atualizada`);
        renderLogistica();
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
$("#novo").onclick=()=>openForm();

/* ---------- controlos ---------- */
$("#seg").onclick=e=>{ const b=e.target.closest("button"); if(b) setMode(b.dataset.m); };
$("#prev").onclick=()=>step(-1);
$("#next").onclick=()=>step(1);
$("#hoje").onclick=()=>{ view.start=Math.max(HOJE,0); setMode(view.mode); };
$("#atualizar").onclick=()=>carregar();
$("#undo").onclick=()=>{
  if(!undoStack.length)return;
  const prev=undoStack.pop(); const c=card(prev.id);
  if(!c)return;
  c.linha=prev.linha; c.gs=prev.gs; c.dur=prev.dur;
  render(); sync(c); log("local","anulado");
};
document.addEventListener("keydown",e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==="z"){e.preventDefault();$("#undo").click();}
  if(e.key==="ArrowLeft"&&!e.target.closest("select"))step(-1);
  if(e.key==="ArrowRight"&&!e.target.closest("select"))step(1);
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
