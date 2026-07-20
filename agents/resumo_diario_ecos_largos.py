# agents/resumo_diario_ecos_largos.py — análise diária do dashboard de
# produção da Ecos Largos, publicada no Mural do projeto deles (não no da
# Gestão — a Ecos Largos é uma equipa à parte).
import json, threading
from persona import PERSONA
from agents.base import client
from tools import basecamp, ecos_largos

_a_correr = threading.Lock()

MISSAO_RESUMO_DIARIO_ECOS_LARGOS = PERSONA + """

Modo atual: análise diária do dashboard de produção da Ecos Largos,
publicada no Mural do projeto Ecos Largos (só visível a essa equipa, não à
Interior Guider).

Regras desta mensagem:
- Foca-te só no dashboard de produção — não é um resumo geral do projeto,
  nem inclui tarefas/cards.
- Tom direto e claro. Destaca sobretudo o que merece atenção: quedas ou
  quebras de padrão na produção, números fora do esperado. Se estiver tudo
  dentro do normal, diz isso brevemente, sem inventar problemas.
- Usa markdown (títulos, negrito, listas) — vai ser convertido em
  formatação real no Basecamp.
- Assina sempre como "— Alma"."""

def _gerar_resumo(conteudo_dashboard: str) -> str:
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system=MISSAO_RESUMO_DIARIO_ECOS_LARGOS,
        messages=[{"role": "user", "content": conteudo_dashboard}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def correr_resumo_diario_ecos_largos():
    """Lê o dashboard de produção e publica uma análise no Mural da Ecos
    Largos. Pensado para correr uma vez por dia depois das 19h (agendado),
    mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[resumo_diario_ecos_largos] já há uma corrida em curso — ignorado")
        return

    try:
        dados = ecos_largos.ler_dashboard_producao()
        if dados.get("erro"):
            print(f"[resumo_diario_ecos_largos] dashboard indisponível: {dados['erro']}")
            return

        conteudo = dados["conteudo"]
        # a API do dashboard devolve JSON estruturado (não texto), por isso
        # tem de ser serializado antes de seguir como conteúdo de mensagem
        if not isinstance(conteudo, str):
            conteudo = json.dumps(conteudo, ensure_ascii=False, indent=2)
        texto = _gerar_resumo(conteudo)
        basecamp.publicar_mural("Análise diária do dashboard de produção", texto, projeto="Ecos Largos")
        print("[resumo_diario_ecos_largos] publicado no mural da Ecos Largos")
    except Exception:
        import traceback
        print(f"[resumo_diario_ecos_largos] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()
