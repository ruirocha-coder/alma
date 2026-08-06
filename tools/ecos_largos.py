# tools/ecos_largos.py — recursos próprios da equipa Ecos Largos, uma
# equipa industrial parceira gerida no mesmo Basecamp mas com o seu próprio
# projeto, à parte da Interior Guider.
import calendar, os, re, time, unicodedata
from datetime import date, timedelta
import httpx
from tools import documentos_empresa, tempo
import db

# servidor próprio da equipa (fora do Basecamp) — configurável por env var
# porque corre num endereço DuckDNS, que pode mudar sem precisar de deploy.
# API oficial de dados (não a página HTML do dashboard): sem parâmetros
# devolve a entrada mais recente da base de dados; com ?data=YYYY-MM-DD
# devolve os dados desse dia.
DASHBOARD_API_URL = os.environ.get(
    "ECOS_LARGOS_DASHBOARD_API_URL",
    "http://ecoslargos.duckdns.org:9000/server/data_api.php"
)

_FORMATO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# regras permanentes pedidas explicitamente pelo Rui, nos comentários do
# mural, depois de o takt ter sido lido ao contrário — ver
# _com_takt_formatado. Ambas as missões que leem este dashboard (o resumo
# diário automático e o agente da consola) partilham este texto.
REGRAS_APRESENTACAO_PRODUCAO = """
Regras permanentes para apresentar dados deste dashboard (pedidas
explicitamente pelo Rui, depois de o takt ter sido lido ao contrário):
- Takt: usa sempre os campos já convertidos "takt_real_min_seg_por_m3" e
  "takt_objetivo_min_seg_por_m3" (formato min:seg por m³, ex: "06:47") —
  nunca apresentes os campos brutos takt_real_m3_h/takt_needed_m3_h (estão
  em horas por m³), e nunca tentes converter tu mesma horas para min:seg,
  usa sempre o campo já convertido. Real maior que o objetivo = mais
  lento = motivo de alerta.
- Rácio de entrada/saída: apresenta sempre com exatamente duas casas
  decimais (ex: 2,70 — nunca 2,6961 nem 2,7).
- Charriots (carrinhos/lotes de troncos que percorrem a linha de
  produção): usa sempre o campo já resumido "charriots_resumo", nunca
  navegues sozinha a estrutura bruta em production_details.charriotRows.
  ATENÇÃO: dentro de charriots_resumo, "numero_de_charriots" é quantos
  charriots passaram (o comprimento da lista "por_charriot"), e
  "total_de_troncos_no_dia" é a SOMA de troncos de todos eles — são
  números diferentes, nunca os confundas nem apresentes um pelo outro.
- Totais de um período (ex: "quanto entrou em junho", "total de saída
  esta semana): dashboard_producao_ecos_largos_intervalo já devolve, em
  "totais", a soma de input_m3 e output_m3 do intervalo inteiro, calculada
  em Python — usa sempre esse valor pronto. NUNCA somes tu mesma os
  valores diários um a um: já aconteceu somares errado (25 valores de
  input_m3 de junho deviam somar 6009,36 m³ e a soma feita à mão veio
  4.521,79 m³) — é aritmética fácil de errar com muitas parcelas
  decimais, exatamente como as datas, e por isso passou a ser calculada
  aqui e não pelo modelo."""

def _horas_para_min_seg(horas) -> str:
    """Converte um valor em horas por m³ para minutos:segundos por m³ (ex:
    0.1131 -> "06:47") — a forma como a equipa da Ecos Largos lê o takt.
    Isto corre sempre aqui, nunca no modelo: converter horas decimais para
    minutos:segundos é aritmética de base 60, fácil de errar (foi assim que
    o takt ficou lido ao contrário da primeira vez). Devolve None se o
    valor não for numérico."""
    try:
        horas = float(horas)
    except (TypeError, ValueError):
        return None
    total_minutos = horas * 60
    minutos = int(total_minutos)
    segundos = round((total_minutos - minutos) * 60)
    if segundos == 60:
        minutos += 1
        segundos = 0
    return f"{minutos:02d}:{segundos:02d}"

# nomes de campo CONFIRMADOS contra uma resposta real da API (2026-07-24,
# JSON partilhado pelo Rui) — a versão anterior destes nomes
# ("taktrealm3h"/"taktneededm3h", sem underscore) nunca bateu certo com a
# API real (que usa snake_case em todo o payload, ex: input_m3,
# stop_total_sec, oee_pct), por isso esta conversão nunca disparava e o
# modelo só via sempre os campos brutos em horas — exatamente o problema
# que esta função existe para evitar (ver nota do takt lido ao contrário).
_CAMPOS_TAKT = {
    "takt_real_m3_h": "takt_real_min_seg_por_m3",
    "takt_needed_m3_h": "takt_objetivo_min_seg_por_m3",
}

def _com_takt_formatado(conteudo):
    """Acrescenta, ao lado dos campos brutos do takt (em h/m³) quando
    presentes, a mesma leitura já convertida para min:seg por m³, e uma
    nota de alerta se o real for mais lento que o objetivo. Deixa o resto
    do conteúdo intocado; se não for um dict (ex: resposta em texto bruto),
    devolve tal e qual."""
    if not isinstance(conteudo, dict):
        return conteudo
    conteudo = dict(conteudo)
    for campo_bruto, campo_formatado in _CAMPOS_TAKT.items():
        if campo_bruto in conteudo:
            formatado = _horas_para_min_seg(conteudo[campo_bruto])
            if formatado:
                conteudo[campo_formatado] = formatado
    if "takt_real_m3_h" in conteudo and "takt_needed_m3_h" in conteudo:
        try:
            conteudo["takt_alerta_mais_lento_que_objetivo"] = (
                float(conteudo["takt_real_m3_h"]) > float(conteudo["takt_needed_m3_h"]))
        except (TypeError, ValueError):
            pass
    return conteudo

def _com_charriots_resumido(conteudo):
    """Resume os dados de "charriots" (cada carrinho/lote de troncos que
    passa pela linha de produção) num formato direto, em vez de obrigar o
    modelo a navegar sozinho a estrutura aninhada de production_details
    (confirmada ao vivo, 2026-07-24 — production_details.charriotRows).

    ATENÇÃO ao nome enganador do campo bruto `charriotTotalCount`: é a
    SOMA de troncos de todos os charriots do dia, não o número de
    charriots em si (esse é o comprimento de charriotRows) — confirmado
    ao vivo comparando os dois: charriotTotalCount bateu certo com a soma
    dos "troncos" de cada linha, não com a contagem de linhas. Esta
    função já separa os dois em campos com nomes inequívocos, para nunca
    se repetir a confusão (o mesmo tipo de erro que já aconteceu com o
    takt lido ao contrário)."""
    if not isinstance(conteudo, dict):
        return conteudo
    detalhes = conteudo.get("production_details")
    if not isinstance(detalhes, dict):
        return conteudo
    linhas = detalhes.get("charriotRows")
    if not isinstance(linhas, list):
        return conteudo
    conteudo = dict(conteudo)
    conteudo["charriots_resumo"] = {
        "numero_de_charriots": len(linhas),
        "total_de_troncos_no_dia": detalhes.get("charriotTotalCount"),
        "volume_total_m3": detalhes.get("charriotTotalVolume"),
        "paragens_total_contagem": detalhes.get("charriotTotalStopCount"),
        "paragens_total_segundos": detalhes.get("charriotTotalStopSec"),
        "por_charriot": [{
            "id": linha.get("id"),
            "troncos": linha.get("troncos"),
            "volume_m3": linha.get("volume"),
            "paragens_contagem": linha.get("stopCount"),
            "paragens_segundos": linha.get("stopTotalSec"),
            "motivos_paragem": [m.get("reason") for m in (linha.get("stopByReason") or [])],
        } for linha in linhas],
    }
    return conteudo

def _resolver_data(data: str) -> str:
    """Aceita "hoje"/"ontem" além de YYYY-MM-DD — o modelo não sabe a data
    de hoje com fiabilidade, por isso essas palavras são resolvidas aqui
    (que sabe sempre a data real), em vez de pedir ao modelo para as
    calcular. Devolve None se não conseguir perceber a data."""
    termo = data.strip().lower()
    if termo in ("hoje", "agora"):
        return date.today().isoformat()
    if termo == "ontem":
        return (date.today() - timedelta(days=1)).isoformat()
    if _FORMATO_DATA.match(data.strip()):
        return data.strip()
    return None

def ler_dashboard_producao(data: str = None) -> dict:
    """Lê os dados de produção da Ecos Largos, da API oficial do dashboard —
    um servidor próprio da equipa (fora do Basecamp), por isso menos fiável
    do que uma chamada normal à API: pode estar offline ou inacessível, o
    que devolve um erro em vez de rebentar quem chamar isto.

    Sem `data`, devolve a entrada mais recente (agora); com `data`
    ("hoje", "ontem", ou YYYY-MM-DD), devolve os dados desse dia."""
    params = None
    if data:
        data_resolvida = _resolver_data(data)
        if data_resolvida is None:
            return {"erro": f"não percebi a data {data!r} — usa \"hoje\", \"ontem\" ou o formato YYYY-MM-DD"}
        params = {"data": data_resolvida}

    try:
        r = httpx.get(DASHBOARD_API_URL, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return {"erro": f"não consegui aceder à API de dados de produção: {e}"}

    try:
        dados = r.json()
    except ValueError:
        # a API não devolveu JSON (ex: página de erro do servidor) — ainda
        # assim devolve o texto em bruto, para não perder informação
        return {"conteudo": r.text.strip()} if r.text.strip() else {
            "erro": "a API de dados de produção respondeu, mas sem conteúdo legível"}
    return {"conteudo": _com_charriots_resumido(_com_takt_formatado(dados))}

def _semana_de(referencia: date) -> tuple:
    """Segunda a sexta da semana que contém `referencia`."""
    inicio = referencia - timedelta(days=referencia.weekday())
    return inicio, inicio + timedelta(days=4)

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

def _mes_de(ano: int, mes: int) -> tuple:
    """Primeiro e último dia de um mês — usa calendar.monthrange para o
    último dia (28/29/30/31), nunca calculado à mão, pela mesma razão de
    sempre: aritmética de datas não se confia ao modelo nem a atalhos."""
    inicio = date(ano, mes, 1)
    return inicio, date(ano, mes, calendar.monthrange(ano, mes)[1])

def _resolver_intervalo(periodo: str):
    """Resolve expressões relativas de período ("esta semana", "semana
    passada", "este mês", "mês passado", ou o nome de um mês como "junho"
    ou "junho de 2026") para datas reais — tal como _resolver_data para
    um único dia, isto corre em Python porque o modelo não tem forma
    fiável de saber a data de hoje, e muito menos calcular semanas/meses a
    partir dela (era isto que fazia pedidos por "a semana passada" virem
    com a semana errada). Um nome de mês sem ano assume o ano corrente.
    Devolve (None, None) se não reconhecer o período."""
    termo = periodo.strip().lower().replace(" de ", " ").replace(" ", "_")
    hoje = date.today()
    if termo == "esta_semana":
        return _semana_de(hoje)
    if termo == "semana_passada":
        return _semana_de(hoje - timedelta(days=7))
    if termo in ("este_mes", "este_mês", "mes_atual", "mês_atual"):
        return _mes_de(hoje.year, hoje.month)
    if termo in ("mes_passado", "mês_passado"):
        ano, mes = (hoje.year, hoje.month - 1) if hoje.month > 1 else (hoje.year - 1, 12)
        return _mes_de(ano, mes)
    partes = termo.split("_")
    nome_mes = partes[0] if partes else ""
    if nome_mes in _MESES_PT:
        mes = _MESES_PT[nome_mes]
        ano = int(partes[1]) if len(partes) > 1 and partes[1].isdigit() else hoje.year
        return _mes_de(ano, mes)
    return None, None

_LIMITE_DIAS_INTERVALO = 31

# campos que fazem sentido somar ao longo de um intervalo — nunca deixar
# o modelo somar isto sozinho (pedido real da Beatriz, 2026-07-24: pediu o
# total de entradas de junho, a Alma somou os 25 valores diários de
# input_m3 à mão e errou o total — 4.521,79 m³ em vez do correto 6009,36
# m³. A mesma razão de sempre para nunca confiar aritmética ao modelo,
# desta vez para somas em vez de datas.)
_CAMPOS_SOMAVEIS = ("input_m3", "output_m3")

def _totais_intervalo(dias: list) -> dict:
    """Soma, em Python, os campos de _CAMPOS_SOMAVEIS ao longo de todos os
    dias com dados válidos do intervalo. Dias sem o campo, ou com erro
    (ex: API offline nesse dia), são simplesmente ignorados na soma — não
    fazem o total falhar nem entram como zero."""
    totais = {}
    for campo in _CAMPOS_SOMAVEIS:
        soma = 0.0
        algum = False
        for dia in dias:
            conteudo = dia.get("conteudo")
            valor = conteudo.get(campo) if isinstance(conteudo, dict) else None
            if valor is None:
                continue
            try:
                soma += float(valor)
                algum = True
            except (TypeError, ValueError):
                continue
        if algum:
            totais[campo] = round(soma, 2)
    return totais

def ler_dashboard_producao_intervalo(periodo: str = None, data_inicio: str = None, data_fim: str = None) -> dict:
    """Lê os dados de produção dia a dia num intervalo — usa `periodo`
    ("esta_semana", "semana_passada", "este_mes", "mes_passado", ou o nome
    de um mês como "junho" ou "junho de 2026" — tudo resolvido aqui para
    as datas reais, nunca calculado pelo modelo) ou `data_inicio`/
    `data_fim` explícitos (YYYY-MM-DD) para outro intervalo qualquer.
    Devolve também, em "totais", a soma já calculada em Python (nunca pelo
    modelo) de input_m3 e output_m3 ao longo do intervalo."""
    if periodo:
        inicio, fim = _resolver_intervalo(periodo)
        if inicio is None:
            return {"erro": f"não percebi o período {periodo!r} — usa \"esta_semana\", \"semana_passada\", "
                            "\"este_mes\", \"mes_passado\", o nome de um mês (ex: \"junho\"), "
                            "ou indica data_inicio e data_fim (YYYY-MM-DD)"}
    elif data_inicio and data_fim:
        try:
            inicio = date.fromisoformat(data_inicio.strip())
            fim = date.fromisoformat(data_fim.strip())
        except ValueError:
            return {"erro": "data_inicio/data_fim têm de estar no formato YYYY-MM-DD"}
    else:
        return {"erro": "indica um período (\"esta_semana\", \"semana_passada\") ou data_inicio e data_fim"}

    if fim < inicio:
        return {"erro": "data_fim não pode ser antes de data_inicio"}
    if (fim - inicio).days + 1 > _LIMITE_DIAS_INTERVALO:
        return {"erro": f"intervalo demasiado grande — pede no máximo {_LIMITE_DIAS_INTERVALO} dias de cada vez"}

    dias = []
    d = inicio
    while d <= fim:
        dias.append({"data": d.isoformat(), "dia_semana": tempo.dia_da_semana(d.isoformat())["abreviado"],
                    **ler_dashboard_producao(d.isoformat())})
        d += timedelta(days=1)
    return {"inicio": inicio.isoformat(), "fim": fim.isoformat(), "dias": dias,
            "totais": _totais_intervalo(dias)}

TOOLS_DASHBOARD_PRODUCAO = [
    {
        "name": "dashboard_producao_ecos_largos",
        "description": "Lê os dados de produção da Ecos Largos, da API oficial do dashboard (servida por um servidor próprio da equipa, fora do Basecamp). Sem argumentos devolve os dados mais recentes (agora); passa `data` para consultar um dia específico. Usa isto sempre que perguntarem pelo estado da produção, números de produção, entrada/receção de madeira, m3 (metros cúbicos) de madeira, quantidade recebida ou processada, linhas de produção, charriots (carrinhos/lotes de troncos que percorrem a linha), troncos, ou pedirem uma análise/resumo — de hoje ou de outro dia. Qualquer pergunta sobre estes números, mesmo em linguagem informal (ex: \"quanto entrou hoje\", \"quantos m3 tivemos\", \"quantos charriots hoje\"), refere-se a este dashboard — usa sempre esta ferramenta antes de responder, nunca digas que não tens essa informação sem primeiro tentares. Mesmo que perguntem por algo mais específico que este dashboard não distinga (ex: um produto ou referência em concreto), tenta sempre primeiro consultar os dados gerais disponíveis e partilha-os — nunca respondas que não tens informação nenhuma sem teres consultado o dashboard.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "\"hoje\", \"ontem\", ou uma data no formato YYYY-MM-DD — omite para os dados mais recentes/agora"}
            }
        }
    },
    {
        "name": "dashboard_producao_ecos_largos_intervalo",
        "description": "Lê os dados de produção dia a dia num intervalo de datas — usa sempre esta ferramenta (nunca dashboard_producao_ecos_largos repetidamente com datas calculadas por ti) sempre que perguntarem por um período como \"esta semana\", \"a semana passada\", \"este mês\", \"o mês passado\", ou um mês pelo nome (ex: \"junho\", \"em maio\", \"quanto entrou em março\"): passa `periodo` com exatamente \"esta_semana\", \"semana_passada\", \"este_mes\", \"mes_passado\", ou o nome do mês (ex: \"junho\" ou \"junho de 2026\" se um ano diferente do atual for mencionado) — as datas certas são sempre calculadas aqui, nunca por ti, tu não sabes a data de hoje com fiabilidade nem quantos dias tem cada mês. Para outro intervalo qualquer, usa `data_inicio` e `data_fim` (YYYY-MM-DD) em vez de `periodo`. A resposta inclui um campo \"totais\" já com a soma de input_m3 e output_m3 do intervalo inteiro, calculada aqui — usa sempre esse total pronto quando perguntarem pelo total/soma do período, NUNCA somes tu mesma os valores diários (é aritmética decimal com muitas parcelas, fácil de errar). Cada dia em \"dias\" já vem com \"dia_semana\" (ex: \"Seg\", \"Dom\") calculado aqui — usa sempre esse valor para rotular o dia num relatório, NUNCA escrevas o dia da semana de memória: já aconteceu rotular uma segunda-feira como domingo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "description": "\"esta_semana\", \"semana_passada\", \"este_mes\", \"mes_passado\", ou o nome de um mês (ex: \"junho\", \"junho de 2026\") — omite se usares data_inicio/data_fim"},
                "data_inicio": {"type": "string", "description": "YYYY-MM-DD — só se não usares periodo"},
                "data_fim": {"type": "string", "description": "YYYY-MM-DD — só se não usares periodo"}
            }
        }
    }
]

# reler este documento do zero em toda avaliação de carga é caro — se for
# um PDF escaneado (sem texto extraível), _ler_conteudo tem de descrever
# cada página por visão, o que sozinho já demora bastante com várias
# páginas. Cache curta (mesmo período da lista de documentos, ver
# documentos_empresa._listar_bruto) para não repetir isto em cada pergunta
# seguida, sem deixar de refletir uma atualização feita há pouco.
_CACHE_MANUAL_QUALIDADE_TOROS = {}  # {"conteudo": (timestamp, dict)}
TTL_MANUAL_QUALIDADE_TOROS = 900  # segundos

def _normalizar_titulo(titulo: str) -> str:
    """Minúsculas, sem acentos, sem hífens — para comparar títulos sem
    depender de a equipa escrever sempre exatamente da mesma forma
    ("Ecos-Q" vs "Ecos Q", "análise" vs "analise")."""
    sem_acentos = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sem_acentos.lower().replace("-", " ")).strip()

def ler_manual_qualidade_cargas_toros() -> dict:
    """Lê o documento "Manual Qualidade de Cargas - Toros" (projeto Ecos
    Largos, em Documentos, no Basecamp — título exato confirmado
    diretamente pelo Rui em 2026-07-22, letra por letra) — as regras
    oficiais de qualidade para avaliar cargas de toros. Nota de histórico:
    esteve temporariamente a apontar para um título diferente ("Ecos-Q -
    Regras de Análise de Cargas"), com base numa informação que se veio a
    confirmar incorreta — a correspondência por título aceita agora ambos
    os nomes (mais "regras"+"análise"+"carga"), para tolerar futuras
    variações sem repetir este erro. Prefere o resultado cujo projeto seja
    mesmo "Ecos Largos" (evita confundir com um documento homónimo noutro
    projeto, se algum dia existir), mas não bloqueia se o campo de
    projeto não bater certo."""
    if "conteudo" in _CACHE_MANUAL_QUALIDADE_TOROS:
        ts, resultado_em_cache = _CACHE_MANUAL_QUALIDADE_TOROS["conteudo"]
        if time.time() - ts < TTL_MANUAL_QUALIDADE_TOROS:
            return resultado_em_cache

    def _candidatos(itens):
        encontrados = [
            item for item in itens
            if ("qualidade" in _normalizar_titulo(item["titulo"])
                and "toros" in _normalizar_titulo(item["titulo"]))
            or "ecos q" in _normalizar_titulo(item["titulo"])
            or ("regras" in _normalizar_titulo(item["titulo"])
                and "analise" in _normalizar_titulo(item["titulo"])
                and "carga" in _normalizar_titulo(item["titulo"]))
        ]
        da_ecos_largos = [c for c in encontrados if "ecos largos" in (c.get("projeto") or "").lower()]
        return da_ecos_largos or encontrados

    candidatos = _candidatos(documentos_empresa._listar_bruto())
    if not candidatos:
        # a lista de documentos fica em cache até 15 min (ver TTL em
        # documentos_empresa) — antes de desistir, tenta uma vez com a
        # lista mesmo atual, para não falhar só por causa de uma cache
        # desatualizada (documento criado/renomeado/partilhado há pouco).
        itens_frescos = documentos_empresa._listar_bruto(forcar=True)
        candidatos = _candidatos(itens_frescos)
        if not candidatos:
            print(f"[ecos_largos] manual não encontrado entre {len(itens_frescos)} "
                  "documentos/ficheiros visíveis à conta da Alma")
            return {"erro": "não encontrei o documento \"Manual Qualidade de Cargas - Toros\" — "
                             "confirma se o título ainda é esse no projeto Ecos Largos, e se está "
                             "partilhado com a conta da Alma no Basecamp"}
    item = candidatos[0]
    conteudo = documentos_empresa._ler_conteudo(item)
    if not conteudo:
        return {"erro": "este documento existe mas não consegui extrair texto legível dele",
                "titulo": item["titulo"], "app_url": item.get("app_url")}

    resultado = {"titulo": item["titulo"], "conteudo": conteudo}
    _CACHE_MANUAL_QUALIDADE_TOROS["conteudo"] = (time.time(), resultado)
    return resultado

TOOLS_MANUAL_QUALIDADE_TOROS = [
    {
        "name": "ler_manual_qualidade_cargas_toros",
        "description": "Lê o documento \"Manual Qualidade de Cargas - Toros\" (projeto Ecos Largos, em Documentos, no Basecamp) — as regras oficiais de qualidade para avaliar cargas de toros. Usa isto SEMPRE antes de responderes a qualquer pergunta sobre critérios, regras ou avaliação de qualidade de uma carga de toros, antes de dizeres que não tens essa informação — nunca respondas de memória nem inventes critérios que não estejam no documento. Lê sempre o conteúdo todo devolvido, não só o início.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]

# pedido do Rui: guardar TODAS as avaliações de cargas de toros, com os
# pontos importantes de cada uma (fornecedor, quantidade/peso, data,
# número do talão, avaliação), ao longo do ano — para poderem ser
# consultadas a qualquer momento (nesta ou em qualquer outra conversa, a
# memória não é por sessão) e para servirem de base ao ficheiro/resumo
# gerado no fim do ano (ver agents/resumo_anual_cargas_toros.py). O ano é
# sempre calculado aqui, nunca pelo modelo — a mesma razão de sempre:
# datas não se confiam ao modelo, ver tools/ecos_largos._resolver_data.
def guardar_avaliacao_carga_toros(fornecedor: str, avaliacao: str, quantidade: str = None,
                                  data_carga: str = None, talao: str = None) -> dict:
    """Guarda o registo de uma avaliação de qualidade de uma carga de
    toros, associado ao ano corrente — fica disponível para perguntas
    futuras (em qualquer conversa) sobre o histórico de avaliações, e
    entra no resumo/ficheiro gerado automaticamente no fim do ano. Usa
    isto sempre que terminares uma avaliação de qualidade de uma carga de
    toros, com os pontos importantes: `fornecedor` é obrigatório (usa
    "(fornecedor não identificado)" se não for mencionado nem ficar claro
    pelo contexto — nunca inventes um nome); `avaliacao` são os pontos
    mais importantes da avaliação em si (o que foi avaliado, se cumpre ou
    não as regras do manual, e porquê — direto, sem rodeios);
    `quantidade` (peso/quantidade da carga), `data_carga` e `talao`
    (número do talão) ficam de fora se não forem mencionados — não
    inventes valores para eles."""
    ano = date.today().year
    # coerção defensiva: o schema da tool já pede strings, mas o modelo por
    # vezes devolve um número (ex: quantidade=11850) — psycopg não converte
    # isso sozinho para a coluna TEXT, e a inserção falhava silenciosamente
    # do ponto de vista de quem pergunta (o erro ficava só nos logs do
    # Railway, e a Alma continuava a dizer "guardado" até à correção da
    # missão que proíbe essa alegação falsa).
    db.guardar_avaliacao_carga_toros(
        str(fornecedor) if fornecedor else "(fornecedor não identificado)",
        str(avaliacao), ano,
        quantidade=str(quantidade) if quantidade is not None else None,
        data_carga=str(data_carga) if data_carga is not None else None,
        talao=str(talao) if talao is not None else None)
    print(f"[ecos_largos] avaliação guardada: fornecedor={fornecedor!r} talao={talao!r} ano={ano}")
    return {"guardado": True, "ano": ano}

def resumo_avaliacoes_cargas_toros(ano: str = None, fornecedor: str = None) -> dict:
    """Lê as avaliações de cargas de toros guardadas (ver
    guardar_avaliacao_carga_toros) — usa isto sempre que perguntarem por um
    resumo ou histórico das avaliações feitas (ex: "quantas cargas foram
    avaliadas este ano", "resume as avaliações do fornecedor X", "o que
    encontrámos nas cargas da empresa Y"). Por omissão devolve o ano
    corrente; passa `ano` (ex: "2026") para outro ano. Passa `fornecedor`
    para filtrar só as avaliações desse fornecedor (corresponde por termo
    parcial, não é preciso o nome exato)."""
    try:
        ano_resolvido = int(ano) if ano else date.today().year
    except (TypeError, ValueError):
        return {"erro": f"não percebi o ano {ano!r} — usa um formato como \"2026\""}
    avaliacoes = db.avaliacoes_cargas_toros_ano(ano_resolvido)
    if fornecedor:
        termo = fornecedor.strip().lower()
        avaliacoes = [a for a in avaliacoes if termo in (a["fornecedor"] or "").lower()]
    return {"ano": ano_resolvido, "total": len(avaliacoes), "avaliacoes": avaliacoes}

TOOLS_AVALIACOES_CARGAS_TOROS = [
    {
        "name": "guardar_avaliacao_carga_toros",
        "description": "Guarda o registo de uma avaliação de qualidade de uma carga de toros, associado ao ano corrente — usa isto sempre que terminares uma avaliação de qualidade de uma carga de toros. O texto que escreveres no campo `avaliacao` é transmitido automaticamente, tal e qual, à pessoa (não precisas de o repetir separadamente na tua resposta) — por isso escreve ali a avaliação DETALHADA completa (critério a critério, com a tabela \"Cálculo do IGQC\", a classificação final, e a recomendação), nunca um resumo curto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fornecedor": {"type": "string", "description": "nome do fornecedor a quem pertence esta carga — usa \"(fornecedor não identificado)\" se não for mencionado, nunca inventes um nome"},
                "avaliacao": {"type": "string", "description": "a avaliação DETALHADA completa desta carga, em markdown: justificação critério a critério (com a pontuação de cada um), a tabela \"Cálculo do IGQC\" (Critério/Peso/Pontuação/Contribuição + Total ponderado), a percentagem e classificação final, e a secção \"Recomendação\". Este texto é mostrado tal e qual à pessoa — nunca um resumo, escreve-o por extenso"},
                "quantidade": {"type": "string", "description": "peso/quantidade da carga, se for mencionado"},
                "data_carga": {"type": "string", "description": "data da carga, se for mencionada"},
                "talao": {"type": "string", "description": "número do talão, se for mencionado"}
            },
            "required": ["fornecedor", "avaliacao"]
        }
    },
    {
        "name": "resumo_avaliacoes_cargas_toros",
        "description": "Lê as avaliações de cargas de toros guardadas — usa isto sempre que perguntarem por um resumo/histórico das avaliações feitas ao longo do ano, no geral ou de um fornecedor em concreto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ano": {"type": "string", "description": "ex: \"2026\" — omite para o ano corrente"},
                "fornecedor": {"type": "string", "description": "filtra só as avaliações deste fornecedor — omite para todas"}
            }
        }
    }
]
