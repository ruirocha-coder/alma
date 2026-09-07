# tools/planeamento_serracao.py — quadro de planeamento de produção da
# Ecos Largos: agenda visual (arrastar e largar) de que OF vai para que
# linha de produção e em que dia, cruzando os cards reais do Basecamp
# (projeto "Ecos Largos") com o agendamento local (linha/início/duração —
# só aqui, o Basecamp não tem nenhum campo para isto).
#
# Regra combinada com o Rui (2026-09): SEM sincronização contínua. Ao criar
# uma encomenda nova aqui, cria-se o card correspondente no Basecamp (coluna
# Triagem — ver basecamp.criar_card). A partir daí, quadro e Basecamp
# trabalham de forma independente: arrastar/reagendar/redimensionar uma OF
# já existente nunca escreve nada de volta no Basecamp, e mudanças feitas
# no Basecamp nunca "empurram" sozinhas para aqui — a página relê o
# Basecamp sempre que é aberta ou atualizada manualmente.
import unicodedata
import db
from tools import basecamp

PROJETO = "Ecos Largos"

# colunas do card table real do Ecos Largos que representam OFs em fluxo de
# fabrico (confirmado ao vivo, 2026-09, contra a API real). As colunas
# "Linha 1" a "Linha 6" / Charriots / Empilhadores do mesmo quadro guardam
# cards de ALOCAÇÃO DE PESSOAL, não OFs — ficam de fora deste quadro, por
# pedido explícito do Rui.
COLUNAS_OF = {"triagem", "programacao", "em producao", "produzido"}

# linhas de produção reais da serração (mesmos nomes vistos nas colunas de
# pessoal do Basecamp, confirmado ao vivo) — usadas aqui só como categorias
# do agendamento local; não têm nenhuma ligação às colunas do Basecamp com
# o mesmo nome.
LINHAS = [
    "Linha 1 - Quad",
    "Linha 2 Reguas/Barrotes",
    "Linha 3 Bartly",
    "Linha 4 Mult.Troncos",
    "Linha 5 Tabuinha",
    "Linha 6 Alinhadeira",
]

def _normalizar(texto: str) -> str:
    sem_acentos = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acentos.lower().strip()

def _cards_of_ativos() -> list[dict]:
    """Todos os cards de OF ativos do quadro Kanban do Ecos Largos, só das
    colunas de fluxo de fabrico (ver COLUNAS_OF) — vai sempre buscar dados
    atuais ao Basecamp (basecamp.cards_de_card_table não tem cache), nunca
    mantém cópia à parte. Passa "" como nome do quadro porque o Ecos Largos
    tem um único card table ativo — não é preciso adivinhar o título exato
    dele (ver basecamp.cards_de_card_table: "" é substring de qualquer
    título)."""
    cards = basecamp.cards_de_card_table("", projeto=PROJETO)
    return [c for c in cards if _normalizar(c.get("estado")) in COLUNAS_OF]

def estado_planeamento_serracao() -> dict:
    """Junta os cards de OF reais do Basecamp com o agendamento local
    (linha/dia/duração) — devolve a bolsa (OFs sem linha/dia atribuídos,
    mais recentes criadas primeiro) e as OFs já agendadas, prontas a
    desenhar no quadro."""
    agendamentos = {a["basecamp_card_id"]: a for a in db.agendamentos_producao_ecos_largos()}
    bolsa, agendadas = [], []
    for c in _cards_of_ativos():
        agendamento = agendamentos.get(c["id"])
        tem_agendamento = bool(agendamento and agendamento["linha"] and agendamento["dia_inicio"])
        # a fila (bolsa por agendar) só mostra OFs ainda em Triagem — uma OF
        # que já avançou no Basecamp (Programação/Em Produção/Produzido) sem
        # nunca ter sido agendada aqui já não é "por agendar", é trabalho já
        # em curso fora deste quadro, por isso fica de fora por completo
        # (pedido explícito do Rui, 2026-09). Só continua a aparecer, na
        # grelha, se já tiver um agendamento local guardado de antes.
        if not tem_agendamento and _normalizar(c.get("estado")) != "triagem":
            continue
        info = {
            "basecamp_card_id": c["id"],
            "titulo": c["titulo"],
            "coluna_basecamp": c["estado"],
            "prazo": c["prazo"],
            "criado_em": c.get("criado_em"),
            "url": c["url"],
        }
        if tem_agendamento:
            info["linha"] = agendamento["linha"]
            info["dia_inicio"] = agendamento["dia_inicio"]
            info["duracao_dias"] = agendamento["duracao_dias"]
            agendadas.append(info)
        else:
            bolsa.append(info)
    # encomendas mais recentes primeiro (pedido explícito do Rui, 2026-09)
    # — não por prazo, para uma encomenda nova (normalmente ainda sem
    # prazo definido) não ficar escondida ao fundo da fila.
    bolsa.sort(key=lambda c: c.get("criado_em") or "", reverse=True)
    return {"linhas": LINHAS, "bolsa": bolsa, "agendadas": agendadas}

def agendar(basecamp_card_id: int, linha: str, dia_inicio: str, duracao_dias: int) -> dict:
    """Agenda (ou reagenda) uma OF numa linha/dia — só na base local, nunca
    escreve nada no Basecamp (ver nota no topo do módulo)."""
    if linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    if not dia_inicio:
        return {"erro": "falta indicar o dia de início"}
    try:
        duracao_dias = int(duracao_dias)
    except (TypeError, ValueError):
        return {"erro": "duração inválida"}
    if duracao_dias < 1:
        return {"erro": "duração tem de ser pelo menos 1 dia"}
    return db.guardar_agendamento_producao(basecamp_card_id, linha, dia_inicio, duracao_dias)

def desagendar(basecamp_card_id: int) -> dict:
    """Devolve uma OF à bolsa por agendar — só na base local."""
    return db.desagendar_producao(basecamp_card_id)

def apagar_encomenda(basecamp_card_id: int) -> dict:
    """Apaga uma encomenda por completo: manda o card real para o lixo do
    Basecamp (ver basecamp.apagar_card — reversível lá, durante algum
    tempo, tal como apagar manualmente) e remove o agendamento local, se
    existir. Ação a usar só quando for mesmo preciso (ex: encomenda criada
    por engano) — pedido explícito do Rui, 2026-09."""
    basecamp.apagar_card(basecamp_card_id, projeto=PROJETO)
    db.remover_agendamento_producao(basecamp_card_id)
    return {"apagado": True, "basecamp_card_id": basecamp_card_id}

def criar_encomenda(titulo: str, notas: str = "", linha: str = None,
                    dia_inicio: str = None, duracao_dias: int = 1) -> dict:
    """Cria uma encomenda nova: um card real na coluna Triagem do Basecamp
    (ver basecamp.criar_card) e, se já vier com linha/dia, o agendamento
    local logo a acompanhar. A partir de criado, este card passa a ser
    totalmente independente — ver nota no topo do módulo."""
    titulo = (titulo or "").strip()
    if not titulo:
        return {"erro": "indica um título para a encomenda"}
    if linha and linha not in LINHAS:
        return {"erro": f"linha desconhecida: {linha!r}"}
    card = basecamp.criar_card("Triagem", titulo, notas or "", projeto=PROJETO)
    resultado = {
        "basecamp_card_id": card["id"],
        "titulo": card["titulo"],
        "coluna_basecamp": card["estado"],
        "prazo": card["prazo"],
        "url": card["url"],
    }
    if linha and dia_inicio:
        duracao_dias = max(1, int(duracao_dias or 1))
        db.guardar_agendamento_producao(card["id"], linha, dia_inicio, duracao_dias)
        resultado.update({"linha": linha, "dia_inicio": dia_inicio, "duracao_dias": duracao_dias})
    return resultado

def pagina_planeamento() -> str:
    return _TEMPLATE

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Planeamento da serração — Ecos Largos</title>
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

  .fila{background:var(--paper);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
  .filaHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
  .filaHead h2{margin:0;font-size:15px;font-weight:700;color:var(--ink)}
  .filaRow{display:flex;gap:10px;overflow-x:auto;padding:2px 2px 4px}
  .qcard{flex:0 0 auto;min-width:190px;max-width:230px;background:var(--paper);border:1px solid var(--line);
    border-left:5px solid var(--grey);border-radius:9px;padding:9px 11px;touch-action:none;cursor:grab;
    box-shadow:0 1px 2px rgba(0,0,0,.07)}
  .qcard.atrasado{border-left-color:var(--red)}
  .qcard:hover{box-shadow:0 2px 6px rgba(0,0,0,.10)}
  .qcard .tt{font-size:14.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .qcard .of{font-size:12px;color:var(--dim);margin-top:2px}
  .empty{color:var(--dim);font-size:13.5px;padding:6px 0}

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
  .blk{position:absolute;height:calc(var(--lane) - 18px);margin-top:9px;
    background:var(--paper);border:1px solid var(--line);border-left:5px solid var(--grey);
    border-radius:9px;padding:6px 9px;overflow:hidden;pointer-events:auto;cursor:grab;
    touch-action:none;user-select:none;box-shadow:0 1px 2px rgba(0,0,0,.09)}
  .blk.atrasado{border-left-color:var(--red)}
  .blk:hover{box-shadow:0 2px 7px rgba(0,0,0,.13)}
  .blk:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
  .blk .tt{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .blk .of{font-size:11.5px;color:var(--dim);margin-top:1px;white-space:nowrap}
  .dense .blk{padding:4px 6px;height:calc(var(--lane) - 14px);margin-top:7px;border-radius:7px}
  .dense .blk .of{display:none}
  .dense .blk .tt{font-size:12px}
  .blk.drag{cursor:grabbing;box-shadow:0 8px 22px rgba(0,0,0,.22);z-index:9;border-color:var(--blue)}
  .blk.flash,.qcard.flash{animation:flash 1s ease-out}
  @keyframes flash{0%{background:var(--hoje)}100%{background:var(--paper)}}
  .blk.clipL{border-top-left-radius:0;border-bottom-left-radius:0;border-left-style:dashed}
  .blk.clipR{border-top-right-radius:0;border-bottom-right-radius:0;border-right:1px dashed var(--edge)}
  .grip{position:absolute;right:0;top:0;bottom:0;width:14px;cursor:ew-resize;touch-action:none}
  .grip::after{content:"";position:absolute;right:4px;top:50%;transform:translateY(-50%);
    width:2px;height:16px;border-radius:2px;background:var(--edge)}
  .ghost{position:fixed;z-index:99;pointer-events:none;box-shadow:0 8px 22px rgba(0,0,0,.22);opacity:.95}

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
  .lt em{font-style:normal;color:var(--dim)}

  .veil{position:fixed;inset:0;background:rgba(26,28,30,.35);display:none}
  .veil.on{display:block}
  .sheet{position:fixed;left:0;right:0;bottom:0;background:var(--paper);
    border-radius:16px 16px 0 0;padding:20px 20px 28px;max-width:560px;margin:0 auto;
    box-shadow:0 -8px 30px rgba(0,0,0,.20);
    transform:translateY(101%);transition:transform .18s ease}
  .sheet.on{transform:none}
  @media (prefers-reduced-motion:reduce){.sheet{transition:none}}
  .sheet h3{margin:0 0 2px;font-size:20px;font-weight:700;letter-spacing:-.01em}
  .sheet .of{font-size:12.5px;color:var(--dim);margin-bottom:10px}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
    border-top:1px solid var(--line);font-size:14px}
  .kv span{color:var(--dim)}
  .owner{font-size:12.5px;color:var(--dim);margin-top:12px;background:var(--canvas);
    border-radius:8px;padding:9px 11px}
  .acts{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
  select,input,textarea{background:var(--paper);color:var(--ink);border:1px solid var(--edge);
    border-radius:8px;padding:6px 9px;font:inherit;font-size:14px}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:0;border-color:var(--blue)}
  .frow{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:9px 0;border-top:1px solid var(--line)}
  .frow label{color:var(--dim);font-size:14px;flex:0 0 auto}
  .frow input,.frow select,.frow textarea{flex:1 1 auto;min-width:0;max-width:62%}
  .err{color:var(--red);font-size:13px;min-height:16px;padding-top:8px}
  a.btn{display:inline-block;text-decoration:none}
  .btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:600}
  .btn.primary:hover{background:#175CAF;border-color:#175CAF}
  .add{border:1px solid var(--edge);background:var(--paper);border-radius:8px;
    padding:5px 12px;font-size:13.5px;color:var(--blue);font-weight:600}
  .add:hover{border-color:var(--blue)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Planeamento da serração</h1>
    <div class="sub">Ecos Largos · dias úteis, domingos fechados</div>
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
    <div class="pill"><b id="statBolsa">—</b> por agendar</div>
    <div class="pill"><b id="statAgendadas">—</b> agendadas</div>
    <button class="btn" id="undo">Anular</button>
    <button class="btn" id="atualizar">Atualizar do Basecamp</button>
  </div>

  <section class="fila">
    <div class="filaHead">
      <h2>Fila — por agendar (coluna Triagem/Programação no Basecamp)</h2>
      <button class="add" id="novo">+ Nova encomenda</button>
    </div>
    <div class="filaRow" id="fila"></div>
  </section>

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
/* ---------- calendário: dias úteis, domingos excluídos ---------- */
const DOW=["dom","seg","ter","qua","qui","sex","sáb"];
const MES=["janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"];
const MESC=["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"];
const MASTER=[];
{
  const inicio=new Date(); inicio.setDate(inicio.getDate()-21);
  const fim=new Date(); fim.setDate(fim.getDate()+150);
  for(let d=new Date(inicio); d<=fim; d.setDate(d.getDate()+1)){
    if(d.getDay()===0) continue;
    MASTER.push({y:d.getFullYear(),mo:d.getMonth(),dd:d.getDate(),dow:d.getDay(),
      iso:`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`});
  }
}
const hojeISO=(()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`})();
const HOJE=MASTER.findIndex(d=>d.iso===hojeISO);
const idxOf=iso=>MASTER.findIndex(d=>d.iso===iso);

let LINHAS=[];
let cards=[];
let view={mode:"semana",start:Math.max(HOJE,0),len:6};
let undoStack=[], logs=[], DAY=92, LANE=78;

const $=s=>document.querySelector(s);
const card=id=>cards.find(c=>c.id===id);
const clamp=(v,a,b)=>Math.max(a,Math.min(v,b));
const atrasado=c=>c.prazo && c.prazo<hojeISO;

/* ---------- carregar dados reais do Basecamp + agendamento local ---------- */
async function carregar(){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A ler o Basecamp…";
  try{
    const r=await fetch("/planeamento-ecos-largos/dados");
    const d=await r.json();
    LINHAS=d.linhas;
    cards=[
      ...d.bolsa.map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
        prazo:c.prazo,url:c.url,linha:null,gs:null,dur:1})),
      ...d.agendadas.map(c=>({id:c.basecamp_card_id,titulo:c.titulo,coluna:c.coluna_basecamp,
        prazo:c.prazo,url:c.url,linha:LINHAS.indexOf(c.linha),
        gs:idxOf(c.dia_inicio),dur:c.duracao_dias})).filter(c=>c.linha>=0&&c.gs>=0)
    ];
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
    render();
    log("local",`lido do Basecamp: ${d.bolsa.length} por agendar, ${d.agendadas.length} agendadas`);
  }catch(e){
    $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Falhou a ligação ao Basecamp";
    log("local",`erro a ler o Basecamp: ${e}`);
  }
}

/* ---------- período ---------- */
function setMode(m){
  view.mode=m;
  const anchor=view.start;
  if(m==="semana"||m==="duas"){
    view.len = m==="semana"?6:12;
    view.start = clamp(Math.floor(anchor/6)*6, 0, Math.max(MASTER.length-view.len,0));
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
function rangeLabel(){
  const a=MASTER[view.start], b=MASTER[Math.min(view.start+view.len-1,MASTER.length-1)];
  if(!a||!b) return "";
  if(view.mode==="mes") return MES[a.mo][0].toUpperCase()+MES[a.mo].slice(1)+" "+a.y;
  if(a.mo===b.mo) return `${a.dd}–${b.dd} ${MESC[a.mo]}`;
  return `${a.dd} ${MESC[a.mo]} – ${b.dd} ${MESC[b.mo]}`;
}

/* ---------- render ---------- */
function metrics(){
  const avail=$("#scroll").clientWidth||600;
  if(view.mode==="mes"){ DAY=44; LANE=64; }
  else if(view.mode==="duas"){ DAY=Math.max(84,Math.floor(avail/12)); LANE=78; }
  else { DAY=Math.max(96,Math.floor(avail/6)); LANE=78; }
  document.documentElement.style.setProperty("--day",DAY+"px");
  document.documentElement.style.setProperty("--lane",LANE+"px");
  $("#board").classList.toggle("dense",DAY<70);
}
function days(){ return MASTER.slice(view.start,view.start+view.len); }
function renderLabels(){
  $("#labels").innerHTML='<div class="head"></div>'+LINHAS.map(n=>
    `<div class="lbl"><div class="n">${n}</div></div>`).join("");
}
function renderDays(){
  const D=days();
  $("#days").innerHTML=D.map((d,i)=>{
    const wk=d.dow===6, hoje=view.start+i===HOJE;
    return `<div class="day${wk?" wk":""}${wk?" sab":""}${hoje?" hoje":""}">
      <div class="dn">${view.mode==="mes"?d.dd:DOW[d.dow]+" "+d.dd}</div>
      <div class="dm">${view.mode==="mes"?DOW[d.dow][0]:MESC[d.mo]}</div></div>`;
  }).join("");
  $("#days").style.width=(D.length*DAY)+"px";
}
function renderLanes(){
  const D=days();
  let h="";
  LINHAS.forEach(()=>{ h+='<div class="row">'+D.map(d=>{
    const hoje=MASTER.indexOf(d)===HOJE;
    return `<div class="cell${d.dow===6?" wk":""}${hoje?" hoje":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  cards.filter(c=>c.linha!==null).forEach(c=>{
    const a=c.gs-view.start, b=a+c.dur;
    if(b<=0||a>=view.len) return;
    const l=Math.max(a,0), r=Math.min(b,view.len);
    const el=document.createElement("div");
    el.className="blk"+(atrasado(c)?" atrasado":"")+(a<0?" clipL":"")+(b>view.len?" clipR":"");
    el.tabIndex=0; el.dataset.id=c.id;
    el.style.left=(l*DAY+3)+"px"; el.style.top=(c.linha*LANE)+"px";
    el.style.width=((r-l)*DAY-8)+"px";
    el.innerHTML=`<div class="tt">${c.titulo}</div>
      <div class="of">${c.prazo?("prazo "+c.prazo):"sem prazo"}</div><div class="grip"></div>`;
    bl.appendChild(el);
  });
  stats();
}
function renderFila(){
  const q=cards.filter(c=>c.linha===null);
  $("#fila").innerHTML = q.length ? q.map(c=>
    `<div class="qcard${atrasado(c)?" atrasado":""}" data-id="${c.id}">
     <div class="tt">${c.titulo}</div>
     <div class="of">${c.coluna||""}${c.prazo?(" · prazo "+c.prazo):""}</div></div>`).join("")
    : '<div class="empty">Fila vazia.</div>';
}
function stats(){
  $("#statBolsa").textContent=cards.filter(c=>c.linha===null).length;
  $("#statAgendadas").textContent=cards.filter(c=>c.linha!==null).length;
}
function render(){
  metrics(); renderLabels(); renderDays(); renderLanes(); renderFila();
  $("#range").textContent=rangeLabel();
  document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.m===view.mode));
}
function renderLog(){
  $("#log").innerHTML=logs.slice(0,40).map(l=>
    `<li><span class="arrow ${l.dir}">${l.dir==="local"?"·":"→"}</span>
     <span class="lt mono">${l.t}</span></li>`).join("");
}
function log(dir,t){ logs.unshift({dir,t}); renderLog(); }

/* ---------- gravar agendamento local (nunca escreve no Basecamp) ---------- */
async function guardarAgendamento(c){
  $("#syncDot").classList.add("busy"); $("#syncTxt").textContent="A gravar…";
  try{
    const r=await fetch("/planeamento-ecos-largos/agendar",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id,linha:LINHAS[c.linha],
        dia_inicio:MASTER[c.gs].iso,duracao_dias:c.dur})});
    const d=await r.json();
    if(d.erro){ log("local",`erro ao guardar: ${d.erro}`); }
    else log("local",`guardado: ${c.titulo} → ${LINHAS[c.linha]}, ${MASTER[c.gs].iso}, ${c.dur}d`);
  }catch(e){ log("local",`erro ao guardar: ${e}`); }
  $("#syncDot").classList.remove("busy"); $("#syncTxt").textContent="Ligado ao Basecamp";
}
async function desagendarServidor(c){
  try{
    await fetch("/planeamento-ecos-largos/desagendar",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:c.id})});
    log("local",`devolvido à fila: ${c.titulo}`);
  }catch(e){ log("local",`erro ao devolver à fila: ${e}`); }
}
function sync(c){ if(c.linha!==null && c.gs!==null) guardarAgendamento(c); else desagendarServidor(c); }

/* ---------- arrastar dentro da grelha ---------- */
let drag=null;
$("#lanes").addEventListener("pointerdown",e=>{
  const b=e.target.closest(".blk"); if(!b)return;
  const c=card(+b.dataset.id);
  drag={mode:e.target.classList.contains("grip")?"resize":"move",el:b,c,
        x0:e.clientX,y0:e.clientY,gs0:c.gs,lin0:c.linha,dur0:c.dur,dx:0,dy:0,dur:c.dur};
  b.setPointerCapture(e.pointerId); b.classList.add("drag"); e.preventDefault();
});
$("#lanes").addEventListener("pointermove",e=>{
  if(!drag)return;
  if(drag.mode==="move"){
    let dd=Math.round((e.clientX-drag.x0)/DAY), dl=Math.round((e.clientY-drag.y0)/LANE);
    dd=clamp(dd, -drag.gs0, MASTER.length-drag.dur0-drag.gs0);
    dl=clamp(dl, -drag.lin0, LINHAS.length-1-drag.lin0);
    drag.dx=dd; drag.dy=dl;
    drag.el.style.transform=`translate(${dd*DAY}px,${dl*LANE}px)`;
  }else{
    let dd=Math.round((e.clientX-drag.x0)/DAY);
    dd=clamp(dd, 1-drag.dur0, MASTER.length-drag.gs0-drag.dur0);
    drag.dur=drag.dur0+dd;
    drag.el.style.width=(drag.dur*DAY-8)+"px";
  }
});
$("#lanes").addEventListener("pointerup",()=>{
  if(!drag)return; const c=drag.c; let moved=false;
  if(drag.mode==="move"&&(drag.dx||drag.dy)){
    undoStack.push({id:c.id,linha:c.linha,gs:c.gs,dur:c.dur});
    c.gs+=drag.dx; c.linha+=drag.dy; moved=true;
  }
  if(drag.mode==="resize"&&drag.dur!==drag.dur0){
    undoStack.push({id:c.id,linha:c.linha,gs:c.gs,dur:c.dur});
    c.dur=drag.dur; moved=true;
  }
  drag.el.classList.remove("drag"); drag=null;
  renderLanes(); if(moved) sync(c);
});

/* fila → grelha */
let qdrag=null;
$("#fila").addEventListener("pointerdown",e=>{
  const q=e.target.closest(".qcard"); if(!q)return;
  const g=q.cloneNode(true); g.className="qcard ghost";
  g.style.width=q.offsetWidth+"px"; document.body.appendChild(g);
  const move=ev=>{ g.style.left=(ev.clientX-q.offsetWidth/2)+"px"; g.style.top=(ev.clientY-22)+"px"; };
  qdrag={id:+q.dataset.id,g,move}; move(e);
  q.setPointerCapture(e.pointerId); e.preventDefault();
});
$("#fila").addEventListener("pointermove",e=>{ if(qdrag) qdrag.move(e); });
$("#fila").addEventListener("pointerup",e=>{
  if(!qdrag)return; const {id,g}=qdrag; g.remove();
  const r=$("#lanes").getBoundingClientRect();
  const x=e.clientX-r.left, y=e.clientY-r.top; qdrag=null;
  if(x<0||y<0||y>r.height||x>view.len*DAY) return;
  const c=card(id);
  undoStack.push({id:c.id,linha:c.linha,gs:c.gs,dur:c.dur});
  c.linha=clamp(Math.floor(y/LANE),0,LINHAS.length-1);
  c.gs=view.start+clamp(Math.floor(x/DAY),0,view.len-c.dur);
  renderFila(); renderLanes(); sync(c);
});

/* ---------- ficha ---------- */
$("#lanes").addEventListener("click",e=>{
  const b=e.target.closest(".blk"); if(b&&!e.target.classList.contains("grip")) openSheet(+b.dataset.id);
});
$("#fila").addEventListener("click",e=>{
  const q=e.target.closest(".qcard"); if(q) openSheet(+q.dataset.id);
});
function openSheet(id){
  const c=card(id);
  const agendado = c.linha!==null && c.gs!==null;
  let corpo = `<div class="kv"><span>Estado</span><b>Por agendar</b></div>`;
  if(agendado){
    const s=MASTER[c.gs], f=MASTER[clamp(c.gs+c.dur-1,0,MASTER.length-1)];
    corpo = `
    <div class="kv"><span>Linha</span><b>${LINHAS[c.linha]}</b></div>
    <div class="kv"><span>Início</span><b>${DOW[s.dow]} ${s.dd} ${MESC[s.mo]}</b></div>
    <div class="kv"><span>Fim</span><b>${DOW[f.dow]} ${f.dd} ${MESC[f.mo]} · ${c.dur} dias</b></div>`;
  }
  $("#sheet").innerHTML=`
    <div class="of mono">card ${c.id}</div>
    <h3>${c.titulo}</h3>
    ${corpo}
    <div class="kv"><span>Coluna no Basecamp</span><b>${c.coluna||"—"}</b></div>
    <div class="kv"><span>Prazo no Basecamp</span><b>${c.prazo||"sem prazo"}</b></div>
    <div class="owner">A linha, o início e a duração vivem só aqui — o Basecamp não tem onde os guardar. Mudar isto aqui não altera nada no Basecamp.</div>
    <div class="acts">
      ${agendado?'<button class="btn" id="toFila">Devolver à fila</button>':""}
      ${c.url?`<a class="btn" id="bcOpen" target="_blank" rel="noopener" href="${c.url}">Abrir card no Basecamp</a>`:""}
      <button class="btn warn" id="apagar">Apagar encomenda</button>
      <button class="btn" id="close">Fechar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  $("#close").onclick=$("#veil").onclick=closeSheet;
  if(agendado) $("#toFila").onclick=()=>{
    undoStack.push({id:c.id,linha:c.linha,gs:c.gs,dur:c.dur});
    c.linha=null; c.gs=null;
    renderFila(); renderLanes(); sync(c); closeSheet();
  };
  $("#apagar").onclick=async()=>{
    if(!confirm(`Apagar definitivamente "${c.titulo}"?\n\nIsto manda o card para o lixo no Basecamp (fica lá recuperável durante algum tempo, tal como apagar manualmente).`)) return;
    $("#apagar").textContent="A apagar…"; $("#apagar").disabled=true;
    try{
      const r=await fetch("/planeamento-ecos-largos/apagar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:c.id})});
      const d=await r.json();
      if(d.erro){ alert(d.erro); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; return; }
      cards=cards.filter(x=>x.id!==c.id);
      log("local",`apagado: ${c.titulo}`);
      render(); closeSheet();
    }catch(e){ alert("Falhou a apagar: "+e); $("#apagar").textContent="Apagar encomenda"; $("#apagar").disabled=false; }
  };
}
function closeSheet(){ $("#veil").classList.remove("on"); $("#sheet").classList.remove("on"); }

/* ---------- nova encomenda ---------- */
function openForm(pref){
  pref=pref||{}; const D=days();
  $("#sheet").innerHTML=`
    <h3>Criar encomenda</h3>
    <div class="frow"><label>Título</label><input id="fTt" placeholder="Ex: OF.530 EVERTIS"></div>
    <div class="frow"><label>Notas</label><textarea id="fNotas" rows="2" placeholder="opcional"></textarea></div>
    <div class="frow"><label>Colocar em</label><select id="fLin">
      <option value="">Fila, por agendar</option>
      ${LINHAS.map((n,i)=>`<option value="${i}"${pref.linha===i?" selected":""}>${n}</option>`).join("")}
    </select></div>
    <div class="frow" id="rowDia"><label>Início</label><select id="fDia">
      ${D.map((d,i)=>`<option value="${view.start+i}"${pref.gs===view.start+i?" selected":""}>${DOW[d.dow]} ${d.dd} ${MESC[d.mo]}</option>`).join("")}
    </select></div>
    <div class="frow" id="rowDur"><label>Duração (dias)</label><input id="fDur" type="number" min="1" max="30" value="1"></div>
    <div class="err" id="fErr"></div>
    <div class="owner">O card nasce sempre na coluna Triagem do Basecamp (projeto Ecos Largos). A linha e o início ficam só aqui.</div>
    <div class="acts">
      <button class="btn primary" id="fSave">Criar encomenda</button>
      <button class="btn" id="close">Cancelar</button>
    </div>`;
  $("#veil").classList.add("on"); $("#sheet").classList.add("on");
  const rowDia=$("#rowDia"), rowDur=$("#rowDur"), selLin=$("#fLin");
  const toggle=()=>{ const on=selLin.value!==""; rowDia.style.display=on?"":"none"; rowDur.style.display=on?"":"none"; };
  selLin.onchange=toggle; toggle();
  $("#close").onclick=$("#veil").onclick=closeSheet;
  $("#fTt").focus();
  $("#fSave").onclick=async()=>{
    const titulo=$("#fTt").value.trim(), notas=$("#fNotas").value.trim();
    if(!titulo){ $("#fErr").textContent="Escreve um título — é o título do card no Basecamp."; $("#fTt").focus(); return; }
    const linhaIdx=selLin.value===""?null:+selLin.value;
    const gs=linhaIdx===null?null:clamp(+$("#fDia").value,0,MASTER.length-1);
    const dur=linhaIdx===null?1:clamp(+$("#fDur").value||1,1,30);
    $("#fSave").textContent="A criar…"; $("#fSave").disabled=true;
    try{
      const body={titulo,notas,
        linha:linhaIdx===null?null:LINHAS[linhaIdx],
        dia_inicio:gs===null?null:MASTER[gs].iso,
        duracao_dias:dur};
      const r=await fetch("/planeamento-ecos-largos/nova-encomenda",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json();
      if(d.erro){ $("#fErr").textContent=d.erro; $("#fSave").textContent="Criar encomenda"; $("#fSave").disabled=false; return; }
      cards.unshift({id:d.basecamp_card_id,titulo:d.titulo,coluna:d.coluna_basecamp,prazo:d.prazo,url:d.url,
        linha:linhaIdx,gs,dur});
      log("local",`criado no Basecamp (Triagem): ${d.titulo}`);
      render(); closeSheet();
      setTimeout(()=>{ const b=document.querySelector(`.blk[data-id="${d.basecamp_card_id}"]`)||
        document.querySelector(`.qcard[data-id="${d.basecamp_card_id}"]`); if(b)b.classList.add("flash"); },30);
    }catch(e){ $("#fErr").textContent="Falhou a criar no Basecamp: "+e; $("#fSave").textContent="Criar encomenda"; $("#fSave").disabled=false; }
  };
}
$("#novo").onclick=()=>openForm();
$("#lanes").addEventListener("dblclick",e=>{
  if(e.target.closest(".blk"))return;
  const r=$("#lanes").getBoundingClientRect();
  openForm({linha:clamp(Math.floor((e.clientY-r.top)/LANE),0,LINHAS.length-1),
            gs:view.start+clamp(Math.floor((e.clientX-r.left)/DAY),0,view.len-1)});
});

/* ---------- controlos ---------- */
$("#seg").onclick=e=>{ const b=e.target.closest("button"); if(b) setMode(b.dataset.m); };
$("#prev").onclick=()=>step(-1);
$("#next").onclick=()=>step(1);
$("#hoje").onclick=()=>{ view.start=Math.max(HOJE,0); setMode(view.mode); };
$("#atualizar").onclick=()=>carregar();
$("#undo").onclick=()=>{
  if(!undoStack.length)return;
  const prev=undoStack.pop(); const c=card(prev.id);
  if(!c)return;
  c.linha=prev.linha; c.gs=prev.gs; c.dur=prev.dur;
  render(); sync(c); log("local","anulado");
};
document.addEventListener("keydown",e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==="z"){e.preventDefault();$("#undo").click();}
  if(e.key==="ArrowLeft"&&!e.target.closest("select"))step(-1);
  if(e.key==="ArrowRight"&&!e.target.closest("select"))step(1);
});
let rt; addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(render,120)});

carregar();
</script>
</body>
</html>
"""
