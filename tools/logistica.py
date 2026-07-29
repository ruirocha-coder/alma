# tools/logistica.py — regras de negócio da equipa de Logística (projeto
# "Entregas" no Basecamp), pedidas pela Isa Moreira/Conceição Costa. Só as
# regras determinísticas ficam aqui (fases do fluxo, aritmética de datas,
# textos fixos) — sem depender do cliente da Anthropic, para poderem ser
# testadas isoladamente. A orquestração (ler cards, extrair dados via IA,
# publicar comentários) vive em agents/logistica_entregas.py, tal como
# agents/monitor_basecamp.py já faz para os atrasos gerais.
import os, unicodedata
from datetime import date, timedelta
from urllib.parse import quote
import httpx

PROJETO_ENTREGAS = "Entregas"

# pedido explícito do Rui (2026-07-29): os posts no Mural do projeto
# Entregas (sugestão semanal, avisos de agenda, calibração da estimativa)
# notificavam, por omissão do Basecamp, toda a gente com acesso ao
# projeto (12 pessoas) — devem notificar só a Conceição e a Isa (ver
# tools.basecamp.publicar_mural, parâmetro notificar_apenas). Constante
# partilhada para nunca haver dois sítios com listas diferentes.
NOTIFICAR_APENAS_ENTREGAS = ["Conceição Costa", "Isa Moreira"]

# pedido explícito do Rui (2026-07-24): este agente só pode ler cards do
# projeto "Entregas", nunca de nenhum outro — em agents/logistica_entregas.py
# e agents/sugestao_logistica_semanal.py, o filtro pelo bucket é sempre uma
# comparação EXATA (case-insensitive) a PROJETO_ENTREGAS, nunca "in"/substring,
# para nunca incluir por engano um projeto diferente cujo nome só contenha
# "Entregas" (ex: um projeto futuro "Pré-Entregas" ou "Entregas 2025").

# a coluna "Produção" (e variantes de escrita) significa que a encomenda
# ainda está no fornecedor; as colunas por região são onde a encomenda é
# entregue ao cliente. MODELO CONFIRMADO em 2026-07-23 contra a API real
# do Basecamp: um card em "On Hold" tem como `parent` um objeto do tipo
# "Kanban::OnHold" (título sempre genérico "On hold") — mas o `url` desse
# objeto aponta diretamente para a coluna real por trás dessa secção (ex:
# confirmado ao vivo, uma secção "On Hold" com url para a coluna "Porto").
# Essa coluna real lê-se buscando esse url (ver
# agents.logistica_entregas._coluna_real_on_hold) — a morada nas notas do
# card só é usada como rede de segurança quando a coluna real for uma
# região mas não corresponder a nenhuma das conhecidas.
#
# Regras de significado do "On Hold" CONFIRMADAS pelo Rui (2026-07-27) —
# o significado muda consoante a coluna real por trás da secção:
# - On Hold por trás de "Produção": a encomenda AINDA está no fornecedor,
#   mas já tem data de entrega confirmada com ele (à espera de chegar ao
#   armazém) — NÃO significa que já chegou. Bug real corrigido: antes
#   disto, qualquer "On Hold" (mesmo por trás de "Produção") era tratado
#   como se já tivesse chegado ao armazém, pronto a agendar — o que
#   fazia a sugestão semanal de entregas incluir encomendas que ainda
#   nem tinham saído do fornecedor, e o alerta diário disparar "chegada
#   confirmada" para encomendas que ainda não chegaram.
# - On Hold por trás de "Lisboa"/"Porto"/"Outro": a encomenda já chegou
#   ao armazém, pronta a ser agendada para entrega (comportamento já
#   correto, mantido).
# - On Hold por trás de "Assistências": aguarda ser agendada (uma visita
#   de assistência, não uma entrega) — fase própria, distinta das
#   entregas; ainda sem condições de alerta próprias definidas.
COLUNAS_REGIAO_ENTREGA = {"lisboa", "porto", "outro", "outros"}
COLUNA_PRONTO_ENTREGA = "on hold"
COLUNA_ASSISTENCIAS = "assistencias"

def normalizar_coluna(nome: str) -> str:
    """Minúsculas e sem acentos — para "On Hold"/"on hold"/"Lisboa" serem
    sempre reconhecidos da mesma forma, sem depender de a equipa escrever
    sempre exatamente da mesma forma."""
    sem_acentos = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return sem_acentos.strip().lower()

def fase_encomenda(estado: str, coluna_real_on_hold: str = None) -> str:
    """Em que fase do fluxo de entrega está uma encomenda, a partir do
    nome do container do Kanban ("estado", o `parent.title` devolvido pela
    API) onde o card está agora, e — só quando `estado` for "On Hold" — a
    coluna REAL por trás dessa secção (`coluna_real_on_hold`, resolvida
    via agents.logistica_entregas._coluna_real_on_hold; None se ainda não
    tiver sido resolvida, ou se a resolução falhar).

    - "producao": ainda no fornecedor, à espera de chegar ao armazém —
      quer esteja diretamente na coluna "Produção", quer esteja em "On
      Hold" por trás de "Produção" (já com data confirmada, mas ainda não
      chegou — ver nota no topo do ficheiro).
    - "pronto_entrega": em "On Hold" por trás de uma coluna de região
      (Lisboa/Porto/Outro) — chegou ao armazém, pronta a ser entregue,
      ainda por agendar.
    - "em_entrega": diretamente numa coluna de região (Lisboa/Porto/
      Outro, fora de "On Hold") — a entrega está em curso, não precisa de
      mais sugestões de logística.
    - "assistencia_aguarda_agendamento": em "On Hold" por trás de
      "Assistências" — aguarda ser agendada.
    - "outro": nenhuma das anteriores (ex: já concluído, uma coluna fora
      deste fluxo, ou "On Hold" cuja coluna real ainda não foi resolvida)."""
    coluna = normalizar_coluna(estado)
    if coluna == COLUNA_PRONTO_ENTREGA:
        coluna_real = normalizar_coluna(coluna_real_on_hold) if coluna_real_on_hold else ""
        if "produ" in coluna_real:
            return "producao"
        if coluna_real in COLUNAS_REGIAO_ENTREGA:
            return "pronto_entrega"
        if COLUNA_ASSISTENCIAS in coluna_real:
            return "assistencia_aguarda_agendamento"
        return "outro"
    if "produ" in coluna:  # "Produção"/"Producao", tolera acentuação/maiúsculas
        return "producao"
    if coluna in COLUNAS_REGIAO_ENTREGA:
        return "em_entrega"
    return "outro"

def dias_uteis_entre(inicio: date, fim: date) -> int:
    """Dias úteis (segunda a sexta) entre duas datas, sem contar o dia de
    início — para "há mais de 3 dias úteis", que a equipa não trata da
    mesma forma que dias corridos. Sempre calculado aqui, nunca pelo
    modelo — a mesma razão de sempre: aritmética de datas é fácil de errar
    e o Python calcula sempre certo."""
    if fim <= inicio:
        return 0
    dias, atual = 0, inicio
    while atual < fim:
        atual += timedelta(days=1)
        if atual.weekday() < 5:  # 0=segunda ... 4=sexta
            dias += 1
    return dias

# --- limiares pedidos explicitamente pela Isa/Conceição (documento
# "Logistica", projeto Alma Data) — ficam aqui como constantes de código,
# não são recalculados pelo modelo em cada ciclo. ---
DIAS_UTEIS_CAMPOS_EM_FALTA = 3
PRESSAO_DIAS_ANTES_MIN = 6
PRESSAO_DIAS_ANTES_MAX = 8
HORAS_SEM_RESPOSTA_FORNECEDOR = 48
HORAS_CONFIRMACAO_FINAL = 48
FOLLOWUP_DIAS_MIN = 3
FOLLOWUP_DIAS_MAX = 5
DIAS_ENTREGA_SEM_FECHO = 7

# janela de repetição de cada condição, em dias — quanto tempo depois de
# um alerta a mesma condição pode voltar a disparar para o mesmo card.
# F, G e H são passos únicos por encomenda (não fazem sentido repetir).
JANELA_REPETICAO_DIAS = {"A": 7, "B": 7, "C": 999999, "D": 3, "E": 999999,
                        "F": 999999, "G": 999999, "H": 999999, "I": 7}

def avaliar_condicao(*, hoje: date, estado: str, criado_em: date,
                     coluna_real_on_hold: str = None,
                     data_entrada_armazem: date = None, data_entrega_cliente: date = None,
                     ja_alertado_recente: dict, horas_desde_alerta_b: float = None,
                     pedido_email_atraso: bool = False):
    """Devolve (condicao, variaveis) para a PRIMEIRA condição (A a I, pela
    mesma ordem do documento) que se aplica agora a esta encomenda, ou
    None se nenhuma se aplicar. `ja_alertado_recente` é um dict
    {"A": bool, "B": bool, ...} já calculado pelo chamador (consultando a
    janela de repetição própria de cada condição em db.py) —
    esta função em si não sabe nada de base de dados, só de regras.
    `coluna_real_on_hold`: quando `estado` for "On Hold", a coluna real
    por trás dessa secção (ver fase_encomenda) — sem isto, um "On Hold"
    por trás de "Produção" seria tratado como já chegado ao armazém."""
    fase = fase_encomenda(estado, coluna_real_on_hold)

    # A — campos críticos em falta, encomenda já em curso há mais de 3 dias úteis
    if ((data_entrada_armazem is None or data_entrega_cliente is None)
            and fase in ("producao", "pronto_entrega", "em_entrega")
            and dias_uteis_entre(criado_em, hoje) > DIAS_UTEIS_CAMPOS_EM_FALTA
            and not ja_alertado_recente.get("A")):
        return "A", {}

    if fase == "producao" and data_entrada_armazem:
        dias_para_entrada = (data_entrada_armazem - hoje).days

        # B — pressão ao fornecedor, entre 8 e 6 dias antes da entrada em armazém
        if (PRESSAO_DIAS_ANTES_MIN <= dias_para_entrada <= PRESSAO_DIAS_ANTES_MAX
                and not ja_alertado_recente.get("B")):
            return "B", {}

        # C — sem resposta do fornecedor 48h depois do alerta B
        if (horas_desde_alerta_b is not None and horas_desde_alerta_b >= HORAS_SEM_RESPOSTA_FORNECEDOR
                and not ja_alertado_recente.get("C")):
            return "C", {}

        # D — data de entrada em armazém já passada (ainda em "produção" —
        # se já tivesse chegado, o card já estaria em "On Hold")
        if dias_para_entrada < 0 and not ja_alertado_recente.get("D"):
            return "D", {}

    # E — pediram uma proposta de email de atraso ao cliente (independente da fase)
    if pedido_email_atraso and not ja_alertado_recente.get("E"):
        return "E", {}

    if fase == "pronto_entrega":
        # F — chegada ao armazém confirmada (é o que "pronto_entrega" significa),
        # falta combinar/comunicar a previsão de entrega ao cliente
        if not ja_alertado_recente.get("F"):
            return "F", {}

        if data_entrega_cliente:
            horas_para_entrega = (data_entrega_cliente - hoje).days * 24
            # G — confirmação final, 48h ou menos antes da entrega ao cliente
            if horas_para_entrega <= HORAS_CONFIRMACAO_FINAL and not ja_alertado_recente.get("G"):
                return "G", {}

    if data_entrega_cliente:
        dias_desde_entrega = (hoje - data_entrega_cliente).days
        # H — follow-up pós-entrega, 3 a 5 dias depois da entrega
        if FOLLOWUP_DIAS_MIN <= dias_desde_entrega <= FOLLOWUP_DIAS_MAX and not ja_alertado_recente.get("H"):
            return "H", {}
        # I — entrega concluída há mais de 7 dias sem o card ter sido fechado
        if dias_desde_entrega > DIAS_ENTREGA_SEM_FECHO and not ja_alertado_recente.get("I"):
            return "I", {}

    return None

def _fmt_data(d: date) -> str:
    return d.strftime("%d/%m/%Y") if d else None

def _campo(valor, rotulo: str) -> str:
    return valor if valor else f"[{rotulo} — por preencher]"

# condições com o texto literal exatamente como pedido pela Isa/Conceição
# (documento "Logistica") — geradas aqui em Python, não pelo modelo, para
# nunca haver deriva na redação combinada com a equipa.
CONDICOES_COM_TEXTO_FIXO = {"A", "B", "C", "D", "E", "I"}

def gerar_texto_condicao_fixa(condicao: str, dados: dict) -> str:
    """Texto do comentário para uma condição com redação fixa (A, B, C, D,
    E, I) — ver CONDICOES_COM_TEXTO_FIXO. As condições F, G, H usam antes
    os templates numerados (8.1/8.2/8.3) do documento "Logistica", lidos
    em tempo real (ver agents/logistica_entregas.py) em vez de fixos aqui,
    porque essa parte do documento não foi transcrita para este ficheiro."""
    numero = _campo(dados.get("numero_encomenda"), "N.º da encomenda")
    projeto_cliente = _campo(dados.get("cliente"), "nome do cliente/projeto")
    fornecedor = _campo(dados.get("fornecedor"), "nome do fornecedor")
    data_entrada = _campo(_fmt_data(dados.get("data_entrada_armazem")), "data de entrada em armazém")
    data_entrega = _campo(_fmt_data(dados.get("data_entrega_cliente")), "data de entrega ao cliente")

    if condicao == "A":
        return ("Alma Logística: esta encomenda não tem data de entrada em armazém ou data de "
                "entrega ao cliente registada. Por favor preenche estes campos para eu poder "
                "monitorizar. CC: @Isa Moreira")

    if condicao == "B":
        data_entrada_dt = dados.get("data_entrada_armazem")
        data_limite = _campo(_fmt_data(data_entrada_dt + timedelta(days=2)) if data_entrada_dt else None,
                             "data limite de resposta")
        return f"""Alma Logística — proposta de email ao fornecedor (para envio pela Conceição após validação):

Assunto: Confirmação de expedição — Encomenda {numero} — {projeto_cliente}

Exmo(a) Sr(a) {fornecedor}, gostaríamos de confirmar o estado da encomenda {numero}, com entrada em armazém prevista para {data_entrada}. Pedimos a indicação da data de expedição e, se possível, os dados de tracking/transportador. Agradecemos a confirmação até {data_limite}. Com os melhores cumprimentos, Conceição Costa | Interior Guider / Boa Safra.

Responsável: @Conceição Costa — por favor valida e envia."""

    if condicao == "C":
        return ("Alma Logística: ainda não há registo de resposta do fornecedor ao email de "
                "confirmação enviado há 48h. Sugere-se contacto telefónico. "
                "CC: @Conceição Costa @Isa Moreira")

    if condicao == "D":
        return (f"Alma Logística: a data de entrada em armazém prevista ({data_entrada}) já passou "
                "sem registo de confirmação. É necessário apurar o estado com o fornecedor e, se "
                "houver atraso, comunicar ao cliente em menos de 24h. Proposta de email ao cliente "
                "disponível a pedido. CC: @Conceição Costa @Isa Moreira")

    if condicao == "E":
        return f"""Alma Logística — proposta de email ao cliente (para envio após validação):

Assunto: Atualização da sua encomenda {numero} — {projeto_cliente}

Exmo(a) Sr(a) {_campo(dados.get('cliente'), 'nome do cliente')}, gostaríamos de o(a) informar que a entrega da sua encomenda sofreu um ajuste no prazo previsto. A nova data estimada de entrega é [nova data — por preencher]. [Se entrega parcial possível: temos disponibilidade para entregar as peças disponíveis na data inicialmente prevista, caso seja do seu interesse.] Lamentamos qualquer inconveniente e estamos à sua disposição. Com os melhores cumprimentos, [Conceição Costa / Isa Moreira] | Interior Guider / Boa Safra.

Responsável: @Conceição Costa ou @Isa Moreira — por favor valida, preenche os campos em falta e envia."""

    if condicao == "I":
        return (f"Alma Logística: esta encomenda tem data de entrega passada há mais de 7 dias "
                f"({data_entrega}) e o card ainda está em aberto. Por favor confirma se a entrega "
                "foi concluída e fecha o card se sim. CC: @Conceição Costa @Isa Moreira")

    raise ValueError(f"condição sem texto fixo definido: {condicao!r}")

# morada do armazém Boa Safra (Rui, 2026-07-27) — ponto de partida (e de
# regresso) de qualquer trajeto de entregas gerado para o Google Maps.
# A morada postal completa não é reconhecida pelo geocoder do Google Maps
# (bug reportado em produção). O Rui confirmou que o armazém funciona nas
# instalações da Ecos Largos, e que pesquisar "Ecos Largos" no Google Maps
# encontra o local certo — por isso usa-se este nome, tal como testado,
# em vez da morada postal.
#
# NOTA (2026-07-29): esse teste foi feito na app/site do Google Maps (que
# usa pesquisa de negócios, muito mais tolerante), não na Directions API
# usada aqui — a Directions API tem um geocoder mais estrito, que pode
# falhar a encontrar um nome de local isolado sem país. Bug real
# reportado em produção: com só "Ecos Largos", a Directions API chegou a
# devolver NOT_FOUND para o próprio armazém — o que, por o armazém ser
# sempre a origem/destino de qualquer trajeto, fazia TODAS as moradas
# serem sinalizadas como más de uma só vez (mesmo moradas corretas,
# entretanto confirmadas manualmente pela equipa). Acrescentar ", Portugal"
# e o parâmetro region=pt (ver _info_trajeto) reduz essa ambiguidade sem
# voltar à morada postal completa (já confirmada que não funciona). Se
# isto ainda assim não resolver, os logs (ver print em _info_trajeto)
# mostram agora o status exato devolvido pela Google, para diagnosticar
# sem precisar de partilhar credenciais outra vez.
MORADA_ARMAZEM = "Ecos Largos, Portugal"

# limite generoso mas defensivo — o Google Maps (sem chave de API, só o
# link público) aceita bem menos de 25 paragens antes de começar a
# recusar/cortar o pedido; nunca visto na prática mais do que isto numa
# única região numa semana, mas evita gerar um link que o Maps rejeite.
LIMITE_PARAGENS_GOOGLE_MAPS = 23

# chave da Google Directions API (Google Cloud, faturação ativa) — usada só
# para CALCULAR a ordem ótima das paragens antes de publicar o trajeto
# (pedido do Rui, 2026-07-27: a rota publicada já deve vir otimizada, sem a
# pessoa ter de clicar "Otimizar ordem" manualmente no Maps). Se não
# estiver configurada, ou se a chamada falhar por qualquer razão, a Alma
# não rebenta — publica o trajeto na ordem original, tal como antes.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

def _info_trajeto(origem: str, moradas: list[str]) -> dict:
    """Chama a Google Directions API uma única vez para calcular a ordem
    ótima das `moradas` (ida e volta a partir de `origem`) E os totais/por
    perna reais de distância/duração da mesma resposta — usados depois
    para o custo de deslocação (ver tools.tempos_montagem.custo_deslocacao)
    e para a proposta de agendamento (ver tools.agendamento_logistica),
    sem precisar de nenhuma chamada extra. Devolve {"ordem": moradas
    (otimizada ou inalterada), "ordem_indices": índices na lista `moradas`
    original (para alinhar outros dados por paragem, ex: título do card,
    à mesma ordem, sem ambiguidade quando duas moradas forem iguais),
    "km": float|None, "duracao_min": float|None, "pernas_minutos":
    list[float]|None (uma por perna, N+1 pernas para N paragens: armazém→
    p1, p1→p2, ..., pN→armazém, já na ordem otimizada), "pernas_km":
    list[float]|None (idem, em km — pedido do Rui 2026-07-29, para o custo
    de viagem por perna na tabela preparatória de agendamento, ver
    agents.sugestao_logistica_semanal._construir_tabela_agendamento)} —
    tudo None (exceto "ordem"/"ordem_indices", que ficam na ordem
    original) sempre que não houver chave configurada, nenhuma morada, ou
    a chamada falhar por qualquer razão (nunca deixa a rota por publicar
    só por causa disto).

    Chama a API mesmo com uma única morada — bug real corrigido
    (2026-07-28): com só 1 paragem não há nada a otimizar em termos de
    ordem, mas a viagem de ida e volta a essa morada continua a ter uma
    distância/duração reais e computáveis; saltar a chamada nesse caso
    fazia o custo de deslocação ficar sempre em falta exatamente no caso
    mais comum (uma só entrega pronta numa região nessa semana)."""
    vazio = {"ordem": moradas, "ordem_indices": list(range(len(moradas))),
            "km": None, "duracao_min": None, "pernas_minutos": None, "pernas_km": None}
    if not GOOGLE_MAPS_API_KEY or not moradas:
        return vazio
    try:
        resposta = httpx.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={
                "origin": origem,
                "destination": origem,
                "waypoints": "optimize:true|" + "|".join(moradas),
                "region": "pt",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=10,
        )
        dados = resposta.json()
        if dados.get("status") != "OK":
            # bug real reportado em produção (2026-07-29): esta falha ficava
            # completamente silenciosa — sem o status exato da Google não
            # havia forma de distinguir "o armazém não geocodifica" de "uma
            # morada é mesmo inválida" sem partilhar credenciais outra vez.
            print(f"[logistica] trajeto para \"{origem}\" com {len(moradas)} paragem(ns) falhou: "
                 f"status={dados.get('status')!r} error_message={dados.get('error_message')!r}")
            return vazio
        rota = dados["routes"][0]
        pernas = rota.get("legs") or []
        km_total = sum(p["distance"]["value"] for p in pernas) / 1000
        duracao_total_min = sum(p["duration"]["value"] for p in pernas) / 60
        pernas_minutos = [p["duration"]["value"] / 60 for p in pernas]
        pernas_km = [p["distance"]["value"] / 1000 for p in pernas]
        ordem_indices = rota.get("waypoint_order") or list(range(len(moradas)))
        return {"ordem": [moradas[i] for i in ordem_indices], "ordem_indices": ordem_indices,
                "km": km_total, "duracao_min": duracao_total_min, "pernas_minutos": pernas_minutos,
                "pernas_km": pernas_km}
    except Exception as e:
        print(f"[logistica] falha ao pedir o trajeto para \"{origem}\" com {len(moradas)} paragem(ns): {e!r}")
        return vazio

def _otimizar_ordem_paragens(origem: str, moradas: list[str]) -> list[str]:
    """Como antes: só a ordem ótima das moradas (ver _info_trajeto para os
    totais de km/duração, usados pelo custo de deslocação)."""
    return _info_trajeto(origem, moradas)["ordem"]

def metricas_trajeto(moradas: list[str], origem: str = MORADA_ARMAZEM) -> dict:
    """Totais REAIS de distância (km) e duração (minutos) do trajeto de ida
    e volta a `origem` passando por `moradas` — pedido do Rui (2026-07-28)
    para calcular o custo de deslocação com dados reais da Google Directions
    API, em vez da tabela fixa de zonas do "Procedimento Tempos de Montagem
    para Logística" (mais preciso, e usa a mesma chamada já feita para
    otimizar a ordem das paragens, sem custo extra). Devolve {"km": None,
    "duracao_min": None} sempre que não for possível calcular (sem chave,
    menos de 2 paragens, ou falha da chamada) — quem usar isto tem de lidar
    com esse caso, nunca inventar um custo sem dados reais."""
    info = _info_trajeto(origem, [m for m in moradas if m and m.strip()])
    return {"km": info["km"], "duracao_min": info["duracao_min"]}

def plano_trajeto(moradas: list[str], origem: str = MORADA_ARMAZEM) -> dict:
    """Como metricas_trajeto, mas devolve também os índices da ordem
    otimizada e a duração de cada perna individual — pedido do Rui
    (2026-07-28), para a proposta de agendamento (ver
    tools.agendamento_logistica.calcular_horario_dia) poder alinhar o
    tempo de montagem de cada card à paragem certa, pela POSIÇÃO
    original na lista `moradas` (nunca pelo texto da morada — evita
    ambiguidade se duas encomendas tiverem a mesma morada).

    Devolve {"ordem_indices": list[int] (índices em `moradas`, já na
    ordem otimizada), "pernas_minutos": list[float]|None (N+1 valores
    para N moradas), "pernas_km": list[float]|None (idem, em km), "km":
    float|None, "duracao_min": float|None} — os campos que dependem da
    API ficam None se não for possível calcular (sem chave, sem moradas,
    ou falha da chamada)."""
    info = _info_trajeto(origem, [m for m in moradas if m and m.strip()])
    return {"ordem_indices": info["ordem_indices"], "pernas_minutos": info["pernas_minutos"],
            "pernas_km": info["pernas_km"], "km": info["km"], "duracao_min": info["duracao_min"]}

def _morada_reconhecida(morada: str, origem: str = MORADA_ARMAZEM) -> bool:
    """Verifica se `morada` é reconhecida pelo Google Maps — usando a
    própria Directions API (não a Geocoding API, que é uma API separada
    e a chave configurada só tem a Directions API autorizada, por pedido
    explícito de restringi-la assim).

    Pede um trajeto trivial de `morada` para ela própria (origem =
    destino = `morada`) em vez de `origem` (o armazém) até `morada` —
    bug real reportado em produção (2026-07-29): com origem=armazém, um
    NOT_FOUND pode ser causado pela geocodificação do PRÓPRIO ARMAZÉM
    falhar (ex: "Ecos Largos" é só um nome de local, não uma morada
    completa — reconhecido pela pesquisa da app do Google Maps, mas nem
    sempre pelo geocoder mais estrito da Directions API), não da morada a
    testar — e nesse caso TODAS as moradas seriam sinalizadas como más de
    uma só vez, mesmo as corretas (foi exatamente o que aconteceu: uma
    morada já confirmada a funcionar diretamente no Google Maps continuou
    a ser excluída). Testar `morada` contra ela própria isola o teste a
    "esta morada geocodifica?", sem depender de o armazém também
    geocodificar bem.

    Se a Google responder que não conseguiu geocodificar ou não encontrou
    rota nenhuma (NOT_FOUND/ZERO_RESULTS), é sinal de que esta morada
    específica é o problema.

    Devolve True se não houver chave configurada, ou se a chamada falhar
    por qualquer razão que não seja claramente sobre a morada em si (ex:
    limite de pedidos, chave inválida) — nunca trata isso como "morada
    errada", só como "não foi possível confirmar"."""
    if not GOOGLE_MAPS_API_KEY:
        return True
    try:
        resposta = httpx.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={"origin": morada, "destination": morada, "region": "pt", "key": GOOGLE_MAPS_API_KEY},
            timeout=10,
        )
        dados = resposta.json()
        if dados.get("status") in ("NOT_FOUND", "ZERO_RESULTS"):
            print(f"[logistica] morada não reconhecida pela Google: \"{morada}\" "
                 f"(status={dados.get('status')!r})")
            return False
        return True
    except Exception:
        return True

def moradas_nao_reconhecidas(moradas: list[str], origem: str = MORADA_ARMAZEM) -> list[str]:
    """Devolve, de entre `moradas`, só as que o Google Maps confirma não
    conseguir encontrar (bug real reportado em produção: um card com uma
    morada mal escrita/incompleta faz o Google Maps falhar a
    geocodificação dessa paragem, o que impede o trajeto todo de ser
    calculado — sem mostrar rota, tempo, nem nada interativo, sem
    nenhum aviso claro do motivo). Verificar isto antes de publicar
    permite avisar logo no texto, em vez de a pessoa só descobrir ao
    abrir um link partido. Devolve sempre [] se não houver chave
    configurada (sem forma de verificar)."""
    if not GOOGLE_MAPS_API_KEY:
        return []
    return [m for m in moradas if m and m.strip() and not _morada_reconhecida(m)]

def separar_moradas_por_reconhecimento(moradas: list[str], origem: str = MORADA_ARMAZEM) -> tuple:
    """Separa `moradas` em (reconhecidas, nao_reconhecidas) — usa isto
    ANTES de pedir um trajeto/custo/agendamento para várias moradas de
    uma vez (ver gerar_link_google_maps/metricas_trajeto/plano_trajeto).

    Bug real reportado em produção (2026-07-28): a Google Directions API
    falha o pedido de trajeto com VÁRIAS paragens por INTEIRO — sem
    ordem otimizada, sem km/duração, sem proposta de horário — assim que
    UMA só das moradas não é geocodificável (não falha "só essa
    paragem", falha tudo). Isolar as moradas más antes de pedir o
    trajeto, e pedi-lo só com as boas, permite continuar a calcular
    trajeto/custo/horário reais para as restantes, em vez de perder tudo
    por causa de uma só morada mal escrita.

    Devolve (moradas, []) sem verificar nada se não houver chave
    configurada (sem forma de confirmar — nunca trata como más sem
    verificar)."""
    if not GOOGLE_MAPS_API_KEY:
        return moradas, []
    nao_reconhecidas = [m for m in moradas if m and m.strip() and not _morada_reconhecida(m, origem)]
    reconhecidas = [m for m in moradas if m and m.strip() and m not in nao_reconhecidas]
    return reconhecidas, nao_reconhecidas

def gerar_link_google_maps(moradas: list[str], origem: str = MORADA_ARMAZEM) -> str:
    """Constrói um link do Google Maps com um trajeto de ida e volta a
    partir de `origem` (por omissão o armazém Boa Safra/Ecos Largos),
    passando por todas as `moradas` dadas. Antes de construir o link, tenta
    otimizar a ordem das paragens via `_otimizar_ordem_paragens` (Google
    Directions API) — se essa otimização não for possível (sem chave
    configurada, ou falha da chamada), usa a ordem original das moradas tal
    como recebidas, e o próprio Google Maps, ao abrir o link, continua a
    permitir otimizar a ordem manualmente e editar o trajeto antes de
    seguir viagem.

    Devolve None se `moradas` estiver vazia (não há trajeto nenhum a
    fazer). Trunca defensivamente a LIMITE_PARAGENS_GOOGLE_MAPS moradas
    (o link público do Google Maps não aceita paragens ilimitadas), sem
    rebentar — quem chamar isto deve avisar se isso aconteceu."""
    if not moradas:
        return None
    moradas_validas = [m for m in moradas if m and m.strip()][:LIMITE_PARAGENS_GOOGLE_MAPS]
    if not moradas_validas:
        return None
    moradas_otimizadas = _otimizar_ordem_paragens(origem, moradas_validas)
    origem_codificada = quote(origem)
    paragens_codificadas = "|".join(quote(m) for m in moradas_otimizadas)
    return (f"https://www.google.com/maps/dir/?api=1&travelmode=driving"
            f"&origin={origem_codificada}&destination={origem_codificada}"
            f"&waypoints={paragens_codificadas}")
