# tools/ecos_largos.py — recursos próprios da equipa Ecos Largos, uma
# equipa industrial parceira gerida no mesmo Basecamp mas com o seu próprio
# projeto, à parte da Interior Guider.
import os, re
from datetime import date, timedelta
import httpx

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
    return {"conteudo": dados}

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
