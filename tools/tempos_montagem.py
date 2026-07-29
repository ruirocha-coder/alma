# tools/tempos_montagem.py — aritmética determinística do "Procedimento
# Tempos de Montagem para Logística" (projeto Alma Data, Basecamp), pedido
# explícito do Rui (2026-07-28). Toda a soma/multiplicação vive aqui, em
# Python puro — nunca pedida a um LLM (mesmo princípio já usado em toda a
# aplicação para datas e somas: aritmética não se confia à IA). O LLM só
# classifica os artigos/acréscimos a partir do texto da encomenda (ver
# agents/estimativa_montagem.py) — os números finais vêm sempre daqui.
#
# Alcance desta fase (ver plano acordado com o Rui, 2026-07-28): as
# validações do §3 do documento (plano excede o tempo útil da zona; valor do
# dia longe dos 15.000€) ficam de fora — a distribuição por dia continua a
# ser uma sugestão em texto livre do modelo, não um escalonamento rígido, por
# isso não há ainda um "dia" estruturado ao qual somar valores. As validações
# aqui aplicam-se por PARAGEM (rendimento fora da banda, peça fixa à parede,
# artigo não classificado, acesso desconhecido).

_CHAVE_MINUTOS_POR_GRUPO = {
    "ligeiro": "minutos_ligeiro",
    "normal": "minutos_normal",
    "pesado": "minutos_pesado",
}

def calcular_conta_a(itens: list, acrescimos: dict, pessoas: int, fatores_local: dict, parametros: dict) -> dict:
    """Conta A do procedimento: soma os artigos classificados (Ligeiro/Normal/
    Pesado), os acréscimos (peça fixa à parede, candeeiro de teto, móvel
    desmontado inesperado) e o fixo de qualquer paragem, ajusta para equipa
    de 3 pessoas, e aplica os fatores do local (sem elevador/obra/centro
    histórico) — por esta ordem exata do documento. Um candeeiro de teto
    conta as DUAS coisas: o seu minuto base como artigo "ligeiro" (é
    decoração/iluminação) E o acréscimo elétrico de +30 min — não é um ou
    outro (confirmado pelo próprio exemplo do documento: 2 candeeiros de teto
    somam 20 min de base + 60 min de acréscimo elétrico, não só um dos dois).

    `itens`: lista de {"grupo": "ligeiro"|"normal"|"pesado"|None,
    "quantidade": int, "descricao": str} — grupo None fica de fora da soma e
    entra em "itens_nao_classificados" (nunca inventado).
    `acrescimos`: {"pecas_fixas_parede": int, "candeeiros_teto": int,
    "moveis_desmontados_inesperados": int} (todos opcionais, 0 por omissão).
    `fatores_local`: {"sem_elevador": bool, "obra": bool, "centro_historico": bool}.

    Devolve {"minutos": float, "decomposicao": [linhas de texto],
    "itens_nao_classificados": [descrições]}."""
    acrescimos = acrescimos or {}
    fatores_local = fatores_local or {}
    decomposicao = []
    itens_nao_classificados = []

    minutos_artigos = 0.0
    for item in itens or []:
        grupo = item.get("grupo")
        quantidade = item.get("quantidade") or 1
        descricao = item.get("descricao") or "(sem descrição)"
        chave = _CHAVE_MINUTOS_POR_GRUPO.get(grupo)
        if not chave:
            itens_nao_classificados.append(descricao)
            continue
        minutos_item = quantidade * parametros[chave]
        minutos_artigos += minutos_item
        decomposicao.append(f"{descricao} ({grupo}, x{quantidade}): {minutos_item:.0f} min")

    minutos_acrescimos = 0.0
    if acrescimos.get("pecas_fixas_parede"):
        qtd = acrescimos["pecas_fixas_parede"]
        m = qtd * parametros["acrescimo_fixa_parede_min"]
        minutos_acrescimos += m
        decomposicao.append(f"Peça(s) à medida fixa(s) à parede (x{qtd}): {m:.0f} min")
    if acrescimos.get("candeeiros_teto"):
        qtd = acrescimos["candeeiros_teto"]
        m = qtd * parametros["acrescimo_candeeiro_teto_min"]
        minutos_acrescimos += m
        decomposicao.append(f"Acréscimo elétrico candeeiro(s) de teto (x{qtd}): {m:.0f} min")
    if acrescimos.get("moveis_desmontados_inesperados"):
        qtd = acrescimos["moveis_desmontados_inesperados"]
        m = qtd * parametros["acrescimo_desmontado_inesperado_min"]
        minutos_acrescimos += m
        decomposicao.append(f"Móvel(is) entregue(s) desmontado(s) inesperadamente (x{qtd}): {m:.0f} min")

    fixo = parametros["fixo_paragem_min"]
    decomposicao.append(f"Fixo da paragem: {fixo:.0f} min")

    total = minutos_artigos + minutos_acrescimos + fixo

    if pessoas == 3:
        fator_equipa = parametros["fator_equipa_3_pessoas"]
        total *= fator_equipa
        decomposicao.append(f"Ajuste equipa de 3 pessoas: ×{fator_equipa}")

    fator_local = 1.0
    if fatores_local.get("sem_elevador"):
        fator_local *= parametros["fator_sem_elevador"]
        decomposicao.append(f"Sem elevador: ×{parametros['fator_sem_elevador']}")
    if fatores_local.get("obra"):
        fator_local *= parametros["fator_obra"]
        decomposicao.append(f"Obra a decorrer no local: ×{parametros['fator_obra']}")
    if fatores_local.get("centro_historico"):
        fator_local *= parametros["fator_centro_historico"]
        decomposicao.append(f"Centro histórico / sem lugar para carga: ×{parametros['fator_centro_historico']}")
    total *= fator_local

    return {"minutos": total, "decomposicao": decomposicao, "itens_nao_classificados": itens_nao_classificados}

def calcular_rendimento(valor: float, minutos: float, parametros: dict) -> dict:
    """Conta B: verifica a Conta A pelo valor da encomenda — rendimento
    (€/hora) e a banda em que cai (abaixo/normal/acima), face aos limiares do
    documento. Nunca substitui a Conta A, só a verifica (ver §4 do
    documento)."""
    if not valor or not minutos:
        return {"euros_hora": None, "banda": None}
    horas = minutos / 60
    euros_hora = valor / horas
    if euros_hora < parametros["banda_baixa_eur_hora"]:
        banda = "abaixo"
    elif euros_hora > parametros["banda_alta_eur_hora"]:
        banda = "acima"
    else:
        banda = "normal"
    return {"euros_hora": euros_hora, "banda": banda}

# valores exatos do documento (ida e volta, Classe 2, a partir de Arada) —
# só para as duas regiões que o sistema já distingue estruturalmente
# (Lisboa/Porto); para "Outro" as portagens não são estimadas (decisão de
# alcance combinada com o Rui, 2026-07-28: nunca inventar um valor de
# portagens para uma zona não confirmada no documento).
TABELA_PORTAGENS_REGIAO = {
    "Lisboa": {"portagens_eur": 76.0},
    "Porto": {"portagens_eur": 12.0},
}

def custo_deslocacao(regiao: str, km: float, duracao_min: float, parametros: dict, pessoas: int = 2) -> dict:
    """Custo de deslocação (ida e volta) para uma região, a partir de km/
    duração REAIS (ver tools.logistica.metricas_trajeto — dados da Google
    Directions API, não a tabela fixa de zonas do documento). Combustível +
    manutenção = 0,35 €/km; tempo de equipa = horas de viagem × custo/hora ×
    pessoas (2 por omissão, o próprio documento não faz variar isto com o
    tamanho da equipa de montagem). Portagens só para regiões conhecidas
    (ver TABELA_PORTAGENS_REGIAO) — devolve total_estimado=None quando as
    portagens não são conhecidas, para nunca dar a entender um total
    completo que na verdade não inclui portagens.

    Devolve None se não houver km/duração disponíveis (sem chave da Google
    configurada, ou falha ao calcular o trajeto)."""
    if km is None or duracao_min is None:
        return None
    combustivel = km * parametros["custo_km_combustivel_eur"]
    manutencao = km * parametros["custo_km_manutencao_eur"]
    horas = duracao_min / 60
    tempo_equipa = horas * parametros["custo_hora_pessoa_eur"] * pessoas
    subtotal_sem_portagens = combustivel + manutencao + tempo_equipa

    info_portagens = TABELA_PORTAGENS_REGIAO.get(regiao)
    if info_portagens:
        portagens = info_portagens["portagens_eur"]
        total_estimado = subtotal_sem_portagens + portagens
        portagens_nota = None
    else:
        portagens = None
        total_estimado = None
        portagens_nota = ("portagens não estimadas para esta região — confirmar no calculador oficial "
                          "da Infraestruturas de Portugal; o total abaixo não inclui portagens")

    return {
        "km": km, "combustivel": combustivel, "manutencao": manutencao, "tempo_equipa": tempo_equipa,
        "portagens": portagens, "portagens_nota": portagens_nota,
        "subtotal_sem_portagens": subtotal_sem_portagens, "total_estimado": total_estimado,
    }

def custo_viagem_perna(km: float, duracao_min: float, parametros: dict, pessoas: int = 2) -> dict:
    """Custo de UMA perna individual de viagem (armazém→paragem, ou
    paragem→paragem) — pedido do Rui (2026-07-29), para a tabela
    preparatória de agendamento (ver
    agents.sugestao_logistica_semanal._construir_tabela_agendamento)
    poder mostrar o custo linha a linha, na ordem real da rota. Mesma
    taxa de combustível+manutenção+tempo de equipa que custo_deslocacao,
    só que aplicada a uma perna em vez do trajeto todo — nunca inclui
    portagens aqui (essas só contam uma vez, para o trajeto de ida e
    volta completo, ver custo_deslocacao).

    Devolve {"combustivel": float|None, "manutencao": float|None,
    "tempo_equipa": float, "subtotal": float|None} — "combustivel"/
    "manutencao"/"subtotal" ficam None se não houver km desta perna (só
    duração), para nunca inventar um custo de combustível sem distância
    real. Devolve None se não houver sequer duração."""
    if duracao_min is None:
        return None
    horas = duracao_min / 60
    tempo_equipa = horas * parametros["custo_hora_pessoa_eur"] * pessoas
    if km is None:
        return {"combustivel": None, "manutencao": None, "tempo_equipa": tempo_equipa, "subtotal": None}
    combustivel = km * parametros["custo_km_combustivel_eur"]
    manutencao = km * parametros["custo_km_manutencao_eur"]
    return {"combustivel": combustivel, "manutencao": manutencao, "tempo_equipa": tempo_equipa,
            "subtotal": combustivel + manutencao + tempo_equipa}

def custo_montagem_paragem(minutos: float, parametros: dict, pessoas: int = 2) -> float:
    """Custo de mão de obra de montagem de uma paragem — horas × custo/hora
    × pessoas, a mesma taxa de custo_hora_pessoa_eur já usada para o
    tempo de equipa em viagem (custo_deslocacao/custo_viagem_perna),
    aplicada agora ao tempo de montagem — pedido do Rui (2026-07-29),
    para a coluna "custo" da tabela preparatória de agendamento. Devolve
    None se não houver minutos (nunca inventa um custo sem tempo real)."""
    if minutos is None:
        return None
    return (minutos / 60) * parametros["custo_hora_pessoa_eur"] * pessoas

def validacoes_necessarias(rendimento_banda: str, tem_peca_fixa_parede: bool,
                           itens_nao_classificados: list, acesso_desconhecido: bool) -> list:
    """Situações em que o documento pede validação humana (§7), aplicadas ao
    nível da paragem (o nível de dia/zona fica fora do alcance desta fase —
    ver nota no topo do ficheiro)."""
    motivos = []
    if rendimento_banda in ("abaixo", "acima"):
        motivos.append(f"o rendimento desta paragem sai da banda normal (está \"{rendimento_banda}\" de 800-4.000 €/h)")
    if tem_peca_fixa_parede:
        motivos.append("há peça(s) à medida fixa(s) à parede")
    if itens_nao_classificados:
        motivos.append(f"não consegui classificar: {', '.join(itens_nao_classificados)}")
    if acesso_desconhecido:
        motivos.append("o acesso ao local (elevador/obra/centro histórico) não está confirmado nas notas")
    return motivos
