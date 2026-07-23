# agents/logistica_entregas.py — monitorização automática das encomendas
# no projeto "Entregas" do Basecamp, pedida pela Isa Moreira/Conceição
# Costa. Mesma forma de trabalhar que agents/monitor_basecamp.py: nunca
# executa ações externas (nunca envia emails, nunca altera cards) — só
# publica comentários a propor o que fazer a seguir, sempre validados por
# um humano antes de qualquer envio.
#
# NOTA (2026-07-23, confirmado contra a API real do Basecamp — ver
# tools.logistica.fase_encomenda): "On Hold" é uma secção dentro de uma
# coluna, do tipo "Kanban::OnHold" — não uma coluna irmã. Um card em "On
# Hold" está pronto a entregar INDEPENDENTEMENTE da coluna onde estiver;
# a coluna real (Lisboa/Porto/Outro) por trás dessa secção é lida
# diretamente do `url` do parent (ver _regiao_estrutural, aqui mesmo,
# para ser partilhada entre a sugestão semanal e o diagnóstico — nunca
# duas versões desta lógica a divergir). A assunção original (um campo
# tipo on_hold_at/on_hold no próprio card) estava errada e foi removida.
# - As duas datas críticas (entrada em armazém / entrega ao cliente) e os
#   restantes dados (cliente, n.º de encomenda, fornecedor) vêm das notas
#   do card em texto livre, por isso são extraídos por IA (não há um
#   formato fixo garantido) — revê os primeiros ciclos para confirmar que
#   a extração está a funcionar bem com o formato real usado pela equipa.
import json, threading
from datetime import date, datetime, timezone
from agents.base import client
from tools import basecamp, logistica, documentos_referencia
import db

_a_correr = threading.Lock()

MAX_CARDS_POR_CORRIDA = 40  # limite defensivo, tal como monitor_basecamp.py

_REGIAO_POR_COLUNA = {"lisboa": "Lisboa", "porto": "Porto", "outro": "Outro", "outros": "Outro"}

def _regiao_estrutural(item: dict):
    """Tenta ler a região diretamente da coluna real por trás da secção
    "On Hold" do card — devolve None (não Lisboa/Porto/Outro) se não
    conseguir, ou se essa coluna real não for uma coluna de região (ex:
    ainda "Produção"), para o chamador cair na morada nesse caso.
    Partilhada entre agents.sugestao_logistica_semanal (a sugestão
    semanal) e diagnostico_cards_regiao (aqui mesmo), para nunca haver
    duas versões desta lógica a divergir uma da outra."""
    parent_url = (item.get("parent") or {}).get("url")
    if not parent_url:
        return None
    try:
        coluna_real = basecamp.obter_recording(parent_url)
    except Exception as e:
        print(f"[logistica_entregas] não consegui obter a coluna real de {item.get('id')}: {e!r}")
        return None
    return _REGIAO_POR_COLUNA.get(logistica.normalizar_coluna(coluna_real.get("title")))

_MISSAO_EXTRACAO = """Extrais dados estruturados do título e das notas de um
card do Basecamp sobre uma encomenda de mobiliário, para a equipa de
logística da Interior Guider / Boa Safra. Responde APENAS com um objeto
JSON, sem mais nenhum texto antes ou depois, com exatamente estas chaves:
{"cliente": string ou null, "numero_encomenda": string ou null,
"fornecedor": string ou null, "designer": string ou null,
"morada": string ou null, "produtos_encomendados": string ou null,
"data_entrada_armazem": "AAAA-MM-DD" ou null,
"data_entrega_cliente": "AAAA-MM-DD" ou null}
Usa null sempre que a informação não estiver mesmo presente no texto —
nunca inventes um valor. "morada" é o endereço de entrega completo, tal
como escrito nas notas (nunca resumido nem alterado). "produtos_encomendados"
resume, em poucas palavras, o que foi encomendado. As datas podem aparecer
em qualquer formato (ex: 25/07/2026, 25-07-2026, 2026-07-25) — converte
sempre para AAAA-MM-DD."""

def _chamar_extracao_llm(titulo: str, notas: str) -> str:
    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=300,
        system=_MISSAO_EXTRACAO,
        messages=[{"role": "user", "content": f"Título: {titulo}\n\nNotas:\n{notas or '(sem notas)'}"}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def _limpar_bloco_codigo(texto: str) -> str:
    """Alguns modelos embrulham o JSON num bloco de código markdown apesar
    da instrução em contrário — remove esse invólucro antes de tentar o
    parse, em vez de rebentar/devolver vazio por causa disso."""
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else texto[3:]
        if texto.endswith("```"):
            texto = texto[:-3]
    return texto.strip()

def _extrair_dados_encomenda(titulo: str, notas: str) -> dict:
    texto = _limpar_bloco_codigo(_chamar_extracao_llm(titulo, notas))
    try:
        dados = json.loads(texto)
    except ValueError:
        print(f"[logistica_entregas] extração não devolveu JSON válido: {texto[:200]!r}")
        return {}
    for campo in ("data_entrada_armazem", "data_entrega_cliente"):
        if dados.get(campo):
            try:
                dados[campo] = date.fromisoformat(dados[campo])
            except ValueError:
                dados[campo] = None
    return dados

# palavras-chave usadas para detetar, num comentário humano recente, um
# pedido de proposta de email de atraso ao cliente (condição E) — um
# atalho simples em vez de uma classificação por IA a mais por card;
# pode precisar de ajuste depois de ver pedidos reais da equipa.
_AUTORES_GESTAO = ("conceição", "isa")
_PALAVRAS_PEDIDO_ATRASO = ("atraso",)
_PALAVRAS_EMAIL_CLIENTE = ("email", "e-mail", "cliente")

def _pediu_email_atraso(comentarios: list, desde: datetime) -> bool:
    for c in comentarios:
        autor = (c.get("autor") or "").lower()
        if not any(nome in autor for nome in _AUTORES_GESTAO):
            continue
        criado_em = c.get("criado_em")
        if criado_em:
            try:
                if datetime.fromisoformat(criado_em.replace("Z", "+00:00")) <= desde:
                    continue
            except ValueError:
                pass
        conteudo = (c.get("conteudo") or "").lower()
        if any(p in conteudo for p in _PALAVRAS_PEDIDO_ATRASO) and any(p in conteudo for p in _PALAVRAS_EMAIL_CLIENTE):
            return True
    return False

def _houve_resposta_apos(comentarios: list, quando: datetime) -> bool:
    """Alguém comentou depois de um dado momento — usado pela condição C
    como um sinal simples de que a situação já está a ser acompanhada por
    uma pessoa, sem tentar perceber o conteúdo exato do que disse."""
    for c in comentarios:
        criado_em = c.get("criado_em")
        if not criado_em:
            continue
        try:
            if datetime.fromisoformat(criado_em.replace("Z", "+00:00")) > quando:
                return True
        except ValueError:
            continue
    return False

def _formatar_documentos_referencia(documentos: dict) -> str:
    return "\n\n---\n\n".join(f"### {titulo}\n{conteudo}" for titulo, conteudo in documentos.items())

def _gerar_texto_fg_h(condicao: str, dados: dict, documentos_texto: str) -> str:
    """F, G e H usam os templates numerados (8.1 previsão de entrega, 8.2
    confirmação final, 8.3 follow-up) — pedido explícito da Isa
    (2026-07-22): estes templates estão no documento "fluxograma", não no
    "Logistica", e pode ser preciso ir buscar informação a outros
    documentos do projeto Alma Data também. Por isso usa-se sempre
    documentos_referencia_empresa (todo o projeto Alma Data, não só um
    documento à parte) — nunca fica restrito a um único documento."""
    secao = {"F": "8.1 (previsão de entrega)", "G": "8.2 (confirmação final)",
             "H": "8.3 (follow-up pós-entrega)"}[condicao]
    if not documentos_texto:
        return (f"Alma Logística: era altura de enviar a comunicação da secção {secao} para a "
                f"encomenda {dados.get('numero_encomenda') or '[N.º a preencher]'}, mas não consegui "
                "aceder aos documentos de referência — segue por favor o procedimento manual. "
                "Responsável: @Conceição Costa.")
    missao = f"""Preparas uma proposta de comunicação de logística da Interior Guider / Boa Safra,
para a Conceição Costa validar e enviar — tu nunca envias nada diretamente.

Usa o template numerado da secção {secao} (o documento certo pode identificar as
secções por número, ex: "8.1", "8.2", "8.3") — usa exatamente esse template,
preenchendo os dados que tiveres. Este template pode estar em QUALQUER um dos
documentos abaixo (ex: no "fluxograma", não necessariamente no de "Logistica")
— procura em todos antes de dizeres que não encontraste, não te restrinjas ao
primeiro que pareça relacionado. Se precisares de outro dado que não esteja na
lista "Dados da encomenda" abaixo, procura-o também nestes documentos antes de
o deixares em branco. Só se, mesmo assim, não encontrares a secção em nenhum
documento, escreve um aviso claro disso em vez de inventares um template.

Dados da encomenda:
- Cliente/projeto: {dados.get('cliente') or '(não identificado)'}
- N.º de encomenda: {dados.get('numero_encomenda') or '(não identificado)'}
- Fornecedor: {dados.get('fornecedor') or '(não identificado)'}
- Designer responsável: {dados.get('designer') or '(não identificado)'}
- Data de entrada em armazém: {dados.get('data_entrada_armazem') or '(não identificada)'}
- Data de entrega ao cliente: {dados.get('data_entrega_cliente') or '(não identificada)'}

Documentos de referência disponíveis (projeto Alma Data e outros documentos
confirmados pela equipa como atuais):
{documentos_texto}

Escreve só o comentário final a publicar no Basecamp, sem comentário teu à volta.
Começa sempre com "Alma Logística —". Termina sempre a indicar o responsável (a
Conceição Costa, ou o designer responsável no caso do follow-up pós-entrega)."""
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system=missao,
        messages=[{"role": "user", "content": "Gera o comentário."}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def _card_criado_em(item_bruto: dict) -> date:
    criado = item_bruto.get("created_at")
    if not criado:
        return date.today()
    try:
        return datetime.fromisoformat(criado.replace("Z", "+00:00")).date()
    except ValueError:
        return date.today()

def _ja_alertado_recente_por_condicao(recording_id: int) -> dict:
    return {c: db.logistica_ja_alertado_recente(recording_id, c, dias)
           for c, dias in logistica.JANELA_REPETICAO_DIAS.items()}

def diagnostico_cards_regiao(limite: int = 5) -> dict:
    """Mostra os campos brutos de cards do projeto Entregas, agrupados
    pela fase atual (ver tools.logistica.fase_encomenda) — útil para
    confirmar ao vivo que a deteção de fase bate certo com o que se vê no
    Basecamp (ex: um nome de coluna diferente do esperado, ou nenhum card
    ainda em "On Hold"). Partilhado entre o endpoint /logistica/diagnostico
    (main.py) e a tool de chat do mesmo nome, para nunca haver duas
    versões desta lógica a divergir uma da outra.

    CONFIRMADO (2026-07-23) contra a API real do Basecamp: o `parent` de
    um card em "On Hold" é um objeto do tipo "Kanban::OnHold" — o `url`
    desse objeto aponta diretamente para a coluna de região REAL por
    trás dessa secção (ex: confirmado ao vivo, uma secção "On Hold" com
    url para a coluna "Porto"). `cards_por_coluna_regiao` devolve, para
    cada região (Lisboa/Porto/Outro), TODOS os cards que lá pertencem —
    tanto os diretamente na coluna (`estado_fluxo: "em_entrega"`) como os
    em "On Hold" cuja coluna real é essa região (`estado_fluxo:
    "pronto_a_entregar"`, via _regiao_estrutural) — bug real (2026-07-23):
    pedir "lista os cards da coluna Porto" só mostrava o primeiro grupo,
    faltavam os prontos a entregar visíveis na própria página da coluna
    no Basecamp.

    bug real (2026-07-23): a sugestão semanal publicada mostrou cliente/
    morada/produto/data "não identificado" em TODOS os 20 cards, apesar
    de as notas conterem moradas reais visíveis (confirmado neste próprio
    diagnóstico) — sinal de que _extrair_dados_encomenda está a falhar
    de forma sistemática, não isolada. Por isso esta função expõe também
    o resultado exato de tentar extrair os dados de cada exemplo
    (`extracao_debug`): o tamanho das notas realmente enviadas, os dados
    extraídos se a extração funcionar, ou a resposta em bruto do modelo
    (ou o erro) se falhar — para se ver com dados reais qual é a causa."""
    try:
        itens = [i for i in basecamp._itens_ativos()
                if i.get("type") == "Kanban::Card"
                and logistica.PROJETO_ENTREGAS.lower() in ((i.get("bucket") or {}).get("name") or "").lower()]
    except Exception as e:
        return {"erro": str(e)}
    if not itens:
        return {"aviso": "nenhum card ativo encontrado no projeto Entregas"}

    itens_prontos = [i for i in itens
                    if logistica.fase_encomenda((i.get("parent") or {}).get("title")) == "pronto_entrega"]

    # pedido do Rui (2026-07-23): quando alguém pede a lista de cards de
    # uma região (ex: "lista os cards da coluna Porto"), tem de incluir
    # tanto os cards diretamente nessa coluna (em entrega a sério) como
    # os cards em "On Hold" cuja coluna real (ver _regiao_estrutural) é
    # essa mesma região — senão fica incompleta (bug real: só mostrava o
    # card em entrega, faltavam os prontos a entregar em On Hold que se
    # veem na própria página da coluna no Basecamp).
    cards_por_coluna_regiao_bruto = {regiao: [] for regiao in _REGIAO_POR_COLUNA.values()}
    for i in itens:
        titulo_coluna = (i.get("parent") or {}).get("title")
        regiao = _REGIAO_POR_COLUNA.get(logistica.normalizar_coluna(titulo_coluna))
        if regiao:
            cards_por_coluna_regiao_bruto[regiao].append((i, "em_entrega"))
    for i in itens_prontos:
        regiao = _regiao_estrutural(i)
        if regiao:
            cards_por_coluna_regiao_bruto[regiao].append((i, "pronto_a_entregar"))

    def _extracao_debug(item: dict) -> dict:
        titulo = item.get("title") or item.get("content") or ""
        notas = basecamp._texto_simples(item.get("description", ""))
        try:
            texto_bruto = _chamar_extracao_llm(titulo, notas)
        except Exception as e:
            return {"notas_enviadas_tamanho": len(notas), "erro": f"falha ao chamar o modelo: {e!r}",
                    "texto_bruto_modelo": None, "dados_extraidos": None}
        texto_limpo = _limpar_bloco_codigo(texto_bruto)
        try:
            dados = json.loads(texto_limpo)
            return {"notas_enviadas_tamanho": len(notas), "erro": None,
                    "texto_bruto_modelo": None, "dados_extraidos": dados}
        except ValueError:
            return {"notas_enviadas_tamanho": len(notas), "erro": "resposta do modelo não é JSON válido",
                    "texto_bruto_modelo": texto_bruto[:300], "dados_extraidos": None}

    def _card_bruto_resumido(item: dict) -> dict:
        resumo = dict(item)
        if resumo.get("description"):
            resumo["description"] = basecamp._texto_simples(resumo["description"])[:200]
        return resumo

    return {
        "total_no_projeto": len(itens),
        "colunas_vistas": sorted({(i.get("parent") or {}).get("title") for i in itens}),
        "total_pronto_a_entregar": len(itens_prontos),
        "exemplos_prontos_a_entregar": [{
            "titulo": i.get("title") or i.get("content"),
            "coluna": (i.get("parent") or {}).get("title"),
            "notas": basecamp._texto_simples(i.get("description", ""))[:300],
            "extracao_debug": _extracao_debug(i),
        } for i in itens_prontos[:limite]],
        "cards_por_coluna_regiao": {
            regiao: [{"estado_fluxo": estado, **_card_bruto_resumido(i)} for i, estado in cards]
            for regiao, cards in cards_por_coluna_regiao_bruto.items()
        },
    }

def correr_monitorizacao_logistica() -> dict:
    """Um ciclo da monitorização de logística: lê as encomendas ativas no
    projeto "Entregas", avalia as condições A a I para cada uma, e publica
    um comentário de proposta quando alguma se aplicar. Pensado para
    correr uma vez por dia de manhã (agendado), mas pode ser disparado
    manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[logistica_entregas] já há uma corrida em curso — ignorado")
        return {"erro": "já está a correr uma monitorização"}

    try:
        hoje = date.today()
        try:
            itens = [i for i in basecamp._itens_ativos()
                    if i.get("type") == "Kanban::Card"
                    and logistica.PROJETO_ENTREGAS.lower() in ((i.get("bucket") or {}).get("name") or "").lower()
                    and not basecamp._em_coluna_terminal(i)]
        except Exception as e:
            print(f"[logistica_entregas] não foi possível obter os cards do Basecamp: {e!r}")
            return {"erro": str(e)}

        itens = itens[:MAX_CARDS_POR_CORRIDA]
        print(f"[logistica_entregas] {len(itens)} encomendas ativas a analisar")

        documentos_referencia_texto = None  # só lido se alguma condição F/G/H precisar mesmo dele
        resumo = {"analisadas": len(itens), "alertas": [], "campos_em_falta": 0,
                 "entregas_esta_semana": 0, "atencao_isa": []}

        for item in itens:
            titulo = item.get("title") or item.get("content") or "(sem título)"
            notas = basecamp._texto_simples(item.get("description", ""))
            estado = (item.get("parent") or {}).get("title") or ""
            projeto = (item.get("bucket") or {}).get("name")
            recording_id = item["id"]

            try:
                dados = _extrair_dados_encomenda(titulo, notas)
            except Exception as e:
                print(f"[logistica_entregas] falhou a extrair dados de {recording_id}: {e!r}")
                dados = {}

            if not dados.get("data_entrada_armazem") or not dados.get("data_entrega_cliente"):
                resumo["campos_em_falta"] += 1
            if dados.get("data_entrega_cliente"):
                dias_para_entrega = (dados["data_entrega_cliente"] - hoje).days
                if 0 <= dias_para_entrega <= 7:
                    resumo["entregas_esta_semana"] += 1

            comentarios = []
            if item.get("comments_url"):
                try:
                    comentarios = basecamp.ler_comentarios(item["comments_url"])
                except Exception as e:
                    print(f"[logistica_entregas] não consegui ler comentários de {recording_id}: {e!r}")

            ultimo_alerta_b = db.logistica_data_ultimo_alerta(recording_id, "B")
            horas_desde_b = None
            if ultimo_alerta_b:
                agora = datetime.now(timezone.utc)
                ultimo_alerta_b_utc = ultimo_alerta_b if ultimo_alerta_b.tzinfo else ultimo_alerta_b.replace(tzinfo=timezone.utc)
                if not _houve_resposta_apos(comentarios, ultimo_alerta_b_utc):
                    horas_desde_b = (agora - ultimo_alerta_b_utc).total_seconds() / 3600

            desde_ultimo_bot = db.logistica_primeiro_alerta(recording_id) or datetime.min.replace(tzinfo=timezone.utc)
            pedido_email_atraso = _pediu_email_atraso(comentarios, desde_ultimo_bot)

            resultado = logistica.avaliar_condicao(
                hoje=hoje, estado=estado, criado_em=_card_criado_em(item),
                data_entrada_armazem=dados.get("data_entrada_armazem"),
                data_entrega_cliente=dados.get("data_entrega_cliente"),
                ja_alertado_recente=_ja_alertado_recente_por_condicao(recording_id),
                horas_desde_alerta_b=horas_desde_b, pedido_email_atraso=pedido_email_atraso,
            )
            if resultado is None:
                continue
            condicao, _ = resultado

            try:
                if condicao in logistica.CONDICOES_COM_TEXTO_FIXO:
                    texto = logistica.gerar_texto_condicao_fixa(condicao, dados)
                else:
                    if documentos_referencia_texto is None:
                        try:
                            documentos_referencia_texto = _formatar_documentos_referencia(
                                documentos_referencia.documentos_referencia_empresa())
                        except Exception as e:
                            print(f"[logistica_entregas] não consegui ler os documentos de referência: {e!r}")
                            documentos_referencia_texto = ""
                    texto = _gerar_texto_fg_h(condicao, dados, documentos_referencia_texto)

                primeiro_alerta = db.logistica_primeiro_alerta(recording_id)
                if primeiro_alerta:
                    primeiro_alerta_utc = primeiro_alerta if primeiro_alerta.tzinfo else primeiro_alerta.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - primeiro_alerta_utc).days > 14 and "Isa Moreira" not in texto:
                        texto += "\n\n(Situação em curso há mais de 2 semanas — @Isa Moreira, por favor acompanha.)"
                        resumo["atencao_isa"].append(titulo)

                basecamp.comentar(recording_id, texto, projeto=projeto)
                db.logistica_registar_alerta(recording_id, condicao)
                resumo["alertas"].append({"titulo": titulo, "condicao": condicao})
                print(f"[logistica_entregas] alerta {condicao} publicado: {titulo}")
            except Exception as e:
                print(f"[logistica_entregas] falhou a publicar alerta para {recording_id}: {e!r}")

        print(f"[logistica_entregas] concluído — {len(resumo['alertas'])} alertas, "
             f"{resumo['campos_em_falta']} encomendas com campos em falta, "
             f"{resumo['entregas_esta_semana']} entregas esta semana")
        return resumo
    finally:
        _a_correr.release()
