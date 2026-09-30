# tools/planeamento_entradas.py — página "Planeamento de Entradas" da Ecos
# Largos: planear o que entra nos charriots (Charriot 1/2/3, Multiserra de
# Toros), um dia antes do início da produção de cada OF (pedido explícito
# do Rui, 2026-10-01) — uma OF pode estar em vários charriots ao mesmo
# tempo (pedido explícito do Rui, 2026-10-02).
#
# Segue exatamente a mesma filosofia da logística (ver
# tools/planeamento_serracao.py): não duplica nome/linha/dia de início/
# volume/tipo de madeira — isso lê-se sempre em direto da OF de produção já
# agendada (mesmos _cards_of_ativos/agendamentos_producao_ecos_largos), e só
# se guarda aqui o que é específico desta página (charriots atribuídos —
# pode estar em vários ao mesmo tempo — + os campos WIP/Toro preenchidos à
# mão). Por isso não existe nenhuma função de
# "replicar" propriamente dita — uma OF agendada na outra página aparece
# aqui sozinha, sempre que se lê o estado (ver estado_planeamento_entradas),
# exatamente como uma OF com tipo de madeira definido aparece sozinha na
# logística.
import re
from datetime import date, timedelta
import db
from tools import planeamento_serracao as ps

CHARRIOTS = ["Charriot 1", "Charriot 2", "Charriot 3", "Multiserra de Toros"]
# valores corrigidos (pedido explícito do Rui, 2026-10-02 — os antigos
# 2600/2500/3100/2350/2550 estavam com a escala errada) + duas opções
# novas com diâmetro marcado ("⌀16"), por isso texto e não número puro —
# ver nota na migração de toro_cmp para TEXT, em db.py.
TORO_CMP_PRESETS = ["260", "250", "310", "235", "255", "255 ⌀16", "235 ⌀16"]
TORO_TIPOS = {"IN", "MT"}

# QTD Toros = volume (m³) × INDICE_TOROS; QTD Wip = volume (m³) × INDICE_WIP
# — pedido explícito do Rui (2026-10-01). Cada OF pode ter o seu próprio
# índice (ver guardar_wip/guardar_toro); estes são só os valores por
# omissão usados enquanto ninguém escolher outro à mão para essa OF em
# concreto (ver db.entradas_charriot_ecos_largos, colunas indice_toros/
# indice_wip).
INDICE_TOROS_DEFAULT = 2.85
INDICE_WIP_DEFAULT = 1.8

# "quadradilho" = título sem "OF" (pedido explícito do Rui, 2026-10-01,
# ex: "Fepal — Quadradilho MT", "Girona — 2600 MT" não têm "OF"; "Palcax
# OF 508" tem). Por omissão entra "em contínuo" (ver _calcular_dia_entrada)
# — a pessoa pode sempre desmarcar à mão para uma OF em concreto (ver
# definir_em_continuo), e essa escolha fica gravada em definitivo.
_PADRAO_OF = re.compile(r"(?<![a-zà-ÿ])of(?![a-zà-ÿ])", re.IGNORECASE)

def _eh_quadradilho(titulo: str) -> bool:
    return not bool(_PADRAO_OF.search(titulo or ""))

# tamanhos WIP mais usados para "quadradilho" (pedido explícito do Rui,
# 2026-10-01) — não são regra fixa, por isso só preenchem por omissão um
# campo que ainda ninguém tenha escrito nada (ver estado_planeamento_
# entradas); continuam totalmente editáveis por OF.
WIP_CMP_QUADRADILHO_DEFAULT = 2500
WIP_LAR_QUADRADILHO_DEFAULT = 32
WIP_ESP_QUADRADILHO_DEFAULT = 32

def _calcular_dia_entrada(dia_inicio: str, em_continuo: bool) -> str:
    """Dia em que os troncos desta OF entram no charriot.

    "Em contínuo" (pedido explícito do Rui, 2026-10-01): a OF entra no
    MESMO dia do início da produção — salta o cálculo habitual (dia
    anterior). Usado sobretudo para quadradilho (ver _eh_quadradilho), mas
    qualquer OF pode ser marcada/desmarcada à mão (ver definir_em_continuo).

    Caso contrário (o cálculo habitual): um dia antes do início da
    produção. Se isso calhar a domingo (acontece sempre que a produção
    começa a uma segunda-feira), recua para o sábado anterior — só
    domingo não é dia de produção; sábado passou a ser (pedido explícito
    do Rui, 2026-10-03: a equipa trabalha ao sábado de manhã, mesma
    capacidade de um dia normal), por isso os charriots também podem
    receber entradas nesse dia."""
    if em_continuo:
        return dia_inicio
    dia = date.fromisoformat(dia_inicio) - timedelta(days=1)
    if dia.weekday() == 6:  # domingo
        dia -= timedelta(days=1)
    return dia.isoformat()

def estado_planeamento_entradas() -> dict:
    """Junta as OFs já agendadas na produção (mesma fonte da outra página)
    com o(s) charriot(s)/WIP/Toro guardados aqui — devolve todas as OFs
    "candidatas a entrada", cada uma já com a lista de charriots atribuídos
    (pode estar em vários ao mesmo tempo; lista vazia = "por atribuir") e o
    dia de entrada calculado (ver _calcular_dia_entrada)."""
    cards_ativos = ps._cards_of_ativos()
    agendamentos = {a["basecamp_card_id"]: a for a in db.agendamentos_producao_ecos_largos()}
    extras = {e["basecamp_card_id"]: e for e in db.entradas_charriot_ecos_largos()}

    entradas = []
    for c in cards_ativos:
        agendamento = agendamentos.get(c["id"])
        tem_agendamento = bool(agendamento and agendamento["linha"] and agendamento["dia_inicio"])
        if not tem_agendamento:
            continue
        # mesma regra da logística (ver estado_planeamento_serracao): uma OF
        # que já devia ter começado a produção e continua presa em
        # Triagem/Programação está em standby — o dia de entrada calculado
        # a partir do início antigo já não faz sentido nenhum, esconde-se
        # até ser reagendada com um novo dia de início.
        if (ps._normalizar(c.get("estado")) in ps.COLUNAS_ANTES_DA_PRODUCAO
                and date.fromisoformat(agendamento["dia_inicio"]) < date.today()):
            continue
        extra = extras.get(c["id"]) or {}
        eh_quadradilho = _eh_quadradilho(c["titulo"])
        em_continuo = extra.get("em_continuo")
        if em_continuo is None:
            em_continuo = eh_quadradilho
        indice_toros = extra.get("indice_toros")
        if indice_toros is None:
            indice_toros = INDICE_TOROS_DEFAULT
        indice_wip = extra.get("indice_wip")
        if indice_wip is None:
            indice_wip = INDICE_WIP_DEFAULT
        # pedido explícito do Rui (2026-10-01): para "quadradilho", o WIP
        # começa logo preenchido com os tamanhos mais usados (2500/32/32)
        # — não são regra fixa, por isso continuam totalmente editáveis;
        # cada campo só usa o valor por omissão enquanto ninguém tiver
        # escrito nada nele (ver guardar_wip: escrever aqui grava o valor
        # escolhido em definitivo para esta OF, mesmo que seja igual ao
        # que já vinha por omissão).
        wip_cmp = extra.get("wip_cmp")
        wip_lar = extra.get("wip_lar")
        wip_esp = extra.get("wip_esp")
        if eh_quadradilho:
            if wip_cmp is None:
                wip_cmp = WIP_CMP_QUADRADILHO_DEFAULT
            if wip_lar is None:
                wip_lar = WIP_LAR_QUADRADILHO_DEFAULT
            if wip_esp is None:
                wip_esp = WIP_ESP_QUADRADILHO_DEFAULT
        volume = agendamento["volume_m3"]
        entradas.append({
            "basecamp_card_id": c["id"],
            "titulo": c["titulo"],
            "url": c["url"],
            "coluna_basecamp": c["estado"],
            "linha": agendamento["linha"],
            "dia_inicio_producao": agendamento["dia_inicio"],
            "volume_m3": volume,
            "tipo_madeira": agendamento["tipo_madeira"],
            # cor da barra lateral (tipo de produto) — pedido explícito do
            # Rui (2026-10-01): mesma cor da produção/logística, para dar
            # para distinguir produtos também aqui (senão ficam todos
            # cinzentos); é só consulta, escolhe-se sempre na outra página.
            "cor": agendamento["cor"],
            "dia_entrada": _calcular_dia_entrada(agendamento["dia_inicio"], em_continuo),
            "em_continuo": em_continuo,
            "charriots": extra.get("charriots") or [],
            "largura_dias": extra.get("largura_dias_visual") or 1,
            "produzido": bool(extra.get("produzido")),
            "wip_cmp": wip_cmp,
            "wip_lar": wip_lar,
            "wip_esp": wip_esp,
            "indice_wip": indice_wip,
            "qtd_wip_m3": round(volume * indice_wip, 2) if volume is not None else None,
            "toro_cmp": extra.get("toro_cmp"),
            "toro_tipo": extra.get("toro_tipo"),
            "indice_toros": indice_toros,
            "qtd_toros_m3": round(volume * indice_toros, 2) if volume is not None else None,
        })
    return {"charriots": CHARRIOTS, "entradas": entradas, "historico": db.historico_valores_entradas(),
            "feriados": db.feriados_ecos_largos()}

def atribuir_charriots(basecamp_card_id: int, charriots: list = None) -> dict:
    """Atribui (substitui) os charriots de uma OF — pode estar em vários ao
    mesmo tempo (ex: Charriot 1 e 2 em simultâneo, pedido explícito do
    Rui, 2026-10-02), ou nenhum (None/lista vazia volta para "por
    atribuir")."""
    charriots = [c for c in (charriots or []) if c] or None
    if charriots is not None:
        desconhecidos = [c for c in charriots if c not in CHARRIOTS]
        if desconhecidos:
            return {"erro": f"charriot desconhecido: {desconhecidos[0]!r}"}
    return db.atribuir_charriots_entrada(basecamp_card_id, charriots)

LARGURA_DIAS_MAX = 14

def definir_largura_dias(basecamp_card_id: int, largura_dias) -> dict:
    """Só visual (pedido explícito do Rui, 2026-10-02): quantos dias um
    card ocupa na tabela, ao ser alargado pela borda direita — nunca mexe
    no dia real usado nos cálculos (ver _calcular_dia_entrada). None/1
    volta ao tamanho normal (um dia)."""
    if largura_dias is None:
        largura_dias = 1
    try:
        largura_dias = int(largura_dias)
    except (TypeError, ValueError):
        return {"erro": "largura em dias inválida"}
    if largura_dias < 1 or largura_dias > LARGURA_DIAS_MAX:
        return {"erro": f"largura em dias tem de estar entre 1 e {LARGURA_DIAS_MAX}"}
    return db.definir_largura_dias_entrada(basecamp_card_id, largura_dias if largura_dias != 1 else None)

def definir_produzido(basecamp_card_id: int, produzido: bool) -> dict:
    """Marca/desmarca "Produzido" (pedido explícito do Rui, 2026-10-03) —
    só um marcador visual à mão (muda o fundo do card para verde), sem
    ligação ao estado real da OF no Basecamp nem a nenhum cálculo desta
    página."""
    return db.definir_produzido_entrada(basecamp_card_id, bool(produzido))

def _validar_numero_positivo(nome: str, valor):
    if valor is None:
        return None
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return f"{nome} inválido"
    if valor <= 0:
        return f"{nome} tem de ser maior que 0"
    return None

def guardar_wip(basecamp_card_id: int, cmp: float = None, lar: float = None, esp: float = None,
                indice_wip: float = None) -> dict:
    for nome, valor in (("comprimento (WIP)", cmp), ("largura (WIP)", lar), ("espessura (WIP)", esp),
                       ("índice de WIP", indice_wip)):
        erro = _validar_numero_positivo(nome, valor)
        if erro:
            return {"erro": erro}
    return db.guardar_wip_entrada(basecamp_card_id, cmp, lar, esp, indice_wip)

def guardar_toro(basecamp_card_id: int, cmp: str = None, tipo: str = None, indice_toros: float = None) -> dict:
    # cmp é texto, não número (ver TORO_CMP_PRESETS — há opções como "255
    # ⌀16" que não são um número puro); só o índice tem de ser validado
    # como número positivo.
    erro = _validar_numero_positivo("índice de toros", indice_toros)
    if erro:
        return {"erro": erro}
    if cmp is not None and not str(cmp).strip():
        return {"erro": "comprimento do toro inválido"}
    if tipo is not None and tipo not in TORO_TIPOS:
        return {"erro": f"tipo de toro desconhecido: {tipo!r} — usa \"IN\" ou \"MT\""}
    return db.guardar_toro_entrada(basecamp_card_id, cmp, tipo, indice_toros)

def definir_em_continuo(basecamp_card_id: int, em_continuo: bool) -> dict:
    """Marca/desmarca "Em contínuo" (ver _calcular_dia_entrada) para uma OF
    em concreto — fica gravado em definitivo, mesmo que o título mude."""
    return db.definir_em_continuo_entrada(basecamp_card_id, bool(em_continuo))

def pagina_planeamento_entradas() -> str:
    return _TEMPLATE

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Planeamento de Entradas — Ecos Largos</title>
<style>
  :root{
    --paper:#FFFFFF; --canvas:#F5F5F3; --raise:#FBFBF9;
    --ink:#1A1C1E; --dim:#75797D;
    --line:#E8E8E3; --edge:#D9D9D2;
    --blue:#1B6AC9; --gold:#E0A02C; --red:#C4452E; --grey:#9AA0A6; --hoje:#FFF6D6;
    --green:#2E9E4F; --green-bg:#E1F5E6; --feriado-bg:#FBDFDD;
    --day:92px; --lane:78px; --label:180px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--canvas);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
    font-size:15px;line-height:1.4;-webkit-font-smoothing:antialiased}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
  button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
  .wrap{max-width:1200px;margin:0 auto;padding:22px 14px 120px}

  header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px}
  h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0}
  .sub{color:var(--dim);font-size:14px}

  .nav{display:flex;align-items:center;gap:8px;margin:16px 0 10px;flex-wrap:wrap}
  .seg{display:flex;background:#EAEAE6;border-radius:9px;padding:3px}
  .seg button{padding:5px 13px;font-size:14px;color:var(--dim);border-radius:7px;font-weight:500}
  .seg button:hover{color:var(--ink)}
  .seg button.on{background:var(--paper);color:var(--ink);font-weight:600;
    box-shadow:0 1px 2px rgba(0,0,0,.10)}
  .step{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    width:32px;height:32px;color:var(--dim);font-size:17px;line-height:1}
  .step:hover{color:var(--ink);border-color:var(--dim)}
  .range{font-size:16px;font-weight:700;min-width:150px}

  .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 16px}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--paper);
    border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13.5px;color:var(--dim)}
  .pill b{color:var(--ink);font-weight:700}
  .dot{width:8px;height:8px;border-radius:50%;background:#4E9A51}
  .dot.busy{background:var(--gold);animation:blink .7s infinite alternate}
  @keyframes blink{to{opacity:.25}}
  .btn{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:6px 12px;font-size:13.5px;color:var(--blue);font-weight:500}
  .btn:hover{border-color:var(--dim)}
  .btn:focus-visible,.step:focus-visible,.seg button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
  .warn{color:var(--red)}

  .board{display:flex;background:var(--paper);border:1px solid var(--line);
    border-radius:12px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .labels{flex:0 0 var(--label);border-right:1px solid var(--edge);background:var(--raise)}
  .labels .head{height:48px;border-bottom:1px solid var(--edge)}
  .lbl{height:var(--lane);border-bottom:1px solid var(--line);padding:10px 12px;
    display:flex;flex-direction:column;justify-content:center}
  .lbl:last-child{border-bottom:none}
  .lbl .n{font-size:14px;font-weight:700}

  .scroll{flex:1;overflow-x:auto;overflow-y:hidden}
  .track{position:relative}
  .days{display:flex;height:48px;border-bottom:1px solid var(--edge)}
  .day{flex:0 0 var(--day);border-right:1px solid var(--line);padding:8px 10px;overflow:hidden}
  .day.wk{border-right:1px solid var(--edge)}
  .day .dn{font-size:13.5px;font-weight:700;white-space:nowrap}
  .day .dm{font-size:11.5px;color:var(--dim);white-space:nowrap}
  .day.sab .dn{color:var(--dim)}
  .day.hoje{background:var(--hoje);box-shadow:inset 0 -3px 0 var(--gold)}
  .dense .day{padding:8px 4px;text-align:center}
  .dense .day .dn{font-size:12.5px}
  .dense .day .dm{font-size:10px}
  /* feriado (pedido explícito do Rui, 2026-10-03) — cor bem diferente do
     "hoje"/fim de semana, para nunca se confundir; continua a poder levar
     cards à mesma (só um marcador visual, ver ehFeriado). Clica no
     cabeçalho do dia para marcar/desmarcar. */
  .day.feriado{background:var(--feriado-bg);box-shadow:inset 0 -3px 0 var(--red);cursor:pointer}
  .day:not(.feriado){cursor:pointer}
  .dm.fer{color:var(--red);font-weight:700}

  .lanes{position:relative}
  .row{display:flex;height:var(--lane);border-bottom:1px solid var(--line)}
  .row:last-child{border-bottom:none}
  .cell{flex:0 0 var(--day);border-right:1px solid var(--line);position:relative;background:var(--paper)}
  .cell.wk{border-right:1px solid var(--edge)}
  .cell.hoje{background:#FFFDF4}
  .cell.feriado{background:var(--feriado-bg)}

  .blocks{position:absolute;inset:0;pointer-events:none}
  .blk{position:absolute;box-sizing:border-box;
    background:var(--paper);border:1px solid var(--line);border-left:10px solid var(--grey);
    border-radius:9px;padding:5px 9px;overflow:hidden;pointer-events:auto;cursor:grab;
    touch-action:none;user-select:none;box-shadow:0 1px 2px rgba(0,0,0,.09)}
  .blk:hover{box-shadow:0 2px 7px rgba(0,0,0,.13)}
  .blk:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
  .blk .tt{display:flex;align-items:center;gap:5px;font-size:13.5px;font-weight:600}
  .blk .tt .ttText{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .blk .of{font-size:11.5px;color:var(--dim);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dense .blk{padding:3px 6px;border-radius:7px}
  .dense .blk .of{font-size:10px}
  .dense .blk .tt{font-size:12px}
  .blk.drag{cursor:grabbing;box-shadow:0 8px 22px rgba(0,0,0,.22);z-index:9;border-color:var(--blue)}
  .blk.semCharriot{border-left-style:dashed}
  /* "Produzido" (pedido explícito do Rui, 2026-10-03, só nesta página) —
     marca/desmarca na ficha (botão "Marcar Produzido", ao lado de
     Guardar/Fechar — de propósito longe do card, para não ser fácil
     demais de carregar sem querer), muda o fundo do card para verde. */
  .blk.produzido{background:var(--green-bg)}
  /* manípulos de arrastar para "alargar" (pedido explícito do Rui,
     2026-10-02) — sempre presentes (não só ao passar o rato: num ecrã
     touch não há hover, por isso têm de já lá estar para se poderem tocar),
     só ficam visualmente destacados ao passar o cursor/tocar. Zona de
     toque maior que a faixa visível, para serem fáceis de agarrar sem
     precisar de acertar num traço fino. */
  /* dentro dos limites do card (que tem overflow:hidden, ver .blk acima —
     um manípulo "para fora" ficaria cortado e impossível de agarrar). */
  .rsz{position:absolute}
  .rsz-h{top:0;right:0;width:9px;height:100%;cursor:ew-resize}
  .rsz-v{left:0;bottom:0;width:100%;height:9px;cursor:ns-resize}
  .rsz::after{content:"";display:block;background:var(--blue);opacity:0;border-radius:3px}
  .rsz-h::after{width:3px;height:60%;margin:20% auto}
  .rsz-v::after{height:3px;width:60%;margin:3px auto}
  .rsz:hover::after{opacity:.55}
  @media (pointer:coarse){
    .rsz-h{width:16px}
    .rsz-v{height:16px}
    .rsz::after{opacity:.25}
  }

  .log{margin-top:14px;background:var(--paper);border:1px solid var(--line);
    border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .log summary{padding:12px 16px;cursor:pointer;font-size:14px;font-weight:600;
    color:var(--ink);list-style:none}
  .log summary::-webkit-details-marker{display:none}
  .log ul{margin:0;padding:0 16px 14px;list-style:none;max-height:215px;overflow:auto}
  .log li{border-top:1px solid var(--line);padding:8px 0;font-size:12px;display:flex;gap:9px;color:#4C5054}
  .arrow{flex:0 0 auto;color:var(--blue);font-weight:700}
  .arrow.local{color:#B4B8BB}
  .lt{word-break:break-word}

  .veil{position:fixed;inset:0;background:rgba(26,28,30,.35);display:none;z-index:100}
  .veil.on{display:block}
  .sheet{position:fixed;top:50%;left:50%;background:var(--paper);z-index:101;
    border-radius:16px;padding:20px 20px 28px;width:min(560px,calc(100vw - 32px));
    box-sizing:border-box;box-shadow:0 12px 40px rgba(0,0,0,.25);
    max-height:calc(100vh - 64px);overflow-y:auto;overscroll-behavior:contain;
    transform:translate(-50%,-50%) scale(.96);opacity:0;pointer-events:none;
    transition:transform .18s ease,opacity .18s ease}
  .sheet.on{transform:translate(-50%,-50%) scale(1);opacity:1;pointer-events:auto}
  @media (prefers-reduced-motion:reduce){.sheet{transition:none}}
  .sheet h3{margin:6px 0 2px;font-size:20px;font-weight:700;letter-spacing:-.01em}
  .sheet .of{font-size:12.5px;color:var(--dim)}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
    border-top:1px solid var(--line);font-size:14px}
  .kv span{color:var(--dim)}
  select,input,textarea{background:var(--paper);color:var(--ink);border:1px solid var(--edge);
    border-radius:8px;padding:6px 9px;font:inherit;font-size:14px}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:0;border-color:var(--blue)}
  .err{color:var(--red);font-size:13px;min-height:16px;padding-top:8px}
  a.btn{display:inline-block;text-decoration:none}
  .btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
  .btn.primary:hover{background:#175CAF;border-color:#175CAF}
  /* botão "Produzido" (pedido explícito do Rui, 2026-10-03) — na ficha, ao
     lado de Guardar/Fechar, de propósito: um botão pequeno no card era
     fácil demais de carregar sem querer. */
  .btn.verde{color:var(--green);border-color:var(--green)}
  .btn.verde.ativo{background:var(--green);color:#fff;font-weight:600}
  .frow{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:9px 0;border-top:1px solid var(--line)}
  .frow label{color:var(--dim);font-size:14px;flex:0 0 auto}
  .frow input,.frow select,.frow textarea{flex:1 1 auto;min-width:0;max-width:62%}
  .owner{font-size:12.5px;color:var(--dim);margin-top:12px;background:var(--canvas);
    border-radius:8px;padding:9px 11px}

  /* grupos WIP/Toro: sub-campos numa única linha, ordenados (pedido
     explícito do Rui, 2026-10-01) */
  .grupoLbl{display:block;font-size:14px;font-weight:700;color:var(--ink);margin-top:16px}
  .charriotOpts{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:8px}
  .charriotOpt{display:flex;align-items:center;gap:6px;font-size:13.5px;color:var(--ink);font-weight:400}
  .charriotOpt input{width:auto}
  /* pedido explícito do Rui (2026-10-01): "Em contínuo" mais destacado */
  .destaque{color:var(--blue)!important;font-weight:700;font-size:15.5px}
  .miniRow{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
  .miniField{display:flex;flex-direction:column;gap:3px}
  .miniField label{font-size:11.5px;color:var(--dim)}
  .miniField input,.miniField select{width:88px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Planeamento de Entradas</h1>
    <div class="sub">Ecos Largos · o que entra nos charriots · fins de semana assinalados</div>
  </header>

  <div class="nav">
    <div class="seg" id="seg">
      <button data-m="semana">Semana</button>
      <button data-m="duas">Duas semanas</button>
      <button data-m="mes">Mês</button>
    </div>
    <button class="step" id="prev" aria-label="Período anterior">‹</button>
    <button class="step" id="next" aria-label="Período seguinte">›</button>
    <div class="range" id="range"></div>
    <button class="btn" id="hoje">Hoje</button>
  </div>

  <div class="bar">
    <div class="pill"><span class="dot" id="syncDot"></span><span id="syncTxt">Ligado ao Basecamp</span></div>
    <div class="pill"><b id="statTotal">—</b> OFs</div>
    <div class="pill"><b id="statPorAtribuir">—</b> por atribuir</div>
    <button class="btn" id="atualizar">Atualizar do Basecamp</button>
  </div>

  <div class="board" id="board">
    <div class="labels" id="labels"><div class="head"></div></div>
    <div class="scroll" id="scroll">
      <div class="track">
        <div class="days" id="days"></div>
        <div class="lanes" id="lanes"></div>
      </div>
    </div>
  </div>

  <details class="log" open>
    <summary>Registo — o que foi gravado</summary>
    <ul id="log"></ul>
  </details>
</div>

<div class="veil" id="veil"></div>
<div class="sheet" id="sheet"></div>

<script>
/* ---------- calendário: todos os dias, fins de semana assinalados ---------- */
const DOW=["dom","seg","ter","qua","qui","sex","sáb"];
const MES=["janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"];
const MESC=["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"];
const MASTER=[];
{
  const inicio=new Date(); inicio.setDate(inicio.getDate()-21);
  const fim=new Date(); fim.setDate(fim.getDate()+150);
  for(let d=new Date(inicio); d<=fim; d.setDate(d.getDate()+1)){
    MASTER.push({y:d.getFullYear(),mo:d.getMonth(),dd:d.getDate(),dow:d.getDay(),
      iso:`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`});
  }
}
/* pedido explícito do Rui (2026-10-03): sábado passou a ser um dia de
   produção normal (a equipa trabalha de manhã, mesma capacidade de
   qualquer outro dia) — só domingo continua a não ser dia de produção,
   por isso só domingo aparece destacado como "sem trabalho" aqui (ver
   _calcular_dia_entrada, que já só recua até sábado, nunca até sexta). */
const NAO_PRODUZ=d=>d.dow===0;
/* feriados marcados à mão (pedido explícito do Rui, 2026-10-03) —
   partilhado com "Planeamento de linhas" (mesmo calendário da mesma
   equipa, ver /ecos-largos/feriado); só um marcador visual, nunca impede
   colocar cards nesse dia. */
let FERIADOS=new Set();
const ehFeriado=d=>FERIADOS.has(d.iso);
const clamp=(v,a,b)=>Math.max(a,Math.min(v,b));
const hojeISO=(()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`})();
const HOJE=MASTER.findIndex(d=>d.iso===hojeISO);
const idxOf=iso=>MASTER.findIndex(d=>d.iso===iso);
const segundaDe=idx=>{ const dow=MASTER[clamp(idx,0,MASTER.length-1)].dow; return idx-((dow+6)%7); };

const $=s=>document.querySelector(s);
const CHARRIOTS=["Charriot 1","Charriot 2","Charriot 3","Multiserra de Toros"];
const LANES=["Por atribuir",...CHARRIOTS];
const TORO_PRESETS=["260","250","310","235","255","255 ⌀16","235 ⌀16"];
const INDICE_TOROS_DEFAULT=2.85, INDICE_WIP_DEFAULT=1.8;
/* mesmas cores da página de planeamento de linhas/logística (pedido
   explícito do Rui, 2026-10-01: "as cores laterais devem permanecer de
   uma página para a outra") — a cor em si só se escolhe lá; aqui é só
   consulta, para dar para distinguir produtos também neste quadro. */
const CORES={
  vermelho_escuro:"#D32F2F", laranja_escuro:"#F57C00", amarelo_escuro:"#FFA000",
  verde_escuro:"#388E3C", azul_escuro:"#1976D2", roxo_escuro:"#7B1FA2",
  vermelho:"#F44336", laranja:"#FF9800", amarelo:"#FFC107",
  verde:"#4CAF50", azul:"#2196F3", roxo:"#9C27B0",
  vermelho_medio:"#E57373", laranja_medio:"#FFB74D", amarelo_medio:"#FFD54F",
  verde_medio:"#81C784", azul_medio:"#64B5F6", roxo_medio:"#BA68C8",
  vermelho_claro:"#FFCDD2", laranja_claro:"#FFE0B2", amarelo_claro:"#FFECB3",
  verde_claro:"#C8E6C9", azul_claro:"#BBDEFB", roxo_claro:"#E1BEE7",
  cinza:"#9AA0A6",
};
const corProduto=e=>CORES[e.cor]||CORES.cinza;
let entradas=[];
let historico={wip_cmp:[],wip_lar:[],wip_esp:[],toro_cmp:[]};
let view={mode:"semana",start:Math.max(segundaDe(HOJE),0),len:7};
let logs=[], DAY=92, LANE=78;

const item=id=>entradas.find(e=>e.id===id);
/* uma OF pode estar em vários charriots ao mesmo tempo (pedido explícito
   do Rui, 2026-10-02) — aparece visivelmente repetida em cada um deles
   (pedido explícito do Rui, 2026-10-03: "quero que o card fique
   visivelmente em todos os que eu colocar"), nunca só numa etiqueta.
   lanesDe devolve todas as lanes onde uma OF deve ser desenhada — [0]
   ("Por atribuir") quando ainda não tem nenhum charriot. */
const lanesDe=e=>{
  if(!e.charriots || !e.charriots.length) return [0];
  return e.charriots.map(c=>CHARRIOTS.indexOf(c)+1);
};
const numCurto=id=>String(id).slice(-4);

function metrics(){
  const avail=$("#scroll").clientWidth||600;
  if(view.mode==="mes"){ DAY=44; LANE=78; }
  else if(view.mode==="duas"){ DAY=Math.max(72,Math.floor(avail/14)); LANE=78; }
  else { DAY=Math.max(84,Math.floor(avail/7)); LANE=78; }
  document.documentElement.style.setProperty("--day",DAY+"px");
  document.documentElement.style.setProperty("--lane",LANE+"px");
  $("#board").classList.toggle("dense",DAY<70);
}
function days(){ return MASTER.slice(view.start,view.start+view.len); }
function diaHtml(d,hoje){
  const wk=NAO_PRODUZ(d), fer=ehFeriado(d);
  // feriado troca a 2ª linha (mês) por "Feriado" em vez de acrescentar uma
  // 3ª linha — o cabeçalho do dia tem altura fixa, não há espaço para mais.
  const linha2 = fer ? `<div class="dm fer">Feriado</div>`
                     : `<div class="dm">${view.mode==="mes"?DOW[d.dow][0]:MESC[d.mo]}</div>`;
  return `<div class="day${wk?" wk":""}${wk?" sab":""}${hoje?" hoje":""}${fer?" feriado":""}"
      data-iso="${d.iso}" title="${fer?"Feriado — clica para desmarcar":"Clica para marcar feriado"}">
    <div class="dn">${view.mode==="mes"?d.dd:DOW[d.dow]+" "+d.dd}</div>
    ${linha2}</div>`;
}
function renderDays(){
  const D=days();
  $("#days").innerHTML=D.map((d,i)=>diaHtml(d,view.start+i===HOJE)).join("");
  $("#days").style.width=(D.length*DAY)+"px";
}

const ITEM_H=58, ITEM_GAP=4, ITEM_PAD=6;
const LARGURA_DIAS_MAX=14;
let ALTURAS_LANE=[], OFFSETS_LANE=[];
/* uma OF pode "alargar" (ver .rsz-h) para ocupar vários dias na mesma lane
   (pedido explícito do Rui, 2026-10-02) — deixa de ser um simples
   agrupamento por dia exato (um card só nunca se sobrepõe a outro): passa
   a ser um empacotamento por intervalo (como um calendário), cada item
   ocupa [gs, gs+larguraDias-1] e só entra na mesma linha vertical de outro
   se os intervalos não se cruzarem. */
function empacotarLinhas(lista){
  const ordenada=[...lista].sort((x,y)=> x.gs-y.gs || x.id-y.id);
  const fimPorLinha=[];
  const linhaPorId=new Map();
  ordenada.forEach(e=>{
    const fim=e.gs+(e.larguraDias||1)-1;
    let linha=fimPorLinha.findIndex(f=>f<e.gs);
    if(linha===-1){ linha=fimPorLinha.length; fimPorLinha.push(fim); }
    else fimPorLinha[linha]=fim;
    linhaPorId.set(e.id,linha);
  });
  return {linhaPorId, nLinhas:fimPorLinha.length};
}
function calcularAlturasLanes(){
  ALTURAS_LANE=LANES.map((_,li)=>{
    const {nLinhas}=empacotarLinhas(entradas.filter(e=>lanesDe(e).includes(li)));
    const maxN=Math.max(1,nLinhas);
    return Math.max(LANE, maxN*ITEM_H+(maxN-1)*ITEM_GAP+ITEM_PAD*2);
  });
  let acumulado=0;
  OFFSETS_LANE=ALTURAS_LANE.map(alt=>{ const topo=acumulado; acumulado+=alt; return topo; });
}
/* a que lane corresponde uma posição vertical (em px, relativa ao topo de
   #lanes) — usado ao arrastar (ver laneDeY/linhaDeY equivalente na outra
   página). */
function laneDeY(y){
  if(y<0) return 0;
  let acumulado=0;
  for(let i=0;i<LANES.length;i++){ acumulado+=ALTURAS_LANE[i]; if(y<acumulado) return i; }
  return LANES.length-1;
}
function renderLabels(){
  $("#labels").innerHTML='<div class="head"></div>'+LANES.map((n,li)=>
    `<div class="lbl" style="height:${ALTURAS_LANE[li]||LANE}px"><div class="n">${n}</div></div>`).join("");
}
function renderLanes(){
  calcularAlturasLanes(); renderLabels();
  const D=days();
  let h="";
  LANES.forEach((nome,li)=>{ h+=`<div class="row" style="height:${ALTURAS_LANE[li]}px">`+D.map(d=>{
    const hoje=MASTER.indexOf(d)===HOJE;
    return `<div class="cell${NAO_PRODUZ(d)?" wk":""}${hoje?" hoje":""}${ehFeriado(d)?" feriado":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  LANES.forEach((nome,li)=>{
    const itens=entradas.filter(e=>lanesDe(e).includes(li));
    const {linhaPorId}=empacotarLinhas(itens);
    itens.forEach(e=>{
      const largura=e.larguraDias||1;
      const aIni=e.gs-view.start, aFim=e.gs+largura-1-view.start;
      if(aFim<0||aIni>=view.len) return; // completamente fora da vista
      const aVis=Math.max(aIni,0), aFimVis=Math.min(aFim,view.len-1);
      const linha=linhaPorId.get(e.id);
      const el=document.createElement("div");
      const temCharriot=e.charriots && e.charriots.length>0;
      el.className="blk"+(temCharriot?"":" semCharriot")+(e.produzido?" produzido":"");
      el.tabIndex=0; el.dataset.id=e.id; el.dataset.lane=li;
      el.style.borderLeftColor=corProduto(e);
      el.style.left=(aVis*DAY+3)+"px";
      el.style.top=(OFFSETS_LANE[li]+ITEM_PAD+linha*(ITEM_H+ITEM_GAP))+"px";
      el.style.width=((aFimVis-aVis+1)*DAY-8)+"px";
      el.style.height=ITEM_H+"px";
      // manípulos de arrastar para alargar (pedido explícito do Rui,
      // 2026-10-02): borda direita alarga em dias (só visual — ver
      // definir_largura_dias); borda de baixo marca mais charriots ao
      // mesmo tempo, a partir desta lane (mesmo resultado das caixas de
      // seleção na ficha, mas mais rápido).
      el.innerHTML=`<div class="tt"><span class="ttText">${e.titulo}</span></div>
        <div class="of">Toros: ${e.qtdToros!=null?e.qtdToros+" m³":"—"}</div>
        <div class="of">Wip: ${e.qtdWip!=null?e.qtdWip+" m³":"—"}</div>
        <div class="rsz rsz-h" title="arrastar para alargar (dias)"></div>
        <div class="rsz rsz-v" title="arrastar para marcar mais charriots"></div>`;
      bl.appendChild(el);
    });
  });
  stats();
}
function stats(){
  $("#statTotal").textContent=entradas.length;
  $("#statPorAtribuir").textContent=entradas.filter(e=>!(e.charriots&&e.charriots.length)).length;
}
function render(){
  metrics(); renderDays(); renderLanes();
  $("#range").textContent=rangeLabel();
  document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.m===view.mode));
}
function rangeLabel(){
  const a=MASTER[view.start], b=MASTER[Math.min(view.start+view.len-1,MASTER.length-1)];
  if(!a||!b) return "";
  if(view.mode==="mes") return MES[a.mo][0].toUpperCase()+MES[a.mo].slice(1)+" "+a.y;
  if(a.mo===b.mo) return `${a.dd}–${b.dd} ${MESC[a.mo]}`;
  return `${a.dd} ${MESC[a.mo]} – ${b.dd} ${MESC[b.mo]}`;
}
function setMode(m){
  view.mode=m;
  const anchor=view.start;
  if(m==="semana"||m==="duas"){
    view.len = m==="semana"?7:14;
    view.start = clamp(segundaDe(anchor), 0, Math.max(MASTER.length-view.len,0));
  }else{
    const d=MASTER[clamp(anchor,0,MASTER.length-1)];
    view.start=MASTER.findIndex(x=>x.mo===d.mo && x.y===d.y);
    view.len=MASTER.filter(x=>x.mo===d.mo && x.y===d.y).length;
  }
  render();
}
function step(dir){
  if(view.mode==="mes"){
    const cur=MASTER[view.start];
    const i=dir>0 ? MASTER.findIndex(x=>x.y>cur.y||(x.y===cur.y&&x.mo>cur.mo))
                  : MASTER.map(x=>x.y*12+x.mo).lastIndexOf(cur.y*12+cur.mo-1);
    if(i<0)return;
    const d=MASTER[i];
    view.start=MASTER.findIndex(x=>x.mo===d.mo && x.y===d.y);
    view.len=MASTER.filter(x=>x.mo===d.mo && x.y===d.y).length;
  }else{
    view.start=clamp(view.start+dir*view.len,0,Math.max(MASTER.length-view.len,0));
  }
  render();
}
document.querySelectorAll("#seg button").forEach(b=>b.onclick=()=>setMode(b.dataset.m));
$("#prev").onclick=()=>step(-1);
$("#next").onclick=()=>step(1);
$("#hoje").onclick=()=>{ view.start=Math.max(HOJE,0); render(); };
window.addEventListener("resize",render);

/* ---------- arrastar entre lanes (só vertical — o dia é sempre calculado,
   nunca se arrasta para outro dia; ver tools/planeamento_entradas
   ._calcular_dia_entrada) ---------- */
let drag=null, resize=null;
$("#lanes").addEventListener("pointerdown",e=>{
  const rh=e.target.closest(".rsz-h"), rv=e.target.closest(".rsz-v");
  if(rh||rv){
    const b=e.target.closest(".blk"); if(!b) return;
    const it=item(+b.dataset.id); if(!it) return;
    resize={eixo:rh?"h":"v", el:b, it, x0:e.clientX, y0:e.clientY,
            larguraIni:it.larguraDias||1, laneIni:+b.dataset.lane};
    b.setPointerCapture(e.pointerId); e.preventDefault(); e.stopPropagation();
    return;
  }
  const b=e.target.closest(".blk"); if(!b) return;
  const it=item(+b.dataset.id); if(!it) return;
  drag={el:b,it,y0:e.clientY,moveu:false};
  b.setPointerCapture(e.pointerId); e.preventDefault();
});
$("#lanes").addEventListener("pointermove",e=>{
  if(resize){
    if(resize.eixo==="h"){
      const dx=e.clientX-resize.x0;
      const nova=Math.max(1,Math.min(LARGURA_DIAS_MAX,resize.larguraIni+Math.round(dx/DAY)));
      resize.larguraAtual=nova;
      resize.el.style.width=(nova*DAY-8)+"px";
    }else{
      const r=$("#lanes").getBoundingClientRect();
      const laneAtual=Math.max(resize.laneIni, laneDeY(e.clientY-r.top));
      resize.laneAtual=laneAtual;
      // pré-visualização: estica o card até ao fundo da lane alcançada,
      // sem alterar ainda nada guardado (só ao largar, ver pointerup).
      const alturaAlvo=(OFFSETS_LANE[laneAtual]+ALTURAS_LANE[laneAtual])-OFFSETS_LANE[resize.laneIni]-ITEM_PAD*2;
      resize.el.style.height=Math.max(ITEM_H,alturaAlvo)+"px";
    }
    return;
  }
  if(!drag) return;
  const dy=e.clientY-drag.y0;
  if(Math.abs(dy)>6){ drag.moveu=true; drag.el.classList.add("drag"); }
  if(drag.moveu) drag.el.style.transform=`translateY(${dy}px)`;
});
$("#lanes").addEventListener("pointerup",e=>{
  if(resize){
    const {eixo,it,laneIni,larguraIni}=resize;
    const larguraFinal=resize.larguraAtual??larguraIni;
    const laneFinal=resize.laneAtual??laneIni;
    resize=null;
    if(eixo==="h"){
      if(larguraFinal===larguraIni){ renderLanes(); return; }
      it.larguraDias=larguraFinal;
      renderLanes();
      definirLarguraServidor(it, larguraIni);
    }else{
      if(laneFinal===laneIni){ renderLanes(); return; }
      // laneIni pode ser 0 ("Por atribuir", quando a OF ainda não tinha
      // nenhum charriot) — 0 não é um charriot real, só serve de ponto de
      // partida do arrasto, por isso o intervalo de charriots começa
      // sempre, no mínimo, em 1.
      const novosCharriots=[];
      for(let li=Math.max(laneIni,1); li<=laneFinal; li++) novosCharriots.push(CHARRIOTS[li-1]);
      const anterior=it.charriots;
      it.charriots=novosCharriots;
      renderLanes();
      atribuirServidor(it, anterior);
    }
    return;
  }
  if(!drag) return;
  const {el,it,moveu}=drag;
  el.classList.remove("drag"); el.style.transform="";
  drag=null;
  if(!moveu) return; // clique simples sem arrastar — não faz nada sozinho
  const r=$("#lanes").getBoundingClientRect();
  const novaLane=laneDeY(e.clientY-r.top);
  // arrastar substitui sempre por um único charriot (ainda que a OF
  // estivesse em vários ao mesmo tempo — ver caixas de seleção na ficha
  // ou o manípulo rsz-v para marcar mais que um sem substituir).
  const novosCharriots = novaLane===0 ? null : [CHARRIOTS[novaLane-1]];
  const atuais = it.charriots||[];
  const semMudanca = (novosCharriots===null && atuais.length===0) ||
    (novosCharriots && atuais.length===1 && atuais[0]===novosCharriots[0]);
  if(semMudanca){ renderLanes(); return; }
  const anterior=it.charriots;
  it.charriots=novosCharriots;
  renderLanes();
  atribuirServidor(it, anterior);
});
/* pedido explícito do Rui (2026-10-02): apesar de aqui não haver segunda
   tabela para alinhar (ver planeamento de linhas/logística, onde o clique
   simples serve para isso), mantém-se o mesmo hábito de duplo clique para
   abrir a ficha — o clique simples não faz nada sozinho. */
$("#lanes").addEventListener("dblclick",e=>{
  if(e.target.closest(".rsz")) return;
  const b=e.target.closest(".blk"); if(!b) return;
  openSheet(+b.dataset.id);
});
async function atribuirServidor(it, anterior){
  try{
    const r=await fetch("/planeamento-entradas/atribuir",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:it.id,charriots:it.charriots||[]})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); it.charriots=anterior; renderLanes(); return; }
    log("local",`"${it.titulo}" atribuída a ${(it.charriots&&it.charriots.length)?it.charriots.join(" + "):"por atribuir"}`);
  }catch(e){ alert("Falhou a guardar: "+e); it.charriots=anterior; renderLanes(); }
}
async function definirLarguraServidor(it, anterior){
  try{
    const r=await fetch("/planeamento-entradas/largura-dias",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:it.id,largura_dias:it.larguraDias})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); it.larguraDias=anterior; renderLanes(); return; }
    log("local",`"${it.titulo}" alargada para ${it.larguraDias} dia(s) — só visual`);
  }catch(e){ alert("Falhou a guardar: "+e); it.larguraDias=anterior; renderLanes(); }
}
async function definirProduzidoServidor(it, anterior){
  try{
    const r=await fetch("/planeamento-entradas/produzido",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:it.id,produzido:it.produzido})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); it.produzido=anterior; renderLanes(); return; }
    log("local",`"${it.titulo}" marcada como ${it.produzido?"produzida":"não produzida"}`);
  }catch(e){ alert("Falhou a guardar: "+e); it.produzido=anterior; renderLanes(); }
}

/* ---------- ficha ---------- */
function toroCmpOptionsHtml(atual){
  const ehPreset = atual!=null && TORO_PRESETS.includes(atual);
  return `<option value="" ${atual==null?"selected":""}>—</option>` +
    TORO_PRESETS.map(v=>`<option value="${v}"${atual===v?" selected":""}>${v}</option>`).join("") +
    `<option value="outro"${(atual!=null && !ehPreset)?" selected":""}>Outro…</option>`;
}
function closeSheet(){ $("#veil").classList.remove("on"); $("#sheet").classList.remove("on"); }
/* QTD Toros/QTD Wip nunca se guardam — recalculam-se aqui ao vivo, à
   medida que o índice muda no formulário (pedido explícito do Rui,
   2026-10-01: "por omissão tem de fazer sempre este cálculo"), a partir
   do volume da OF (fixo, vem da produção) × o índice atual no campo. */
function recalcularQtd(it){
  const indiceWip=parseFloat($("#fIndiceWip").value);
  const indiceToros=parseFloat($("#fIndiceToros").value);
  $("#fQtdWip").textContent = (it.volume!=null && !isNaN(indiceWip)) ? (Math.round(it.volume*indiceWip*100)/100)+" m³" : "—";
  $("#fQtdToros").textContent = (it.volume!=null && !isNaN(indiceToros)) ? (Math.round(it.volume*indiceToros*100)/100)+" m³" : "—";
}
/* sugestões de valores já usados antes (pedido explícito do Rui,
   2026-10-01) — <datalist> nativo do browser: ao escrever, por exemplo,
   "2" num campo com histórico, a lista de valores já usados que começam
   por "2" aparece sozinha, sem precisar de nenhum JS próprio. */
function datalistHtml(idLista, valores){
  return `<datalist id="${idLista}">${(valores||[]).map(v=>`<option value="${v}">`).join("")}</datalist>`;
}
function openSheet(id){
  const it=item(id); if(!it) return;
  const toroEhPreset = it.toroCmp!=null && TORO_PRESETS.includes(it.toroCmp);
  const toroOutroValor = (it.toroCmp!=null && !toroEhPreset) ? it.toroCmp : "";
  $("#sheet").innerHTML=`
    <div class="of mono">card #${numCurto(it.id)}</div>
    <h3>${it.titulo}</h3>

    <label class="grupoLbl" style="margin-top:0">Onde vai ser produzido</label>
    <div class="charriotOpts">
      ${CHARRIOTS.map(c=>`<label class="charriotOpt">
        <input type="checkbox" class="fCharriot" value="${c}" ${(it.charriots&&it.charriots.includes(c))?"checked":""}>${c}</label>`).join("")}
    </div>
    <div class="owner" style="font-size:12.5px;color:var(--dim);margin-top:4px">
      Pode marcar mais que um charriot ao mesmo tempo (ex: a produzir em simultâneo no Charriot 1 e no
      Charriot 2) — nenhum marcado fica "por atribuir".</div>

    <div class="frow"><label class="destaque">Em contínuo</label>
      <input id="fEmContinuo" type="checkbox" style="width:auto;flex:0 0 auto;transform:scale(1.3)" ${it.emContinuo?"checked":""}></div>
    <div class="owner" style="font-size:12.5px;color:var(--dim);margin-top:4px">
      Marcado: entra no charriot no mesmo dia do início da produção. Desmarcado: entra no dia anterior
      (ou sábado, se isso cair a domingo).</div>

    <label class="grupoLbl">WIP</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><input id="fWipCmp" type="number" min="0" step="0.1" value="${it.wipCmp??""}" list="histWipCmp"></div>
      <div class="miniField"><label>Lar</label><input id="fWipLar" type="number" min="0" step="0.1" value="${it.wipLar??""}" list="histWipLar"></div>
      <div class="miniField"><label>Esp</label><input id="fWipEsp" type="number" min="0" step="0.1" value="${it.wipEsp??""}" list="histWipEsp"></div>
      <div class="miniField"><label>Índice</label><input id="fIndiceWip" type="number" min="0" step="0.01" value="${it.indiceWip}"></div>
      <div class="miniField"><label>QTD Wip</label><b id="fQtdWip" class="mono" style="align-self:center">—</b></div>
    </div>

    <label class="grupoLbl">Toro</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><select id="fToroCmp">${toroCmpOptionsHtml(it.toroCmp)}</select></div>
      <div class="miniField" id="fToroCmpOutroWrap" style="${(toroEhPreset||it.toroCmp==null)?"display:none":""}">
        <label>Valor</label><input id="fToroCmpOutro" type="number" min="0" step="1" value="${toroOutroValor}" list="histToroCmp"></div>
      <div class="miniField"><label>Tipo</label><select id="fToroTipo">
        <option value="" ${!it.toroTipo?"selected":""}>—</option>
        <option value="IN"${it.toroTipo==="IN"?" selected":""}>IN</option>
        <option value="MT"${it.toroTipo==="MT"?" selected":""}>MT</option>
      </select></div>
      <div class="miniField"><label>Índice</label><input id="fIndiceToros" type="number" min="0" step="0.01" value="${it.indiceToros}"></div>
      <div class="miniField"><label>QTD Toros</label><b id="fQtdToros" class="mono" style="align-self:center">—</b></div>
    </div>
    ${datalistHtml("histWipCmp",historico.wip_cmp)}
    ${datalistHtml("histWipLar",historico.wip_lar)}
    ${datalistHtml("histWipEsp",historico.wip_esp)}
    ${datalistHtml("histToroCmp",historico.toro_cmp)}

    <div class="kv" style="margin-top:16px"><span>Nome</span><b>${it.titulo}</b></div>
    <div class="kv"><span>Linha</span><b>${it.linha||"—"}</b></div>
    <div class="kv"><span>Início produção</span><b>${it.dataInicioProducao||"—"}</b></div>
    <div class="kv"><span>Volume</span><b>${it.volume?(it.volume+" m³"):"—"}</b></div>
    <div class="kv"><span>Madeira</span><b>${it.madeira||"—"}</b></div>

    <div class="err" id="fErro"></div>
    <div class="acts" style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
      ${it.url?`<a class="btn" target="_blank" rel="noopener" href="${it.url}">Abrir card no Basecamp</a>`:""}
      <button class="btn verde${it.produzido?" ativo":""}" id="btnProduzido">${it.produzido?"Produzido ✓":"Marcar Produzido"}</button>
      <button class="btn primary" id="guardar">Guardar</button>
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
  $("#btnProduzido").onclick=async()=>{
    const anterior=it.produzido;
    it.produzido=!it.produzido;
    await definirProduzidoServidor(it, anterior);
    const btn=$("#btnProduzido");
    if(btn){ btn.textContent=it.produzido?"Produzido ✓":"Marcar Produzido"; btn.classList.toggle("ativo",it.produzido); }
  };
  $("#fToroCmp").onchange=()=>{
    $("#fToroCmpOutroWrap").style.display = $("#fToroCmp").value==="outro" ? "" : "none";
  };
  $("#fIndiceWip").oninput=()=>recalcularQtd(it);
  $("#fIndiceToros").oninput=()=>recalcularQtd(it);
  recalcularQtd(it);
  $("#guardar").onclick=async()=>{
    $("#fErro").textContent="";
    const wipCmp=$("#fWipCmp").value?parseFloat($("#fWipCmp").value):null;
    const wipLar=$("#fWipLar").value?parseFloat($("#fWipLar").value):null;
    const wipEsp=$("#fWipEsp").value?parseFloat($("#fWipEsp").value):null;
    const indiceWip=$("#fIndiceWip").value?parseFloat($("#fIndiceWip").value):null;
    const indiceToros=$("#fIndiceToros").value?parseFloat($("#fIndiceToros").value):null;
    // toroCmp é texto (não número — há opções como "255 ⌀16", ver
    // TORO_PRESETS): nunca usar parseFloat aqui, perderia o "⌀16".
    let toroCmp=null;
    if($("#fToroCmp").value==="outro"){ toroCmp=$("#fToroCmpOutro").value||null; }
    else if($("#fToroCmp").value){ toroCmp=$("#fToroCmp").value; }
    const toroTipo=$("#fToroTipo").value||null;
    const emContinuo=$("#fEmContinuo").checked;
    const charriots=Array.from(document.querySelectorAll(".fCharriot:checked")).map(x=>x.value);
    try{
      if(JSON.stringify(charriots)!==JSON.stringify(it.charriots||[])){
        const r0=await fetch("/planeamento-entradas/atribuir",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id,charriots})});
        const d0=await r0.json();
        if(d0.erro){ $("#fErro").textContent=d0.erro; return; }
      }
      const r1=await fetch("/planeamento-entradas/wip",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:it.id,cmp:wipCmp,lar:wipLar,esp:wipEsp,indice_wip:indiceWip})});
      const d1=await r1.json();
      if(d1.erro){ $("#fErro").textContent=d1.erro; return; }
      const r2=await fetch("/planeamento-entradas/toro",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:it.id,cmp:toroCmp,tipo:toroTipo,indice_toros:indiceToros})});
      const d2=await r2.json();
      if(d2.erro){ $("#fErro").textContent=d2.erro; return; }
      if(emContinuo!==it.emContinuo){
        const r3=await fetch("/planeamento-entradas/em-continuo",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id,em_continuo:emContinuo})});
        const d3=await r3.json();
        if(d3.erro){ $("#fErro").textContent=d3.erro; return; }
      }
      it.charriots=charriots;
      it.wipCmp=wipCmp; it.wipLar=wipLar; it.wipEsp=wipEsp; it.toroCmp=toroCmp; it.toroTipo=toroTipo;
      it.indiceWip=indiceWip??INDICE_WIP_DEFAULT; it.indiceToros=indiceToros??INDICE_TOROS_DEFAULT;
      it.emContinuo=emContinuo;
      log("local",`dados de "${it.titulo}" guardados`);
      closeSheet();
      await carregar(); // o dia de entrada pode ter mudado (ver "em contínuo")
    }catch(e){ $("#fErro").textContent="Falhou a guardar: "+e; }
  };
}

/* ---------- registo ---------- */
function renderLog(){
  $("#log").innerHTML=logs.slice(0,40).map(l=>
    `<li><span class="arrow ${l.dir}">${l.dir==="local"?"·":"→"}</span>
     <span class="lt mono">${l.t}</span></li>`).join("");
}
function log(dir,t){ logs.unshift({dir,t}); renderLog(); }

/* ---------- carregar do servidor ---------- */
async function carregar(){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A ler o Basecamp…";
  try{
    const r=await fetch("/planeamento-entradas/dados");
    const d=await r.json();
    entradas=(d.entradas||[]).map(e=>({
      id:e.basecamp_card_id, titulo:e.titulo, url:e.url, coluna:e.coluna_basecamp,
      linha:e.linha, dataInicioProducao:e.dia_inicio_producao, volume:e.volume_m3, madeira:e.tipo_madeira, cor:e.cor,
      gs:idxOf(e.dia_entrada), charriots:e.charriots||[], larguraDias:e.largura_dias||1,
      produzido:!!e.produzido, emContinuo:!!e.em_continuo,
      wipCmp:e.wip_cmp, wipLar:e.wip_lar, wipEsp:e.wip_esp,
      indiceWip:e.indice_wip, qtdWip:e.qtd_wip_m3,
      toroCmp:e.toro_cmp, toroTipo:e.toro_tipo,
      indiceToros:e.indice_toros, qtdToros:e.qtd_toros_m3,
    })).filter(e=>e.gs>=0);
    historico=d.historico||historico;
    FERIADOS=new Set(d.feriados||[]);
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    render();
    log("local",`lido do Basecamp: ${entradas.length} OFs`);
  }catch(e){
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Falhou a ligação ao Basecamp";
    log("local",`erro a ler o Basecamp: ${e}`);
  }
}
$("#atualizar").onclick=()=>carregar();
/* marcar/desmarcar feriado (pedido explícito do Rui, 2026-10-03) —
   clicar no cabeçalho do dia, com confirmação (é um marcador partilhado
   por toda a equipa nas duas páginas, não uma preferência pessoal). */
$("#days").addEventListener("click",async e=>{
  const d=e.target.closest(".day"); if(!d) return;
  const iso=d.dataset.iso;
  const jaEhFeriado=FERIADOS.has(iso);
  if(!confirm(jaEhFeriado?`Desmarcar ${iso} como feriado?`:`Marcar ${iso} como feriado?`)) return;
  try{
    const r=await fetch("/ecos-largos/feriado",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({dia:iso,feriado:!jaEhFeriado})});
    const resp=await r.json();
    if(resp.erro){ alert(resp.erro); return; }
    if(jaEhFeriado) FERIADOS.delete(iso); else FERIADOS.add(iso);
    render();
    log("local",`${iso} ${jaEhFeriado?"deixou de ser":"passou a ser"} feriado`);
  }catch(err){ alert("Falhou a guardar: "+err); }
});
carregar();
</script>
</body>
</html>
"""
