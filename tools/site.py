# tools/site.py — lê o site publicado diretamente, em vez da API do BigCommerce.
#
# Muitas páginas (Método, Como Funciona, Academia, Planos, ...) são construídas
# com o Page Builder do BigCommerce: o texto vive em widgets, não no campo
# "body" da API de Páginas, por isso procurar_paginas não as encontra. Lendo o
# HTML publicado funciona sempre, seja qual for a forma como a página foi feita.
import os, re, time
import httpx
from bs4 import BeautifulSoup

_cache = {}  # {url: (timestamp, texto_extraido)}
TTL_PAGINA = 1800  # segundos — páginas institucionais mudam pouco

def _site_url():
    return os.environ["SITE_URL"].rstrip("/")

def _get_texto(url, params=None, cache_key=None, ttl=TTL_PAGINA):
    if cache_key and cache_key in _cache:
        ts, texto = _cache[cache_key]
        if time.time() - ts < ttl:
            return texto
    r = httpx.get(url, params=params, timeout=30, follow_redirects=True)
    r.raise_for_status()
    if cache_key:
        _cache[cache_key] = (time.time(), r.text)
    return r.text

def _extrair_texto(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    texto = soup.body.get_text(separator=" ", strip=True) if soup.body else soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", texto).strip()

def _listar_sitemap(tipo: str):
    base = _site_url()
    urls = []
    pagina = 1
    while pagina <= 5:  # proteção contra sites enormes; o típico cabe numa página
        xml = _get_texto(f"{base}/xmlsitemap.php", params={"type": tipo, "page": pagina},
                          cache_key=f"sitemap_{tipo}_{pagina}")
        encontrados = re.findall(r"<loc>([^<]+)</loc>", xml)
        if not encontrados:
            break
        urls.extend(encontrados)
        if len(encontrados) < 200:  # abaixo do tamanho de página do sitemap: é a última
            break
        pagina += 1
    return urls

def listar_paginas_site():
    """Lista os URLs de todas as páginas institucionais e artigos (blog/academia) do site."""
    # "pages" = páginas institucionais; "news" = artigos de blog (a Academia, neste site)
    return _listar_sitemap("pages") + _listar_sitemap("news")

def ler_pagina_site(url: str):
    """Lê o texto principal de uma página do site, a partir do seu URL."""
    base = _site_url()
    if not url.startswith("http"):
        url = f"{base}/{url.lstrip('/')}"
    if not url.startswith(base):
        raise ValueError("O URL tem de pertencer ao domínio do site.")
    html = _get_texto(url, cache_key=url)
    return {"url": url, "conteudo": _extrair_texto(html)[:4000]}

TOOLS_SITE = [
    {
        "name": "listar_paginas_site",
        "description": "Lista os URLs de todas as páginas institucionais (Método, Como Funciona, Academia, Planos, Design de Interiores, Termos e Condições, etc.) e de todos os artigos de blog/Academia do site. Usa isto para descobrir que páginas ou artigos existem antes de leres um com ler_pagina_site — inclui sempre os artigos, mesmo que procurar_posts_blog não encontre nada.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "ler_pagina_site",
        "description": "Lê o texto completo de uma página do site publicado, a partir do seu URL (de listar_paginas_site ou procurar_paginas). Funciona mesmo para páginas construídas com o Page Builder, cujo texto não aparece em procurar_paginas — usa esta ferramenta sempre que precisares de saber mesmo o que uma página institucional diz.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    }
]
