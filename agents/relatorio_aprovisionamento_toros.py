# agents/relatorio_aprovisionamento_toros.py — relatório de Aprovisionamento
# de Toros, publicado no Mural da Ecos Largos (pedido explícito do Rui,
# 2026-10-12): cruza as necessidades de madeira do Planeamento de Entradas
# (m³, por tipo+comprimento de toro) com o stock atual do Park In (kg), para
# mostrar o que falta encomendar antes de haver falhas na produção. Emitido
# às segundas-feiras, ou por solicitação (ver correr_relatorio_
# aprovisionamento_toros/main.py).
#
# "Isto não é fixo então as páginas têm de ser sempre lidas" (pedido
# explícito do Rui) — nunca guarda nada; lê sempre em direto o Planeamento
# de Entradas (tools/planeamento_entradas.estado_planeamento_entradas) e o
# stock do Park In (tools/parkin.stock_por_artigo) no momento em que corre.
import threading
from tools import basecamp, parkin
from tools import planeamento_entradas as pe

PROJETO = "Ecos Largos"
ASSUNTO = "Aprovisionamento de Toros"
# pedido explícito do Rui (2026-10-12): "a Alma sabe tagar no basecamp. e
# tem de identificar a Sandra Martins nesse post" — basta o nome aparecer no
# texto (ver tools/basecamp._adicionar_arroba_em_nomes_conhecidos) para virar
# uma menção real, desde que ela tenha acesso ao projeto Ecos Largos.
PESSOA_APROVISIONAMENTO = "Sandra Martins"

_a_correr = threading.Lock()

# Conversão kg -> m³ (pedido explícito do Rui, 2026-10-12): "usamos o valor
# de 1:1, ou seja 1m3 equivale a 1 tonelada. Seja a madeira IN ou MT" — por
# isso é exatamente a mesma conta que o Park In já faz para mostrar
# toneladas (ver tools/parkin.py, função `t()` no template: kg/1000).
KG_POR_M3 = 1000

#  menor comprimento real de toro já visto (TORO_CMP_PRESETS vai de 2350 a
# 3100mm) — qualquer valor bem abaixo disto é quase de certeza um erro de
# preenchimento (ex: um diâmetro escrito no campo do comprimento por
# engano), nunca um toro real; nunca vai corresponder a nenhum artigo do
# Park In, por isso entra em `suspeitos` em vez de numa linha de
# necessidade/stock que nunca bate certo.
COMPRIMENTO_MIN_PLAUSIVEL_MM = 1000

def _comprimento_m_de_toro_cmp(toro_cmp):
    """Converte o "Cmp" do toro do Planeamento de Entradas (texto, em
    milímetros — ex: "2640") para metros, para poder cruzar-se com o
    comprimento do Park In (número, em metros — ex: 2.64). Exige o texto
    TODO como só dígitos (como os TORO_CMP_PRESETS) — nunca interpreta um
    valor com qualificador de diâmetro (ex: "255 ⌀16") como um comprimento
    simples. None sempre que não for um comprimento plausível de toro (ver
    COMPRIMENTO_MIN_PLAUSIVEL_MM) — faz essa OF cair em `suspeitos` em vez
    de entrar errado numa linha de necessidade/stock que nunca vai bater
    certo."""
    if not toro_cmp:
        return None
    texto = str(toro_cmp).strip()
    if not texto.isdigit() or int(texto) < COMPRIMENTO_MIN_PLAUSIVEL_MM:
        return None
    return round(int(texto) / 1000, 2)

def necessidades_por_artigo() -> dict:
    """Para cada combinação tipo+comprimento de toro com necessidade
    identificada no Planeamento de Entradas (OFs já agendadas com Tipo/Cmp
    de toro preenchidos), soma essa necessidade (m³) e compara com o stock
    atual desse artigo no Park In (kg, somado por cima das categorias de
    qualidade e das duas espessuras — o Planeamento de Entradas não
    distingue espessura). `falta_m3` positivo = por encomendar; zero ou
    negativo = já coberto pelo stock atual.

    Devolve também:
    - `sem_info`: títulos das OFs com necessidade de toros (`qtd_toros_m3`
      já calculado a partir do volume) mas sem Tipo e/ou Cmp de toro ainda
      preenchidos — pedido explícito do Rui: "algumas ainda não estão
      preenchidas, mas vão ser tratadas agora", para o relatório lembrar
      quais faltam.
    - `suspeitos`: títulos das OFs com Tipo e Cmp PREENCHIDOS mas cujo Cmp
      não é um comprimento plausível de toro (ver
      COMPRIMENTO_MIN_PLAUSIVEL_MM — ex: um diâmetro escrito por engano no
      campo do comprimento) — ficam de fora das contas (não entram em
      nenhuma linha de necessidade/stock), mas são um erro de preenchimento
      a corrigir, não uma falta de informação como `sem_info`."""
    entradas = pe.estado_planeamento_entradas()["entradas"]
    stock = parkin.stock_por_artigo()

    stock_m3_por_chave = {}
    for s in stock:
        if s["artigo"] == "desconhecido":
            continue
        chave = (s["tipo"], s["comprimento"])
        stock_m3_por_chave[chave] = stock_m3_por_chave.get(chave, 0.0) + s["total_kg"] / KG_POR_M3

    grupos = {}
    sem_info = []
    suspeitos = []
    for e in entradas:
        # "Produzido" (marcador manual desta página, ver tools/
        # planeamento_entradas.definir_produzido) significa que os toros
        # desta OF já entraram — não conta como necessidade por vir.
        if e.get("produzido") or e.get("qtd_toros_m3") is None:
            continue
        toro_tipo, toro_cmp = e.get("toro_tipo"), e.get("toro_cmp")
        if not toro_tipo or not toro_cmp:
            sem_info.append(e["titulo"])
            continue
        comprimento_m = _comprimento_m_de_toro_cmp(toro_cmp)
        if comprimento_m is None:
            suspeitos.append({"titulo": e["titulo"], "toro_cmp": toro_cmp})
            continue
        chave = (toro_tipo, comprimento_m)
        g = grupos.setdefault(chave, {"tipo": toro_tipo, "comprimento_m": comprimento_m, "necessidade_m3": 0.0})
        g["necessidade_m3"] += e["qtd_toros_m3"]

    linhas = []
    for chave, g in grupos.items():
        stock_m3 = stock_m3_por_chave.get(chave, 0.0)
        linhas.append({
            "tipo": g["tipo"], "comprimento_m": g["comprimento_m"],
            "necessidade_m3": round(g["necessidade_m3"], 2),
            "stock_m3": round(stock_m3, 2),
            "falta_m3": round(g["necessidade_m3"] - stock_m3, 2),
        })
    linhas.sort(key=lambda l: -l["falta_m3"])
    return {"linhas": linhas, "sem_info": sem_info, "suspeitos": suspeitos}

def _m3(v: float) -> str:
    return f"{v:.2f}".replace(".", ",") + " m³"

def _gerar_texto(dados: dict) -> str:
    linhas, sem_info, suspeitos = dados["linhas"], dados["sem_info"], dados["suspeitos"]
    partes = [f"{PESSOA_APROVISIONAMENTO}, segue o ponto de aprovisionamento de toros de hoje."]

    if not linhas:
        partes.append("\nSem necessidades de toros identificadas de momento — nenhuma OF agendada tem "
                      "ainda Tipo e Comprimento de toro preenchidos no Planeamento de Entradas.")
    else:
        faltam = [l for l in linhas if l["falta_m3"] > 0.05]
        cobertas = [l for l in linhas if l["falta_m3"] <= 0.05]
        if faltam:
            partes.append("\nPor encomendar:")
            for l in faltam:
                partes.append(f"- {l['tipo']} · {l['comprimento_m']:.2f}m — necessário {_m3(l['necessidade_m3'])}, "
                             f"em stock {_m3(l['stock_m3'])}, falta {_m3(l['falta_m3'])}")
        else:
            partes.append("\nTudo coberto pelo stock atual — sem necessidade de encomendar mais toros.")
        if cobertas:
            partes.append("\nJá cobertas pelo stock atual:")
            for l in cobertas:
                partes.append(f"- {l['tipo']} · {l['comprimento_m']:.2f}m — necessário {_m3(l['necessidade_m3'])}, "
                             f"em stock {_m3(l['stock_m3'])}")

    if sem_info:
        partes.append("\nOFs com necessidade de toros mas ainda sem Tipo/Comprimento preenchido no "
                      "Planeamento de Entradas (por isso fora das contas acima):")
        for titulo in sem_info:
            partes.append(f"- {titulo}")

    if suspeitos:
        partes.append("\nOFs com um Cmp de toro preenchido mas que não parece um comprimento real "
                      "(por confirmar/corrigir no Planeamento de Entradas — também fora das contas acima):")
        for s in suspeitos:
            partes.append(f"- {s['titulo']} (Cmp: {s['toro_cmp']!r})")

    partes.append("\n(stock e necessidades lidos em direto do Park In e do Planeamento de Entradas — "
                  "podem mudar a qualquer momento)")
    return "\n".join(partes)

def correr_relatorio_aprovisionamento_toros():
    """Publica já o relatório no Mural da Ecos Largos — pensado para correr
    às segundas-feiras (agendado) ou por solicitação (disparado à mão)."""
    if not _a_correr.acquire(blocking=False):
        print("[relatorio_aprovisionamento_toros] já há uma corrida em curso — ignorado")
        return
    try:
        dados = necessidades_por_artigo()
        texto = _gerar_texto(dados)
        basecamp.publicar_mural(ASSUNTO, texto, projeto=PROJETO)
        print("[relatorio_aprovisionamento_toros] publicado no mural da Ecos Largos")
    except Exception:
        import traceback
        print(f"[relatorio_aprovisionamento_toros] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()

# ferramenta de chat (pedido explícito do Rui, 2026-10-12: "por
# solicitação") — ao contrário de correr_relatorio_aprovisionamento_toros
# (usada pelo agendamento/endpoint HTTP, que nunca deve rebentar o
# processo), aqui o erro propaga-se mesmo, para a Alma poder dizer à
# pessoa que pediu que algo correu mal, em vez de responder "publicado"
# quando na verdade falhou.
TOOLS_RELATORIO_APROVISIONAMENTO = [
    {
        "name": "emitir_relatorio_aprovisionamento_toros",
        "description": "Publica já no Mural da Ecos Largos o relatório de Aprovisionamento de Toros — cruza as necessidades de madeira do Planeamento de Entradas com o stock atual do Park In e mostra o que falta encomendar, tagando a Sandra Martins. Já corre sozinho às segundas-feiras às 9h30; usa esta ferramenta só quando alguém pedir explicitamente para o emitir/publicar agora, fora desse horário — nunca por iniciativa própria.",
        "input_schema": {"type": "object", "properties": {}}
    }
]

def emitir_agora() -> dict:
    dados = necessidades_por_artigo()
    texto = _gerar_texto(dados)
    basecamp.publicar_mural(ASSUNTO, texto, projeto=PROJETO)
    return {"ok": True, "publicado_em": "Mural Ecos Largos", "artigos_com_necessidade": len(dados["linhas"]),
           "ofs_sem_toro_definido": len(dados["sem_info"]), "ofs_com_cmp_suspeito": len(dados["suspeitos"])}
