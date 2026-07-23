# agents/sugestao_logistica_semanal.py — sugestão semanal de organização
# das entregas, pedida explicitamente pelo Rui (2026-07-23): toda
# segunda de manhã, publica no Mural "Programação" do projeto Entregas
# uma sugestão de como organizar a semana de entregas, dirigida à
# Conceição Costa (e só a ela).
#
# Os cards já em "On Hold" nas colunas Lisboa/Porto/Outro significam que
# a encomenda já foi feita ao fornecedor e o produto já está em armazém,
# pronto a ser entregue (ver tools.logistica.fase_encomenda) — é isso que
# esta sugestão organiza: que dia visitar cada região, e por que ordem
# dentro de cada dia. Nunca calcula uma rota otimizada real (sem API de
# mapas configurada neste projeto, e sem necessidade pedida) — só agrupa
# por dia/região e sugere uma ordem sensata dentro de cada dia.
import threading
from datetime import date, timedelta
from agents.base import client
from agents.logistica_entregas import _extrair_dados_encomenda
from tools import basecamp, logistica

_a_correr = threading.Lock()
MAX_CARDS_POR_CORRIDA = 40

# nome completo tal como já usado nas menções existentes (ver
# tools/logistica.gerar_texto_condicao_fixa) — mantém-se o mesmo em toda
# a aplicação, para a menção ser sempre resolvida para a mesma pessoa.
RESPONSAVEL_MENCAO = "Conceição Costa"

# nome real da 3ª coluna confirmado ao vivo no Basecamp (2026-07-23):
# "Outro", no singular — "outros" também aceite ao ler a coluna (ver
# _COLUNA_PARA_REGIAO), por tolerância a uma futura renomeação.
_REGIOES = ("Lisboa", "Porto", "Outro")
_COLUNA_PARA_REGIAO = {"lisboa": "Lisboa", "porto": "Porto", "outro": "Outro", "outros": "Outro"}

def _semana_atual() -> tuple:
    """Segunda a sexta da semana corrente — calculado aqui, nunca pelo
    modelo (a mesma razão de sempre: aritmética de datas não se confia à
    IA, ver tools/ecos_largos._semana_de para o mesmo padrão)."""
    hoje = date.today()
    inicio = hoje - timedelta(days=hoje.weekday())
    return inicio, inicio + timedelta(days=4)

def _formatar_card_pronto(titulo: str, dados: dict) -> str:
    data_entrega = dados.get("data_entrega_cliente")
    return (
        f"- **{titulo}**\n"
        f"  Cliente: {dados.get('cliente') or '(não identificado)'}\n"
        f"  Morada: {dados.get('morada') or '(não identificada — verificar notas do card)'}\n"
        f"  Encomendado: {dados.get('produtos_encomendados') or '(não identificado)'}\n"
        f"  Data prevista de entrega: {data_entrega.isoformat() if data_entrega else '(não identificada)'}"
    )

def _gerar_texto_sugestao(cards_por_regiao: dict, inicio_semana: date, fim_semana: date) -> str:
    blocos = [f"### {regiao} ({len(cards)} pronta(s) a entregar)\n" + "\n\n".join(cards)
             for regiao, cards in cards_por_regiao.items() if cards]
    contexto = "\n\n".join(blocos) if blocos else "(nenhum card pronto a entregar esta semana, em nenhuma região)"

    missao = f"""Preparas, para a Conceição Costa (responsável pela logística de
entregas da Interior Guider / Boa Safra), uma sugestão semanal de
organização das entregas — a publicar no Mural "Programação" do projeto
Entregas no Basecamp. Semana de {inicio_semana.strftime('%d/%m/%Y')} a
{fim_semana.strftime('%d/%m/%Y')}.

Abaixo estão os cards já em "On Hold" nas colunas Lisboa, Porto e Outros —
significa que a encomenda já foi feita ao fornecedor e o produto já está
em armazém, pronto a ser entregue.

{contexto}

Organiza uma sugestão de calendário para a semana: que dia(s) visitar
cada região (agrupa sempre por região — nunca misturar Lisboa e Porto no
mesmo dia), e dentro de cada dia, sugere uma ordem sensata de visita
pelos endereços (usando o teu conhecimento geral da zona/ruas
mencionadas). Isto NÃO é uma rota GPS otimizada com distâncias/tempos —
é só uma organização sensata para não andar às voltas; diz isso
claramente se não tiveres informação suficiente para ordenar com
confiança. Para cada entrega, inclui sempre o cliente, a morada, o que
foi encomendado, e a data prevista de entrega, exatamente como aparecem
acima — nunca inventes nem alteres estes dados.

Se não houver nenhum card pronto nalguma região, ou nenhum de todo, diz
isso claramente em vez de inventar entregas.

Termina sempre a mensagem a mencionar "@Conceição Costa" (e só ela,
nenhuma outra pessoa) — é a destinatária desta sugestão.

Escreve só o texto final da mensagem do mural, sem comentário à parte."""
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        system=missao, messages=[{"role": "user", "content": "Gera a sugestão semanal."}]
    )
    texto = "".join(b.text for b in resposta.content if b.type == "text").strip()
    if RESPONSAVEL_MENCAO not in texto:
        texto += f"\n\n@{RESPONSAVEL_MENCAO}"
    return texto

def correr_sugestao_semanal_logistica() -> dict:
    """Uma corrida da sugestão semanal de logística de entregas: lê os
    cards ativos do projeto "Entregas", filtra os que estão prontos a
    entregar (On Hold nas colunas Lisboa/Porto/Outros), e publica uma
    sugestão de organização no Mural "Programação", dirigida à Conceição
    Costa. Pensado para correr às segundas de manhã (agendado), mas pode
    ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[sugestao_logistica_semanal] já há uma corrida em curso — ignorado")
        return {"erro": "já está a correr uma sugestão semanal"}

    try:
        try:
            itens = [i for i in basecamp._itens_ativos()
                    if i.get("type") == "Kanban::Card"
                    and logistica.PROJETO_ENTREGAS.lower() in ((i.get("bucket") or {}).get("name") or "").lower()]
        except Exception as e:
            print(f"[sugestao_logistica_semanal] não foi possível obter os cards do Basecamp: {e!r}")
            return {"erro": str(e)}

        itens = itens[:MAX_CARDS_POR_CORRIDA]
        cards_por_regiao = {regiao: [] for regiao in _REGIOES}

        for item in itens:
            estado = ((item.get("parent") or {}).get("title") or "").strip()
            regiao = _COLUNA_PARA_REGIAO.get(estado.lower())
            if regiao is None:
                continue
            on_hold = logistica.esta_em_on_hold(item)
            if logistica.fase_encomenda(estado, on_hold) != "pronto_entrega":
                continue

            titulo = item.get("title") or item.get("content") or "(sem título)"
            notas = basecamp._texto_simples(item.get("description", ""))
            try:
                dados = _extrair_dados_encomenda(titulo, notas)
            except Exception as e:
                print(f"[sugestao_logistica_semanal] falhou a extrair dados de {item['id']}: {e!r}")
                dados = {}

            cards_por_regiao[regiao].append(_formatar_card_pronto(titulo, dados))

        inicio_semana, fim_semana = _semana_atual()
        texto = _gerar_texto_sugestao(cards_por_regiao, inicio_semana, fim_semana)
        basecamp.publicar_mural("Sugestão de logística semanal", texto, projeto=logistica.PROJETO_ENTREGAS)

        contagens = {regiao: len(cards) for regiao, cards in cards_por_regiao.items()}
        total_prontos = sum(contagens.values())
        print(f"[sugestao_logistica_semanal] publicado — {total_prontos} entrega(s) pronta(s): {contagens}")
        return {"total_prontos": total_prontos, "por_regiao": contagens}
    finally:
        _a_correr.release()
