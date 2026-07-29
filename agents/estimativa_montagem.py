# agents/estimativa_montagem.py — estimativa de tempo de montagem e custo de
# deslocação para as entregas, seguindo o "Procedimento Tempos de Montagem
# para Logística" (projeto Alma Data, Basecamp) — pedido explícito do Rui
# (2026-07-28). Toda a aritmética vive em tools/tempos_montagem.py (nunca
# pedida a um LLM); aqui só a orquestração: ler o PDF da encomenda, extrair
# os dados que exigem julgamento semântico (classificar artigos, ler um
# valor, detetar acréscimos/fatores do local), publicar a estimativa como
# comentário no card, e mais tarde ler o "Real" registado pela equipa para
# calibrar os parâmetros.
#
# Ciclo completo do documento (§6-8): (1) antes da entrega, publica a
# estimativa como comentário na tarefa; (2) a equipa regista o real depois
# de entregar; (3) de 2 em 2 meses, compara-se estimativa vs. real e
# reporta-se o desvio — nunca ajusta parâmetros sozinha, só relata (quem
# decide mudar um parâmetro é sempre uma pessoa, via atualizar_parametro_estimativa).
import json
from datetime import datetime, timezone
from agents.base import client
from tools import basecamp, documentos_empresa, logistica, tempos_montagem
import db

_MISSAO_EXTRACAO_ITENS = """Extrais dados estruturados do texto de uma
encomenda de mobiliário (PDF da encomenda, ou notas do card se não houver
PDF), para calcular o tempo de montagem previsto, seguindo o "Procedimento
Tempos de Montagem para Logística" da Interior Guider / Boa Safra.

Responde APENAS com um objeto JSON, sem mais nenhum texto antes ou depois,
com exatamente estas chaves:
{"itens": [{"grupo": "ligeiro"|"normal"|"pesado"|null, "quantidade": integer, "descricao": string}],
 "acrescimos": {"pecas_fixas_parede": integer, "candeeiros_teto": integer, "moveis_desmontados_inesperados": integer},
 "cortinados_presentes": boolean,
 "valor_total_encomenda": number ou null,
 "fatores_local": {"sem_elevador": boolean ou null, "obra": boolean ou null, "centro_historico": boolean ou null},
 "confianca": "alta"|"media"|"baixa",
 "notas_confianca": string ou null}

Classificação dos artigos (grupo, minutos por artigo só para referência —
não calcules tu os minutos, isso é feito à parte):
- "ligeiro" (10 min): decoração, tapetes, almofadas, candeeiros de mesa e de
  pé, candeeiros de TETO também entram aqui como artigo base, cadeiras,
  mesas de apoio, espelhos pequenos.
- "normal" (30 min): sofás, poltronas, mesas de jantar e de centro, camas,
  colchões, cómodas, aparadores, mesas de cabeceira.
- "pesado" (75 min): roupeiros, estantes, móveis desmontados a montar no
  local, peças à medida.

Acréscimos (contam-se À PARTE dos itens, nunca em vez deles):
- "pecas_fixas_parede": quantas peças à medida ficam fixas à parede
  (estante, roupeiro, painel) — cada uma é TAMBÉM um item "pesado" normal, o
  acréscimo é só a fixação em si.
- "candeeiros_teto": quantos candeeiros de teto existem (ligação elétrica)
  — IMPORTANTE: um candeeiro de teto conta as DUAS coisas — é também um item
  "ligeiro" na lista de itens (não o omitas de "itens" só porque também está
  aqui) — e este número é só a contagem para o acréscimo elétrico.
- "moveis_desmontados_inesperados": móveis entregues desmontados que eram
  esperados montados.

Cortinados são sempre em outsourcing — nunca entram em "itens" nem em
"acrescimos"; se existirem, marca "cortinados_presentes": true.

"valor_total_encomenda": o valor monetário total da encomenda, tal como
aparece no texto (nunca calculado, nunca inventado — null se não
encontrares).

"fatores_local": só true/false quando o texto disser isso explicitamente
(ex: "sem elevador", "obra a decorrer", "centro histórico", "sem lugar para
carga") — usa null quando não for mencionado (nunca assumas false por
omissão, null é o valor correto para "não sei").

"confianca": "alta" se todos os artigos foram reconhecidos com confiança e a
morada/acesso é conhecida; "media" se algum artigo ficou por classificar ou
algum dado não foi encontrado; "baixa" se muitos artigos são peças à medida
ou a informação é escassa. "notas_confianca" explica brevemente porquê,
nunca null se confianca não for "alta"."""

def _chamar_extracao_itens(texto: str) -> str:
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        system=_MISSAO_EXTRACAO_ITENS,
        messages=[{"role": "user", "content": texto[:15000]}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def _limpar_bloco_codigo(texto: str) -> str:
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else texto[3:]
        if texto.endswith("```"):
            texto = texto[:-3]
    return texto.strip()

def _extrair_itens_montagem(texto: str) -> dict:
    """Extrai, via LLM, os dados de uma encomenda necessários para a Conta
    A/B (ver tools.tempos_montagem) — nunca inventa um valor que não
    encontrar no texto."""
    if not texto or not texto.strip():
        return {"itens": [], "acrescimos": {}, "valor_total_encomenda": None,
                "fatores_local": {}, "confianca": "baixa",
                "notas_confianca": "sem texto disponível (nem PDF nem notas) para extrair dados"}
    texto_resposta = _limpar_bloco_codigo(_chamar_extracao_itens(texto))
    try:
        dados = json.loads(texto_resposta)
    except ValueError:
        print(f"[estimativa_montagem] extração não devolveu JSON válido: {texto_resposta[:200]!r}")
        return {"itens": [], "acrescimos": {}, "valor_total_encomenda": None,
                "fatores_local": {}, "confianca": "baixa",
                "notas_confianca": "falha a extrair dados estruturados da encomenda"}
    return dados

def _texto_pdf_encomenda(item: dict) -> str:
    """Lê o PDF da encomenda anexado ao card (descrição ou comentário) — ver
    tools.documentos_empresa.ler_anexos_registo_basecamp, já corrigida para
    verificar description_attachments e content_attachments. Cai para as
    notas do card em texto simples se não houver nenhum PDF anexado."""
    try:
        anexos = documentos_empresa.ler_anexos_registo_basecamp(item["url"])
        textos = [a["conteudo"] for a in (anexos.get("anexos") or []) if a.get("conteudo")]
        if textos:
            return "\n\n".join(textos)
    except Exception as e:
        print(f"[estimativa_montagem] não consegui ler anexos de {item.get('id')}: {e!r}")
    return basecamp._texto_simples(item.get("description", ""))

def _texto_estimativa(titulo: str, conta_a: dict, rendimento: dict, confianca: str,
                      notas_confianca: str, validacoes: list) -> str:
    linhas = [f"**Estimativa de montagem — {titulo}**", ""]
    linhas.append("Conta A (decomposição):")
    linhas.extend(f"- {l}" for l in conta_a["decomposicao"])
    minutos = conta_a["minutos"]
    linhas.append(f"- **Total: {minutos:.0f} min ≈ {minutos / 60:.1f}h**")
    linhas.append("")
    if rendimento["euros_hora"] is not None:
        linhas.append(f"Conta B (verificação pelo valor): {rendimento['euros_hora']:.0f} €/h "
                      f"→ banda \"{rendimento['banda']}\"")
    else:
        linhas.append("Conta B (verificação pelo valor): não disponível (falta o valor da encomenda)")
    linhas.append("")
    linhas.append(f"Confiança: {confianca}" + (f" — {notas_confianca}" if notas_confianca else ""))
    if validacoes:
        linhas.append("")
        linhas.append("⚠️ Precisa de validação humana:")
        linhas.extend(f"- {v}" for v in validacoes)
    return "\n".join(linhas)

def _estimar_e_publicar_card(item: dict, projeto: str, texto_pdf: str = None) -> dict:
    """Calcula e publica a estimativa de montagem de um card, se ainda não
    tiver sido publicada (ver db.estimativa_existente) — chamado pela
    sugestão semanal para cada card pronto a entregar. Devolve a
    estimativa (sempre com "minutos" e "rendimento" — Conta A e Conta B,
    ver tools.tempos_montagem — quer tenha acabado de ser publicada agora,
    quer já existisse de uma corrida anterior — pedido do Rui, 2026-07-28:
    a proposta de agendamento precisa do tempo de montagem de TODAS as
    entregas prontas, não só das que ainda não tinham estimativa), ou None
    se não foi possível calcular nada (ex: falha ao publicar o comentário).

    `texto_pdf`, quando fornecido, é usado em vez de ler o PDF outra vez
    (ver _texto_pdf_encomenda) — pedido do Rui (2026-07-29): a sugestão
    semanal já lê o PDF de cada card para extrair os dados da encomenda
    (produtos, cliente, etc. — nunca a morada), e reaproveita aqui esse
    mesmo texto para nunca ler e processar o mesmo PDF duas vezes."""
    recording_id = item["id"]
    existente = db.estimativa_existente(recording_id)
    if existente:
        rendimento = (existente.get("decomposicao") or {}).get("rendimento") or {"euros_hora": None, "banda": None}
        return {"recording_id": recording_id, "minutos": float(existente["estimativa_minutos"]),
                "rendimento": rendimento, "ja_publicada": True}

    titulo = item.get("title") or item.get("content") or "(sem título)"
    texto_fonte = texto_pdf if texto_pdf is not None else _texto_pdf_encomenda(item)
    extraido = _extrair_itens_montagem(texto_fonte)
    parametros = db.obter_parametros_estimativa()

    conta_a = tempos_montagem.calcular_conta_a(
        extraido.get("itens") or [], extraido.get("acrescimos") or {},
        pessoas=2, fatores_local=extraido.get("fatores_local") or {}, parametros=parametros)
    valor = extraido.get("valor_total_encomenda")
    rendimento = tempos_montagem.calcular_rendimento(valor, conta_a["minutos"], parametros)

    fatores_local = extraido.get("fatores_local") or {}
    acesso_desconhecido = fatores_local.get("sem_elevador") is None
    tem_peca_fixa_parede = bool((extraido.get("acrescimos") or {}).get("pecas_fixas_parede"))
    validacoes = tempos_montagem.validacoes_necessarias(
        rendimento["banda"], tem_peca_fixa_parede, conta_a["itens_nao_classificados"], acesso_desconhecido)

    confianca = extraido.get("confianca") or "baixa"
    texto = _texto_estimativa(titulo, conta_a, rendimento, confianca,
                              extraido.get("notas_confianca"), validacoes)

    try:
        basecamp.comentar(recording_id, texto, projeto=projeto)
    except Exception as e:
        print(f"[estimativa_montagem] falhou a publicar estimativa para {recording_id}: {e!r}")
        return None

    db.registar_estimativa_montagem(
        recording_id, titulo, item.get("url"), item.get("comments_url"),
        conta_a["minutos"], valor,
        {"decomposicao": conta_a["decomposicao"], "rendimento": rendimento, "extraido": extraido},
        confianca)
    print(f"[estimativa_montagem] estimativa publicada para {recording_id}: {conta_a['minutos']:.0f} min")
    return {"recording_id": recording_id, "minutos": conta_a["minutos"], "rendimento": rendimento}

_MISSAO_EXTRACAO_REAL = """A mensagem abaixo foi escrita por uma equipa de
montagem depois de uma entrega, registando o tempo real que a montagem
demorou — formato livre, tipicamente parecido com "Real: 3h45 · 2 pessoas ·
Ocorrências: roupeiro veio desmontado, sem elevador", mas pode variar.

Responde APENAS com um objeto JSON, sem mais nenhum texto antes ou depois:
{"minutos": number ou null, "pessoas": integer ou null, "ocorrencias": string ou null}

Converte sempre horas/minutos para minutos totais (ex: "3h45" = 225,
"4h" = 240). Usa null para o que não conseguires identificar com confiança —
nunca inventes um valor."""

def _extrair_real(texto_comentario: str) -> dict:
    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=200,
        system=_MISSAO_EXTRACAO_REAL,
        messages=[{"role": "user", "content": texto_comentario}]
    )
    texto = _limpar_bloco_codigo("".join(b.text for b in resposta.content if b.type == "text").strip())
    try:
        return json.loads(texto)
    except ValueError:
        return {"minutos": None, "pessoas": None, "ocorrencias": None}

def _comentario_apos(comentario: dict, quando) -> bool:
    criado_em = comentario.get("criado_em")
    if not criado_em or not quando:
        return False
    try:
        criado_em_dt = datetime.fromisoformat(criado_em.replace("Z", "+00:00"))
    except ValueError:
        return False
    quando_dt = quando if quando.tzinfo else quando.replace(tzinfo=timezone.utc)
    return criado_em_dt > quando_dt

def verificar_entregas_concluidas_e_ler_real() -> dict:
    """Corrida diária: para cada estimativa ainda sem "Real" registado,
    verifica se o card já deixou de estar ativo (sinal de entrega concluída
    — não há hoje nenhum diffing de coluna, este é o sinal mais simples e já
    disponível), e se sim procura nos comentários um registo do "Real"
    postado depois da estimativa, extraindo os dados via IA (tolerante a
    formato livre, nunca inventa o que não encontrar)."""
    pendentes = db.estimativas_aguardando_real()
    if not pendentes:
        return {"pendentes": 0, "concluidas": 0, "reais_registados": 0}

    ids_ativos = {item["id"] for item in basecamp._itens_ativos()}
    resumo = {"pendentes": len(pendentes), "concluidas": 0, "reais_registados": 0}

    for pendente in pendentes:
        recording_id = pendente["recording_id"]
        if recording_id in ids_ativos:
            continue  # ainda em curso, ainda não há "Real" para ler
        resumo["concluidas"] += 1

        comments_url = pendente.get("comments_url")
        if not comments_url:
            continue
        try:
            comentarios = basecamp.ler_comentarios(comments_url)
        except Exception as e:
            print(f"[estimativa_montagem] não consegui ler comentários de {recording_id}: {e!r}")
            continue

        publicado_em = pendente.get("publicado_em")
        candidatos = [c for c in comentarios
                     if _comentario_apos(c, publicado_em) and "real" in (c.get("conteudo") or "").lower()]
        if not candidatos:
            continue

        real = _extrair_real(candidatos[-1]["conteudo"])
        if real.get("minutos") is None:
            continue
        db.marcar_real_estimativa(recording_id, real["minutos"], real.get("pessoas"), real.get("ocorrencias"))
        resumo["reais_registados"] += 1
        print(f"[estimativa_montagem] real registado para {recording_id}: {real['minutos']} min")

    return resumo

def correr_calibracao_estimativa() -> dict:
    """Corrida bimestral: compara estimativa vs. real de todos os casos
    ainda por calibrar, e publica um relatório de desvio no Mural do projeto
    Entregas — NUNCA ajusta parâmetros sozinha (o documento é explícito:
    "ajusta-se um parâmetro de cada vez, para se perceber o efeito"), só
    reporta para o Rui decidir."""
    pendentes = db.estimativas_por_calibrar()
    if not pendentes:
        return {"casos": 0}

    linhas = []
    razoes = []
    for p in pendentes:
        estimado = float(p["estimativa_minutos"] or 0)
        real = float(p["real_minutos"] or 0)
        if estimado <= 0 or real <= 0:
            continue
        razao = real / estimado
        razoes.append(razao)
        linhas.append(f"- {p['titulo']}: estimado {estimado:.0f} min, real {real:.0f} min (razão {razao:.2f})")

    ids_processados = [p["recording_id"] for p in pendentes]
    if not razoes:
        db.marcar_calibrado(ids_processados)
        return {"casos": 0}

    media = sum(razoes) / len(razoes)
    texto = (
        f"### Calibração da estimativa de tempos de montagem\n\n"
        f"Nas últimas {len(razoes)} entregas com registo completo (estimativa + real), "
        f"a estimativa desviou-se do real em média por um fator de {media:.2f} "
        "(real ÷ estimado — 1,00 seria perfeito; acima de 1 a estimativa ficou curta, "
        "abaixo de 1 ficou longa).\n\n"
        + "\n".join(linhas) +
        "\n\n@Rui Rocha, se este desvio for consistente ao longo do tempo, pode valer a pena "
        "ajustar algum parâmetro com a tool atualizar_parametro_estimativa — ajusta um de cada "
        "vez, para se perceber o efeito de cada mudança."
    )
    basecamp.publicar_mural("Calibração da estimativa de montagem", texto, projeto=logistica.PROJETO_ENTREGAS)
    db.marcar_calibrado(ids_processados)
    return {"casos": len(razoes), "desvio_medio": media}
