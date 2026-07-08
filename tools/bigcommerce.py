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
    """Pesquisa no catálogo. Devolve nome, descrição, preço, custo, stock, URL e variantes — só produtos visíveis na loja."""
    dados = _get(f"{_base_url()}/v3/catalog/products",
                 params={"keyword": termo, "limit": limite,
                         "include": "variants",
                         "include_fields": "name,description,price,cost_price,inventory_level,custom_url,sku,variants,is_visible"})
    produtos = dados.get("data", [])
    site = os.environ.get("SITE_URL", "").rstrip("/")
    resultado = []
    for produto in produtos:
        if not produto.pop("is_visible", True):
            continue  # produto oculto na loja — nunca mostrar nem mencionar
        produto["description"] = _texto_simples(produto.get("description"))
        caminho = (produto.pop("custom_url", None) or {}).get("url", "")
        produto["url"] = f"{site}{caminho}" if site else caminho
        resultado.append(produto)
    return resultado

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

def procurar_paginas(termo: str, limite: int = 5):
    """Pesquisa páginas estáticas do site (ex: sobre nós, entregas, garantias, FAQ)."""
    dados = _get(f"{_base_url()}/v3/content/pages",
                 params={"keyword": termo, "limit": limite,
                         "include_fields": "name,url,body,is_visible"})
    paginas = dados.get("data", [])
    return [
        {"nome": p.get("name"), "url": p.get("url"), "conteudo": _texto_simples(p.get("body"))[:1500]}
        for p in paginas if p.get("is_visible", True)
    ]

def procurar_posts_blog(termo: str, limite: int = 5):
    """Pesquisa posts do blog do site por palavra-chave no título ou corpo do texto."""
    # a API de blog não tem filtro de keyword nativo — traz os publicados mais
    # recentes (até 250) e filtra aqui; blogs maiores podem não ficar 100% cobertos.
    posts = _get(f"{_base_url()}/v2/blog/posts",
                 params={"is_published": "true", "limit": 250},
                 cache_key="blog_posts", ttl=TTL["catalogo"]) or []
    termo_lower = termo.lower()
    encontrados = []
    for post in posts:
        texto = _texto_simples(post.get("body", ""))
        if termo_lower in (post.get("title", "") + " " + texto).lower():
            encontrados.append({"titulo": post.get("title"), "url": post.get("url"), "resumo": texto[:1500]})
        if len(encontrados) >= limite:
            break
    return encontrados

TOOLS_COMUNS = [
    {
        "name": "procurar_produtos",
        "description": "Pesquisa produtos no catálogo BigCommerce por palavra-chave. Devolve nome, descrição (texto simples), preço de venda, preço de custo, stock, URL completo e a lista completa de variantes (sku, preço, custo, opções como cor/tamanho, stock por variante). Já exclui produtos ocultos na loja — o que é devolvido é sempre o que o cliente também vê. Se um produto tiver descrição e/ou variantes, vêm sempre incluídas nesta chamada — nunca é preciso perguntar ao utilizador ou especular se existem.",
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
        "name": "procurar_paginas",
        "description": "Pesquisa nas páginas de conteúdo estático do site por palavra-chave no nome ou no corpo da página. Só encontra páginas com texto simples guardado no BigCommerce — muitas páginas do site (ex: Método, Como Funciona, Academia, Planos) são construídas com o Page Builder e não têm corpo de texto pesquisável aqui. Se isto não devolver nada, usa listar_paginas_site + ler_pagina_site.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string"},
                "limite": {"type": "integer", "default": 5}
            },
            "required": ["termo"]
        }
    },
    {
        "name": "procurar_posts_blog",
        "description": "Pesquisa posts do blog do site por palavra-chave. Devolve título, URL e um resumo do texto de cada post encontrado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string"},
                "limite": {"type": "integer", "default": 5}
            },
            "required": ["termo"]
        }
    }
]

TOOL_RESUMO_VENDAS = {
    "name": "resumo_vendas",
    "description": "Resumo de vendas: total, número de encomendas e ticket médio nos últimos N dias.",
    "input_schema": {
        "type": "object",
        "properties": {"dias": {"type": "integer", "default": 30}},
        "required": []
    }
}
