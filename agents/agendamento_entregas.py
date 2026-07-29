# agents/agendamento_entregas.py — cria eventos reais no calendário
# (Agenda/Schedule) do projeto "Entregas" no Basecamp, pedido explícito
# do Rui (2026-07-28): a proposta de agendamento da sugestão semanal
# (ver agents/sugestao_logistica_semanal.py) só é uma proposta — a Alma
# nunca cria nada no calendário sozinha a partir dela. Só depois de a
# Conceição ou a Isa confirmarem o agendamento (com ou sem ajustes numa
# conversa) é que esta função é chamada, com os dados finais tal como
# combinados — nunca inventados aqui.
from datetime import date
from tools import basecamp, logistica, agendamento_logistica

# quem pode confirmar o agendamento e mandar criar eventos reais no
# calendário — uma ação visível a toda a equipa de entregas e difícil de
# desfazer discretamente, por isso restrita, à semelhança de
# atualizar_empresa_pessoa/atualizar_parametro_estimativa (verificado no
# servidor, nunca só pela missão do modelo).
_AUTORIZADOS_CRIAR_EVENTOS = ("conceição", "isa", "rui", "beatriz")

def _validar_evento(evento: dict) -> tuple:
    """Valida e converte os campos de um evento (data/hora sempre
    verificadas em Python, nunca confiadas ao texto que o modelo passou)
    — devolve (dia, inicio_iso, fim_iso). Rebenta com ValueError/KeyError
    claro se algum campo estiver em falta ou for inválido, em vez de
    criar um evento com data/hora erradas."""
    dia = date.fromisoformat(evento["data"])
    inicio_iso = agendamento_logistica.horario_para_iso(dia, evento["hora_inicio"])
    fim_iso = agendamento_logistica.horario_para_iso(dia, evento["hora_fim"])
    return dia, inicio_iso, fim_iso

def criar_eventos_calendario_entregas_restrito(utilizador: str, eventos: list) -> dict:
    """Cria um evento na Agenda do projeto Entregas por cada item de
    `eventos` — cada um {"titulo": str, "data": "AAAA-MM-DD",
    "hora_inicio": "HH:MM", "hora_fim": "HH:MM", "descricao": str
    (opcional)} — os valores finais já confirmados numa conversa com a
    Conceição ou a Isa, NUNCA inventados ou adivinhados aqui (ver
    agents/ceo.py para a instrução de nunca chamar isto sem confirmação
    explícita).

    `eventos` deve incluir sempre um item por entrega E um item por cada
    viagem entre paragens (pedido explícito do Rui, 2026-07-29) — esta
    função em si é genérica, não distingue os dois tipos, trata qualquer
    item da mesma forma (é a missão do CEO que garante que os dois tipos
    são sempre incluídos, ver agents/ceo.py).

    Restrito a quem pode confirmar este agendamento (ver
    _AUTORIZADOS_CRIAR_EVENTOS) — verificado aqui, não só pela missão do
    modelo, à semelhança de atualizar_empresa_pessoa/
    atualizar_parametro_estimativa.

    Devolve {"criados": [{"titulo", "app_url"}, ...], "falhas": [{"titulo",
    "erro"}, ...]} — uma falha num evento não impede os restantes de
    serem criados."""
    if not any(nome in utilizador.lower() for nome in _AUTORIZADOS_CRIAR_EVENTOS):
        return {"erro": f"{utilizador} não tem autorização para criar eventos no calendário do projeto Entregas"}

    criados, falhas = [], []
    for evento in eventos:
        titulo = evento.get("titulo") or "(sem título)"
        try:
            _dia, inicio_iso, fim_iso = _validar_evento(evento)
            resultado = basecamp.criar_evento_calendario(
                titulo, inicio_iso, fim_iso, evento.get("descricao", ""), logistica.PROJETO_ENTREGAS)
            criados.append({"titulo": titulo, "app_url": resultado.get("app_url")})
        except Exception as e:
            falhas.append({"titulo": titulo, "erro": str(e)})
    return {"criados": criados, "falhas": falhas}
