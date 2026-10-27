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
import re
import threading
import db
from tools import parkin

_lock_abates = threading.Lock()

_RE_NUMERO_ACIMA = re.compile(r"(\d+)\s*acima", re.IGNORECASE)

def _numero_acima(texto):
    """Extrai o número de uma nota/OBS tipo "30 acima" ou "MADEIRA 30
    ACIMA" — None se não encontrar nada parecido. Ver
    _obs_bate_com_notas_card."""
    if not texto:
        return None
    m = _RE_NUMERO_ACIMA.search(texto)
    return int(m.group(1)) if m else None

def _card_pode_abater_entrada(data_encomenda, data_entrada) -> bool:
    """Um card só pode abater um talão da mesma data da sua encomenda ou
    posterior — nunca um talão mais antigo do que a própria encomenda
    (pedido explícito do Rui, 2026-10-23: um card nunca pode ser
    retroativo sobre um talão que já existia antes de a encomenda ser
    feita). Compara com `data_encomenda` (a data real da encomenda ao
    fornecedor, escolhida por quem cria o card), nunca com `criado_em` (a
    data em que a linha foi mesmo escrita na base de dados, "agora") —
    bug real (Rui, 2026-10-26): um card criado hoje mas com
    `data_encomenda` de sexta-feira tem de apanhar um talão chegado na
    própria sexta, coisa que nunca acontecia comparando com `criado_em`
    (sempre "hoje", portanto sempre posterior a "sexta"). Um talão
    chegado antes da encomenda ser feita fica em excesso (ver
    db.compras_toros_excessos) até alguém o mover à mão para o card
    correto — nunca é apanhado sozinho por este motor."""
    return data_encomenda <= data_entrada


def _obs_bate_com_notas_card(notas_card, obs_entrada) -> bool:
    """Pedido explícito do Rui (2026-10-20): um card de Compras de Toro
    pode ter uma nota tipo "30 acima" (madeira de diâmetro 30cm e acima),
    e nesse caso só pode abater talões cujo campo OBS (ex: "MADEIRA 30
    ACIMA" — linha própria do talão, separada do campo "Produto") tenha
    EXATAMENTE esse mesmo número, nunca um maior nem menor (ex: "30 acima"
    não aceita uma carga "35 ACIMA"). Um card SEM essa nota continua a
    aceitar qualquer talão do mesmo fornecedor+artigo, com ou sem OBS,
    exatamente como antes desta regra — só os cards COM a nota ficam mais
    restritos."""
    numero_card = _numero_acima(notas_card)
    if numero_card is None:
        return True
    return _numero_acima(obs_entrada) == numero_card


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


def criar_card(coluna_id: int, fornecedor: str, data_encomenda: str, linhas: list, criado_por: str = None,
               notas: str = None) -> dict:
    """Cria um card (encomenda a um fornecedor) com uma ou mais linhas
    (artigo + toneladas encomendadas). `linhas`: lista de
    {"tipo","comprimento","espessura","encomendado_kg"}. `data_encomenda`
    (data em que a encomenda foi feita ao fornecedor) é obrigatória
    (pedido explícito do Rui, 2026-10-15). `notas` é um campo livre que
    fica visível no card e, em especial, quando o card fecha (pedido
    explícito do Rui, 2026-10-14) — ao contrário de nota_fecho, pode ser
    escrito logo na criação, antes de o card fechar."""
    fornecedor = (fornecedor or "").strip()
    if not fornecedor:
        return {"erro": "o fornecedor é obrigatório"}
    if not data_encomenda:
        return {"erro": "a data da encomenda é obrigatória"}
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

    notas = (notas or "").strip() or None
    card_id = db.criar_card_compras_toros(coluna_id, fornecedor, data_encomenda, criado_por, notas)
    for l in linhas:
        db.criar_linha_compras_toros(card_id, l["tipo"], float(l["comprimento"]), l["espessura"],
                                     float(l["encomendado_kg"]))
    processar_abates_pendentes()
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


def editar_card(card_id: int, fornecedor: str, data_encomenda: str, notas: str = None) -> dict:
    """Edita fornecedor/data da encomenda/notas de um card já existente
    (aberto ou fechado) — pedido explícito do Rui, 2026-10-15: poder
    corrigir um card depois de criado. Não mexe no estado nem nas linhas
    (ver atualizar_linha/adicionar_linha/remover_linha, abaixo, para
    editar artigos/toneladas)."""
    card = db.card_compras_toros(card_id)
    if not card:
        return {"erro": "card não encontrado"}
    fornecedor = (fornecedor or "").strip()
    if not fornecedor:
        return {"erro": "o fornecedor é obrigatório"}
    if not data_encomenda:
        return {"erro": "a data da encomenda é obrigatória"}
    db.atualizar_card_compras_toros(card_id, fornecedor, data_encomenda, (notas or "").strip() or None)
    return {"ok": True}


def reabrir_card(card_id: int) -> dict:
    """Reabre um card fechado (à mão ou automaticamente) — pedido
    explícito do Rui, 2026-10-15. Limpa fechado_por/fechado_em/nota_fecho
    (fica como se nunca tivesse sido fechado); se fechar outra vez,
    fica tudo registado de novo."""
    card = db.card_compras_toros(card_id)
    if not card:
        return {"erro": "card não encontrado"}
    if card["estado"] != "fechado":
        return {"erro": "este card já está aberto"}
    db.reabrir_card_compras_toros(card_id)
    return {"ok": True}


def atualizar_linha(linha_id: int, encomendado_kg: float) -> dict:
    """Corrige as toneladas encomendadas de uma linha já existente —
    pedido explícito do Rui, 2026-10-15. Não pode ficar abaixo do que já
    foi entregue (perderia o sentido do "em falta" e do "excesso")."""
    linha = db.linha_compras_toros(linha_id)
    if not linha:
        return {"erro": "linha não encontrada"}
    if not encomendado_kg or float(encomendado_kg) <= 0:
        return {"erro": "as toneladas encomendadas têm de ser maiores que zero"}
    encomendado_kg = float(encomendado_kg)
    if encomendado_kg < float(linha["entregue_kg"]):
        return {"erro": f"não pode ficar abaixo do que já foi entregue ({round(float(linha['entregue_kg'])/1000, 1)} t)"}
    db.atualizar_linha_compras_toros(linha_id, encomendado_kg)
    _tentar_fechar_card_automatico(linha["card_id"])
    return {"ok": True}


def adicionar_linha(card_id: int, tipo: str, comprimento: float, espessura: str, encomendado_kg: float) -> dict:
    """Acrescenta um artigo novo a um card já existente — pedido
    explícito do Rui, 2026-10-15."""
    card = db.card_compras_toros(card_id)
    if not card:
        return {"erro": "card não encontrado"}
    if tipo not in parkin.TIPOS_VALIDOS:
        return {"erro": f"tipo inválido: {tipo!r} — tem de ser um de {parkin.TIPOS_VALIDOS}"}
    if espessura not in parkin.ESPESSURAS_VALIDAS:
        return {"erro": f"espessura inválida: {espessura!r} — tem de ser uma de {parkin.ESPESSURAS_VALIDAS}"}
    if not comprimento or float(comprimento) <= 0:
        return {"erro": "o comprimento é obrigatório e tem de ser maior que zero"}
    if not encomendado_kg or float(encomendado_kg) <= 0:
        return {"erro": "as toneladas encomendadas têm de ser maiores que zero"}
    linha_id = db.criar_linha_compras_toros(card_id, tipo, float(comprimento), espessura, float(encomendado_kg))
    return {"ok": True, "linha_id": linha_id}


def remover_linha(linha_id: int) -> dict:
    """Remove uma linha de um card — só se ainda não tiver nada entregue
    (já teria talões abatidos a perder o rasto). O card tem de ficar
    sempre com pelo menos uma linha. Pedido explícito do Rui, 2026-10-15."""
    linha = db.linha_compras_toros(linha_id)
    if not linha:
        return {"erro": "linha não encontrada"}
    if float(linha["entregue_kg"]) > 0:
        return {"erro": "não é possível remover — já há talões abatidos a esta linha"}
    if len(db.linhas_compras_toros_do_card(linha["card_id"])) <= 1:
        return {"erro": "o card tem de ter pelo menos uma linha"}
    db.remover_linha_compras_toros(linha_id)
    return {"ok": True}


def arquivar_coluna(coluna_id: int, arquivada_por: str = None) -> dict:
    db.arquivar_coluna_compras_toros(coluna_id, arquivada_por)
    return {"ok": True}


def mover_card(card_id: int, nova_coluna_id: int) -> dict:
    """Move um card (criado no levantamento errado) para outro
    levantamento ainda aberto — nunca mexe nas linhas/entregue/abates do
    card, só a que coluna pertence. Reavalia o arquivo automático da
    coluna de onde saiu (pode ter ficado com todos os cards fechados e a
    necessidade coberta)."""
    card = db.card_compras_toros(card_id)
    if not card:
        return {"erro": "card não encontrado"}
    if card["coluna_id"] == nova_coluna_id:
        return {"erro": "o card já está nesse levantamento"}
    nova_coluna = db.obter_coluna_compras_toros(nova_coluna_id)
    if not nova_coluna:
        return {"erro": "levantamento de destino não encontrado"}
    if nova_coluna["estado"] != "aberta":
        return {"erro": "não é possível mover um card para um levantamento já arquivado"}
    coluna_antiga_id = card["coluna_id"]
    db.mover_card_compras_toros(card_id, nova_coluna_id)
    _tentar_arquivar_coluna_automatica(coluna_antiga_id)
    return {"ok": True}


def reordenar_cards(coluna_id: int, ordem_cards: list) -> dict:
    """Grava a ordem com que os cards ficaram depois de arrastados à mão
    (para cima/para baixo dentro do mesmo levantamento, ou entre
    levantamentos) — pedido explícito do Rui, 2026-10-22, como no
    Basecamp. `ordem_cards` é a lista completa dos ids de card deste
    levantamento, já na ordem final (um card vindo de outro levantamento
    muda também de coluna_id). Puramente visual — nunca mexe no FIFO."""
    coluna = db.obter_coluna_compras_toros(coluna_id)
    if not coluna:
        return {"erro": "levantamento não encontrado"}
    if coluna["estado"] != "aberta":
        return {"erro": "não é possível reordenar cards num levantamento já arquivado"}
    colunas_origem = set()
    for card_id in ordem_cards:
        card = db.card_compras_toros(card_id)
        if not card:
            return {"erro": f"card {card_id} não encontrado"}
        colunas_origem.add(card["coluna_id"])
    db.reordenar_cards_compras_toros(coluna_id, ordem_cards)
    for origem_id in colunas_origem - {coluna_id}:
        _tentar_arquivar_coluna_automatica(origem_id)
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


def _reconciliar_excessos_pendentes():
    """Pedido explícito do Rui, 2026-10-23: antes só se reavaliava um
    excesso já registado no momento em que se criava um card novo (ver
    git history), o que obrigava a pedir sempre para reavaliar à mão
    quando um excesso ficava para trás e nenhum card novo era criado a
    seguir. Corre agora sempre que o motor de abates corre (ver
    processar_abates_pendentes, chamado a cada leitura do quadro): para
    cada excesso ainda por reconciliar, verifica se alguma linha aberta
    do mesmo fornecedor+artigo já tem espaço para o LEVAR TODO, foi
    criada na data do talão ou antes (ver _card_pode_abater_entrada) e
    bate com o "X acima" (ver _obs_bate_com_notas_card) — se sim, desfaz
    o excesso para a entrada voltar a ser vista como não processada e o
    motor a abater do zero, já a seguir, na mesma passagem. Um talão
    chegado antes de existir qualquer card certo para ele fica em
    excesso para sempre, até alguém criar ou corrigir manualmente o card
    certo (pedido explícito do Rui, 2026-10-23: nunca retroativo).
    Pedido explícito do Rui, 2026-10-27: uma carga nunca se divide por
    vários cards — só reconcilia quando um card consegue levá-la POR
    INTEIRO (ver processar_abates_pendentes)."""
    for exc in db.todos_excessos_compras_toros():
        candidatas = db.linhas_abertas_compras_toros_por_fornecedor_artigo(
            exc["fornecedor"], exc["tipo"], float(exc["comprimento"]), exc["espessura"])
        for linha in candidatas:
            if not _card_pode_abater_entrada(linha["data_encomenda"], exc["data_entrada"]):
                continue
            if not _obs_bate_com_notas_card(linha["notas"], exc["obs"]):
                continue
            falta = float(linha["encomendado_kg"]) - float(linha["entregue_kg"])
            if falta + 1e-6 < float(exc["quantidade_kg"]):
                continue
            db.desfazer_excesso_compras_toros(exc["id"], exc["entrada_id"])
            break


def processar_abates_pendentes():
    """Motor de FIFO: cada entrada real do Park In ainda não vista abate
    a uma linha aberta do mesmo fornecedor+artigo, a mais antiga primeiro
    — só contam linhas de cards criados até à data do talão (ver
    _card_pode_abater_entrada). Pedido explícito do Rui, 2026-10-27: uma
    carga nunca se divide por vários cards (ficava confuso de seguir) —
    ou um card consegue levá-la POR INTEIRO, ou passa à candidata
    seguinte; se nenhuma conseguir, fica tudo em excesso, nunca só uma
    parte. O que fica sem card fica registado como excesso — nunca
    perdido, reavaliado sozinho sempre que este motor corre (ver
    _reconciliar_excessos_pendentes), mas só abate a um card criado na
    data do talão ou antes, nunca a um criado depois. Chamado sempre que
    o quadro é lido (ver dados_quadro) — uma entrada pode ser
    reprocessada mais que uma vez (sempre que o seu excesso é desfeito
    por _reconciliar_excessos_pendentes), por isso o `restante` desconta
    sempre o que já foi abatido em passagens anteriores (ver
    db.total_abatido_entrada_compras_toros)."""
    with _lock_abates:
        _reconciliar_excessos_pendentes()
        for entrada in db.entradas_parkin_nao_processadas_compras_toros():
            comprimento = float(entrada["comprimento"])
            candidatas = db.linhas_abertas_compras_toros_por_fornecedor_artigo(
                entrada["fornecedor"], entrada["tipo"], comprimento, entrada["espessura"])
            # Bug real (talão 11400, Rui 2026-10-27): uma entrada pode ser
            # reprocessada mais que uma vez (ver desfazer_excesso_compras_toros,
            # chamado por _reconciliar_excessos_pendentes acima) — sem descontar
            # o que já tinha sido abatido numa passagem anterior, o `restante`
            # reiniciava do peso_liquido_kg todo e contava esse peso a dobrar.
            ja_abatido = db.total_abatido_entrada_compras_toros(entrada["id"])
            restante = float(entrada["peso_liquido_kg"] or 0) - ja_abatido
            for linha in candidatas:
                if restante <= 1e-6:
                    break
                if not _card_pode_abater_entrada(linha["data_encomenda"], entrada["data"]):
                    continue
                if not _obs_bate_com_notas_card(linha["notas"], entrada["obs"]):
                    continue
                falta = float(linha["encomendado_kg"]) - float(linha["entregue_kg"])
                if falta + 1e-6 < restante:
                    continue
                db.registar_abate_compras_toros(linha["id"], entrada["id"], restante)
                _tentar_fechar_card_automatico(linha["card_id"])
                restante = 0.0
                break
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
                "id": card["id"], "fornecedor": card["fornecedor"],
                "data_encomenda": card["data_encomenda"].isoformat() if card["data_encomenda"] else None,
                "estado": card["estado"],
                "nota_fecho": card["nota_fecho"], "notas": card["notas"], "criado_por": card["criado_por"],
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
        "data_talao": e["data_talao"].isoformat() if e["data_talao"] else None,
    } for e in db.excessos_compras_toros()]

    return {"colunas": colunas_resultado, "excessos": excessos}
