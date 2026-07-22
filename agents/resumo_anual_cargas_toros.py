# agents/resumo_anual_cargas_toros.py — no fim do ano, consolida todas as
# avaliações de cargas de toros guardadas ao longo do ano (ver
# tools/ecos_largos.guardar_avaliacao_carga_toros) num documento permanente
# no Basecamp — pedido explícito do Rui, para não se perder o histórico do
# ano assim que a conversa onde cada avaliação foi feita ficar esquecida.
import threading
from datetime import date
from persona import PERSONA
from agents.base import client
from tools import basecamp
import db

_a_correr = threading.Lock()

MISSAO_RESUMO_ANUAL_CARGAS_TOROS = PERSONA + """

Modo atual: consolidar num único documento todas as avaliações de
qualidade de cargas de toros guardadas ao longo do ano, para o projeto
Ecos Largos no Basecamp.

Regras deste documento:
- Organiza por cliente — para cada cliente, um resumo dos pontos mais
  importantes de todas as avaliações feitas às cargas dele durante o ano
  (problemas recorrentes, se cumpriu ou não as regras do manual, e
  quaisquer padrões que valha a pena assinalar).
- Não te limites a listar as avaliações uma a uma tal como vieram — destaca
  o que é mais relevante para quem for ler isto o ano todo depois.
- Usa markdown (títulos, negrito, listas) — vai ser convertido em
  formatação real no Basecamp.
- Termina com um resumo geral do ano (total de cargas avaliadas, clientes
  envolvidos, e qualquer tendência notável)."""

def _gerar_documento(ano: int, avaliacoes: list) -> str:
    linhas = [f"- Cliente: {a['cliente']} | Data: {a['data']} | Resumo: {a['resumo']}" for a in avaliacoes]
    conteudo = (f"Avaliações de cargas de toros guardadas em {ano} ({len(avaliacoes)} no total):\n\n"
                + "\n".join(linhas))
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=3000,
        system=MISSAO_RESUMO_ANUAL_CARGAS_TOROS,
        messages=[{"role": "user", "content": conteudo}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def correr_resumo_anual_cargas_toros():
    """Gera o documento anual de avaliações de cargas de toros e publica-o
    no Vault do projeto Ecos Largos, com um aviso no Mural a apontar para
    lá. Pensado para correr uma vez por ano (agendado a 31 de dezembro),
    mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[resumo_anual_cargas_toros] já há uma corrida em curso — ignorado")
        return

    try:
        ano = date.today().year
        avaliacoes = db.avaliacoes_cargas_toros_ano(ano)
        if not avaliacoes:
            print(f"[resumo_anual_cargas_toros] sem avaliações guardadas em {ano} — nada a gerar")
            return

        texto = _gerar_documento(ano, avaliacoes)
        titulo = f"Avaliações de Cargas de Toros — Resumo Anual {ano}"
        documento = basecamp.criar_documento(titulo, texto, projeto="Ecos Largos")
        basecamp.publicar_mural(
            f"Resumo anual de avaliações de cargas de toros ({ano})",
            f"Ficou pronto o resumo de todas as avaliações de cargas de toros deste ano — "
            f"consulta o documento completo: {documento.get('app_url', '')}",
            projeto="Ecos Largos"
        )
        print(f"[resumo_anual_cargas_toros] documento criado e anunciado no mural da Ecos Largos ({ano})")
    except Exception:
        import traceback
        print(f"[resumo_anual_cargas_toros] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()
