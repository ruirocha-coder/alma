# tools/documentos_gerados.py — gera documentos formatados (PDF) a partir
# de markdown, para partilhar na conversa (ex: um relatório longo, uma
# proposta, um resumo estruturado de várias páginas) em vez de despejar
# tudo como texto corrido no chat. Guardado em Postgres, não em disco — o
# Railway não persiste ficheiros locais entre deploys — e servido pelo
# próprio endpoint de download em main.py.
import os
import markdown as _markdown
from xhtml2pdf import pisa
import io
import db

_CSS = """
<style>
  @page { size: A4; margin: 2.2cm; }
  body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #1a1a1a; }
  h1 { font-size: 20pt; margin-top: 0; }
  h2 { font-size: 15pt; margin-top: 1.2em; }
  h3 { font-size: 12.5pt; margin-top: 1em; }
  table { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
  th, td { border: 1px solid #999; padding: 6px 8px; text-align: left; }
  code { font-family: Courier, monospace; background: #f0f0f0; padding: 1px 4px; }
  pre { background: #f0f0f0; padding: 8px; }
</style>
"""

def _markdown_para_html(titulo: str, conteudo_markdown: str) -> str:
    corpo = _markdown.markdown(conteudo_markdown, extensions=["extra", "sane_lists"])
    return f"<html><head>{_CSS}</head><body><h1>{titulo}</h1>{corpo}</body></html>"

def gerar_pdf(titulo: str, conteudo_markdown: str) -> dict:
    """Gera um documento PDF formatado a partir de conteúdo em markdown
    (títulos, negrito, listas, tabelas — a mesma sintaxe que já usas nas
    respostas normais) e devolve um url para o partilhares na conversa.
    Usa isto sempre que o pedido for um documento longo/formal (um
    relatório, uma proposta, um resumo estruturado de várias páginas), ou
    sempre que pedirem explicitamente um PDF — em vez de escreveres tudo
    como texto corrido no chat. Inclui sempre o url devolvido na tua
    resposta em formato de link markdown (ex: "[título](url)"), para a
    pessoa poder abrir/descarregar o documento."""
    html = _markdown_para_html(titulo, conteudo_markdown)
    buffer = io.BytesIO()
    resultado = pisa.CreatePDF(html, dest=buffer)
    if resultado.err:
        return {"erro": "não consegui gerar o PDF a partir deste conteúdo"}
    id_gerado = db.guardar_documento_gerado(titulo, buffer.getvalue())
    url = f"{os.environ['ALMA_APP_URL'].rstrip('/')}/documentos-gerados/{id_gerado}"
    return {"titulo": titulo, "url": url}

TOOLS_DOCUMENTOS_GERADOS = [
    {
        "name": "gerar_pdf",
        "description": "Gera um documento PDF formatado a partir de conteúdo em markdown (títulos, negrito, listas, tabelas) e devolve um url para partilhares na conversa. Usa isto sempre que o pedido for um documento longo/formal (relatório, proposta, resumo de várias páginas) ou sempre que pedirem explicitamente um PDF, em vez de escreveres tudo como texto corrido no chat. Inclui sempre o url devolvido na tua resposta em formato de link markdown, ex: \"[título](url)\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "description": "título do documento"},
                "conteudo_markdown": {"type": "string", "description": "o conteúdo completo do documento, em markdown (títulos com #, negrito, listas, tabelas)"}
            },
            "required": ["titulo", "conteudo_markdown"]
        }
    }
]
