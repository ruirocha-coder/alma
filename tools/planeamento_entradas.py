# tools/planeamento_entradas.py — página "Planeamento de Entradas" da Ecos
# Largos: planear o que entra nos charriots (Charriot 1/2/3), um dia antes
# do início da produção de cada OF (pedido explícito do Rui, 2026-10-01).
#
# Segue exatamente a mesma filosofia da logística (ver
# tools/planeamento_serracao.py): não duplica nome/linha/dia de início/
# volume/tipo de madeira — isso lê-se sempre em direto da OF de produção já
# agendada (mesmos _cards_of_ativos/agendamentos_producao_ecos_largos), e só
# se guarda aqui o que é específico desta página (charriot atribuído + os
# campos WIP/Toro preenchidos à mão). Por isso não existe nenhuma função de
# "replicar" propriamente dita — uma OF agendada na outra página aparece
# aqui sozinha, sempre que se lê o estado (ver estado_planeamento_entradas),
# exatamente como uma OF com tipo de madeira definido aparece sozinha na
# logística.
import re
from datetime import date, timedelta
import db
from tools import planeamento_serracao as ps

CHARRIOTS = ["Charriot 1", "Charriot 2", "Charriot 3"]
TORO_CMP_PRESETS = [2600, 2500, 3100, 2350, 2550]
TORO_TIPOS = {"IN", "MT"}

# QTD Toros = volume (m³) × INDICE_TOROS; QTD Wip = volume (m³) × INDICE_WIP
# — pedido explícito do Rui (2026-10-01). Cada OF pode ter o seu próprio
# índice (ver guardar_wip/guardar_toro); estes são só os valores por
# omissão usados enquanto ninguém escolher outro à mão para essa OF em
# concreto (ver db.entradas_charriot_ecos_largos, colunas indice_toros/
# indice_wip).
INDICE_TOROS_DEFAULT = 1.58
INDICE_WIP_DEFAULT = 1.8

# "quadradilho" = título sem "OF" (pedido explícito do Rui, 2026-10-01,
# ex: "Fepal — Quadradilho MT", "Girona — 2600 MT" não têm "OF"; "Palcax
# OF 508" tem). Por omissão entra "em contínuo" (ver _calcular_dia_entrada)
# — a pessoa pode sempre desmarcar à mão para uma OF em concreto (ver
# definir_em_continuo), e essa escolha fica gravada em definitivo.
_PADRAO_OF = re.compile(r"(?<![a-zà-ÿ])of(?![a-zà-ÿ])", re.IGNORECASE)

def _eh_quadradilho(titulo: str) -> bool:
    return not bool(_PADRAO_OF.search(titulo or ""))

def _calcular_dia_entrada(dia_inicio: str, em_continuo: bool) -> str:
    """Dia em que os troncos desta OF entram no charriot.

    "Em contínuo" (pedido explícito do Rui, 2026-10-01): a OF entra no
    MESMO dia do início da produção — salta o cálculo habitual (dia
    anterior). Usado sobretudo para quadradilho (ver _eh_quadradilho), mas
    qualquer OF pode ser marcada/desmarcada à mão (ver definir_em_continuo).

    Caso contrário (o cálculo habitual): um dia antes do início da
    produção. Se isso calhar a sábado ou domingo (acontece sempre que a
    produção começa a uma segunda-feira), recua para a sexta-feira
    anterior — os charriots não trabalham ao fim de semana, tal como a
    produção em si."""
    if em_continuo:
        return dia_inicio
    dia = date.fromisoformat(dia_inicio) - timedelta(days=1)
    if dia.weekday() == 5:  # sábado
        dia -= timedelta(days=1)
    elif dia.weekday() == 6:  # domingo
        dia -= timedelta(days=2)
    return dia.isoformat()

def estado_planeamento_entradas() -> dict:
    """Junta as OFs já agendadas na produção (mesma fonte da outra página)
    com o charriot/WIP/Toro guardados aqui — devolve todas as OFs
    "candidatas a entrada", cada uma já com o charriot atribuído (ou None,
    "por atribuir") e o dia de entrada calculado (ver
    _calcular_dia_entrada)."""
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
        em_continuo = extra.get("em_continuo")
        if em_continuo is None:
            em_continuo = _eh_quadradilho(c["titulo"])
        indice_toros = extra.get("indice_toros")
        if indice_toros is None:
            indice_toros = INDICE_TOROS_DEFAULT
        indice_wip = extra.get("indice_wip")
        if indice_wip is None:
            indice_wip = INDICE_WIP_DEFAULT
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
            "charriot": extra.get("charriot"),
            "wip_cmp": extra.get("wip_cmp"),
            "wip_lar": extra.get("wip_lar"),
            "wip_esp": extra.get("wip_esp"),
            "indice_wip": indice_wip,
            "qtd_wip_m3": round(volume * indice_wip, 2) if volume is not None else None,
            "toro_cmp": extra.get("toro_cmp"),
            "toro_tipo": extra.get("toro_tipo"),
            "indice_toros": indice_toros,
            "qtd_toros_m3": round(volume * indice_toros, 2) if volume is not None else None,
        })
    return {"charriots": CHARRIOTS, "entradas": entradas, "historico": db.historico_valores_entradas()}

def atribuir_charriot(basecamp_card_id: int, charriot: str = None) -> dict:
    """Atribui (ou remove, com charriot=None) o charriot de uma OF."""
    if charriot is not None and charriot not in CHARRIOTS:
        return {"erro": f"charriot desconhecido: {charriot!r}"}
    return db.atribuir_charriot_entrada(basecamp_card_id, charriot)

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

def guardar_toro(basecamp_card_id: int, cmp: float = None, tipo: str = None, indice_toros: float = None) -> dict:
    for nome, valor in (("comprimento do toro", cmp), ("índice de toros", indice_toros)):
        erro = _validar_numero_positivo(nome, valor)
        if erro:
            return {"erro": erro}
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

  .lanes{position:relative}
  .row{display:flex;height:var(--lane);border-bottom:1px solid var(--line)}
  .row:last-child{border-bottom:none}
  .cell{flex:0 0 var(--day);border-right:1px solid var(--line);position:relative;background:var(--paper)}
  .cell.wk{border-right:1px solid var(--edge)}
  .cell.hoje{background:#FFFDF4}

  .blocks{position:absolute;inset:0;pointer-events:none}
  .blk{position:absolute;box-sizing:border-box;
    background:var(--paper);border:1px solid var(--line);border-left:10px solid var(--grey);
    border-radius:9px;padding:5px 9px;overflow:hidden;pointer-events:auto;cursor:grab;
    touch-action:none;user-select:none;box-shadow:0 1px 2px rgba(0,0,0,.09)}
  .blk:hover{box-shadow:0 2px 7px rgba(0,0,0,.13)}
  .blk:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
  .blk .tt{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .blk .of{font-size:11.5px;color:var(--dim);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dense .blk{padding:3px 6px;border-radius:7px}
  .dense .blk .of{font-size:10px}
  .dense .blk .tt{font-size:12px}
  .blk.drag{cursor:grabbing;box-shadow:0 8px 22px rgba(0,0,0,.22);z-index:9;border-color:var(--blue)}
  .blk.semCharriot{border-left-style:dashed}

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
  .frow{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:9px 0;border-top:1px solid var(--line)}
  .frow label{color:var(--dim);font-size:14px;flex:0 0 auto}
  .frow input,.frow select,.frow textarea{flex:1 1 auto;min-width:0;max-width:62%}
  .owner{font-size:12.5px;color:var(--dim);margin-top:12px;background:var(--canvas);
    border-radius:8px;padding:9px 11px}

  /* grupos WIP/Toro: sub-campos numa única linha, ordenados (pedido
     explícito do Rui, 2026-10-01) */
  .grupoLbl{display:block;font-size:14px;font-weight:700;color:var(--ink);margin-top:16px}
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
const FDS=d=>d.dow===6||d.dow===0;
const clamp=(v,a,b)=>Math.max(a,Math.min(v,b));
const hojeISO=(()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`})();
const HOJE=MASTER.findIndex(d=>d.iso===hojeISO);
const idxOf=iso=>MASTER.findIndex(d=>d.iso===iso);
const segundaDe=idx=>{ const dow=MASTER[clamp(idx,0,MASTER.length-1)].dow; return idx-((dow+6)%7); };

const $=s=>document.querySelector(s);
const CHARRIOTS=["Charriot 1","Charriot 2","Charriot 3"];
const LANES=["Por atribuir",...CHARRIOTS];
const TORO_PRESETS=[2600,2500,3100,2350,2550];
const INDICE_TOROS_DEFAULT=1.58, INDICE_WIP_DEFAULT=1.8;
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
const laneIdx=e=>e.charriot ? CHARRIOTS.indexOf(e.charriot)+1 : 0;
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
  const wk=FDS(d);
  return `<div class="day${wk?" wk":""}${wk?" sab":""}${hoje?" hoje":""}">
    <div class="dn">${view.mode==="mes"?d.dd:DOW[d.dow]+" "+d.dd}</div>
    <div class="dm">${view.mode==="mes"?DOW[d.dow][0]:MESC[d.mo]}</div></div>`;
}
function renderDays(){
  const D=days();
  $("#days").innerHTML=D.map((d,i)=>diaHtml(d,view.start+i===HOJE)).join("");
  $("#days").style.width=(D.length*DAY)+"px";
}

const ITEM_H=58, ITEM_GAP=4, ITEM_PAD=6;
let ALTURAS_LANE=[], OFFSETS_LANE=[];
function calcularAlturasLanes(){
  ALTURAS_LANE=LANES.map((_,li)=>{
    const porDia={};
    entradas.filter(e=>laneIdx(e)===li).forEach(e=>{ (porDia[e.gs]=porDia[e.gs]||[]).push(e); });
    const maxN=Math.max(1, ...Object.values(porDia).map(l=>l.length));
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
    return `<div class="cell${FDS(d)?" wk":""}${hoje?" hoje":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  LANES.forEach((nome,li)=>{
    const porDia={};
    entradas.filter(e=>laneIdx(e)===li).forEach(e=>{ (porDia[e.gs]=porDia[e.gs]||[]).push(e); });
    Object.values(porDia).forEach(lista=>lista.sort((x,y)=>x.id-y.id));
    Object.entries(porDia).forEach(([gs,lista])=>{
      const a=+gs-view.start;
      if(a<0||a>=view.len) return;
      lista.forEach((e,i)=>{
        const el=document.createElement("div");
        el.className="blk"+(e.charriot?"":" semCharriot");
        el.tabIndex=0; el.dataset.id=e.id;
        el.style.borderLeftColor=corProduto(e);
        el.style.left=(a*DAY+3)+"px";
        el.style.top=(OFFSETS_LANE[li]+ITEM_PAD+i*(ITEM_H+ITEM_GAP))+"px";
        el.style.width=(DAY-8)+"px";
        el.style.height=ITEM_H+"px";
        el.innerHTML=`<div class="tt">${e.titulo}</div>
          <div class="of">QTD Toros: ${e.qtdToros!=null?e.qtdToros+" m³":"—"}</div>
          <div class="of">QTD Wip: ${e.qtdWip!=null?e.qtdWip+" m³":"—"}</div>`;
        bl.appendChild(el);
      });
    });
  });
  stats();
}
function stats(){
  $("#statTotal").textContent=entradas.length;
  $("#statPorAtribuir").textContent=entradas.filter(e=>!e.charriot).length;
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
let drag=null, arrastouAgora=false;
$("#lanes").addEventListener("pointerdown",e=>{
  const b=e.target.closest(".blk"); if(!b) return;
  const it=item(+b.dataset.id); if(!it) return;
  drag={el:b,it,y0:e.clientY,moveu:false};
  b.setPointerCapture(e.pointerId); e.preventDefault();
});
$("#lanes").addEventListener("pointermove",e=>{
  if(!drag) return;
  const dy=e.clientY-drag.y0;
  if(Math.abs(dy)>6){ drag.moveu=true; drag.el.classList.add("drag"); }
  if(drag.moveu) drag.el.style.transform=`translateY(${dy}px)`;
});
$("#lanes").addEventListener("pointerup",e=>{
  if(!drag) return;
  const {el,it,moveu}=drag;
  el.classList.remove("drag"); el.style.transform="";
  drag=null;
  if(!moveu) return; // clique simples — deixa o "click" nativo abrir a ficha
  arrastouAgora=true;
  const r=$("#lanes").getBoundingClientRect();
  const novaLane=laneDeY(e.clientY-r.top);
  const novoCharriot = novaLane===0 ? null : CHARRIOTS[novaLane-1];
  if(novoCharriot===it.charriot){ renderLanes(); return; }
  const anterior=it.charriot;
  it.charriot=novoCharriot;
  renderLanes();
  atribuirServidor(it, anterior);
});
$("#lanes").addEventListener("click",e=>{
  if(arrastouAgora){ arrastouAgora=false; return; }
  const b=e.target.closest(".blk"); if(!b) return;
  openSheet(+b.dataset.id);
});
async function atribuirServidor(it, anterior){
  try{
    const r=await fetch("/planeamento-entradas/atribuir",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:it.id,charriot:it.charriot})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); it.charriot=anterior; renderLanes(); return; }
    log("local",`"${it.titulo}" atribuída a ${it.charriot||"por atribuir"}`);
  }catch(e){ alert("Falhou a guardar: "+e); it.charriot=anterior; renderLanes(); }
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

    <div class="frow"><label class="destaque">Em contínuo</label>
      <input id="fEmContinuo" type="checkbox" style="width:auto;flex:0 0 auto;transform:scale(1.3)" ${it.emContinuo?"checked":""}></div>
    <div class="owner" style="font-size:12.5px;color:var(--dim);margin-top:4px">
      Marcado: entra no charriot no mesmo dia do início da produção. Desmarcado: entra no dia anterior
      (ou sexta-feira, se isso cair a fim de semana).</div>

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
      <button class="btn primary" id="guardar">Guardar</button>
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
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
    let toroCmp=null;
    if($("#fToroCmp").value==="outro"){ toroCmp=$("#fToroCmpOutro").value?parseFloat($("#fToroCmpOutro").value):null; }
    else if($("#fToroCmp").value){ toroCmp=parseFloat($("#fToroCmp").value); }
    const toroTipo=$("#fToroTipo").value||null;
    const emContinuo=$("#fEmContinuo").checked;
    try{
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
      gs:idxOf(e.dia_entrada), charriot:e.charriot, emContinuo:!!e.em_continuo,
      wipCmp:e.wip_cmp, wipLar:e.wip_lar, wipEsp:e.wip_esp,
      indiceWip:e.indice_wip, qtdWip:e.qtd_wip_m3,
      toroCmp:e.toro_cmp, toroTipo:e.toro_tipo,
      indiceToros:e.indice_toros, qtdToros:e.qtd_toros_m3,
    })).filter(e=>e.gs>=0);
    historico=d.historico||historico;
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    render();
    log("local",`lido do Basecamp: ${entradas.length} OFs`);
  }catch(e){
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Falhou a ligação ao Basecamp";
    log("local",`erro a ler o Basecamp: ${e}`);
  }
}
$("#atualizar").onclick=()=>carregar();
carregar();
</script>
</body>
</html>
"""
