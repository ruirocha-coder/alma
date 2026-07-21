# tools/ecos_largos.py — recursos próprios da equipa Ecos Largos, uma
# equipa industrial parceira gerida no mesmo Basecamp mas com o seu próprio
# projeto, à parte da Interior Guider.
import os, re, time
from datetime import date, timedelta
import httpx
from tools import documentos_empresa

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
  nunca apresentes os campos brutos taktrealm3h/taktneededm3h (estão em
  horas por m³), e nunca tentes converter tu mesma horas para min:seg, usa
  sempre o campo já convertido. Real maior que o objetivo = mais lento =
  motivo de alerta.
- Rácio de entrada/saída: apresenta sempre com exatamente duas casas
  decimais (ex: 2,70 — nunca 2,6961 nem 2,7)."""

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

_CAMPOS_TAKT = {
    "taktrealm3h": "takt_real_min_seg_por_m3",
    "taktneededm3h": "takt_objetivo_min_seg_por_m3",
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
    if "taktrealm3h" in conteudo and "taktneededm3h" in conteudo:
        try:
            conteudo["takt_alerta_mais_lento_que_objetivo"] = (
                float(conteudo["taktrealm3h"]) > float(conteudo["taktneededm3h"]))
        except (TypeError, ValueError):
            pass
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
    return {"conteudo": _com_takt_formatado(dados)}

def _semana_de(referencia: date) -> tuple:
    """Segunda a sexta da semana que contém `referencia`."""
    inicio = referencia - timedelta(days=referencia.weekday())
    return inicio, inicio + timedelta(days=4)

def _resolver_intervalo(periodo: str):
    """Resolve expressões relativas de período ("esta semana", "semana
    passada") para datas reais — tal como _resolver_data para um único dia,
    isto corre em Python porque o modelo não tem forma fiável de saber a
    data de hoje, e muito menos calcular semanas a partir dela (era isto que
    fazia pedidos por "a semana passada" virem com a semana errada).
    Devolve (None, None) se não reconhecer o período."""
    termo = periodo.strip().lower().replace(" ", "_")
    hoje = date.today()
    if termo == "esta_semana":
        return _semana_de(hoje)
    if termo == "semana_passada":
        return _semana_de(hoje - timedelta(days=7))
    return None, None

_LIMITE_DIAS_INTERVALO = 31

def ler_dashboard_producao_intervalo(periodo: str = None, data_inicio: str = None, data_fim: str = None) -> dict:
    """Lê os dados de produção dia a dia num intervalo — usa `periodo`
    ("esta_semana" ou "semana_passada", resolvido aqui para as datas reais)
    ou `data_inicio`/`data_fim` explícitos (YYYY-MM-DD) para outro
    intervalo qualquer."""
    if periodo:
        inicio, fim = _resolver_intervalo(periodo)
        if inicio is None:
            return {"erro": f"não percebi o período {periodo!r} — usa \"esta_semana\", "
                            "\"semana_passada\", ou indica data_inicio e data_fim (YYYY-MM-DD)"}
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
        dias.append({"data": d.isoformat(), **ler_dashboard_producao(d.isoformat())})
        d += timedelta(days=1)
    return {"inicio": inicio.isoformat(), "fim": fim.isoformat(), "dias": dias}

TOOLS_DASHBOARD_PRODUCAO = [
    {
        "name": "dashboard_producao_ecos_largos",
        "description": "Lê os dados de produção da Ecos Largos, da API oficial do dashboard (servida por um servidor próprio da equipa, fora do Basecamp). Sem argumentos devolve os dados mais recentes (agora); passa `data` para consultar um dia específico. Usa isto sempre que perguntarem pelo estado da produção, números de produção, entrada/receção de madeira, m3 (metros cúbicos) de madeira, quantidade recebida ou processada, linhas de produção, ou pedirem uma análise/resumo — de hoje ou de outro dia. Qualquer pergunta sobre estes números, mesmo em linguagem informal (ex: \"quanto entrou hoje\", \"quantos m3 tivemos\"), refere-se a este dashboard — usa sempre esta ferramenta antes de responder, nunca digas que não tens essa informação sem primeiro tentares. Mesmo que perguntem por algo mais específico que este dashboard não distinga (ex: um produto ou referência em concreto), tenta sempre primeiro consultar os dados gerais disponíveis e partilha-os — nunca respondas que não tens informação nenhuma sem teres consultado o dashboard.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "\"hoje\", \"ontem\", ou uma data no formato YYYY-MM-DD — omite para os dados mais recentes/agora"}
            }
        }
    },
    {
        "name": "dashboard_producao_ecos_largos_intervalo",
        "description": "Lê os dados de produção dia a dia num intervalo de datas — usa sempre esta ferramenta (nunca dashboard_producao_ecos_largos repetidamente com datas calculadas por ti) sempre que perguntarem por um período como \"esta semana\" ou \"a semana passada\": passa `periodo` com exatamente \"esta_semana\" ou \"semana_passada\" e as datas certas são calculadas aqui, nunca por ti — tu não sabes a data de hoje com fiabilidade. Para outro intervalo qualquer, usa `data_inicio` e `data_fim` (YYYY-MM-DD) em vez de `periodo`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "description": "\"esta_semana\" ou \"semana_passada\" — omite se usares data_inicio/data_fim"},
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

def ler_manual_qualidade_cargas_toros() -> dict:
    """Lê o documento "Manual Qualidade de Cargas - Toros" (projeto Ecos
    Largos, no Basecamp) — as regras oficiais de qualidade para avaliar
    cargas de toros. Prefere o resultado cujo projeto seja mesmo "Ecos
    Largos" (evita confundir com um documento homónimo noutro projeto, se
    algum dia existir), mas não bloqueia se o campo de projeto não bater
    certo — o título já é bastante específico por si só."""
    if "conteudo" in _CACHE_MANUAL_QUALIDADE_TOROS:
        ts, resultado_em_cache = _CACHE_MANUAL_QUALIDADE_TOROS["conteudo"]
        if time.time() - ts < TTL_MANUAL_QUALIDADE_TOROS:
            return resultado_em_cache

    candidatos = [
        item for item in documentos_empresa._listar_bruto()
        if "qualidade" in item["titulo"].lower() and "toros" in item["titulo"].lower()
    ]
    da_ecos_largos = [c for c in candidatos if "ecos largos" in (c.get("projeto") or "").lower()]
    candidatos = da_ecos_largos or candidatos
    if not candidatos:
        return {"erro": "não encontrei o documento \"Manual Qualidade de Cargas - Toros\" — "
                         "confirma se o título ainda é esse no projeto Ecos Largos"}
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
        "description": "Lê o documento \"Manual Qualidade de Cargas - Toros\" (projeto Ecos Largos, Basecamp) — as regras oficiais de qualidade para avaliar cargas de toros. Usa isto SEMPRE antes de responderes a qualquer pergunta sobre critérios, regras ou avaliação de qualidade de uma carga de toros, antes de dizeres que não tens essa informação — nunca respondas de memória nem inventes critérios que não estejam no documento. Lê sempre o conteúdo todo devolvido, não só o início.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]
