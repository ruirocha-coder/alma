# tools/compras_toros.py — "Compras de Toro": quadro Kanban de requisições
# de compra de madeira (pedido explícito do Rui, 2026-10-13), dentro do
# Park In. Unidade mostrada ao utilizador: toneladas (1 t = 1 m³ — mesma
# convenção já usada no resto do Park In); internamente tudo fica em kg,
# tal como o resto do Park In.
#
# Modelo:
# - COLUNA = um "levantamento de necessidade" (tipicamente mensal, mas
#   pode ser criado em qualquer data). Ao criar, congela-se a necessidade
#   por artigo nesse momento (Lmi/Lmax/S vêm do Park In; Pendente vem dos
#   cards ainda abertos de qualquer coluna) — nunca recalculada depois;
#   só a própria necessidade_kg é editável à mão (é uma PROPOSTA).
# - CARD = uma encomenda a um fornecedor dentro de uma coluna, com uma ou
#   mais LINHAS (um artigo + toneladas encomendadas cada).
# - Talões de entrada reais do Park In (nunca os sintéticos de uma
#   correção de inventário) abatem às linhas abertas do mesmo
#   fornecedor+artigo por FIFO (a linha do card mais antigo primeiro);
#   o que sobra sem card para abater fica registado como "excesso",
#   nunca descartado (ver db.compras_toros_excessos).
import threading
import db
from tools import parkin

_lock_abates = threading.Lock()


def _artigos_com_limite() -> list[dict]:
    """Artigos do Park In com um Lmax definido (sem isso a "necessidade"
    não tem sentido nenhum para calcular) — Lmi assume 0 quando não
    definido."""
    artigos = []
    for a in parkin.stock_por_artigo():
        if a["artigo"] == parkin._ARTIGO_DESCONHECIDO:
            continue
        if a.get("maximo_kg") is None:
            continue
        artigos.append(a)
    return artigos


def _pendente_por_artigo() -> dict:
    """{(tipo,comprimento,espessura): kg pendente} — soma de
    (encomendado-entregue) de todas as linhas de cards ainda ABERTOS, de
    qualquer coluna (pedido explícito do Rui: "quantidade encomendada em
    cards abertos e ainda não entregue")."""
    pendente = {}
    for coluna in db.colunas_compras_toros():
        for l in db.linhas_compras_toros_da_coluna(coluna["id"]):
            if l["card_estado"] != "aberto":
                continue
            chave = (l["tipo"], float(l["comprimento"]), l["espessura"])
            falta = float(l["encomendado_kg"]) - float(l["entregue_kg"])
            pendente[chave] = pendente.get(chave, 0.0) + max(0.0, falta)
    return pendente


def calcular_necessidades_propostas() -> list[dict]:
    """Necessidade proposta por artigo, agora mesmo — usada para pré-ver
    antes de criar uma coluna nova (ver criar_coluna). Necessidade =
    max(0, (Lmi - S) + Lmax - Pendente)."""
    pendente_por_artigo = _pendente_por_artigo()
    linhas = []
    for a in _artigos_com_limite():
        chave = (a["tipo"], float(a["comprimento"]), a["espessura"])
        lmi = float(a.get("minimo_kg") or 0)
        lmax = float(a["maximo_kg"])
        s = float(a["total_kg"])
        pendente = pendente_por_artigo.get(chave, 0.0)
        necessidade = max(0.0, (lmi - s) + lmax - pendente)
        linhas.append({
            "tipo": a["tipo"], "comprimento": a["comprimento"], "espessura": a["espessura"],
            "lmi_kg": round(lmi, 1), "lmax_kg": round(lmax, 1), "s_kg": round(s, 1),
            "pendente_kg": round(pendente, 1), "necessidade_kg": round(necessidade, 1),
        })
    linhas.sort(key=lambda l: parkin._chave_ordenacao_artigo(l["tipo"], l["comprimento"], l["espessura"]))
    return linhas


def criar_coluna(data_levantamento: str, criado_por: str = None) -> dict:
    """Cria uma coluna nova (levantamento de necessidade) e congela a
    necessidade por artigo nesse momento."""
    propostas = calcular_necessidades_propostas()
    coluna_id = db.criar_coluna_compras_toros(data_levantamento, criado_por)
    for p in propostas:
        db.guardar_necessidade_compras_toros(
            coluna_id, p["tipo"], p["comprimento"], p["espessura"],
            p["lmi_kg"], p["lmax_kg"], p["s_kg"], p["pendente_kg"], p["necessidade_kg"])
    return {"ok": True, "coluna_id": coluna_id}


def atualizar_necessidade(necessidade_id: int, necessidade_kg: float) -> dict:
    """Correção manual da necessidade proposta — pedido explícito do
    Rui: "tem de ser editável à mão antes de confirmar"."""
    if necessidade_kg < 0:
        return {"erro": "a necessidade não pode ser negativa"}
    db.atualizar_necessidade_compras_toros(necessidade_id, necessidade_kg)
    return {"ok": True}


def criar_card(coluna_id: int, fornecedor: str, linhas: list, criado_por: str = None) -> dict:
    """Cria um card (encomenda a um fornecedor) com uma ou mais linhas
    (artigo + toneladas encomendadas). `linhas`: lista de
    {"tipo","comprimento","espessura","encomendado_kg"}."""
    fornecedor = (fornecedor or "").strip()
    if not fornecedor:
        return {"erro": "o fornecedor é obrigatório"}
    if not linhas:
        return {"erro": "o card tem de ter pelo menos uma linha (artigo + toneladas)"}
    for l in linhas:
        if l.get("tipo") not in parkin.TIPOS_VALIDOS:
            return {"erro": f"tipo inválido: {l.get('tipo')!r} — tem de ser um de {parkin.TIPOS_VALIDOS}"}
        if l.get("espessura") not in parkin.ESPESSURAS_VALIDAS:
            return {"erro": f"espessura inválida: {l.get('espessura')!r} — tem de ser uma de {parkin.ESPESSURAS_VALIDAS}"}
        if not l.get("comprimento") or float(l["comprimento"]) <= 0:
            return {"erro": "o comprimento é obrigatório e tem de ser maior que zero"}
        if not l.get("encomendado_kg") or float(l["encomendado_kg"]) <= 0:
            return {"erro": "as toneladas encomendadas têm de ser maiores que zero"}

    card_id = db.criar_card_compras_toros(coluna_id, fornecedor, criado_por)
    for l in linhas:
        db.criar_linha_compras_toros(card_id, l["tipo"], float(l["comprimento"]), l["espessura"],
                                     float(l["encomendado_kg"]))
    return {"ok": True, "card_id": card_id}


def fechar_card(card_id: int, fechado_por: str, nota_fecho: str = None) -> dict:
    """Fecho à mão (entrega parcial, cancelamento) — nota sempre
    obrigatória, pedido explícito do Rui."""
    if not nota_fecho or not nota_fecho.strip():
        return {"erro": "a nota é obrigatória para fechar um card à mão"}
    card = db.card_compras_toros(card_id)
    if not card:
        return {"erro": "card não encontrado"}
    if card["estado"] != "aberto":
        return {"erro": "este card já está fechado"}
    db.fechar_card_compras_toros(card_id, fechado_por, nota_fecho.strip())
    _tentar_arquivar_coluna_automatica(card["coluna_id"])
    return {"ok": True}


def arquivar_coluna(coluna_id: int, arquivada_por: str = None) -> dict:
    db.arquivar_coluna_compras_toros(coluna_id, arquivada_por)
    return {"ok": True}


def _tentar_fechar_card_automatico(card_id: int):
    """Fecha sozinho quando entregue >= encomendado, somado por todas as
    linhas do card (pedido explícito do Rui)."""
    linhas = db.linhas_compras_toros_do_card(card_id)
    if not linhas:
        return
    encomendado = sum(float(l["encomendado_kg"]) for l in linhas)
    entregue = sum(float(l["entregue_kg"]) for l in linhas)
    if entregue + 1e-6 >= encomendado:
        db.fechar_card_compras_toros(card_id, "automático", "entrega completa")


def _tentar_arquivar_coluna_automatica(coluna_id: int):
    """Arquiva sozinha quando todos os cards estão fechados e a
    necessidade está coberta em todos os artigos (pedido explícito do
    Rui)."""
    cards = db.cards_compras_toros(coluna_id)
    if not cards or any(c["estado"] != "fechado" for c in cards):
        return
    entregue_por_artigo = {}
    for l in db.linhas_compras_toros_da_coluna(coluna_id):
        chave = (l["tipo"], float(l["comprimento"]), l["espessura"])
        entregue_por_artigo[chave] = entregue_por_artigo.get(chave, 0.0) + float(l["entregue_kg"])
    for n in db.necessidades_compras_toros(coluna_id):
        chave = (n["tipo"], float(n["comprimento"]), n["espessura"])
        if entregue_por_artigo.get(chave, 0.0) + 1e-6 < float(n["necessidade_kg"]):
            return
    db.arquivar_coluna_compras_toros(coluna_id, "automático")


def processar_abates_pendentes():
    """Motor de FIFO: cada entrada real do Park In ainda não vista abate
    às linhas abertas do mesmo fornecedor+artigo, a mais antiga primeiro
    — só contam linhas de cards criados até à data do talão (pedido
    explícito do Rui: "só contam talões com data igual ou posterior à
    criação do card"). O que sobrar sem linha para abater fica registado
    como excesso. Chamado sempre que o quadro é lido (ver dados_quadro) —
    cada entrada é processada exatamente uma vez, para sempre."""
    with _lock_abates:
        for entrada in db.entradas_parkin_nao_processadas_compras_toros():
            comprimento = float(entrada["comprimento"])
            candidatas = db.linhas_abertas_compras_toros_por_fornecedor_artigo(
                entrada["fornecedor"], entrada["tipo"], comprimento, entrada["espessura"])
            restante = float(entrada["peso_liquido_kg"] or 0)
            for linha in candidatas:
                if restante <= 1e-6:
                    break
                if linha["criado_em_data"] > entrada["data"]:
                    continue
                falta = float(linha["encomendado_kg"]) - float(linha["entregue_kg"])
                if falta <= 1e-6:
                    continue
                aplicar = min(restante, falta)
                db.registar_abate_compras_toros(linha["id"], entrada["id"], aplicar)
                restante -= aplicar
                _tentar_fechar_card_automatico(linha["card_id"])
            if restante > 1e-6:
                db.registar_excesso_compras_toros(
                    entrada["id"], entrada["fornecedor"], entrada["tipo"], comprimento,
                    entrada["espessura"], round(restante, 1))
            db.marcar_entrada_processada_compras_toros(entrada["id"])


def _rotulo_artigo(tipo: str, comprimento, espessura: str) -> str:
    return f"{tipo} · {float(comprimento):.2f}m · {'Fina' if espessura == 'fina' else 'Normal'}"


def dados_quadro() -> dict:
    """Tudo o que o quadro precisa para se desenhar — colunas (abertas e
    arquivadas), cada uma com a sua necessidade por artigo e os seus
    cards (cada card já com as suas linhas e os totais
    encomendado/entregue/em falta), mais os excessos visíveis. Processa
    sempre os abates pendentes primeiro, para nunca mostrar dados
    desatualizados."""
    processar_abates_pendentes()

    colunas_resultado = []
    for coluna in db.colunas_compras_toros():
        necessidades = db.necessidades_compras_toros(coluna["id"])
        linhas_coluna = db.linhas_compras_toros_da_coluna(coluna["id"])

        encomendado_por_artigo, entregue_por_artigo = {}, {}
        for l in linhas_coluna:
            chave = (l["tipo"], float(l["comprimento"]), l["espessura"])
            encomendado_por_artigo[chave] = encomendado_por_artigo.get(chave, 0.0) + float(l["encomendado_kg"])
            entregue_por_artigo[chave] = entregue_por_artigo.get(chave, 0.0) + float(l["entregue_kg"])

        necessidades_resultado = []
        for n in necessidades:
            chave = (n["tipo"], float(n["comprimento"]), n["espessura"])
            encomendado = round(encomendado_por_artigo.get(chave, 0.0), 1)
            entregue = round(entregue_por_artigo.get(chave, 0.0), 1)
            necessidade_kg = float(n["necessidade_kg"])
            necessidades_resultado.append({
                "id": n["id"], "tipo": n["tipo"], "comprimento": float(n["comprimento"]), "espessura": n["espessura"],
                "artigo": _rotulo_artigo(n["tipo"], n["comprimento"], n["espessura"]),
                "lmi_kg": float(n["lmi_kg"]), "lmax_kg": float(n["lmax_kg"]), "s_kg": float(n["s_kg"]),
                "pendente_kg": float(n["pendente_kg"]), "necessidade_kg": round(necessidade_kg, 1),
                "encomendado_kg": encomendado, "entregue_kg": entregue,
                "em_falta_kg": round(max(0.0, necessidade_kg - entregue), 1),
                "excesso_encomenda": encomendado > necessidade_kg + 1e-6,
            })
        necessidades_resultado.sort(key=lambda n: parkin._chave_ordenacao_artigo(n["tipo"], n["comprimento"], n["espessura"]))

        cards_resultado = []
        for card in db.cards_compras_toros(coluna["id"]):
            linhas_card = db.linhas_compras_toros_do_card(card["id"])
            encomendado = sum(float(l["encomendado_kg"]) for l in linhas_card)
            entregue = sum(float(l["entregue_kg"]) for l in linhas_card)
            cards_resultado.append({
                "id": card["id"], "fornecedor": card["fornecedor"], "estado": card["estado"],
                "nota_fecho": card["nota_fecho"], "criado_por": card["criado_por"],
                "criado_em": card["criado_em"].isoformat() if card["criado_em"] else None,
                "fechado_por": card["fechado_por"],
                "fechado_em": card["fechado_em"].isoformat() if card["fechado_em"] else None,
                "linhas": [{
                    "id": l["id"], "tipo": l["tipo"], "comprimento": float(l["comprimento"]), "espessura": l["espessura"],
                    "artigo": _rotulo_artigo(l["tipo"], l["comprimento"], l["espessura"]),
                    "encomendado_kg": float(l["encomendado_kg"]), "entregue_kg": float(l["entregue_kg"]),
                } for l in linhas_card],
                "encomendado_kg": round(encomendado, 1), "entregue_kg": round(entregue, 1),
                "em_falta_kg": round(max(0.0, encomendado - entregue), 1),
            })

        colunas_resultado.append({
            "id": coluna["id"], "data_levantamento": coluna["data_levantamento"].isoformat(),
            "estado": coluna["estado"], "criado_por": coluna["criado_por"],
            "criado_em": coluna["criado_em"].isoformat() if coluna["criado_em"] else None,
            "arquivada_por": coluna["arquivada_por"],
            "arquivada_em": coluna["arquivada_em"].isoformat() if coluna["arquivada_em"] else None,
            "necessidades": necessidades_resultado, "cards": cards_resultado,
        })

    excessos = [{
        "id": e["id"], "talao": e["talao"], "fornecedor": e["fornecedor"],
        "artigo": _rotulo_artigo(e["tipo"], e["comprimento"], e["espessura"]),
        "quantidade_kg": float(e["quantidade_kg"]),
        "criado_em": e["criado_em"].isoformat() if e["criado_em"] else None,
    } for e in db.excessos_compras_toros()]

    return {"colunas": colunas_resultado, "excessos": excessos}
