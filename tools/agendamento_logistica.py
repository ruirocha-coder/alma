# tools/agendamento_logistica.py — proposta de agendamento das entregas
# (dia + hora por paragem), pedido explícito do Rui (2026-07-28): a
# partir do tempo de montagem (Conta A) e do trajeto real (Google
# Directions API, já usado para o link do Google Maps e o custo de
# deslocação — ver tools.logistica.plano_trajeto), calcula quando cada
# entrega deve acontecer. Toda a aritmética de horários fica aqui,
# determinística — nunca pedida a um LLM (mesmo princípio de sempre:
# datas/horas não se confiam à IA).
#
# Segue a estrutura do turno do "Procedimento Tempos de Montagem para
# Logística": preparação e carga 8:00-8:40, saída às 8:40, turno normal
# até às 17:30.
#
# Alcance desta fase (decisão combinada com o Rui, 2026-07-28): se uma
# região não couber num só dia dentro do horário normal, a Alma não tenta
# dividir sozinha as paragens por vários dias — isso exigiria recalcular
# o trajeto para cada sub-grupo (a otimização de ordem/distância que já
# temos assume todas as paragens no mesmo dia), e é exatamente o tipo de
# decisão que o próprio documento diz ser da logística, não da Alma:
# "Regra: a Alma propõe; quem fecha o plano do dia é a logística" (§7).
# Sinaliza isso claramente ("cabe_no_turno_normal": False), para decisão
# manual, em vez de adivinhar uma divisão.
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

INICIO_TURNO_MIN = 8 * 60 + 40  # 8:40 — logo depois da preparação/carga 8:00-8:40 do documento
LIMITE_TURNO_NORMAL_MIN = 17 * 60 + 30  # 17:30 — turno normal (as horas extra são margem para o imprevisto, não para planear)

# pausa de almoço (pedido do Rui, 2026-07-29): o documento não fixa uma
# hora exata, por isso insere-se uma pausa fixa de 60 min, uma única vez
# por dia, assim que o relógio simulado atinge o meio-dia (12:00) — no
# primeiro ponto de paragem natural (fim de uma viagem ou de uma
# montagem), nunca a meio de uma viagem ou de uma montagem em curso.
DURACAO_ALMOCO_MIN = 60
JANELA_ALMOCO_INICIO_MIN = 12 * 60  # 12:00

FUSO_HORARIO = ZoneInfo("Europe/Lisbon")

def _minutos_para_hora(minutos_desde_meia_noite: float) -> str:
    minutos_inteiros = round(minutos_desde_meia_noite)
    return f"{minutos_inteiros // 60:02d}:{minutos_inteiros % 60:02d}"

def calcular_horario_dia(paragens: list, pernas_minutos: list, pernas_km: list = None) -> dict:
    """Simula um único dia de entregas: dado `paragens` (já na ordem
    otimizada do trajeto, cada uma com pelo menos "minutos_montagem") e
    `pernas_minutos` (N+1 durações — armazém→p1, p1→p2, ..., pN→armazém,
    na MESMA ordem — ver tools.logistica.plano_trajeto), calcula a hora
    de chegada/saída de cada paragem e a hora de regresso ao armazém, a
    partir das 8:40, com uma pausa de almoço de 60 min inserida uma única
    vez assim que o dia atinge o meio-dia (ver DURACAO_ALMOCO_MIN acima).
    `pernas_km` (opcional, mesma forma que `pernas_minutos` — ver
    tools.logistica.plano_trajeto) anexa a distância de cada perna aos
    "eventos" devolvidos, para o custo de viagem por perna (ver
    tools.tempos_montagem.custo_viagem_perna) — fica None em cada evento
    de viagem se não for fornecido.

    Devolve {"paragens": [{**paragem original, "chegada": "HH:MM",
    "saida": "HH:MM"}, ...], "eventos": [...], "regresso": "HH:MM",
    "cabe_no_turno_normal": bool}.

    "eventos" é a mesma informação, mas em ORDEM CRONOLÓGICA/DE ROTA real
    (viagem, cliente, almoço intercalados) — pedido explícito do Rui
    (2026-07-29), para a tabela preparatória de agendamento (ver
    agents.sugestao_logistica_semanal._construir_tabela_agendamento)
    poder listar os eventos pela ordem em que acontecem mesmo, sem
    reconstruir esta lógica. Cada evento tem "tipo" ("viagem"|"cliente"|
    "almoco") e "minutos"; um evento "viagem" tem também "de"/"para"/
    "km"; um evento "cliente" tem também os campos originais da paragem
    (id/titulo/cliente/morada/produtos_encomendados) mais "chegada"/
    "saida".

    Nunca decide sozinha como dividir por mais de um dia se não couber —
    só sinaliza (ver nota no topo do ficheiro)."""
    n = len(paragens)
    if len(pernas_minutos) != n + 1:
        raise ValueError(
            f"pernas_minutos tem de ter uma perna a mais que paragens "
            f"({len(pernas_minutos)} pernas para {n} paragens — esperava {n + 1})")
    if pernas_km is not None and len(pernas_km) != n + 1:
        raise ValueError(
            f"pernas_km tem de ter uma perna a mais que paragens "
            f"({len(pernas_km)} pernas para {n} paragens — esperava {n + 1})")

    tempo = INICIO_TURNO_MIN
    resultado_paragens = []
    eventos = []
    almoco_feito = False
    local_anterior = "Armazém"

    def _talvez_inserir_almoco():
        nonlocal tempo, almoco_feito
        if not almoco_feito and tempo >= JANELA_ALMOCO_INICIO_MIN:
            eventos.append({"tipo": "almoco", "minutos": DURACAO_ALMOCO_MIN})
            tempo += DURACAO_ALMOCO_MIN
            almoco_feito = True

    for i, paragem in enumerate(paragens):
        tempo += pernas_minutos[i]
        eventos.append({"tipo": "viagem", "de": local_anterior, "para": paragem.get("titulo"),
                        "minutos": pernas_minutos[i],
                        "km": pernas_km[i] if pernas_km is not None else None})
        _talvez_inserir_almoco()
        chegada = tempo
        tempo += paragem["minutos_montagem"]
        saida = tempo
        paragem_com_horario = {**paragem, "chegada": _minutos_para_hora(chegada),
                               "saida": _minutos_para_hora(saida)}
        resultado_paragens.append(paragem_com_horario)
        eventos.append({"tipo": "cliente", **paragem_com_horario, "minutos": paragem["minutos_montagem"]})
        _talvez_inserir_almoco()
        local_anterior = paragem.get("titulo")

    tempo += pernas_minutos[-1]
    eventos.append({"tipo": "viagem", "de": local_anterior, "para": "Armazém",
                    "minutos": pernas_minutos[-1],
                    "km": pernas_km[-1] if pernas_km is not None else None})
    regresso = tempo

    return {
        "paragens": resultado_paragens,
        "eventos": eventos,
        "regresso": _minutos_para_hora(regresso),
        "cabe_no_turno_normal": regresso <= LIMITE_TURNO_NORMAL_MIN,
    }

def proximo_dia_util(a_partir_de: date, deslocamento: int) -> date:
    """O dia útil (segunda a sexta) `deslocamento` dias úteis depois de
    `a_partir_de` (deslocamento=0 devolve o próprio `a_partir_de` se já
    for dia útil, senão o primeiro dia útil a seguir) — usado para
    atribuir um dia da semana a cada região com entregas prontas, um dia
    útil consecutivo por região."""
    dia = a_partir_de
    contados = 0
    while True:
        if dia.weekday() < 5:  # 0=segunda ... 4=sexta
            if contados == deslocamento:
                return dia
            contados += 1
        dia += timedelta(days=1)

def horario_para_iso(dia: date, hora_str: str) -> str:
    """Combina uma data e uma hora "HH:MM" num datetime ISO8601 com o
    fuso horário de Portugal (a transição horário de verão/inverno é
    tratada automaticamente pelo zoneinfo) — para usar em
    tools.basecamp.criar_evento_calendario. Nunca construído à mão sem
    fuso horário — evita assumir sempre a mesma diferença para UTC ao
    longo do ano."""
    hora, minuto = map(int, hora_str.split(":"))
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=FUSO_HORARIO).isoformat()
