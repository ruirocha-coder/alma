# tools/ecos_largos.py — recursos próprios da equipa Ecos Largos, uma
# equipa industrial parceira gerida no mesmo Basecamp mas com o seu próprio
# projeto, à parte da Interior Guider.
import os
import httpx
from bs4 import BeautifulSoup

# servidor próprio da equipa (fora do Basecamp) — configurável por env var
# porque corre num endereço DuckDNS, que pode mudar sem precisar de deploy.
DASHBOARD_PRODUCAO_URL = os.environ.get(
    "ECOS_LARGOS_DASHBOARD_URL",
    "http://ecoslargos.duckdns.org:9000/server/dashboard.php"
)

def _tabelas_para_texto(soup: BeautifulSoup) -> str:
    """Converte as tabelas HTML da página em texto linha a linha — extrair só
    o texto corrido (get_text) perderia o alinhamento de colunas, que é onde
    está a informação real de um dashboard de produção."""
    blocos = []
    for tabela in soup.find_all("table"):
        linhas = []
        for tr in tabela.find_all("tr"):
            celulas = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(celulas):
                linhas.append(" | ".join(celulas))
        if linhas:
            blocos.append("\n".join(linhas))
    return "\n\n".join(blocos)

def ler_dashboard_producao() -> dict:
    """Lê o dashboard de produção da Ecos Largos — uma página servida por um
    servidor próprio da equipa (fora do Basecamp), por isso menos fiável do
    que uma chamada normal à API: pode estar offline ou inacessível, o que
    devolve um erro em vez de rebentar quem chamar isto."""
    try:
        r = httpx.get(DASHBOARD_PRODUCAO_URL, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return {"erro": f"não consegui aceder ao dashboard de produção: {e}"}

    soup = BeautifulSoup(r.text, "html.parser")
    tabelas = _tabelas_para_texto(soup)
    if tabelas:
        return {"conteudo": tabelas}

    # sem tabelas reconhecíveis — cai para o texto simples da página toda
    texto = soup.get_text("\n", strip=True)
    if not texto:
        return {"erro": "o dashboard de produção respondeu, mas não consegui extrair conteúdo legível"}
    return {"conteudo": texto}

TOOLS_DASHBOARD_PRODUCAO = [
    {
        "name": "dashboard_producao_ecos_largos",
        "description": "Lê o dashboard de produção da Ecos Largos (dados de produção, servidos por um servidor próprio da equipa, fora do Basecamp). Usa isto sempre que perguntarem pelo estado da produção, números de produção, ou pedirem uma análise/resumo do dashboard.",
        "input_schema": {"type": "object", "properties": {}}
    }
]
