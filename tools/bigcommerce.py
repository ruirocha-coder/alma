import httpx, os, re, time

_cache = {}  # {chave: (timestamp, dados)}
TTL = {"catalogo": 900, "encomendas": 300}  # segundos

def _base_url():
    return f"https://api.bigcommerce.com/stores/{os.environ['BIGCOMMERCE_STORE_HASH']}"

def _headers():
    return {"X-Auth-Token": os.environ["BIGCOMMERCE_ACCESS_TOKEN"], "Accept": "application/json"}

def _get(url, params=None, cache_key=None, ttl=900):
    if cache_key and cache_key in _cache:
        ts, dados = _cache[cache_key]
        if time.time() - ts < ttl:
            return dados
    r = httpx.get(url, headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    dados = r.json()
    if cache_key:
        _cache[cache_key] = (time.time(), dados)
    return dados

def _texto_simples(html: str) -> str:
    """A descrição do BigCommerce vem em HTML; simplifica para texto corrido."""
    if not html:
        return html
    texto = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", texto).strip()

def procurar_produtos(termo: str, limite: int = 10):
    """Pesquisa no catálogo. Devolve nome, descrição, preço, custo, stock, URL e variantes."""
    dados = _get(f"{_base_url()}/v3/catalog/products",
                 params={"keyword": termo, "limit": limite,
                         "include": "variants",
                         "include_fields": "name,description,price,cost_price,inventory_level,custom_url,sku,variants"})
    produtos = dados.get("data", [])
    for produto in produtos:
        produto["description"] = _texto_simples(produto.get("description"))
    return produtos

def encomendas_recentes(dias: int = 30):
    """Encomendas dos últimos N dias (API V2 de orders)."""
    from datetime import datetime, timedelta
    desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S")
    return _get(f"{_base_url()}/v2/orders",
                params={"min_date_created": desde, "limit": 250},
                cache_key=f"orders_{dias}", ttl=TTL["encomendas"])

def resumo_vendas(dias: int = 30):
    """Total de vendas, nº de encomendas, ticket médio."""
    orders = encomendas_recentes(dias) or []
    total = sum(float(o["total_inc_tax"]) for o in orders)
    n = len(orders)
    return {"periodo_dias": dias, "total_eur": round(total, 2),
            "n_encomendas": n, "ticket_medio": round(total / n, 2) if n else 0}
TOOLS_CEO = [
    {
        "name": "procurar_produtos",
        "description": "Pesquisa produtos no catálogo BigCommerce por palavra-chave. Devolve nome, descrição (texto simples), preço de venda, preço de custo, stock, URL e a lista completa de variantes (sku, preço, custo, opções como cor/tamanho, stock por variante). Se um produto tiver descrição e/ou variantes, vêm sempre incluídas nesta chamada — nunca é preciso perguntar ao utilizador ou especular se existem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string"},
                "limite": {"type": "integer", "default": 10}
            },
            "required": ["termo"]
        }
    },
    {
        "name": "resumo_vendas",
        "description": "Resumo de vendas: total, número de encomendas e ticket médio nos últimos N dias.",
        "input_schema": {
            "type": "object",
            "properties": {"dias": {"type": "integer", "default": 30}},
            "required": []
        }
    }
]
