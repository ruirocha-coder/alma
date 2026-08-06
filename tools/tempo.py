# tools/tempo.py — relógio e calendário deterministas para a Alma: nunca
# adivinhar a data de hoje nem o dia da semana de uma data, calcular sempre.
#
# Bug real (Rui, 2026-08-06): num relatório semanal do dashboard de
# produção, a Alma escreveu "Dom 3 ago" para uma data que era uma
# segunda-feira. Causa raiz: a Alma nunca recebe a data/hora atual em
# lado nenhum do sistema (nenhuma chamada a datetime.now()/date.today()
# no prompt), e não tinha nenhuma forma de calcular o dia da semana de
# uma data à parte de "hoje"/"ontem" (já resolvidos em código em
# tools/ecos_largos.py). Isto dá-lhe um relógio real e uma forma de
# calcular o dia da semana de qualquer data, sempre em código.
from datetime import datetime, date
from zoneinfo import ZoneInfo

FUSO_HORARIO = ZoneInfo("Europe/Lisbon")

_DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"]
_DIAS_SEMANA_ABREV = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _formatar_extenso(d: date) -> str:
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


def contexto_data_atual() -> str:
    """Linha pronta a incluir no contexto de qualquer conversa, com a
    data e o dia da semana de hoje reais — para a Alma nunca ter de
    adivinhar "hoje". Usada em agents/base.py, fora do bloco de system
    prompt cacheado (para nunca ficar presa à data de um dia anterior)."""
    d = datetime.now(FUSO_HORARIO).date()
    return f"Hoje é {_DIAS_SEMANA[d.weekday()]}, {_formatar_extenso(d)}."


def agora() -> dict:
    """O relógio da Alma: data e hora reais, agora, em Portugal (Europe/
    Lisbon) — nunca adivinhes a data ou o dia da semana de hoje, usa
    sempre isto se tiveres alguma dúvida (o contexto da conversa já traz
    normalmente a data de hoje, mas esta função também dá a hora)."""
    agora_dt = datetime.now(FUSO_HORARIO)
    d = agora_dt.date()
    return {
        "data_iso": d.isoformat(),
        "hora": agora_dt.strftime("%H:%M"),
        "dia_semana": _DIAS_SEMANA[d.weekday()],
        "data_extenso": f"{_DIAS_SEMANA[d.weekday()].capitalize()}, {_formatar_extenso(d)}",
    }


def dia_da_semana(data: str) -> dict:
    """O dia da semana de QUALQUER data (passada ou futura), calculado
    aqui — nunca adivinhado. Passa `data` no formato YYYY-MM-DD. Usa
    sempre isto quando precisares de rotular uma data com o dia da
    semana (ex: "Dom 3 ago", "3ª feira") em qualquer relatório ou
    resposta — bug real, 2026-08-06: um relatório semanal rotulou uma
    segunda-feira como "Dom" (domingo) por ter sido escrito de memória
    em vez de calculado."""
    try:
        d = date.fromisoformat(data.strip())
    except ValueError:
        return {"erro": f"data inválida {data!r} — usa o formato YYYY-MM-DD"}
    return {
        "data_iso": d.isoformat(),
        "dia_semana": _DIAS_SEMANA[d.weekday()],
        "abreviado": _DIAS_SEMANA_ABREV[d.weekday()],
        "data_extenso": _formatar_extenso(d),
    }


TOOLS_TEMPO = [
    {
        "name": "agora",
        "description": ("O relógio da Alma: devolve a data e hora reais, agora, em Portugal — usa isto se "
                        "tiveres alguma dúvida sobre a data/hora atual (nunca adivinhes; o contexto da "
                        "conversa já costuma trazer a data de hoje, mas esta função também dá a hora)."),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "dia_da_semana",
        "description": ("Calcula o dia da semana de qualquer data (passada ou futura) — usa isto SEMPRE que "
                        "precisares de rotular uma data com o dia da semana (ex: num relatório \"Dom 3 ago\", "
                        "\"3ª feira\", \"segunda-feira\") em vez de escreveres isso de memória: já aconteceu "
                        "escrever o dia da semana errado (uma segunda-feira rotulada como domingo)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "YYYY-MM-DD"}
            },
            "required": ["data"]
        }
    }
]
