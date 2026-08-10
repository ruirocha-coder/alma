import threading
from datetime import date
from persona import PERSONA
from agents.base import client
from tools import basecamp, procedimentos
import db

_a_correr = threading.Lock()

# Itens com mais de tantos dias de atraso são tratados como conteúdo
# esquecido/abandonado, não trabalho realmente pendente — não geram alerta.
IDADE_MAXIMA_DIAS = 60

# Máximo de alertas novos publicados por corrida, para não inundar o
# Basecamp de uma vez; o que ficar de fora entra na corrida seguinte.
MAX_ALERTAS_POR_CORRIDA = 15

MISSAO_MONITOR = PERSONA + """

Modo atual: monitorização automática do Basecamp. Vais publicar um único
comentário na tarefa/card abaixo, sinalizando o atraso e, quando fizer
sentido, uma sugestão relacionada com os procedimentos da empresa.

Regras deste comentário:
- Curto (3 a 5 linhas), tom calmo e construtivo — nunca acusatório.
- Refere quantos dias de atraso tem.
- Usa o estado/coluna e os responsáveis para dares um alerta mais preciso
  (ex: se já está numa coluna de aprovação/revisão, isso muda o que faz
  sentido dizer). Não inventes o nome de quem é responsável se não vier
  na lista de responsáveis.
- Quando houver responsável(is) na lista, escreve o nome como "@Nome
  Completo" (ex: "@Rui Rocha") em vez de texto simples — se corresponder a
  alguém real, vira uma menção a sério que a notifica, o que é o objetivo
  de um alerta de atraso.
- Lê sempre as notas da tarefa/card — costumam ter contexto essencial
  (o que falta, condições combinadas) que muda o que faz sentido dizer.
- Se os comentários já existentes explicarem o atraso (ex: à espera de
  aprovação, cliente não respondeu), reconhece isso em vez de repetir o óbvio.
- Se os procedimentos da empresa (abaixo, quando disponíveis) forem
  relevantes para este caso, aponta o que deveria ter acontecido segundo
  esses procedimentos.
- Termina sempre a assinar como "— Alma", para ficar claro que é automático.
- Escreve só o comentário em si — sem saudações, sem perguntas, isto não é
  uma conversa."""

def _procedimentos_ou_aviso() -> str:
    try:
        return procedimentos.procedimentos_empresa()
    except Exception as e:
        print(f"[monitor_basecamp] procedimentos indisponíveis: {e!r}")
        return "(Documento de procedimentos ainda não está configurado.)"

def _linha_historico(item: dict) -> str:
    if "conteudo" in item:
        return f"- {item['autor']}: {item['conteudo']}"
    return f"- {item['autor']} {item['acao']}"

def _gerar_comentario(item: dict, comentarios: list, eventos: list, procedimentos_texto: str) -> str:
    historico_itens = basecamp.ordenar_por_data(comentarios, eventos)
    historico = "\n".join(_linha_historico(c) for c in historico_itens) or "(sem comentários ainda)"
    contexto = f"""Tarefa/card: {item['titulo']}
Projeto: {item['projeto']}
Tipo: {item['tipo']}
Estado/coluna: {item.get('estado') or '(sem estado)'}
Responsáveis: {', '.join(item.get('responsaveis') or []) or '(sem responsável atribuído)'}
Prazo: {item['prazo']} ({item['dias_atraso']} dias de atraso)

Notas da tarefa/card:
{item.get('notas') or '(sem notas)'}

Histórico (comentários e mudanças de coluna/estado, por ordem cronológica):
{historico}

Procedimentos da empresa:
{procedimentos_texto}"""
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system=MISSAO_MONITOR,
        messages=[{"role": "user", "content": contexto}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def correr_monitorizacao() -> list[dict]:
    """Verifica tarefas/cards atrasados no Basecamp e publica um alerta em cada um (uma vez por prazo).

    Contas com muito histórico podem ter milhares de itens em aberto — isto
    pode demorar vários minutos (o Basecamp não permite filtrar por prazo no
    servidor). É sempre corrido em segundo plano (agendado ou via
    /basecamp/monitorizar), nunca a bloquear um pedido HTTP."""
    if not _a_correr.acquire(blocking=False):
        print("[monitor_basecamp] já há uma corrida em curso — ignorado")
        return [{"erro": "já está a correr uma monitorização", "ok": False}]

    try:
        pausa = db.pausa_automatica_ativa(date.today())
        if pausa:
            print(f"[monitor_basecamp] pausa automática ativa ({pausa['motivo']}, "
                  f"até {pausa['data_fim']}) — sem alertas hoje")
            return [{"info": f"pausa automática ativa: {pausa['motivo']}", "ok": True}]

        procedimentos_texto = _procedimentos_ou_aviso()

        try:
            itens = basecamp.tarefas_e_cards_atrasados()
        except Exception as e:
            print(f"[monitor_basecamp] não foi possível obter tarefas do Basecamp: {e!r}")
            return [{"erro": str(e), "ok": False}]

        print(f"[monitor_basecamp] {len(itens)} itens atrasados encontrados")

        elegiveis = [i for i in itens
                    if i["dias_atraso"] <= IDADE_MAXIMA_DIAS and not db.ja_alertado(i["id"], i["prazo"])]
        elegiveis.sort(key=lambda i: -i["dias_atraso"])  # mais urgentes primeiro
        a_processar = elegiveis[:MAX_ALERTAS_POR_CORRIDA]
        print(f"[monitor_basecamp] {len(elegiveis)} elegíveis (≤{IDADE_MAXIMA_DIAS}d, ainda não alertados), "
              f"a processar {len(a_processar)} nesta corrida")

        resultado = []
        for item in a_processar:
            try:
                comentarios = basecamp.ler_comentarios(item["comments_url"]) if item["comments_url"] else []
                eventos = basecamp.ler_eventos(item["id"])
                texto = _gerar_comentario(item, comentarios, eventos, procedimentos_texto)
                basecamp.comentar(item["id"], texto, projeto=item["projeto"])
                db.registar_alerta(item["id"], item["prazo"], texto)
                resultado.append({"item": item, "comentario": texto, "ok": True})
                print(f"[monitor_basecamp] comentado: {item['titulo']} ({item['dias_atraso']}d atraso)")
            except Exception as e:
                print(f"[monitor_basecamp] falhou para {item.get('id')}: {e!r}")
                resultado.append({"item": item, "erro": str(e), "ok": False})
        print(f"[monitor_basecamp] concluído — {len(resultado)} alertas novos")
        return resultado
    finally:
        _a_correr.release()
