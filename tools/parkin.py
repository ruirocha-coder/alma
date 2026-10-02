# tools/parkin.py — "Park In": stock de toros da Ecos Largos, a partir dos
# talões de pesagem fotografados na balança (entrada de madeira), de fotos
# de talões de consumo (saída), e de correções manuais (erros/inventário).
#
# Modelo (pedido explícito do Rui, 2026-10-02, confirmado contra um talão
# real): cada ENTRADA é um "lote" identificado pelo nº de talão, com um
# artigo (tipo IN/MT + comprimento em metros + espessura normal/fina — lido
# do campo "Produto" do talão, ex: "006-IN - MADEIRA PINHO 2,35 16 ACIMA";
# a espécie é sempre Pinho, por isso não é guardada como campo variável) e
# uma categoria de qualidade, derivada do índice IGQC já avaliado para esse
# talão (ver agents/qualidade_toros_ecos_largos — a avaliação em si NUNCA é
# duplicada aqui, só consultada por número de talão; se ainda não existir,
# corre-se a mesma avaliação na hora, a partir da mesma foto).
#
# Uma SAÍDA ou CORREÇÃO negativa consome por FIFO as entradas mais antigas
# do MESMO artigo com saldo — cada kg que sai carrega consigo a categoria
# de qualidade exata de onde veio, nunca uma aproximação (ver _aplicar_fifo
# e a tabela parkin_depletions, partilhada pelas duas, para auditoria).
import base64, io, re
from datetime import date, datetime
import anthropic
import db
from tools import visao

_client = anthropic.Anthropic()

CATEGORIAS_QUALIDADE = ["Boa", "Media", "Fraca"]
TIPOS_VALIDOS = ("IN", "MT")
ESPESSURAS_VALIDAS = ("normal", "fina")

# faixas do manual real "Manual Qualidade de Cargas - Toros" (Basecamp),
# secção "Classificação Final": 90-100% Excelente, 75-89% Boa, 60-74%
# Aceitável, 40-59% Fraca, <40% Rejeição. O Park In usa só 3 categorias
# (pedido do Rui, 2026-10-02) — Excelente+Boa juntam-se em "Boa" (≥75%),
# Aceitável fica "Media" (60-74%), Fraca+Rejeição juntam-se em "Fraca"
# (<60%), para nunca mostrar uma classificação diferente da que já está
# escrita na avaliação detalhada da mesma carga (bug real, 2026-10-02: os
# limiares 80/50 que o Rui confirmou sem cruzar com o manual classificavam
# uma carga de 78% — "Boa (faixa 75-89%)" no texto — como "Media" aqui).
LIMIAR_BOA = 75
LIMIAR_MEDIA = 60


def _categoria_de_indice(indice: float) -> str:
    if indice is None:
        return None
    if indice >= LIMIAR_BOA:
        return "Boa"
    if indice >= LIMIAR_MEDIA:
        return "Media"
    return "Fraca"


def _chave_artigo(tipo: str, comprimento: float, espessura: str) -> str:
    return f"{tipo}|{comprimento}|{espessura}"


_FERRAMENTA_EXTRAIR_TALAO = {
    "name": "extrair_talao",
    "description": "Extrai os campos do talão de pesagem fotografado, exatamente como estão impressos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "talao": {"type": "string", "description": "número do talão de pesagem (ex: \"11293\")"},
            "data": {"type": "string", "description": "data da pesagem, no formato YYYY-MM-DD"},
            "fornecedor": {"type": "string", "description": "fornecedor, tal como escrito (ex: \"018 - UNIMADEIRAS\") — vazio se não houver (ex: num talão de consumo interno)"},
            "matricula": {"type": "string", "description": "matrícula do veículo, se visível"},
            "guia_req": {"type": "string", "description": "nº da guia/requisição, se visível"},
            "produto": {"type": "string", "description": "o texto completo do campo \"Produto\", tal como escrito (ex: \"006-IN - MADEIRA PINHO 2,35 16 ACIMA\")"},
            "peso_bruto_kg": {"type": "number", "description": "peso bruto em kg, só o número"},
            "tara_kg": {"type": "number", "description": "tara em kg, só o número"},
            "peso_liquido_kg": {"type": "number", "description": "peso líquido em kg, só o número"},
        },
        "required": ["talao", "produto", "peso_liquido_kg"],
    },
}


def _extrair_campos_talao(bruto: bytes, content_type: str) -> dict:
    """Lê um talão de pesagem fotografado e devolve os campos em bruto,
    tal como impressos — nunca interpretados/calculados aqui (ver
    _interpretar_produto para o parsing de tipo/comprimento/espessura a
    partir do campo "produto", sempre em código, nunca confiado ao
    modelo). Devolve {"erro": ...} se não conseguir ler um talão na foto."""
    bruto, media_type = visao._preparar_imagem(bruto, content_type)
    imagem_b64 = base64.b64encode(bruto).decode()
    try:
        resposta = _client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            tools=[_FERRAMENTA_EXTRAIR_TALAO], tool_choice={"type": "tool", "name": "extrair_talao"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": imagem_b64}},
                    {"type": "text", "text": "Lê este talão de pesagem da balança e extrai os campos indicados, "
                                             "exatamente como estão impressos — nunca calcules nem corrijas "
                                             "nada (ex: nunca recalcules o peso líquido a partir do bruto e da "
                                             "tara), transcreve tal e qual aparece."}
                ]
            }]
        )
    except Exception as e:
        return {"erro": f"não consegui processar esta imagem: {e}"}
    bloco = next((b for b in resposta.content if b.type == "tool_use"), None)
    if not bloco:
        return {"erro": "não consegui reconhecer um talão de pesagem nesta foto"}
    return bloco.input


def _interpretar_produto(produto: str) -> dict:
    """Extrai tipo (IN/MT), comprimento (metros) e espessura (normal/fina)
    do campo "Produto" do talão (ex: "006-IN - MADEIRA PINHO 2,35 16
    ACIMA") — sempre em código (regex), nunca confiado ao modelo, pela
    mesma razão de sempre neste projeto: valores estruturados extraídos
    de texto nunca se confiam à interpretação livre de um LLM. Pedido
    explícito do Rui (2026-10-02): "madeira fina é 16 acima" — o sufixo
    "ACIMA" no talão marca espessura fina; qualquer outro caso (incluindo
    "ABAIXO" ou nenhum sufixo) é normal."""
    m_tipo = re.search(r"-\s*(IN|MT)\b", produto, re.IGNORECASE) or re.search(r"\b(IN|MT)\b", produto, re.IGNORECASE)
    tipo = m_tipo.group(1).upper() if m_tipo else None

    m_comp = re.search(r"(\d+,\d+)", produto)
    comprimento = float(m_comp.group(1).replace(",", ".")) if m_comp else None

    espessura = "fina" if "ACIMA" in produto.upper() else "normal"

    if not tipo or comprimento is None:
        return {"tipo": None, "comprimento": None, "espessura": None,
               "erro": f"não consegui reconhecer o tipo (IN/MT) e/ou o comprimento no campo "
                      f"\"Produto\" deste talão: {produto!r}"}
    return {"tipo": tipo, "comprimento": comprimento, "espessura": espessura, "erro": None}


_RE_PERCENTAGEM_AVALIACAO = re.compile(r"Percentagem[:\*\s]*.*?(\d{1,3}(?:[.,]\d+)?)\s*%", re.IGNORECASE)


def _extrair_percentagem_de_texto(avaliacao_texto: str):
    """Fallback para avaliações antigas (anteriores à coluna indice_igqc):
    lê a percentagem a partir da linha "Percentagem:" do texto já escrito
    pela avaliação, em vez de tentar reavaliar a carga sem as fotos
    originais da madeira (que o Park In não tem — só a foto do talão).
    Best-effort: se o texto não seguir o formato esperado, devolve None,
    nunca inventa um número."""
    if not avaliacao_texto:
        return None
    m = _RE_PERCENTAGEM_AVALIACAO.search(avaliacao_texto)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _resolver_data(data_str: str):
    try:
        return datetime.strptime((data_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _avaliar_qualidade_inline(bruto: bytes, content_type: str, talao: str, fornecedor: str = None):
    """Corre a MESMA missão de avaliação de qualidade usada no chat (ver
    agents/qualidade_toros_ecos_largos) a partir desta mesma foto, só
    quando este talão ainda não tinha sido avaliado antes — para o
    registo de entrada no Park In nunca ficar bloqueado à espera de
    alguém enviar a foto ao chat primeiro. Import feito aqui dentro (não
    no topo do módulo) de propósito: mantém tools/ não dependente de
    agents/ no caminho normal de arranque, só quando mesmo é preciso.
    Devolve o índice IGQC (0-100) se a avaliação tiver corrido e sido
    guardada com sucesso, ou None caso contrário."""
    from agents import qualidade_toros_ecos_largos
    transcricao = visao.descrever_imagem(bruto, content_type)
    contexto = (f"[Foto do talão anexada, já transcrita abaixo]\n{transcricao}\n\n"
               f"Nº do talão (já confirmado, usa exatamente este valor): {talao}\n"
               + (f"Fornecedor (já confirmado): {fornecedor}\n" if fornecedor else "")
               + "\nAvalia a qualidade desta carga de toros.")
    try:
        qualidade_toros_ecos_largos.responder("Park In (registo automático)",
                                              [{"role": "user", "content": contexto}])
    except Exception as e:
        print(f"[parkin] falhou a avaliação de qualidade em linha do talão {talao!r}: {e!r}")
        return None
    avaliacao = db.avaliacao_carga_toros_por_talao(talao)
    if avaliacao and avaliacao.get("indice_igqc") is not None:
        return float(avaliacao["indice_igqc"])
    return None


def registar_entrada(bruto: bytes, content_type: str, registado_por: str = None) -> dict:
    """Regista uma entrada no Park In a partir da foto de um talão de
    pesagem — extrai os campos, procura (ou corre na hora) a avaliação de
    qualidade desse talão, e grava o lote com saldo_kg = peso líquido."""
    campos = _extrair_campos_talao(bruto, content_type)
    if "erro" in campos:
        return campos

    talao = str(campos.get("talao") or "").strip()
    if not talao:
        return {"erro": "não consegui ler o número do talão nesta foto"}

    interpretado = _interpretar_produto(campos.get("produto") or "")
    if interpretado["erro"]:
        return {"erro": interpretado["erro"]}

    peso_liquido = campos.get("peso_liquido_kg")
    if not peso_liquido:
        return {"erro": "não consegui ler o peso líquido neste talão"}

    fornecedor = (campos.get("fornecedor") or "").strip() or "(fornecedor não identificado)"
    data_resolvida = _resolver_data(campos.get("data"))

    indice = None
    avaliacao = db.avaliacao_carga_toros_por_talao(talao)
    if avaliacao and avaliacao.get("indice_igqc") is not None:
        indice = float(avaliacao["indice_igqc"])
    elif avaliacao and avaliacao.get("avaliacao"):
        # já avaliado antes de indice_igqc existir como coluna — lê a
        # percentagem do texto em vez de reavaliar sem as fotos da
        # madeira (que não temos aqui, só a foto do talão), e guarda o
        # valor lido para os próximos lookups não precisarem de repetir
        # este parsing.
        indice = _extrair_percentagem_de_texto(avaliacao["avaliacao"])
        if indice is not None:
            db.definir_indice_igqc_avaliacao(avaliacao["id"], indice)
    if indice is None:
        indice = _avaliar_qualidade_inline(bruto, content_type, talao, fornecedor)

    categoria = _categoria_de_indice(indice)
    id_gerado = db.guardar_entrada_parkin(
        talao=talao, fornecedor=fornecedor, data=data_resolvida,
        tipo=interpretado["tipo"], comprimento=interpretado["comprimento"], espessura=interpretado["espessura"],
        peso_liquido_kg=float(peso_liquido), matricula=campos.get("matricula"), guia_req=campos.get("guia_req"),
        peso_bruto_kg=campos.get("peso_bruto_kg"), tara_kg=campos.get("tara_kg"),
        indice_igqc=indice, categoria_qualidade=categoria, registado_por=registado_por)

    resultado = {"ok": True, "id": id_gerado, "talao": talao, "fornecedor": fornecedor,
                "artigo": _chave_artigo(interpretado["tipo"], interpretado["comprimento"], interpretado["espessura"]),
                "peso_liquido_kg": float(peso_liquido), "indice_igqc": indice, "categoria_qualidade": categoria}
    if categoria is None:
        resultado["aviso"] = ("a entrada ficou registada, mas ainda sem categoria de qualidade — não foi "
                              "possível avaliar esta carga automaticamente")
    return resultado


def _saldo_disponivel(tipo: str, comprimento: float, espessura: str) -> float:
    return sum(float(e["saldo_kg"]) for e in db.entradas_parkin_com_saldo(tipo, comprimento, espessura))


def _aplicar_fifo(tipo: str, comprimento: float, espessura: str, quantidade_kg: float,
                  saida_id: int = None, correcao_id: int = None) -> dict:
    """Deplete `quantidade_kg` das entradas mais antigas deste artigo
    exato com saldo (FIFO), gravando cada depleção para auditoria (ver
    db.guardar_depletion_parkin). Assume que o chamador já confirmou que
    há saldo suficiente (ver _saldo_disponivel) — mesmo assim devolve
    {"erro": ...} sem tocar em nada se não houver, como rede de segurança."""
    entradas = db.entradas_parkin_com_saldo(tipo, comprimento, espessura)
    disponivel = sum(float(e["saldo_kg"]) for e in entradas)
    if disponivel + 1e-6 < quantidade_kg:
        return {"erro": (f"só há {disponivel:.0f} kg em stock para o artigo {tipo} {comprimento} {espessura} "
                         f"— não é possível tirar {quantidade_kg:.0f} kg")}
    restante = quantidade_kg
    depletadas = []
    for e in entradas:
        if restante <= 1e-9:
            break
        tirar = min(float(e["saldo_kg"]), restante)
        db.descontar_saldo_entrada_parkin(e["id"], tirar)
        db.guardar_depletion_parkin(e["id"], tirar, saida_id=saida_id, correcao_id=correcao_id)
        depletadas.append({"entrada_id": e["id"], "talao": e["talao"], "quantidade_kg": round(tirar, 1)})
        restante -= tirar
    return {"depletadas": depletadas}


def registar_saida(bruto: bytes, content_type: str, registado_por: str = None) -> dict:
    """Regista uma saída (consumo) a partir da foto de um talão — extrai o
    artigo e a quantidade, e deplete por FIFO as entradas mais antigas
    desse artigo com saldo."""
    campos = _extrair_campos_talao(bruto, content_type)
    if "erro" in campos:
        return campos

    interpretado = _interpretar_produto(campos.get("produto") or "")
    if interpretado["erro"]:
        return {"erro": interpretado["erro"]}

    peso_liquido = campos.get("peso_liquido_kg")
    if not peso_liquido:
        return {"erro": "não consegui ler o peso líquido neste talão"}

    tipo, comprimento, espessura = interpretado["tipo"], interpretado["comprimento"], interpretado["espessura"]
    quantidade = float(peso_liquido)
    disponivel = _saldo_disponivel(tipo, comprimento, espessura)
    if disponivel + 1e-6 < quantidade:
        return {"erro": (f"só há {disponivel:.0f} kg em stock para o artigo {tipo} {comprimento} {espessura} "
                         f"— não é possível tirar {quantidade:.0f} kg")}

    data_resolvida = _resolver_data(campos.get("data"))
    saida_id = db.guardar_saida_parkin(tipo, comprimento, espessura, quantidade, data_resolvida,
                                       talao=campos.get("talao"), registado_por=registado_por)
    resultado = _aplicar_fifo(tipo, comprimento, espessura, quantidade, saida_id=saida_id)
    return {"ok": True, "id": saida_id, "artigo": _chave_artigo(tipo, comprimento, espessura),
           "quantidade_kg": quantidade, **resultado}


def registar_correcao(tipo: str, comprimento: float, espessura: str, quantidade_kg: float, motivo: str,
                      categoria_qualidade: str = None, registado_por: str = None) -> dict:
    """Lançamento manual de uma correção de stock — `quantidade_kg` com
    sinal: positivo é uma sobra/ajuste de inventário (pede
    `categoria_qualidade` à mão, porque não vem de nenhuma entrada);
    negativo é uma remoção, depletada por FIFO como uma saída normal."""
    if tipo not in TIPOS_VALIDOS:
        return {"erro": f"tipo inválido: {tipo!r} — tem de ser um de {TIPOS_VALIDOS}"}
    if espessura not in ESPESSURAS_VALIDAS:
        return {"erro": f"espessura inválida: {espessura!r} — tem de ser uma de {ESPESSURAS_VALIDAS}"}
    if not quantidade_kg:
        return {"erro": "a quantidade não pode ser zero"}
    if not motivo or not motivo.strip():
        return {"erro": "o motivo é obrigatório"}

    data_hoje = date.today()
    if quantidade_kg > 0:
        if categoria_qualidade not in CATEGORIAS_QUALIDADE:
            return {"erro": f"categoria de qualidade obrigatória para uma correção positiva — "
                           f"uma de {CATEGORIAS_QUALIDADE}"}
        id_gerado = db.guardar_correcao_parkin(tipo, comprimento, espessura, quantidade_kg, motivo, data_hoje,
                                               categoria_qualidade=categoria_qualidade, registado_por=registado_por)
        return {"ok": True, "id": id_gerado}

    disponivel = _saldo_disponivel(tipo, comprimento, espessura)
    if disponivel + 1e-6 < abs(quantidade_kg):
        return {"erro": (f"só há {disponivel:.0f} kg em stock para este artigo — não é possível remover "
                         f"{abs(quantidade_kg):.0f} kg")}
    id_gerado = db.guardar_correcao_parkin(tipo, comprimento, espessura, quantidade_kg, motivo, data_hoje,
                                           registado_por=registado_por)
    resultado = _aplicar_fifo(tipo, comprimento, espessura, abs(quantidade_kg), correcao_id=id_gerado)
    return {"ok": True, "id": id_gerado, **resultado}


def stock_total() -> dict:
    """Stock total atual, por categoria de qualidade — para a barra
    empilhada principal do dashboard. Inclui o saldo das entradas e as
    correções positivas; as negativas já estão refletidas no saldo das
    entradas que depletaram (ver _aplicar_fifo)."""
    por_categoria = {c: 0.0 for c in CATEGORIAS_QUALIDADE}
    sem_categoria = 0.0
    for e in db.entradas_parkin_todas():
        saldo = float(e["saldo_kg"] or 0)
        if saldo <= 1e-6:
            continue
        if e["categoria_qualidade"] in por_categoria:
            por_categoria[e["categoria_qualidade"]] += saldo
        else:
            sem_categoria += saldo
    for c in db.correcoes_parkin_positivas():
        if c["categoria_qualidade"] in por_categoria:
            por_categoria[c["categoria_qualidade"]] += float(c["quantidade_kg"])

    limites = db.limites_parkin().get("total") or {}
    return {
        "total_kg": round(sum(por_categoria.values()) + sem_categoria, 1),
        "por_categoria_kg": {k: round(v, 1) for k, v in por_categoria.items()},
        "sem_categoria_kg": round(sem_categoria, 1),
        "minimo_kg": limites.get("minimo_kg"),
        "maximo_kg": limites.get("maximo_kg"),
    }


def stock_por_artigo() -> list[dict]:
    """Stock atual por artigo (tipo+comprimento+espessura), com a mesma
    repartição por categoria de qualidade — para as barras de "stock por
    artigo" do dashboard, ordenadas da maior para a menor quantidade."""
    artigos = {}

    def _bucket(chave, tipo, comprimento, espessura):
        return artigos.setdefault(chave, {
            "tipo": tipo, "comprimento": comprimento, "espessura": espessura,
            "por_categoria_kg": {c: 0.0 for c in CATEGORIAS_QUALIDADE}, "sem_categoria_kg": 0.0,
        })

    for e in db.entradas_parkin_todas():
        saldo = float(e["saldo_kg"] or 0)
        if saldo <= 1e-6:
            continue
        comprimento = float(e["comprimento"])
        chave = _chave_artigo(e["tipo"], comprimento, e["espessura"])
        a = _bucket(chave, e["tipo"], comprimento, e["espessura"])
        if e["categoria_qualidade"] in a["por_categoria_kg"]:
            a["por_categoria_kg"][e["categoria_qualidade"]] += saldo
        else:
            a["sem_categoria_kg"] += saldo

    for c in db.correcoes_parkin_positivas():
        comprimento = float(c["comprimento"])
        chave = _chave_artigo(c["tipo"], comprimento, c["espessura"])
        a = _bucket(chave, c["tipo"], comprimento, c["espessura"])
        if c["categoria_qualidade"] in a["por_categoria_kg"]:
            a["por_categoria_kg"][c["categoria_qualidade"]] += float(c["quantidade_kg"])

    limites = db.limites_parkin()
    resultado = []
    for chave, a in artigos.items():
        total = sum(a["por_categoria_kg"].values()) + a["sem_categoria_kg"]
        if total <= 1e-6:
            continue
        lim = limites.get(chave) or {}
        resultado.append({
            "artigo": chave, "tipo": a["tipo"], "comprimento": a["comprimento"], "espessura": a["espessura"],
            "total_kg": round(total, 1),
            "por_categoria_kg": {k: round(v, 1) for k, v in a["por_categoria_kg"].items()},
            "sem_categoria_kg": round(a["sem_categoria_kg"], 1),
            "minimo_kg": lim.get("minimo_kg"), "maximo_kg": lim.get("maximo_kg"),
        })
    resultado.sort(key=lambda x: -x["total_kg"])
    return resultado


def dados_dashboard(dias_top_entradas: int = 30) -> dict:
    return {
        "stock_total": stock_total(),
        "stock_por_artigo": stock_por_artigo(),
        "top_qualidade": db.top_qualidade_fornecedores_parkin(3),
        "top_entradas": db.top_entradas_fornecedores_parkin(dias_top_entradas, 5),
    }


def definir_limite(chave: str, minimo_kg: float = None, maximo_kg: float = None) -> dict:
    db.definir_limite_parkin(chave, minimo_kg, maximo_kg)
    return {"ok": True}


def pagina_park_in() -> str:
    return _TEMPLATE


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Park In — Ecos Largos</title>
<style>
  :root{
    --paper:#FFFFFF; --canvas:#F5F5F3; --raise:#FBFBF9;
    --ink:#1A1C1E; --dim:#75797D;
    --line:#E8E8E3; --edge:#D9D9D2;
    --blue:#1B6AC9; --gold:#E0A02C; --red:#C4452E; --grey:#9AA0A6; --green:#2E9E4F;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--canvas);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
    font-size:15px;line-height:1.4;-webkit-font-smoothing:antialiased}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
  button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
  .wrap{max-width:1100px;margin:0 auto;padding:22px 14px 120px}
  header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px}
  h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0}
  .sub{color:var(--dim);font-size:14px}
  .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:16px 0}
  .btn{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:7px 14px;font-size:14px;color:var(--blue);font-weight:600}
  .btn:hover{border-color:var(--dim)}
  .btn.primary{background:var(--blue);color:#fff;border-color:var(--blue)}
  .btn.primary:hover{background:#175CAF;border-color:#175CAF}
  .btn.ghost{color:var(--dim)}
  .btn:disabled{opacity:.5;cursor:default}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:4px}
  @media (max-width:760px){.grid{grid-template-columns:1fr}}
  .board{background:var(--paper);border:1px solid var(--line);border-radius:12px;
    padding:18px 18px 20px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .board h2{font-size:15px;font-weight:700;margin:0 0 14px}
  .board h2 .nota{font-weight:400;font-size:11.5px;color:var(--dim)}

  .gauge{position:relative;height:28px;background:var(--canvas);border-radius:7px;overflow:visible;margin:22px 0 6px}
  .gauge .fill{position:absolute;top:0;left:0;height:100%;display:flex;border-radius:7px;overflow:hidden;transition:width .3s}
  .gauge .seg{height:100%}
  .gauge .lim{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ink);z-index:2}
  .gauge .lim.max{background:var(--red)}
  .gauge .lim span{position:absolute;top:-16px;left:50%;transform:translateX(-50%);
    font-size:10px;color:var(--dim);white-space:nowrap}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--dim)}
  .legend .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
  .artigo-row{margin-bottom:22px}
  .artigo-row:last-child{margin-bottom:0}
  .artigo-row .titulo{font-size:13.5px;font-weight:600;display:flex;justify-content:space-between}
  .artigo-row .titulo .tot{color:var(--dim);font-weight:500}

  .rank{list-style:none;margin:0;padding:0}
  .rank li{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:13.5px}
  .rank li:first-child{border-top:none}
  .rank .nome{font-weight:600}
  .rank .val{color:var(--dim);white-space:nowrap}
  .rank .vazio{color:var(--dim);font-weight:400}
  .cols2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .cols2 h3{font-size:11.5px;color:var(--dim);margin:0 0 6px;text-transform:uppercase;letter-spacing:.03em}

  .veil{position:fixed;inset:0;background:rgba(26,28,30,.35);display:none;z-index:100}
  .veil.on{display:block}
  .sheet{position:fixed;top:50%;left:50%;background:var(--paper);z-index:101;
    border-radius:16px;padding:22px 20px 24px;width:min(480px,calc(100vw - 32px));
    box-sizing:border-box;box-shadow:0 12px 40px rgba(0,0,0,.25);
    max-height:calc(100vh - 64px);overflow-y:auto;overscroll-behavior:contain;
    transform:translate(-50%,-50%) scale(.96);opacity:0;pointer-events:none;
    transition:transform .18s ease,opacity .18s ease}
  .sheet.on{transform:translate(-50%,-50%) scale(1);opacity:1;pointer-events:auto}
  .sheet h3{margin:0 0 14px;font-size:19px;font-weight:700;letter-spacing:-.01em}
  .frow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
  .frow:first-of-type{border-top:none}
  .frow label{color:var(--dim);font-size:14px;flex:0 0 auto}
  .frow input,.frow select{flex:1 1 auto;min-width:0;max-width:62%}
  select,input,textarea{background:var(--paper);color:var(--ink);border:1px solid var(--edge);
    border-radius:8px;padding:6px 9px;font:inherit;font-size:14px}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:0;border-color:var(--blue)}
  .acts{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;justify-content:flex-end}
  .err{color:var(--red);font-size:13px;margin-top:10px}
  .ok{color:var(--green);font-size:13px;margin-top:10px}
  .close{position:absolute;top:14px;right:16px;color:var(--dim);font-size:22px;line-height:1}
  .vazio{color:var(--dim);font-size:13.5px;padding:14px 0;text-align:center}
</style>
</head>
<body>
<div class="veil" id="veil"></div>

<div class="sheet" id="sheetEntrada">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Nova entrada</h3>
  <p class="sub" style="margin:0 0 12px">Foto do talão de pesagem da balança.</p>
  <input type="file" id="fEntradaFoto" accept="image/*" capture="environment">
  <div class="acts">
    <button class="btn" data-close>Cancelar</button>
    <button class="btn primary" id="bEntradaGuardar">Guardar</button>
  </div>
  <div id="entradaMsg"></div>
</div>

<div class="sheet" id="sheetSaida">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Nova saída</h3>
  <p class="sub" style="margin:0 0 12px">Fotos dos talões de consumo — escolhe várias de uma vez.</p>
  <input type="file" id="fSaidaFotos" accept="image/*" multiple>
  <div class="acts">
    <button class="btn" data-close>Cancelar</button>
    <button class="btn primary" id="bSaidaGuardar">Guardar</button>
  </div>
  <div id="saidaMsg"></div>
</div>

<div class="sheet" id="sheetCorrecao">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Correção de stock</h3>
  <div class="frow"><label>Tipo</label>
    <select id="cTipo"><option value="IN">IN</option><option value="MT">MT</option></select>
  </div>
  <div class="frow"><label>Comprimento (m)</label>
    <input type="text" inputmode="decimal" id="cComprimento" placeholder="ex: 2,35">
  </div>
  <div class="frow"><label>Espessura</label>
    <select id="cEspessura"><option value="normal">Normal</option><option value="fina">Fina</option></select>
  </div>
  <div class="frow"><label>Quantidade (kg)</label>
    <input type="text" inputmode="decimal" id="cQuantidade" placeholder="+ adiciona, − remove">
  </div>
  <div class="frow" id="cCategoriaRow"><label>Categoria</label>
    <select id="cCategoria"><option value="Boa">Boa</option><option value="Media">Média</option><option value="Fraca">Fraca</option></select>
  </div>
  <div class="frow"><label>Motivo</label>
    <input type="text" id="cMotivo" placeholder="ex: inventário, erro de leitura…">
  </div>
  <div class="acts">
    <button class="btn" data-close>Cancelar</button>
    <button class="btn primary" id="bCorrecaoGuardar">Guardar</button>
  </div>
  <div id="correcaoMsg"></div>
</div>

<div class="sheet" id="sheetLimites" style="width:min(560px,calc(100vw - 32px))">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Limites de stock</h3>
  <div id="limitesLista"></div>
  <div class="acts">
    <button class="btn" data-close>Fechar</button>
    <button class="btn primary" id="bLimitesGuardar">Guardar</button>
  </div>
  <div id="limitesMsg"></div>
</div>

<div class="wrap">
  <header>
    <h1>Park In</h1>
    <div class="sub">Stock de toros · Ecos Largos</div>
  </header>

  <div class="bar">
    <button class="btn primary" id="bAbrirEntrada">+ Entrada</button>
    <button class="btn primary" id="bAbrirSaida">+ Saída</button>
    <button class="btn" id="bAbrirCorrecao">Correção</button>
    <button class="btn ghost" id="bAbrirLimites" style="margin-left:auto">Limites</button>
  </div>

  <div class="board" style="margin-bottom:16px">
    <h2>Stock total</h2>
    <div id="stockTotal"></div>
  </div>

  <div class="board" style="margin-bottom:16px">
    <h2>Stock por artigo</h2>
    <div id="stockArtigo"></div>
  </div>

  <div class="grid">
    <div class="board">
      <h2>Top qualidade</h2>
      <div class="cols2">
        <div><h3>Melhores</h3><ul class="rank" id="topQualidadeMelhores"></ul></div>
        <div><h3>Piores</h3><ul class="rank" id="topQualidadePiores"></ul></div>
      </div>
    </div>
    <div class="board">
      <h2>Top entradas <span class="nota">últimos 30 dias</span></h2>
      <ul class="rank" id="topEntradas"></ul>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const CORES={Boa:"#2E9E4F",Media:"#E0A02C",Fraca:"#C4452E"};
const NOMES_CAT={Boa:"Boa",Media:"Média",Fraca:"Fraca"};
function t(kg){ return (kg/1000).toLocaleString("pt-PT",{minimumFractionDigits:1,maximumFractionDigits:1})+" t"; }
function pct(v,tot){ return tot>0 ? Math.round(v/tot*100) : 0; }

function renderGauge(porCategoria, semCategoria, totalKg, minimo, maximo){
  if(totalKg<=0) return "";
  const escala = Math.max(totalKg, maximo||0, 1) * 1.15;
  let segs = "";
  for(const cat of ["Boa","Media","Fraca"]){
    const v = porCategoria[cat]||0;
    if(v<=0) continue;
    segs += `<div class="seg" style="width:${v/totalKg*100}%;background:${CORES[cat]}" title="${NOMES_CAT[cat]}: ${t(v)}"></div>`;
  }
  if(semCategoria>0){
    segs += `<div class="seg" style="width:${semCategoria/totalKg*100}%;background:var(--grey)" title="Sem avaliação: ${t(semCategoria)}"></div>`;
  }
  const largura = totalKg/escala*100;
  let linhas = "";
  if(minimo!=null) linhas += `<div class="lim" style="left:${minimo/escala*100}%"><span>mín ${t(minimo)}</span></div>`;
  if(maximo!=null) linhas += `<div class="lim max" style="left:${maximo/escala*100}%"><span>máx ${t(maximo)}</span></div>`;
  return `<div class="gauge"><div class="fill" style="width:${largura}%">${segs}</div>${linhas}</div>`;
}

function legenda(porCategoria, semCategoria, totalKg){
  let html = "";
  for(const cat of ["Boa","Media","Fraca"]){
    const v = porCategoria[cat]||0;
    if(v<=0 && totalKg<=0) continue;
    html += `<span><span class="dot" style="background:${CORES[cat]}"></span>${NOMES_CAT[cat]} ${pct(v,totalKg)}% · ${t(v)}</span>`;
  }
  if(semCategoria>0) html += `<span><span class="dot" style="background:var(--grey)"></span>Sem avaliação ${pct(semCategoria,totalKg)}% · ${t(semCategoria)}</span>`;
  return `<div class="legend">${html}</div>`;
}

let DADOS = null;

async function carregar(){
  const r = await fetch("/park-in/dados");
  DADOS = await r.json();
  renderStockTotal();
  renderStockArtigo();
  renderTopQualidade();
  renderTopEntradas();
}

function renderStockTotal(){
  const s = DADOS.stock_total;
  const el = $("#stockTotal");
  if(s.total_kg<=0){ el.innerHTML = '<div class="vazio">Ainda sem stock registado.</div>'; return; }
  el.innerHTML = legenda(s.por_categoria_kg, s.sem_categoria_kg, s.total_kg)
    + renderGauge(s.por_categoria_kg, s.sem_categoria_kg, s.total_kg, s.minimo_kg, s.maximo_kg)
    + `<div class="sub mono">Total: ${t(s.total_kg)}</div>`;
}

function renderStockArtigo(){
  const el = $("#stockArtigo");
  const lista = DADOS.stock_por_artigo;
  if(!lista.length){ el.innerHTML = '<div class="vazio">Ainda sem stock registado.</div>'; return; }
  el.innerHTML = lista.map(a => `
    <div class="artigo-row">
      <div class="titulo"><span>${a.tipo} · ${a.comprimento.toFixed(2)}m · ${a.espessura==="fina"?"Fina":"Normal"}</span>
        <span class="tot mono">${t(a.total_kg)}</span></div>
      ${renderGauge(a.por_categoria_kg, a.sem_categoria_kg, a.total_kg, a.minimo_kg, a.maximo_kg)}
    </div>
  `).join("");
}

function renderTopQualidade(){
  const tq = DADOS.top_qualidade;
  $("#topQualidadeMelhores").innerHTML = tq.melhores.length ? tq.melhores.map(f =>
    `<li><span class="nome">${f.fornecedor}</span><span class="val mono">${f.media}% (${f.n})</span></li>`
  ).join("") : '<li class="vazio">Sem dados</li>';
  $("#topQualidadePiores").innerHTML = tq.piores.length ? tq.piores.map(f =>
    `<li><span class="nome">${f.fornecedor}</span><span class="val mono">${f.media}% (${f.n})</span></li>`
  ).join("") : '<li class="vazio">Sem dados</li>';
}

function renderTopEntradas(){
  const lista = DADOS.top_entradas;
  $("#topEntradas").innerHTML = lista.length ? lista.map(f =>
    `<li><span class="nome">${f.fornecedor}</span><span class="val mono">${f.entregas}× · ${t(f.total_kg)}</span></li>`
  ).join("") : '<li class="vazio">Sem entregas recentes</li>';
}

function abrirSheet(id){ $("#veil").classList.add("on"); $(id).classList.add("on"); }
function fecharSheets(){ $("#veil").classList.remove("on");
  document.querySelectorAll(".sheet").forEach(s=>s.classList.remove("on")); }
$("#veil").onclick = fecharSheets;
document.querySelectorAll("[data-close]").forEach(b => b.onclick = fecharSheets);

$("#bAbrirEntrada").onclick = () => { $("#entradaMsg").innerHTML=""; $("#fEntradaFoto").value=""; abrirSheet("#sheetEntrada"); };
$("#bAbrirSaida").onclick = () => { $("#saidaMsg").innerHTML=""; $("#fSaidaFotos").value=""; abrirSheet("#sheetSaida"); };
$("#bAbrirCorrecao").onclick = () => { $("#correcaoMsg").innerHTML=""; abrirSheet("#sheetCorrecao"); };
$("#bAbrirLimites").onclick = () => { renderLimites(); abrirSheet("#sheetLimites"); };

$("#cCategoriaRow").style.display = "none";
$("#cQuantidade").addEventListener("input", () => {
  const v = parseFloat($("#cQuantidade").value.replace(",","."));
  $("#cCategoriaRow").style.display = (v>0) ? "flex" : "none";
});

$("#bEntradaGuardar").onclick = async () => {
  const f = $("#fEntradaFoto").files[0];
  if(!f){ $("#entradaMsg").innerHTML = '<div class="err">Escolhe uma foto do talão.</div>'; return; }
  $("#bEntradaGuardar").disabled = true;
  $("#entradaMsg").innerHTML = '<div class="sub">A ler o talão e avaliar a qualidade — pode demorar uns segundos…</div>';
  const fd = new FormData(); fd.append("ficheiro", f);
  try{
    const r = await fetch("/park-in/entrada", {method:"POST", body:fd});
    const j = await r.json();
    if(j.erro){ $("#entradaMsg").innerHTML = `<div class="err">${j.erro}</div>`; }
    else {
      $("#entradaMsg").innerHTML = `<div class="ok">Registado: talão ${j.talao}, ${t(j.peso_liquido_kg)}, `+
        `categoria ${j.categoria_qualidade||"(por avaliar)"}.</div>`;
      carregar();
    }
  } catch(e){ $("#entradaMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bEntradaGuardar").disabled = false;
};

$("#bSaidaGuardar").onclick = async () => {
  const fs = $("#fSaidaFotos").files;
  if(!fs.length){ $("#saidaMsg").innerHTML = '<div class="err">Escolhe pelo menos uma foto.</div>'; return; }
  $("#bSaidaGuardar").disabled = true;
  $("#saidaMsg").innerHTML = '<div class="sub">A processar…</div>';
  const fd = new FormData();
  for(const f of fs) fd.append("ficheiros", f);
  try{
    const r = await fetch("/park-in/saida", {method:"POST", body:fd});
    const j = await r.json();
    $("#saidaMsg").innerHTML = j.resultados.map(res =>
      res.erro ? `<div class="err">${res.ficheiro}: ${res.erro}</div>`
               : `<div class="ok">${res.ficheiro}: ${t(res.quantidade_kg)} (${res.artigo})</div>`
    ).join("");
    carregar();
  } catch(e){ $("#saidaMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bSaidaGuardar").disabled = false;
};

$("#bCorrecaoGuardar").onclick = async () => {
  const corpo = {
    tipo: $("#cTipo").value,
    comprimento: parseFloat($("#cComprimento").value.replace(",",".")),
    espessura: $("#cEspessura").value,
    quantidade_kg: parseFloat($("#cQuantidade").value.replace(",",".")),
    motivo: $("#cMotivo").value,
    categoria_qualidade: $("#cCategoria").value,
  };
  if(!corpo.comprimento || !corpo.quantidade_kg || !corpo.motivo){
    $("#correcaoMsg").innerHTML = '<div class="err">Preenche comprimento, quantidade e motivo.</div>'; return;
  }
  $("#bCorrecaoGuardar").disabled = true;
  try{
    const r = await fetch("/park-in/correcao", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(corpo)});
    const j = await r.json();
    if(j.erro){ $("#correcaoMsg").innerHTML = `<div class="err">${j.erro}</div>`; }
    else { $("#correcaoMsg").innerHTML = '<div class="ok">Correção registada.</div>'; carregar(); }
  } catch(e){ $("#correcaoMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bCorrecaoGuardar").disabled = false;
};

function renderLimites(){
  const linhas = [{chave:"total", rotulo:"Stock total", lim:DADOS.stock_total}]
    .concat(DADOS.stock_por_artigo.map(a => ({chave:a.artigo,
      rotulo:`${a.tipo} · ${a.comprimento.toFixed(2)}m · ${a.espessura==="fina"?"Fina":"Normal"}`, lim:a})));
  $("#limitesLista").innerHTML = linhas.map(l => `
    <div class="frow"><label>${l.rotulo}</label>
      <span style="display:flex;gap:6px">
        <input type="text" inputmode="decimal" style="width:80px" data-chave="${l.chave}" data-campo="minimo_kg" placeholder="mín kg" value="${l.lim.minimo_kg??''}">
        <input type="text" inputmode="decimal" style="width:80px" data-chave="${l.chave}" data-campo="maximo_kg" placeholder="máx kg" value="${l.lim.maximo_kg??''}">
      </span>
    </div>
  `).join("");
}

$("#bLimitesGuardar").onclick = async () => {
  const chaves = {};
  document.querySelectorAll("#limitesLista input").forEach(inp => {
    const chave = inp.dataset.chave;
    chaves[chave] = chaves[chave] || {chave};
    const v = inp.value.trim();
    chaves[chave][inp.dataset.campo] = v ? parseFloat(v.replace(",",".")) : null;
  });
  $("#bLimitesGuardar").disabled = true;
  try{
    for(const chave in chaves){
      await fetch("/park-in/limites", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(chaves[chave])});
    }
    $("#limitesMsg").innerHTML = '<div class="ok">Guardado.</div>';
    carregar();
  } catch(e){ $("#limitesMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bLimitesGuardar").disabled = false;
};

carregar();
setInterval(carregar, 60000);
</script>
</body>
</html>
"""
