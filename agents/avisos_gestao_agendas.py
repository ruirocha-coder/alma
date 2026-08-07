# agents/avisos_gestao_agendas.py — avisos periódicos à Conceição Costa,
# baseados no documento "GESTÃO DAS AGENDAS" (projeto Alma Data, lido em
# 2026-07-29), pedido explícito do Rui (2026-07-29): "com base no
# documento de Gestão das Agendas e no acompanhamento dos cards em
# entregas, emite avisos à Conceição quer para verificar o cumprimento
# das datas e avisar clientes de potenciais atrasos".
#
# O documento define, por região (Lisboa/Porto), marcos fixos da semana:
# - "confirmação da ida pela Sede" + "informação da previsão ao cliente":
#   ambos até 5ª feira da semana ANTERIOR (N-1) à semana de entrega (N).
# - "confirmação final": Lisboa à 3ª feira da própria semana de entrega
#   (N); Porto ao sábado da semana anterior (N-1).
# Estes marcos são sempre calculados aqui, em Python, nunca pedidos a um
# LLM — o mesmo princípio de sempre nesta aplicação para datas/dias da
# semana (ver tools.agendamento_logistica para o mesmo padrão).
#
# Reaproveita a mesma deteção de "pronto a entregar" e a mesma regra de
# prazo de armazém por região do documento (ver
# agents.sugestao_logistica_semanal._cards_prontos_a_entregar e
# _prazo_armazem_semana) — nunca duas versões desta lógica a divergir.
# Não recalcula trajeto real nem estimativa de tempo de montagem (isso é
# só para a proposta de agendamento em si, ver
# agents.sugestao_logistica_semanal._calcular_agendamento_por_regiao) —
# este aviso é só sobre CUMPRIMENTO DE DATAS, não sobre rota/custo.
#
# Vigo (ES), tal como no resto da aplicação (pedido do Rui, 2026-07-29),
# fica de fora por agora — ainda sem forma de identificar os cards de
# Vigo no quadro Kanban do Basecamp.
import threading
from datetime import date, timedelta
from tools import basecamp, logistica

_a_correr = threading.Lock()

RESPONSAVEL_MENCAO = "Conceição Costa"

# marcos do documento "GESTÃO DAS AGENDAS", por região: cada marco é
# (offset_semanas, dia_semana) — offset_semanas é relativo à semana de
# ENTREGA N (0 = a própria semana N, -1 = a semana anterior N-1);
# dia_semana é 0=segunda ... 6=domingo (ver date.weekday()).
MARCOS_POR_REGIAO = {
    "Lisboa": {
        "confirmacao_sede_e_previsao_cliente": (-1, 3),  # 5ª feira da semana N-1
        "confirmacao_final": (0, 1),                     # 3ª feira da semana N
    },
    "Porto": {
        "confirmacao_sede_e_previsao_cliente": (-1, 3),  # 5ª feira da semana N-1
        "confirmacao_final": (-1, 5),                    # sábado da semana N-1
    },
}

_DESCRICAO_MARCO = {
    "confirmacao_sede_e_previsao_cliente":
        "confirmar com a Sede a ida a {regiao} e informar os clientes da previsão de entrega",
    "confirmacao_final": "fazer a confirmação final da entrega em {regiao}",
}

def _marcos_de_hoje(hoje: date) -> list:
    """Devolve [(regiao, marco, inicio_semana_entrega), ...] — todos os
    marcos do documento "GESTÃO DAS AGENDAS" que caem exatamente hoje,
    com o início (segunda-feira) da semana de entrega (N) a que cada um
    se refere. `inicio_semana_entrega` pode ser esta semana ou a
    seguinte, consoante o marco (ver MARCOS_POR_REGIAO) — nunca a
    semana anterior, portanto."""
    segunda_desta_semana = hoje - timedelta(days=hoje.weekday())
    resultado = []
    for regiao, marcos in MARCOS_POR_REGIAO.items():
        for marco, (offset_semanas, dia_semana) in marcos.items():
            if hoje.weekday() == dia_semana:
                inicio_semana_entrega = segunda_desta_semana - timedelta(weeks=offset_semanas)
                resultado.append((regiao, marco, inicio_semana_entrega))
    return resultado

def _separar_por_prazo(entregas: list, prazo) -> tuple:
    """Separa `entregas` (dicts com "data_entrada_armazem", ver
    agents.sugestao_logistica_semanal._cards_prontos_a_entregar) em três
    grupos: dentro do prazo, confirmado fora do prazo, e sem data
    confirmada. Um card em "On Hold" atrás de uma região já confirma
    ESTRUTURALMENTE que chegou ao armazém (ver
    tools.logistica.fase_encomenda) — por isso a ausência de uma data em
    texto nas notas NUNCA conta como "fora do prazo" (bug real corrigido
    em 2026-07-29 na lógica de agendamento, mesma correção aqui), só como
    "por confirmar". Se `prazo` for None (região sem prazo definido no
    documento), tudo conta como dentro do prazo."""
    if prazo is None:
        return entregas, [], []
    dentro_do_prazo = [e for e in entregas
                       if e.get("data_entrada_armazem") and e["data_entrada_armazem"] <= prazo]
    fora_do_prazo = [e for e in entregas
                     if e.get("data_entrada_armazem") and e["data_entrada_armazem"] > prazo]
    sem_data = [e for e in entregas if not e.get("data_entrada_armazem")]
    return dentro_do_prazo, fora_do_prazo, sem_data

def _texto_aviso(regiao: str, marco: str, inicio_semana_entrega: date,
                 dentro_do_prazo: list, fora_do_prazo: list, sem_data: list) -> str:
    fim_semana_entrega = inicio_semana_entrega + timedelta(days=4)
    tarefa = _DESCRICAO_MARCO[marco].format(regiao=regiao)
    linhas = [
        f"Hoje é o dia, segundo o documento \"GESTÃO DAS AGENDAS\", para {tarefa}, "
        f"da semana de entrega de {inicio_semana_entrega.strftime('%d/%m')} a {fim_semana_entrega.strftime('%d/%m')}.",
        "",
    ]
    if dentro_do_prazo:
        linhas.append(f"Entregas de {regiao} confirmadas prontas em armazém dentro do prazo:")
        linhas.extend(f"- {e['titulo']} — {e.get('cliente') or '(cliente não identificado)'}"
                      for e in dentro_do_prazo)
    if fora_do_prazo:
        linhas.append("")
        linhas.append("⚠️ Fora do prazo de armazém desta semana — considera avisar o cliente de um "
                      "possível atraso antes que ele próprio o note:")
        linhas.extend(f"- {e['titulo']} — {e.get('cliente') or '(cliente não identificado)'}"
                      for e in fora_do_prazo)
    if sem_data:
        linhas.append("")
        linhas.append("ℹ️ Já em \"On Hold\" (confirma que chegaram ao armazém), mas sem data de entrada "
                      "confirmada nas notas — confirma a data exata se quiseres ter a certeza do prazo:")
        linhas.extend(f"- {e['titulo']} — {e.get('cliente') or '(cliente não identificado)'}"
                      for e in sem_data)
    if not dentro_do_prazo and not fora_do_prazo and not sem_data:
        linhas.append(f"Nenhuma entrega encontrada para {regiao} nesta semana de entrega.")
    linhas.append("")
    linhas.append(f"@{RESPONSAVEL_MENCAO}")
    return "\n".join(linhas)

def correr_avisos_gestao_agendas() -> dict:
    """Corrida diária: verifica se hoje é um dos marcos do documento
    "GESTÃO DAS AGENDAS" para alguma região (Lisboa/Porto — ver
    MARCOS_POR_REGIAO), e publica, para cada marco que se aplique hoje,
    um aviso no Mural "Programação" do projeto Entregas, dirigido à
    Conceição Costa — listando as entregas previstas para essa semana de
    entrega e sinalizando claramente quais ainda não têm a data de
    entrada em armazém confirmada dentro do prazo dessa região (para
    avisar o cliente de um possível atraso com antecedência, nunca
    depois do prazo passado). Não publica nada, e não lê cards nenhuns,
    se hoje não for nenhum desses dias (evita leitura/custo desnecessário
    na maioria dos dias)."""
    if not _a_correr.acquire(blocking=False):
        print("[avisos_gestao_agendas] já há uma corrida em curso — ignorado")
        return {"erro": "já está a correr uma verificação de avisos"}
    try:
        hoje = date.today()
        marcos_hoje = _marcos_de_hoje(hoje)
        if not marcos_hoje:
            return {"avisos_publicados": 0, "marcos": []}

        # import adiado (não no topo do módulo): agents.sugestao_logistica_semanal
        # importa `client` de agents/base.py, tal como agents/base.py importa
        # daqui indiretamente via FUNCOES — um import direto no topo criava
        # um ciclo (ver o mesmo padrão em agents/base.py, _disparar_*).
        from agents import sugestao_logistica_semanal as sls
        try:
            (_cards_por_regiao, _moradas_por_regiao, _nao_confirmados, _itens_prontos,
             entregas_por_regiao, _textos_pdf_por_id) = sls._cards_prontos_a_entregar()
        except Exception as e:
            print(f"[avisos_gestao_agendas] não foi possível obter os cards do Basecamp: {e!r}")
            return {"erro": str(e)}

        publicados = 0
        resumo_marcos = []
        for regiao, marco, inicio_semana_entrega in marcos_hoje:
            prazo = sls._prazo_armazem_semana(regiao, inicio_semana_entrega)
            entregas = entregas_por_regiao.get(regiao) or []
            dentro_do_prazo, fora_do_prazo, sem_data = _separar_por_prazo(entregas, prazo)

            texto = _texto_aviso(regiao, marco, inicio_semana_entrega,
                                 dentro_do_prazo, fora_do_prazo, sem_data)
            try:
                basecamp.publicar_mural(f"Aviso de agenda — {regiao}", texto, projeto=logistica.PROJETO_ENTREGAS,
                                        notificar_apenas=logistica.NOTIFICAR_APENAS_ENTREGAS)
                publicados += 1
            except Exception as e:
                print(f"[avisos_gestao_agendas] falhou a publicar o aviso de {regiao}/{marco}: {e!r}")
            resumo_marcos.append({"regiao": regiao, "marco": marco,
                                  "dentro_do_prazo": len(dentro_do_prazo),
                                  "fora_do_prazo": len(fora_do_prazo), "sem_data": len(sem_data)})

        print(f"[avisos_gestao_agendas] {publicados} aviso(s) publicado(s): {resumo_marcos}")
        return {"avisos_publicados": publicados, "marcos": resumo_marcos}
    finally:
        _a_correr.release()
