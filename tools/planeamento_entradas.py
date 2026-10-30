# tools/planeamento_entradas.py — página "Planeamento de Entradas" da Ecos
# Largos: planear o que entra nos charriots (Charriot 1/2/3, Multiserra de
# Toros, Aproveitamentos — este último acrescentado a pedido explícito do
# Rui, 2026-10-06), um dia antes do início da produção de cada OF (pedido
# explícito do Rui, 2026-10-01) — uma OF pode estar em vários charriots ao
# mesmo tempo (pedido explícito do Rui, 2026-10-02).
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
import itertools, json, re, threading
from datetime import date, timedelta
import anthropic
import db
from tools import basecamp, documentos_empresa
from tools import planeamento_serracao as ps

# cliente próprio (não agents.base.client): agents/base.py importa
# agents/relatorio_aprovisionamento_toros.py, que importa este módulo —
# importar agents.base daqui fecharia um ciclo. Mesmo padrão já usado por
# tools/parkin.py pela mesma razão.
_client = anthropic.Anthropic()

CHARRIOTS = ["Charriot 1", "Charriot 2", "Charriot 3", "Multiserra de Toros", "Aproveitamentos"]
# valores em milímetros (pedido explícito do Rui, 2026-10-04 — antes
# estavam em cm) — guardados como texto, não número, para poder vir a ter
# opções que não sejam um número puro (ex: com diâmetro) — ver nota na
# migração de toro_cmp para TEXT, em db.py.
TORO_CMP_PRESETS = ["2550", "2640", "3100", "2350"]
TORO_TIPOS = {"IN", "MT"}

# WIP: Cmp é uma lista fechada em mm (sem "outro", ao contrário do Toro);
# Lar é sempre fixa, já não se escolhe por OF; Esp só pode ser um dos
# valores da tabela "WIP Lotes" (documento no Basecamp, Ecos Largos/
# Documentos — espessura em mm, atualizado pelo Rui em 2026-10-04 para
# bater certo com o resto, que já estava todo em mm), cada um já
# associado ao seu nº de fiadas.
WIP_CMP_PRESETS = ["2350", "2550", "2650", "3100"]
WIP_LAR_FIXA = 1200
WIP_LOTES_TABELA = [
    (18, 40), (22, 35), (25, 30), (32, 25), (36, 20), (42, 16),
    (52, 14), (60, 12), (70, 10), (80, 8), (88, 7), (90, 7), (100, 6), (107, 6), (110, 6),
    (120, 5), (130, 5), (140, 4), (150, 4), (160, 4), (170, 4), (180, 4), (200, 3),
]
FIADAS_POR_ESPESSURA = {esp: fiadas for esp, fiadas in WIP_LOTES_TABELA}

def _fiadas_mais_proxima(esp_mm):
    """Nº de fiadas da tabela "WIP Lotes" para este WIP Esp — exata se
    bater com uma linha da tabela, senão a da espessura mais PRÓXIMA
    (nunca None só por não bater em cheio); a meio caminho exato entre
    duas linhas da tabela (ex: 125 entre 120 e 130), arredonda por
    EXCESSO — fica sempre com a mais alta das duas (pedido explícito do
    Rui, 2026-10-30: "arredondamento por excesso a partir de 5").
    Pedido explícito do Rui, 2026-10-30: desde que o WIP Esp passou a vir
    da largura (L) do nome do artigo (ver _wip_esp_do_nome_artigo), já
    raramente bate exatamente com uma linha da tabela. None só se esp_mm
    for None."""
    if esp_mm is None:
        return None
    esp = round(float(esp_mm))
    if esp in FIADAS_POR_ESPESSURA:
        return FIADAS_POR_ESPESSURA[esp]
    mais_proxima = min(FIADAS_POR_ESPESSURA, key=lambda e: (abs(e - esp), -e))
    return FIADAS_POR_ESPESSURA[mais_proxima]

# QTD Toros = volume da linha (m³) × INDICE_TOROS; QTD Wip = volume da
# linha (m³) × INDICE_WIP — pedido explícito do Rui (2026-10-01). Cada
# linha pode ter o seu próprio índice (ver guardar_linhas_entrada); estes
# são só os valores por omissão usados enquanto ninguém escolher outro à
# mão para essa linha em concreto.
INDICE_TOROS_DEFAULT = 2.85
INDICE_WIP_DEFAULT = 1.8

# "Algoritmo de Comprimentos" (pedido explícito do Rui, 2026-10-23): ao
# arrastar uma OF de "Por atribuir" para um charriot, lê-se o PDF da OF
# anexado ao card no Basecamp (nome tipo "OF <número> <cliente>.pdf"),
# identifica-se o(s) artigo(s) e o comprimento de cada um, e cria-se uma
# linha de WIP+Toro por artigo — WIP escolhido para encaixar o maior
# número de peças do artigo com a menor sobra (ver _otimizar_wip_cmp; um
# WIP serve para cortar VÁRIAS peças pequenas, nunca só uma — pedido
# explícito do Rui, 2026-10-30, correção de um erro real: "porque é que
# não escolheu o cmp 3100, na medida de 990x52x52?" — a regra anterior
# ("o preset mais pequeno que ainda chegue para uma peça") escolhia
# sempre 2350mm para qualquer peça pequena, mesmo quando um WIP maior
# desperdiçava relativamente menos ao encaixar mais peças) e Toro sempre
# o comprimento CORRESPONDENTE a esse WIP (ver _toro_cmp_de_wip) — nunca
# otimizado sozinho a partir do comprimento do artigo final: o toro dá
# origem ao WIP, nunca diretamente ao artigo (pedido explícito do Rui,
# 2026-10-30: "o comprimento do toro terá que ser semelhante ao
# comprimento da tábua cortada (wip)"). O volume de cada linha é o campo
# "QTD" desse artigo no PDF, tal como está escrito — já é o volume em m³
# dessa linha (ver _linhas_com_calculo), nunca uma proporção do volume
# total da OF (pedido explícito do Rui, 2026-10-30: "tens de ir buscar os
# m3 à coluna qtd no documento pdf, referente a cada artigo. e não à qtd
# total").
TOLERANCIA_CORTE_MM = 5

def _aproveitamento_pct_multi(comprimentos_mm, preset_mm):
    """% de aproveitamento de um WIP/Toro de `preset_mm` a cortar peças de
    uma LISTA de um ou mais comprimentos (cada um a precisar do seu
    comprimento + TOLERANCIA_CORTE_MM de espaço, a folga de corte/serra
    entre peças) — pelo menos UMA peça de CADA comprimento da lista tem de
    caber, senão devolve None (nunca combina só alguns). Testa todas as
    combinações de nº de peças de cada comprimento (1 até ao máximo que
    caiba sozinho) e fica com a que ocupa mais do preset. Com um único
    comprimento dá exactamente o mesmo resultado que antes desta função
    existir (só testava um). Generalizado (pedido explícito do Rui,
    2026-10-30: "dois comprimentos podem vir de um mesmo comprimento de
    wip... é necessário fazer uma análise prévia... e otimizar") para as
    linhas combinadas de dois artigos (ver _juntar_pares_otimizados) —
    mostrado junto da QTD Toros na ficha de uma OF, recalculado sempre ao
    vivo a partir dos comprimentos do(s) artigo(s) (nunca congelado, para
    continuar certo mesmo que o WIP/Toro Cmp seja corrigido à mão
    depois)."""
    comprimentos_mm = [float(c) for c in (comprimentos_mm or []) if c]
    if not comprimentos_mm or not preset_mm:
        return None
    preset_mm = float(preset_mm)
    unidades = [c + TOLERANCIA_CORTE_MM for c in comprimentos_mm]
    maximos = [int(preset_mm // u) for u in unidades]
    if any(m < 1 for m in maximos):
        return None
    melhor_ocupado = 0.0
    for combo in itertools.product(*[range(1, m + 1) for m in maximos]):
        ocupado = sum(n * u for n, u in zip(combo, unidades))
        if preset_mm >= ocupado > melhor_ocupado:
            melhor_ocupado = ocupado
    if melhor_ocupado <= 0:
        return None
    return round(melhor_ocupado / preset_mm * 100, 1)

def _aproveitamento_pct(comprimento_artigo_mm, preset_mm):
    """% de aproveitamento de um único comprimento de artigo — ver
    _aproveitamento_pct_multi (caso particular com um só comprimento na
    lista)."""
    return _aproveitamento_pct_multi([comprimento_artigo_mm], preset_mm)

def _otimizar_wip_cmp(comprimento_artigo_mm: float):
    """Entre WIP_CMP_PRESETS, o comprimento de WIP que encaixa o maior
    número de peças de `comprimento_artigo_mm` com a menor sobra possível
    (ver _aproveitamento_pct). Devolve o texto do preset, ou None se o
    artigo for maior do que todos os presets (nenhum chega para uma peça
    só)."""
    melhor = None
    for preset in WIP_CMP_PRESETS:
        pct = _aproveitamento_pct(comprimento_artigo_mm, preset)
        if pct is None:
            continue
        if melhor is None or pct > melhor[0]:
            melhor = (pct, preset)
    return melhor[1] if melhor else None

def _melhor_combinacao_par(comprimento1_mm: float, comprimento2_mm: float):
    """Entre WIP_CMP_PRESETS, o preset que, cortando os dois comprimentos
    JUNTOS (pelo menos uma peça de cada, ver _aproveitamento_pct_multi),
    consegue o maior aproveitamento — devolve (preset, aproveitamento_pct),
    ou None se não houver nenhum preset onde caibam os dois juntos (ver
    _juntar_pares_otimizados)."""
    melhor = None
    for preset in WIP_CMP_PRESETS:
        pct = _aproveitamento_pct_multi([comprimento1_mm, comprimento2_mm], preset)
        if pct is None:
            continue
        if melhor is None or pct > melhor[1]:
            melhor = (preset, pct)
    return melhor

# Correspondência 1 para 1 entre um preset de WIP_CMP_PRESETS e o preset
# de TORO_CMP_PRESETS mais próximo (pedido explícito do Rui, 2026-10-30)
# — são praticamente a mesma lista de comprimentos reais, só com "2650"
# do lado do WIP e "2640" do lado do Toro a designar o mesmo comprimento.
_WIP_CMP_PARA_TORO_CMP = {"2350": "2350", "2550": "2550", "2650": "2640", "3100": "3100"}

def _toro_cmp_de_wip(wip_cmp_texto: str):
    """O preset de Toro correspondente a este WIP Cmp (ver
    _WIP_CMP_PARA_TORO_CMP) — None se `wip_cmp_texto` for None (artigo
    maior que todos os presets de WIP, ver _otimizar_wip_cmp)."""
    if wip_cmp_texto is None:
        return None
    return _WIP_CMP_PARA_TORO_CMP.get(wip_cmp_texto)

def _juntar_pares_otimizados(artigos: list) -> list:
    """Pedido explícito do Rui, 2026-10-30: "na OF por vezes temos artigos
    cujo dois comprimentos podem vir de um mesmo comprimento de wip, é
    necessário fazer uma análise prévia aos comprimentos dos artigos e
    comparar com os comprimentos de wip disponíveis e otimizar... no card
    deve aparecer os dois artigos no campo 'artigo' e apresentar as
    quantidades de wip e toros somadas." Antes de criar as linhas de
    WIP+Toro de uma OF, testa se algum PAR de artigos com a mesma Largura
    (WIP Esp — só artigos da mesma secção podem sair fisicamente do mesmo
    WIP) aproveita GENUINAMENTE melhor cortado junto num preset do que
    cada um sozinho no seu melhor preset (ver _melhor_combinacao_par) — só
    então os junta numa única "linha" com nome "Artigo A + Artigo B" e
    "qtd" somada (QTD Wip/Toros somam-se sozinhas a partir daí, ver
    calcular_linhas_automaticamente/_linhas_com_calculo, que usam sempre o
    "peso"/"qtd" de cada linha diretamente). Só pares — nunca 3+ artigos
    na mesma linha, mesmo que a mesma Largura tenha mais que dois
    comprimentos (o melhor par ganha, os restantes ficam sozinhos, pedido
    explícito do Rui). Artigos sem Largura (nome sem padrão "CxLxE") nunca
    entram nesta análise. Devolve uma nova lista de artigos (combinados
    primeiro, depois os que ficaram sozinhos, pela ordem original) — nunca
    modifica `artigos`."""
    por_esp = {}
    for idx, a in enumerate(artigos):
        if a.get("wip_esp_mm"):
            por_esp.setdefault(a["wip_esp_mm"], []).append(idx)
    pares_escolhidos = []  # (i, j, preset, pct)
    for indices in por_esp.values():
        pool = list(indices)
        while len(pool) >= 2:
            melhor = None  # (ganho, i, j, preset, pct)
            for x in range(len(pool)):
                for y in range(x + 1, len(pool)):
                    i, j = pool[x], pool[y]
                    combinacao = _melhor_combinacao_par(artigos[i]["comprimento_mm"], artigos[j]["comprimento_mm"])
                    if combinacao is None:
                        continue
                    preset, pct = combinacao
                    pcts_separados = []
                    for k in (i, j):
                        preset_sozinho = _otimizar_wip_cmp(artigos[k]["comprimento_mm"])
                        if preset_sozinho is not None:
                            pcts_separados.append(_aproveitamento_pct(artigos[k]["comprimento_mm"], preset_sozinho))
                    melhor_separado = max(pcts_separados) if pcts_separados else 0
                    if pct <= melhor_separado:
                        continue  # só junta se bater GENUINAMENTE melhor que cada um sozinho
                    ganho = pct - melhor_separado
                    if melhor is None or ganho > melhor[0]:
                        melhor = (ganho, i, j, preset, pct)
            if melhor is None:
                break
            _, i, j, preset, pct = melhor
            pares_escolhidos.append((i, j, preset, pct))
            pool.remove(i)
            pool.remove(j)
    combinados_idx = set()
    resultado = []
    for i, j, preset, pct in pares_escolhidos:
        a1, a2 = artigos[i], artigos[j]
        qtds = [a1.get("qtd"), a2.get("qtd")]
        resultado.append({
            "artigo": f'{a1["artigo"]} + {a2["artigo"]}',
            "comprimento_mm": a1["comprimento_mm"],
            "qtd": sum(q for q in qtds if q) if any(q for q in qtds) else None,
            "wip_esp_mm": a1["wip_esp_mm"],
            "wip_cmp_combinado": preset,
            "aproveitamento_combinado": pct,
        })
        combinados_idx.add(i)
        combinados_idx.add(j)
    for idx, a in enumerate(artigos):
        if idx not in combinados_idx:
            resultado.append(a)
    return resultado

_MISSAO_EXTRACAO_ARTIGOS_OF = """Extrais, do PDF de uma Ordem de Fabrico
(OF) de madeira serrada, a lista de artigos/produtos encomendados (cada
um com o seu comprimento e quantidade) e o tipo de madeira do toro desta
OF. Responde APENAS com um objeto JSON, sem mais nenhum texto antes ou
depois:
{"tipo_madeira": "MT" ou "IN", "artigos": [{"artigo": string, "comprimento_mm": number, "qtd": number ou null}]}
- "tipo_madeira": por OMISSÃO é sempre "MT" — só respondes "IN" se as
  OBSERVAÇÕES do documento disserem explicitamente que a madeira é "IN"
  (nunca adivinhes a partir de outro sinal, ex: nome do cliente ou do
  artigo; na dúvida, "MT").
- "artigo": a designação da coluna "Descrição" — e SÓ dessa coluna.
- "comprimento_mm": o comprimento dessa peça, SEMPRE convertido para
  milímetros (ex: "2,55 m" ou "255 cm" -> 2550; já em mm, usa tal e qual).
- "qtd": o valor da coluna "Qtd." (sempre em M3) desse artigo — ver a
  regra abaixo para a reconheceres corretamente. NUNCA o Nº de Lotes,
  mesmo que pareça estar "colado" à Qtd no texto extraído.

A tabela tem SEMPRE exatamente UM artigo por linha de produto, e cada
linha de produto tem SEMPRE esta estrutura, nesta ordem exata, depois de
extraído o texto do PDF:
1. Uma unidade ("M3") seguida logo de um número — isto é o Nº de LOTES,
   NUNCA a Qtd, descarta sempre este número para efeitos de "qtd".
2. A "Descrição" do artigo (nome + medida "CxLxE").
3. Às vezes, colada logo a seguir à Descrição, na mesma linha, uma
   SEGUNDA medida "CxLxE" — é o "Suporte", descarta sempre, nunca é um
   artigo novo.
4. No FIM dessa mesma linha (depois da Descrição, ou do Suporte quando
   colado), outro número — este sim é a "Qtd." (sempre em M3), o valor
   certo para o campo "qtd". A soma de todos estes números, de todas as
   linhas, bate sempre com "Quantidade Total ... M3" perto do fim do
   documento — usa isto para confirmares sempre que tiveres dúvida (a
   soma dos números do passo 1, Nº de Lotes, é que bate com "Total
   Lotes", NUNCA com "Quantidade Total").
5. Depois, uma ou mais linhas no formato "NxM (P)" (ex: "4x2 (8)") são
   "Fiadas", descarta sempre.
6. Às vezes uma linha só com um número entre parêntesis (ex: "(338)") —
   descarta sempre.
7. Por fim, uma medida solta "CxLxE" numa linha à parte (Suporte ou
   Travamentos, varia consoante o documento) — descarta sempre, nunca é
   um artigo novo, mesmo que pareça ter a forma de um artigo normal
   (medida "CxLxE" + número à frente).

Dois exemplos reais:

Exemplo 1 (OF.640 Interpall) — Suporte colado à Descrição:
"M327,00Régua 1450x95x22 800x95x22 37,42
4x2 (8)
36x12 (432)
17x2 (34)
1200x95x22"
-> UM só artigo: "artigo" = "Régua 1450x95x22", "qtd" = 37,42 (o número
no FIM da primeira linha — NUNCA o 27,00 logo a seguir ao "M3", que é o
Nº de Lotes). "800x95x22" (Suporte) e "1200x95x22" (Travamentos) são
sempre descartados, nunca artigos novos.

Exemplo 2 (OF.622 Green Box) — Suporte isolado no fim, depois das Fiadas:
"M328,00Barrote 720x52x52    17,18
1x2 (2)
   (338)
990x52x52
M314,00Barrote 990x52x52    11,78
1x2 (2)
   (338)
990x52x52
Obs.: Total Lotes  42,00
Quantidade Total 28,96 M3"
-> DOIS artigos: "Barrote 720x52x52" com "qtd" = 17,18, e "Barrote
990x52x52" com "qtd" = 11,78 (17,18 + 11,78 = 28,96, exatamente a
"Quantidade Total" no fim do documento — NUNCA uses 28,00/14,00, que são
os Nº de Lotes de cada linha, cuja soma dá "Total Lotes" 42,00, nunca a
Qtd). As medidas soltas "990x52x52" depois das Fiadas são sempre o
Suporte, descarta sempre, nunca um artigo novo.

Nunca inventes um artigo, um comprimento ou uma qtd que não estejam mesmo
no PDF. Se não conseguires identificar nenhum artigo com comprimento,
"artigos" fica uma lista vazia []."""

_RE_QUANTIDADE_TOTAL_PDF = re.compile(r"Quantidade\s*Total\s*([\d.,]+)\s*M3", re.IGNORECASE)

def _quantidade_total_do_pdf(texto_pdf: str):
    """O volume total (m³) desta OF, lido do campo "Quantidade Total ...
    M3" no canto inferior direito do PDF (ex: "Quantidade Total 28,96
    M3" -> 28.96) — pedido explícito do Rui, 2026-10-30: "sempre que for
    feito o calcular automaticamente tens de usar e atualizar o volume da
    encomenda total... tens de ir buscar ao documento o campo quantidade
    total". None se o PDF não tiver esse campo ou não for legível."""
    if not texto_pdf:
        return None
    m = _RE_QUANTIDADE_TOTAL_PDF.search(texto_pdf)
    if not m:
        return None
    try:
        valor = float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None
    return valor if valor > 0 else None

def _ler_pdf_of(card: dict):
    """Encontra e lê o PDF da OF anexado ao card do Basecamp (nome tipo
    "OF <número> <cliente>.pdf" — pedido explícito do Rui, 2026-10-23),
    primeiro na descrição do card e, se não encontrar lá, nos comentários
    (mesmo padrão de tools.documentos_empresa.procurar_anexo_em_
    comentarios: o PDF pode ter sido anexado a um comentário em vez da
    descrição). Devolve (texto_pdf, nome_ficheiro) ou (None, erro) se não
    encontrar nenhum PDF que pareça ser o da OF."""
    try:
        recording = basecamp.obter_recording(card["url_api"])
    except Exception as e:
        return None, f"não consegui aceder ao card desta OF no Basecamp: {e}"
    anexos = (recording.get("description_attachments") or []) + (recording.get("content_attachments") or [])
    padrao_of = re.compile(r"\bOF\b", re.IGNORECASE)
    candidatos = [a for a in anexos
                 if (a.get("filename") or "").lower().endswith(".pdf") and padrao_of.search(a.get("filename") or "")]
    if not candidatos and card.get("comments_url"):
        try:
            comentarios = basecamp.ler_comentarios(card["comments_url"])
        except Exception:
            comentarios = []
        for c in comentarios:
            for nome in (c.get("anexos") or []):
                if nome.lower().endswith(".pdf") and padrao_of.search(nome):
                    try:
                        c_completo = basecamp.obter_recording(c["url"])
                    except Exception:
                        continue
                    candidatos.extend(a for a in (c_completo.get("content_attachments") or [])
                                      if (a.get("filename") or "") == nome)
    if not candidatos:
        return None, "não encontrei nenhum PDF com \"OF\" no nome anexado a este card (nem na descrição, nem nos comentários)"
    anexo = candidatos[0]
    nome = anexo.get("filename")
    try:
        bruto = basecamp._get_bytes(anexo["download_url"])
        ctype = documentos_empresa._tipo_efetivo(anexo.get("content_type"), nome)
        texto = documentos_empresa._extrair_por_tipo(bruto, ctype)
    except Exception as e:
        return None, f"encontrei o ficheiro {nome!r} mas não consegui lê-lo: {e}"
    if not texto or not texto.strip():
        return None, f"o ficheiro {nome!r} não tem texto legível"
    return texto, nome

def _limpar_bloco_codigo(texto: str) -> str:
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else texto[3:]
        if texto.endswith("```"):
            texto = texto[:-3]
    return texto.strip()

_RE_TITULO_OF = re.compile(r"\bOF\b", re.IGNORECASE)

def _eh_quadradilho(titulo: str) -> bool:
    """Um "quadradilho" é uma OF sem número (título sem "OF", ex: "Girona
    — 2600MT", "Serraenvas — Quadradilho MT") — já era a regra usada para
    "em contínuo" por omissão (ver entradas_charriot_ecos_largos em
    db.py). Confirmado ao vivo pelo Rui, 2026-10-26: estas OFs NUNCA têm
    PDF anexado (não têm processo formal de encomenda com desenho
    técnico) — o Algoritmo de Comprimentos nunca se aplica, e nem vale a
    pena tentar (ler o Basecamp + chamar o modelo só para falhar sempre
    da mesma forma)."""
    return not _RE_TITULO_OF.search(titulo or "")

_RE_DIMENSAO_ARTIGO = re.compile(r"(\d+)(?:/\d+)?\s*[xX]\s*(\d+)\s*[xX]\s*(\d+)")

def _truncar_apos_primeira_dimensao(nome):
    """Rede de segurança determinística (pedido explícito do Rui,
    2026-10-30, confirmado ao vivo na OF.640 Interpall): a coluna
    "Suporte" do PDF cola-se ao texto da "Descrição" sem separador nenhum
    depois de extraído (ex: "Régua 1450x95x22 800x95x22" — a segunda
    medida "CxLxE" é sempre o Suporte, nunca parte do nome real). Mesmo
    que o modelo não siga à letra a instrução de ignorar isso (ver
    _MISSAO_EXTRACAO_ARTIGOS_OF), se o nome tiver mais do que uma medida
    "CxLxE", corta sempre logo a seguir à primeira. Devolve `nome` tal
    qual se só tiver uma medida (ou nenhuma)."""
    if not nome:
        return nome
    medidas = list(_RE_DIMENSAO_ARTIGO.finditer(nome))
    if len(medidas) < 2:
        return nome
    return nome[:medidas[0].end()].strip()

def _comprimento_do_nome_artigo(nome: str):
    """Extrai o comprimento diretamente do nome/descrição do artigo, pelo
    padrão "<comprimento>x<largura>x<espessura>" (ex: "Barrote 800x90x65"
    -> 800; "1200/1000X82X16" -> 1200, o primeiro de dois comprimentos
    alternativos). Bug real confirmado ao vivo (OF.629 Lampre,
    2026-10-26): o modelo, ao extrair o comprimento do texto do PDF,
    confundiu-se com o valor de volume (M3) de cada linha, bem mais perto
    do número na mesma linha do que a própria descrição do artigo (ex:
    linha "Barrote 2980x80x50 ... 8,67" extraída como comprimento_mm
    8670, quando o comprimento real, 2980, já estava sem ambiguidade
    nenhuma no próprio nome do artigo). Como este padrão aparece sempre
    que o nome do artigo já traz as 3 dimensões (confirmado em várias
    OFs reais), este valor substitui sempre o do modelo quando bate
    certo — nunca o contrário, por mais nenhuma razão para confiar mais
    num texto solto do PDF do que no nome do próprio artigo já
    identificado. None se o nome não tiver este padrão (PDF com formato
    diferente — aí usa-se o valor do modelo, sem alternativa melhor)."""
    if not nome:
        return None
    m = _RE_DIMENSAO_ARTIGO.search(nome)
    return float(m.group(1)) if m else None

def _todos_comprimentos_do_nome_artigo(nome: str) -> list:
    """Como _comprimento_do_nome_artigo, mas devolve TODOS os comprimentos
    "CxLxE" encontrados no nome, pela ordem em que aparecem — uma linha
    combinada de dois artigos (ver _juntar_pares_otimizados) tem sempre
    dois nomes unidos por " + ", logo dois comprimentos; uma linha normal
    tem só um (lista de 1 elemento), ou nenhum se o nome não tiver o
    padrão. Usado para o aproveitamento ao vivo continuar certo mesmo
    numa linha combinada (ver _linhas_com_calculo/_aproveitamento_pct_multi)."""
    if not nome:
        return []
    return [float(m.group(1)) for m in _RE_DIMENSAO_ARTIGO.finditer(nome)]

def _wip_esp_do_nome_artigo(nome: str):
    """O valor do campo "WIP Esp" vem da LARGURA (L), não da espessura
    (E), do nome/descrição do artigo — mesmo padrão "CxLxE" usado em
    _comprimento_do_nome_artigo (ex: "Régua 1450x95x22" -> 95, não 22).
    Pedido explícito do Rui, 2026-10-30, correção de um erro real: a
    primeira versão disto lia o "E" (espessura) do nome, mas a regra
    certa é "a espessura vai ser colocada automaticamente... vai ser
    preenchida pela largura que está no artigo... vais ter de ir buscar
    o L e preencher tal e qual nesse campo" — o campo "WIP Esp" já não
    precisa de bater com nenhuma linha da tabela "WIP Lotes" (esse valor
    pode ser qualquer um, tal como veio do nome); a tabela só entra
    depois, ao calcular as Fiadas, pela fiada mais próxima quando não
    bate exatamente (ver _fiadas_mais_proxima). None se o nome não tiver
    este padrão."""
    if not nome:
        return None
    m = _RE_DIMENSAO_ARTIGO.search(nome)
    return float(m.group(2)) if m else None

def _extrair_artigos_of(texto_pdf: str) -> tuple:
    """Chama o modelo para identificar os artigos (comprimento + qtd) e o
    tipo de madeira (ver _MISSAO_EXTRACAO_ARTIGOS_OF) no texto do PDF da
    OF. Devolve sempre (tipo_madeira, artigos) — tipo_madeira é sempre
    "MT" ou "IN" (nunca None, "MT" por omissão se a extração falhar ou
    não devolver JSON válido), artigos é sempre uma lista (vazia nesses
    casos), nunca inventa dados. O comprimento do modelo é sempre
    substituído pelo lido diretamente do nome do artigo quando possível
    (ver _comprimento_do_nome_artigo) — mais fiável do que confiar no
    número que o modelo escolher no texto do PDF. O valor do WIP Esp
    ("wip_esp_mm", pedido explícito do Rui, 2026-10-30) vem sempre da
    LARGURA (L) do mesmo padrão no nome do artigo (ver
    _wip_esp_do_nome_artigo) — o modelo nunca é chamado para a extrair."""
    resposta = _client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=2048,
        system=_MISSAO_EXTRACAO_ARTIGOS_OF,
        messages=[{"role": "user", "content": texto_pdf[:30000]}]
    )
    texto = _limpar_bloco_codigo("".join(b.text for b in resposta.content if b.type == "text").strip())
    try:
        resultado = json.loads(texto)
    except ValueError:
        print(f"[planeamento_entradas] extração de artigos não devolveu JSON válido: {texto[:200]!r}")
        return "MT", []
    if not isinstance(resultado, dict):
        return "MT", []
    tipo_madeira = "IN" if resultado.get("tipo_madeira") == "IN" else "MT"
    artigos = resultado.get("artigos")
    if not isinstance(artigos, list):
        return tipo_madeira, []
    validos = []
    for a in artigos:
        if not isinstance(a, dict):
            continue
        nome_artigo = _truncar_apos_primeira_dimensao(a.get("artigo") or None)
        comprimento = _comprimento_do_nome_artigo(nome_artigo)
        if comprimento is None:
            try:
                comprimento = float(a["comprimento_mm"]) if a.get("comprimento_mm") else None
            except (TypeError, ValueError):
                comprimento = None
        if not comprimento or comprimento <= 0:
            continue
        try:
            qtd = float(a["qtd"]) if a.get("qtd") is not None else None
        except (TypeError, ValueError):
            qtd = None
        validos.append({"artigo": nome_artigo, "comprimento_mm": comprimento, "qtd": qtd,
                        "wip_esp_mm": _wip_esp_do_nome_artigo(nome_artigo)})
    return tipo_madeira, validos

def calcular_linhas_automaticamente(basecamp_card_id: int) -> dict:
    """"Algoritmo de Comprimentos" (pedido explícito do Rui, 2026-10-23):
    lê o PDF da OF, identifica os artigos/comprimentos, otimiza o
    comprimento de toro de cada um, e substitui TODAS as linhas de
    WIP+Toro desta OF pelo resultado — nunca junta às que já lá estavam
    (pedido explícito do Rui: "substitui sempre pelo resultado do
    cálculo"). O "peso" de cada linha (campo QTD do PDF, já em m³ por
    artigo) fica gravado tal como lido; é usado diretamente como o volume
    dessa linha só calculado ao vivo, nunca aqui (ver
    estado_planeamento_entradas/_linhas_com_calculo, a única fonte desse
    cálculo). Índices (WIP/Toro) assumem sempre os valores por omissão
    (INDICE_WIP_DEFAULT/INDICE_TOROS_DEFAULT) — não há nenhum índice
    "correto" por artigo a ler de lado nenhum. WIP Esp vem da LARGURA (L)
    do mesmo nome do artigo (ver _wip_esp_do_nome_artigo, pedido
    explícito do Rui, 2026-10-30) — None se o nome não tiver o padrão
    "CxLxE", fica então por preencher à mão como antes. Toro Tipo
    (pedido explícito do Rui, 2026-10-30) vem das observações do PDF (ver
    _MISSAO_EXTRACAO_ARTIGOS_OF): "MT" por omissão, só "IN" se o
    documento o disser explicitamente — é uma propriedade da OF toda,
    por isso o mesmo valor aplica-se a todas as linhas. O volume total da
    OF (usado na logística/produção para calcular a duração) é sempre
    retificado pelo campo "Quantidade Total ... M3" do PDF (ver
    _quantidade_total_do_pdf, pedido explícito do Rui, 2026-10-30) — nunca
    deixado com um valor manual desatualizado. Se a OF já estiver
    agendada (linha/dia já atribuídos), usa-se ps.agendar (recalcula
    também a duração a partir do novo volume); senão só ps.definir_volume
    (ainda na fila, sem duração a recalcular). Uma falha aqui (ex: nova
    duração não cabe na linha) fica só reportada em "aviso_volume", nunca
    impede o cálculo das linhas WIP+Toro em si. Antes de criar as linhas,
    tenta sempre juntar pares de artigos que aproveitem melhor cortados
    juntos (ver _juntar_pares_otimizados, pedido explícito do Rui,
    2026-10-30) — nesse caso o "artigo" da linha mostra os dois nomes
    ("A + B") e o "peso"/qtd já vem somado."""
    card = next((c for c in ps._cards_of_ativos() if c["id"] == basecamp_card_id), None)
    if not card:
        return {"erro": "card desta OF não encontrado entre as OFs ativas"}
    if _eh_quadradilho(card["titulo"]):
        return {"erro": "esta OF é \"quadradilho\" (título sem número) — nunca tem PDF anexado, "
                        "o Algoritmo de Comprimentos não se aplica aqui"}

    texto_pdf, nome_ou_erro = _ler_pdf_of(card)
    if texto_pdf is None:
        return {"erro": nome_ou_erro}
    tipo_madeira, artigos = _extrair_artigos_of(texto_pdf)
    if not artigos:
        return {"erro": f"li o PDF ({nome_ou_erro}) mas não identifiquei nenhum artigo com comprimento"}
    artigos = _juntar_pares_otimizados(artigos)

    linhas = []
    for a in artigos:
        if "wip_cmp_combinado" in a:
            wip_cmp_texto = a["wip_cmp_combinado"]
        else:
            wip_cmp_texto = _otimizar_wip_cmp(a["comprimento_mm"])
        toro_cmp = _toro_cmp_de_wip(wip_cmp_texto)
        linhas.append({
            "artigo": a["artigo"],
            "peso": a["qtd"],
            "wip_cmp": float(wip_cmp_texto) if wip_cmp_texto is not None else None,
            "wip_esp": a["wip_esp_mm"],
            "indice_wip": INDICE_WIP_DEFAULT,
            "toro_cmp": toro_cmp,
            "toro_tipo": tipo_madeira,
            "indice_toros": INDICE_TOROS_DEFAULT,
            "comprimento_artigo_mm": a["comprimento_mm"],
        })
    db.substituir_linhas_entrada_ecos_largos(basecamp_card_id, linhas)

    aviso_volume = None
    volume_total_pdf = _quantidade_total_do_pdf(texto_pdf)
    if volume_total_pdf is not None:
        existente = db.agendamento_producao(basecamp_card_id)
        if existente and existente.get("linha") and existente.get("dia_inicio"):
            resultado_volume = ps.agendar(basecamp_card_id, existente["linha"], existente["dia_inicio"],
                                          volume_m3=volume_total_pdf)
        else:
            resultado_volume = ps.definir_volume(basecamp_card_id, volume_total_pdf)
        if resultado_volume.get("erro"):
            aviso_volume = (f"linhas calculadas, mas não consegui atualizar o volume total para "
                           f"{volume_total_pdf} m³ (lido do PDF): {resultado_volume['erro']}")

    resposta = {"ok": True, "ficheiro": nome_ou_erro, "linhas": linhas, "volume_m3": volume_total_pdf}
    if aviso_volume:
        resposta["aviso_volume"] = aviso_volume
    return resposta

def _calcular_automaticamente_em_fundo(basecamp_card_id: int):
    """Corre calcular_linhas_automaticamente numa thread (chamado ao
    atribuir o primeiro charriot — ver atribuir_charriots) para nunca
    atrasar o arrastar do card na tabela à espera de ler um PDF e chamar o
    modelo; qualquer erro fica só nos logs, nunca interrompe quem
    arrastou o card."""
    try:
        resultado = calcular_linhas_automaticamente(basecamp_card_id)
        if "erro" in resultado:
            print(f"[planeamento_entradas] cálculo automático de comprimentos falhou para "
                 f"a OF {basecamp_card_id}: {resultado['erro']}")
        else:
            print(f"[planeamento_entradas] cálculo automático de comprimentos para a OF "
                 f"{basecamp_card_id}: {len(resultado['linhas'])} linha(s) a partir de {resultado['ficheiro']!r}")
    except Exception as e:
        print(f"[planeamento_entradas] cálculo automático de comprimentos rebentou para a OF "
             f"{basecamp_card_id}: {e!r}")

def _volume_real_lote(wip_cmp_mm, wip_esp_mm):
    """Volume real de um lote WIP, em m³: comprimento x (largura fixa x
    0,7) x nº de fiadas x espessura (cmp/lar/esp em mm, convertidos para
    metros aqui) — fiadas vêm sempre da tabela "WIP Lotes", pela
    espessura mais próxima quando não bate exatamente (ver
    _fiadas_mais_proxima). None sempre que faltar algum dos dados."""
    if wip_cmp_mm is None or wip_esp_mm is None:
        return None
    fiadas = _fiadas_mais_proxima(wip_esp_mm)
    if fiadas is None:
        return None
    volume = (float(wip_cmp_mm) / 1000) * ((WIP_LAR_FIXA / 1000) * 0.7) * fiadas * (float(wip_esp_mm) / 1000)
    return volume if volume > 0 else None

def _linhas_com_calculo(linhas: list, volume_total_m3) -> list:
    """Para cada linha de WIP+Toro de uma OF real, usa diretamente o "peso"
    dessa linha (campo "QTD" lido do PDF da OF, por artigo) como o volume
    em m³ dessa linha — pedido explícito do Rui, 2026-10-30: "para os
    cálculos da wip, por artigo, tens de ir buscar os m3 à coluna qtd no
    documento pdf, referente a cada artigo. e não à qtd total" (o PDF já
    traz o volume de cada artigo, ex: "M327,00Régua..." -> 27,00 m³ dessa
    Régua — nunca é só uma proporção do volume total da OF, como se fazia
    antes). Só quando uma linha não tem "peso" nenhum (ex: adicionada à
    mão, sem PDF) é que se cai para um valor de recurso: parte igual
    (1/nº de linhas) do volume_total_m3 da OF, a única informação
    disponível nesse caso. Calcula também QTD Toros/Nº de lotes dessa
    linha a partir desse volume. Devolve uma lista pronta a mostrar/editar,
    nunca grava nada."""
    if not linhas:
        return []
    n = len(linhas)
    resultado = []
    for l in linhas:
        if l.get("peso"):
            volume_linha = round(float(l["peso"]), 3)
        else:
            volume_linha = round((volume_total_m3 or 0) / n, 3) if volume_total_m3 is not None else None
        indice_wip = l["indice_wip"] if l["indice_wip"] is not None else INDICE_WIP_DEFAULT
        indice_toros = l["indice_toros"] if l["indice_toros"] is not None else INDICE_TOROS_DEFAULT
        qtd_wip_m3 = round(volume_linha * indice_wip, 2) if volume_linha is not None else None
        qtd_toros_m3 = round(volume_linha * indice_toros, 2) if volume_linha is not None else None
        try:
            toro_cmp_mm = float(l["toro_cmp"]) if l.get("toro_cmp") else None
        except (TypeError, ValueError):
            toro_cmp_mm = None  # "outro" com texto não numérico — sem % para mostrar
        # pedido explícito do Rui, 2026-10-30: "o desperdício tem de
        # aparecer, mesmo sem o cálculo automático" — uma linha criada ou
        # editada à mão não tem comprimento_artigo_mm guardado (nunca leu
        # nenhum PDF), mas se o nome do artigo tiver o padrão "CxLxE"
        # (ex: escrito à mão "Barrote 990x52x52"), o comprimento lê-se
        # dali na mesma, tal como já acontece no cálculo automático (ver
        # _comprimento_do_nome_artigo) — só cai para o valor guardado
        # quando o nome não tiver esse padrão. Uma linha combinada de dois
        # artigos (ver _juntar_pares_otimizados) tem DOIS comprimentos no
        # nome ("A + B") — _todos_comprimentos_do_nome_artigo apanha-os
        # todos, para o aproveitamento continuar certo também aí.
        comprimentos_do_nome = _todos_comprimentos_do_nome_artigo(l["artigo"])
        comprimento_artigo_mm = comprimentos_do_nome[0] if comprimentos_do_nome else l.get("comprimento_artigo_mm")
        comprimentos_para_aproveitamento = comprimentos_do_nome or (
            [l["comprimento_artigo_mm"]] if l.get("comprimento_artigo_mm") else [])
        resultado.append({
            "id": l["id"],
            "artigo": l["artigo"],
            "peso": l["peso"],
            "wip_cmp": l["wip_cmp"],
            "wip_esp": l["wip_esp"],
            "indice_wip": indice_wip,
            "qtd_wip_m3": qtd_wip_m3,
            "n_lotes": _calcular_n_lotes(qtd_wip_m3, l["wip_cmp"], l["wip_esp"]),
            "toro_cmp": l["toro_cmp"],
            "toro_tipo": l["toro_tipo"],
            "indice_toros": indice_toros,
            "qtd_toros_m3": qtd_toros_m3,
            "comprimento_artigo_mm": comprimento_artigo_mm,
            "aproveitamento_pct": _aproveitamento_pct_multi(comprimentos_para_aproveitamento, toro_cmp_mm),
        })
    return resultado

def _calcular_n_lotes(qtd_wip_m3, wip_cmp_mm, wip_esp_mm):
    """Nº de lotes (pedido explícito do Rui, 2026-10-04) = QTD Wip a
    dividir pelo volume real de um lote (ver _volume_real_lote). None
    sempre que faltar algum dos dados."""
    volume_lote = _volume_real_lote(wip_cmp_mm, wip_esp_mm)
    if qtd_wip_m3 is None or volume_lote is None:
        return None
    return round(qtd_wip_m3 / volume_lote)

def _calcular_wip_e_toros_de_lotes(n_lotes, wip_cmp_mm, wip_esp_mm, indice_wip, indice_toros):
    """Inverso de _calcular_n_lotes — usado pelo card "Buffer" (pedido
    explícito do Rui, 2026-10-04): a partir de um Nº de lotes escrito à
    mão, calcula QTD Wip (m³) = nº de lotes x volume real de um lote, e
    QTD Toros (m³) a partir de um volume "implícito" (QTD Wip ÷
    indice_wip) vezes indice_toros — a mesma relação volume→QTD Wip/QTD
    Toros de uma OF normal, só a trabalhar ao contrário a partir do Nº de
    lotes em vez do volume real da OF. Devolve (qtd_wip_m3, qtd_toros_m3),
    cada um None sempre que faltar algum dos dados necessários."""
    volume_lote = _volume_real_lote(wip_cmp_mm, wip_esp_mm)
    if n_lotes is None or volume_lote is None:
        return None, None
    qtd_wip_m3 = round(n_lotes * volume_lote, 2)
    qtd_toros_m3 = None
    if indice_wip:
        volume_implicito = qtd_wip_m3 / indice_wip
        qtd_toros_m3 = round(volume_implicito * indice_toros, 2) if indice_toros else None
    return qtd_wip_m3, qtd_toros_m3

def _calcular_dia_entrada(dia_inicio: str, em_continuo: bool) -> str:
    """Dia em que os troncos desta OF entram no charriot.

    "Em contínuo" (pedido explícito do Rui, 2026-10-01; por omissão
    sempre ligado para qualquer OF, pedido explícito do Rui, 2026-10-06 —
    ver estado_planeamento_entradas): a OF entra no MESMO dia do início da
    produção — salta o cálculo habitual (dia anterior). Qualquer OF pode
    ser desmarcada à mão quando não for esse o caso (ver
    definir_em_continuo), e essa escolha fica gravada em definitivo.

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
    linhas_por_card = {}
    for l in db.linhas_entrada_ecos_largos():
        linhas_por_card.setdefault(l["basecamp_card_id"], []).append(l)

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
        # por omissão toda a gente entra "em contínuo" (pedido explícito
        # do Rui, 2026-10-06) — só deixa de ser assim quando alguém
        # desmarca à mão para uma OF em concreto (ver definir_em_continuo),
        # o que fica gravado em definitivo (extra["em_continuo"] passa a
        # False, nunca mais None).
        em_continuo = extra.get("em_continuo")
        if em_continuo is None:
            em_continuo = True
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
            "dia_entrada": extra.get("dia_entrada_manual") or _calcular_dia_entrada(agendamento["dia_inicio"], em_continuo),
            "em_continuo": em_continuo,
            "charriots": extra.get("charriots") or [],
            "largura_dias": extra.get("largura_dias_visual") or 1,
            "produzido": bool(extra.get("produzido")),
            "wip_lar": WIP_LAR_FIXA,
            "linhas": _linhas_com_calculo(linhas_por_card.get(c["id"], []), volume),
        })

    # cards "Fazer Buffer" (pedido explícito do Rui, 2026-10-04): duplicados
    # independentes de um card real, com o Nº de lotes escrito à mão em vez
    # de calculado a partir do volume da OF — ver criar_buffer/guardar_buffer
    # e db.buffers_entrada_ecos_largos. Juntam-se aqui ao mesmo quadro, com
    # basecamp_card_id negativo (nunca colide com um id real) e "buffer":
    # True, para o frontend saber a distinguir (título a verde escuro).
    cards_por_id = {c["id"]: c for c in cards_ativos}
    for b in db.buffers_entrada_ecos_largos():
        origem = cards_por_id.get(b["basecamp_card_id_origem"])
        indice_wip = b["indice_wip"] if b["indice_wip"] is not None else INDICE_WIP_DEFAULT
        indice_toros = b["indice_toros"] if b["indice_toros"] is not None else INDICE_TOROS_DEFAULT
        qtd_wip_m3, qtd_toros_m3 = _calcular_wip_e_toros_de_lotes(
            b["n_lotes"], b["wip_cmp"], b["wip_esp"], indice_wip, indice_toros)
        entradas.append({
            "basecamp_card_id": -b["id"],
            "titulo": b["titulo"],
            "url": origem["url"] if origem else None,
            "coluna_basecamp": origem["estado"] if origem else None,
            "linha": b["linha"],
            "dia_inicio_producao": None,
            "volume_m3": None,
            "tipo_madeira": None,
            "cor": b["cor"],
            "dia_entrada": b["dia_entrada"],
            "em_continuo": None,
            "charriots": b["charriots"],
            "largura_dias": b["largura_dias_visual"] or 1,
            "produzido": b["produzido"],
            "wip_cmp": b["wip_cmp"],
            "wip_lar": WIP_LAR_FIXA,
            "wip_esp": b["wip_esp"],
            "indice_wip": indice_wip,
            "qtd_wip_m3": qtd_wip_m3,
            "n_lotes": b["n_lotes"],
            "toro_cmp": b["toro_cmp"],
            "toro_tipo": b["toro_tipo"],
            "indice_toros": indice_toros,
            "qtd_toros_m3": qtd_toros_m3,
            "buffer": True,
        })

    return {"charriots": CHARRIOTS, "entradas": entradas, "historico": db.historico_valores_entradas(),
            "feriados": db.feriados_ecos_largos()}

def procura_toros_por_artigo(dias: int) -> dict:
    """Soma, em kg (1 m³ = 1 tonelada, mesma conversão usada em todo o
    lado — ver agents.relatorio_aprovisionamento_toros.KG_POR_M3), a
    procura de toros das OFs/buffers deste quadro cujo dia de entrada
    caia nos próximos `dias` dias (hoje incluído, entradas já passadas
    ficam de fora) — por (tipo, comprimento_m). Pedido explícito do Rui,
    2026-10-29, para calcular os Limites do Park In a partir da procura
    real (ver tools.parkin.limites_propostos_por_procura) — nunca por
    espessura, que este quadro não regista."""
    hoje = date.today()
    fim = hoje + timedelta(days=dias)
    procura = {}

    def _somar(toro_tipo, toro_cmp_mm, qtd_toros_m3):
        if not toro_tipo or not toro_cmp_mm or not qtd_toros_m3:
            return
        try:
            comprimento_m = round(float(toro_cmp_mm) / 1000, 2)
        except (TypeError, ValueError):
            return  # preset não numérico (ex: "outro" por definir) — não entra nesta conta
        chave = (toro_tipo, comprimento_m)
        procura[chave] = procura.get(chave, 0.0) + float(qtd_toros_m3) * 1000

    for e in estado_planeamento_entradas()["entradas"]:
        dia_entrada = e.get("dia_entrada")
        if not dia_entrada:
            continue
        try:
            d = date.fromisoformat(dia_entrada)
        except ValueError:
            continue
        if not (hoje <= d <= fim):
            continue
        if e.get("buffer"):
            _somar(e.get("toro_tipo"), e.get("toro_cmp"), e.get("qtd_toros_m3"))
        else:
            for l in e.get("linhas") or []:
                _somar(l.get("toro_tipo"), l.get("toro_cmp"), l.get("qtd_toros_m3"))
    return procura

def atribuir_charriots(basecamp_card_id: int, charriots: list = None) -> dict:
    """Atribui (substitui) os charriots de uma OF — pode estar em vários ao
    mesmo tempo (ex: Charriot 1 e 2 em simultâneo, pedido explícito do
    Rui, 2026-10-02), ou nenhum (None/lista vazia volta para "por
    atribuir"). basecamp_card_id negativo = card "Buffer" (ver
    criar_buffer) — mesmo gesto de arrastar, só muda a tabela de destino.

    Ao sair de "por atribuir" pela primeira vez (nenhum charriot antes,
    pelo menos um agora) numa OF real, dispara em segundo plano o
    "Algoritmo de Comprimentos" (pedido explícito do Rui, 2026-10-23: "assim
    que o card é arrastado do por atribuir para os charriots/locais
    corretos") — nunca ao mudar de um charriot já atribuído para outro,
    só nesta transição específica."""
    charriots = [c for c in (charriots or []) if c] or None
    if charriots is not None:
        desconhecidos = [c for c in charriots if c not in CHARRIOTS]
        if desconhecidos:
            return {"erro": f"charriot desconhecido: {desconhecidos[0]!r}"}
    if basecamp_card_id < 0:
        return db.atribuir_charriots_buffer(-basecamp_card_id, charriots)
    tinha_charriots_antes = bool(next(
        (e["charriots"] for e in db.entradas_charriot_ecos_largos() if e["basecamp_card_id"] == basecamp_card_id),
        None))
    resultado = db.atribuir_charriots_entrada(basecamp_card_id, charriots)
    if not tinha_charriots_antes and charriots:
        threading.Thread(target=_calcular_automaticamente_em_fundo, args=(basecamp_card_id,), daemon=True).start()
    return resultado

LARGURA_DIAS_MAX = 14

def definir_largura_dias(basecamp_card_id: int, largura_dias) -> dict:
    """Só visual (pedido explícito do Rui, 2026-10-02): quantos dias um
    card ocupa na tabela, ao ser alargado pela borda direita — nunca mexe
    no dia real usado nos cálculos (ver _calcular_dia_entrada). None/1
    volta ao tamanho normal (um dia). basecamp_card_id negativo = card
    "Buffer" (ver criar_buffer)."""
    if largura_dias is None:
        largura_dias = 1
    try:
        largura_dias = int(largura_dias)
    except (TypeError, ValueError):
        return {"erro": "largura em dias inválida"}
    if largura_dias < 1 or largura_dias > LARGURA_DIAS_MAX:
        return {"erro": f"largura em dias tem de estar entre 1 e {LARGURA_DIAS_MAX}"}
    largura_dias = largura_dias if largura_dias != 1 else None
    if basecamp_card_id < 0:
        return db.definir_largura_dias_buffer(-basecamp_card_id, largura_dias)
    return db.definir_largura_dias_entrada(basecamp_card_id, largura_dias)

def definir_produzido(basecamp_card_id: int, produzido: bool) -> dict:
    """Marca/desmarca "Produzido" (pedido explícito do Rui, 2026-10-03) —
    só um marcador visual à mão (muda o fundo do card para verde), sem
    ligação ao estado real da OF no Basecamp nem a nenhum cálculo desta
    página. basecamp_card_id negativo = card "Buffer" (ver criar_buffer)."""
    if basecamp_card_id < 0:
        return db.definir_produzido_buffer(-basecamp_card_id, bool(produzido))
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

def _validar_linha(linha: dict):
    for nome, valor in (("comprimento (WIP)", linha.get("wip_cmp")), ("espessura (WIP)", linha.get("wip_esp")),
                       ("índice de WIP", linha.get("indice_wip")), ("índice de toros", linha.get("indice_toros"))):
        erro = _validar_numero_positivo(nome, valor)
        if erro:
            return erro
    # pedido explícito do Rui, 2026-10-30: o WIP Esp já não precisa de
    # bater exatamente com uma linha da tabela "WIP Lotes" (agora vem da
    # largura do artigo, não da espessura) — as Fiadas assumem sempre a
    # mais próxima (ver _fiadas_mais_proxima), nunca bloqueiam a gravação.
    toro_cmp = linha.get("toro_cmp")
    if toro_cmp is not None and not str(toro_cmp).strip():
        return "comprimento do toro inválido"
    toro_tipo = linha.get("toro_tipo")
    if toro_tipo is not None and toro_tipo not in TORO_TIPOS:
        return f"tipo de toro desconhecido: {toro_tipo!r} — usa \"IN\" ou \"MT\""
    return None

def guardar_linhas_entrada(basecamp_card_id: int, linhas: list) -> dict:
    """Grava a ficha completa de linhas de WIP+Toro de uma OF real (pedido
    explícito do Rui, 2026-10-23: "permite a sua edição e alteração") —
    substitui sempre todas as linhas desta OF pelas dadas, a mesma função
    usada pelo cálculo automático (ver calcular_linhas_automaticamente),
    para nunca haver dois caminhos diferentes a escrever a mesma tabela.
    `linhas`: lista de {"artigo","peso","wip_cmp","wip_esp","indice_wip",
    "toro_cmp","toro_tipo","indice_toros"}, todos opcionais."""
    if basecamp_card_id < 0:
        return {"erro": "um card \"Buffer\" não tem linhas de WIP+Toro — usa guardar_buffer"}
    for linha in linhas or []:
        erro = _validar_linha(linha)
        if erro:
            return {"erro": erro}
    db.substituir_linhas_entrada_ecos_largos(basecamp_card_id, linhas or [])
    return {"ok": True, "basecamp_card_id": basecamp_card_id}

def criar_buffer(basecamp_card_id_origem: int) -> dict:
    """Botão "Fazer Buffer" (pedido explícito do Rui, 2026-10-04): duplica
    um card real num novo card independente, copiando os valores atuais
    (charriots, dia, WIP, Toro, cor, título) e arrancando com o Nº de
    lotes já calculado nesse card (ou 1, se ainda não houver nenhum) — a
    partir daqui, o Nº de lotes do duplicado escreve-se à mão (ver
    guardar_buffer), e QTD Wip/QTD Toros recalculam-se sempre a partir
    dele. Um "Buffer" só tem um único WIP+Toro (nunca várias linhas, ao
    contrário de uma OF real — ver guardar_linhas_entrada); se a OF de
    origem já tiver mais de uma linha, copia-se só a primeira. Devolve o
    novo card já no formato de estado_planeamento_entradas
    (basecamp_card_id negativo, "buffer": True)."""
    estado = estado_planeamento_entradas()
    origem = next((e for e in estado["entradas"] if e["basecamp_card_id"] == basecamp_card_id_origem), None)
    if origem is None:
        return {"erro": "card de origem não encontrado"}
    primeira_linha = (origem.get("linhas") or [{}])[0]
    novo_id = db.criar_buffer_entrada(
        basecamp_card_id_origem=basecamp_card_id_origem,
        titulo=origem["titulo"] + " (Buffer)",
        linha=origem["linha"], cor=origem["cor"], dia_entrada=origem["dia_entrada"],
        charriots=origem["charriots"], largura_dias_visual=origem["largura_dias"],
        wip_cmp=primeira_linha.get("wip_cmp"), wip_esp=primeira_linha.get("wip_esp"),
        indice_wip=primeira_linha.get("indice_wip"),
        n_lotes=primeira_linha.get("n_lotes") or 1, toro_cmp=primeira_linha.get("toro_cmp"),
        toro_tipo=primeira_linha.get("toro_tipo"), indice_toros=primeira_linha.get("indice_toros"))
    novo_estado = estado_planeamento_entradas()
    return next(e for e in novo_estado["entradas"] if e["basecamp_card_id"] == -novo_id)

def guardar_buffer(id: int, charriots: list = None, largura_dias=None, wip_cmp: float = None,
                   wip_esp: float = None, indice_wip: float = None, n_lotes: float = None,
                   toro_cmp: str = None, toro_tipo: str = None, indice_toros: float = None) -> dict:
    """Grava a ficha completa de um card "Buffer" já criado (ver
    criar_buffer) — ao contrário de guardar_linhas_entrada, grava tudo de
    uma vez (um único WIP+Toro, nunca várias linhas), porque é um card
    próprio (não lê nada de uma OF real, não tem PDF nenhum para o
    "Algoritmo de Comprimentos" ler)."""
    charriots = [c for c in (charriots or []) if c] or None
    if charriots is not None:
        desconhecidos = [c for c in charriots if c not in CHARRIOTS]
        if desconhecidos:
            return {"erro": f"charriot desconhecido: {desconhecidos[0]!r}"}
    if largura_dias is None:
        largura_dias = 1
    try:
        largura_dias = int(largura_dias)
    except (TypeError, ValueError):
        return {"erro": "largura em dias inválida"}
    if largura_dias < 1 or largura_dias > LARGURA_DIAS_MAX:
        return {"erro": f"largura em dias tem de estar entre 1 e {LARGURA_DIAS_MAX}"}
    for nome, valor in (("comprimento (WIP)", wip_cmp), ("espessura (WIP)", wip_esp),
                       ("índice de WIP", indice_wip), ("Nº de lotes", n_lotes),
                       ("índice de toros", indice_toros)):
        erro = _validar_numero_positivo(nome, valor)
        if erro:
            return {"erro": erro}
    if toro_cmp is not None and not str(toro_cmp).strip():
        return {"erro": "comprimento do toro inválido"}
    if toro_tipo is not None and toro_tipo not in TORO_TIPOS:
        return {"erro": f"tipo de toro desconhecido: {toro_tipo!r} — usa \"IN\" ou \"MT\""}
    return db.guardar_buffer_entrada(id, charriots, largura_dias if largura_dias != 1 else None,
                                     wip_cmp, wip_esp, indice_wip, n_lotes, toro_cmp, toro_tipo, indice_toros)

def apagar_buffer(id: int) -> dict:
    """Apaga um card "Buffer" (ver criar_buffer) — nunca afeta a OF real de
    onde foi duplicado."""
    return db.apagar_buffer_entrada(id)

def definir_dia_entrada(basecamp_card_id: int, dia_entrada: str = None) -> dict:
    """Grava um dia de entrada à mão (pedido explícito do Rui, 2026-10-04:
    deixar arrastar um card na horizontal para outra coluna de dia, com
    confirmação no cliente antes de gravar) — passa a ter prioridade sobre
    o calculado a partir do início da produção (ver _calcular_dia_entrada).
    `dia_entrada` None volta ao cálculo automático. basecamp_card_id
    negativo = card "Buffer" (ver criar_buffer) — aí é o único dia
    guardado (não há nenhum cálculo automático de reserva)."""
    if dia_entrada is not None:
        try:
            date.fromisoformat(dia_entrada)
        except ValueError:
            return {"erro": "dia_entrada inválido — tem de ser AAAA-MM-DD"}
    if basecamp_card_id < 0:
        return db.definir_dia_entrada_buffer(-basecamp_card_id, dia_entrada)
    return db.definir_dia_entrada_manual(basecamp_card_id, dia_entrada)

def definir_em_continuo(basecamp_card_id: int, em_continuo: bool) -> dict:
    """Marca/desmarca "Em contínuo" (ver _calcular_dia_entrada) para uma OF
    em concreto — por omissão todas entram marcadas (ver
    estado_planeamento_entradas); isto só é preciso para desmarcar
    alguma, e fica gravado em definitivo."""
    return db.definir_em_continuo_entrada(basecamp_card_id, bool(em_continuo))

def pagina_planeamento_entradas() -> str:
    return _TEMPLATE

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-PT">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Planeamento Entrada — Ecos Largos</title>
<style>
  :root{
    --paper:#FFFFFF; --canvas:#F5F5F3; --raise:#FBFBF9;
    --ink:#1A1C1E; --dim:#75797D;
    --line:#E8E8E3; --edge:#D9D9D2;
    --blue:#1B6AC9; --gold:#E0A02C; --red:#C4452E; --grey:#9AA0A6; --hoje:#FFF6D6;
    --green:#2E9E4F; --green-bg:#E1F5E6; --feriado-bg:#FBDFDD;
    --day:92px; --lane:78px; --label:180px;
  }
  /* ecrã de telemóvel (pedido explícito do Rui, 2026-10-06): a coluna de
     etiquetas (180px) sozinha já não deixava espaço nenhum para os dias
     num ecrã estreito — ver também metrics() (JS), que baixa a largura
     mínima de cada dia só em ecrãs estreitos, para o "dense" (fonte/
     espaçamento mais pequenos, já existente) entrar em ação em vez de
     forçar sempre 84px por dia. */
  @media (max-width:640px){
    :root{ --label:68px; }
    .wrap{padding:14px 8px 100px}
    h1{font-size:21px}
    .lbl{padding:6px 5px}
    .lbl .n{font-size:11px;line-height:1.2}
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
  /* "Fazer Buffer" (pedido explícito do Rui, 2026-10-04): duplicado
     independente de um card, título com cor própria para se distinguir
     sempre à vista dos cards reais — esteve a verde escuro, mudado para
     azul royal (pedido explícito do Rui, 2026-10-07: "tem de ficar a azul
     royal para se notar diferença do preto" — o verde escuro, em cascata
     sobre outros cards, ficava perto demais do preto do texto normal). */
  .blk.buffer .ttText{color:#4169E1}
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
     fácil demais de carregar sem querer. Esteve a azul forte por um dia
     (2026-10-06) mas voltou a verde a pedido explícito do Rui, 2026-10-07. */
  .btn.verde{color:var(--green);border-color:var(--green)}
  .btn.verde.ativo{background:var(--green);color:#fff;font-weight:600}
  /* "Fazer Buffer" (pedido explícito do Rui, 2026-10-04) — mesmo verde
     escuro do título do card duplicado, para se perceber de imediato que
     é o botão que o cria. */
  .btn.buffer{color:#1B5E20;border-color:#1B5E20}
  .btn.buffer:hover{background:#1B5E20;color:#fff}
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
    <h1>Planeamento Entrada</h1>
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
/* limite mínimo de view.start que mantém a semana sempre alinhada a
   segunda-feira ao navegar para trás — MASTER pode começar a meio de uma
   semana (bug real, 2026-10-04: recuar demasiadas semanas desalinhava os
   dias mostrados, porque o limite antigo, 0, é só o primeiro dia de
   MASTER, não necessariamente uma segunda-feira). */
const INICIO_MIN_SEMANA=(()=>{ const s=segundaDe(0); return s>=0?s:s+7; })();

const $=s=>document.querySelector(s);
const CHARRIOTS=["Charriot 1","Charriot 2","Charriot 3","Multiserra de Toros","Aproveitamentos"];
const LANES=["Por atribuir",...CHARRIOTS];
const TORO_PRESETS=["2550","2640","3100","2350"];
const WIP_CMP_PRESETS=["2350","2550","2650","3100"];
const WIP_LAR_FIXA=1200;
const WIP_LOTES_TABELA=[[18,40],[22,35],[25,30],[32,25],[36,20],[42,16],
  [52,14],[60,12],[70,10],[80,8],[88,7],[90,7],[100,6],[107,6],[110,6],[120,5],[130,5],
  [140,4],[150,4],[160,4],[170,4],[180,4],[200,3]];
/* mesma regra de tools.planeamento_entradas._eh_quadradilho: uma OF sem
   número (título sem "OF") nunca tem PDF anexado. */
function ehQuadradilho(titulo){ return !/\bOF\b/i.test(titulo||""); }
/* pedido explícito do Rui, 2026-10-30: para um "quadradilho" (sem PDF
   para ler observações), o tipo de madeira do toro vem do próprio nome
   do card — "MT" por omissão, "IN" só se a palavra aparecer no título
   (mesma convenção já vista em títulos reais, ex: "Girona — 2600MT").
   Usado só para pré-preencher o campo "Tipo" ao adicionar uma linha à
   mão (ver "+ Adicionar linha") — nunca impede a correção manual
   depois. */
function tipoMadeiraDoTitulo(titulo){ return /\bIN\b/i.test(titulo||"") ? "IN" : "MT"; }
/* exata se bater com uma linha da tabela "WIP Lotes", senão a mais
   PRÓXIMA (mesma regra de tools.planeamento_entradas._fiadas_mais_
   proxima) — pedido explícito do Rui, 2026-10-30: desde que o WIP Esp
   passou a vir da largura do artigo, raramente bate em cheio. A meio
   caminho exato entre duas linhas da tabela, arredonda por EXCESSO —
   fica com a mais alta das duas ("arredondamento por excesso a partir
   de 5"). */
function fiadasPorEspessura(espMm){
  if(espMm==null || isNaN(espMm)) return null;
  const exata=WIP_LOTES_TABELA.find(([e])=>Math.round(e)===Math.round(espMm));
  if(exata) return exata[1];
  let melhor=null;
  for(const [e,f] of WIP_LOTES_TABELA){
    const dist=Math.abs(e-espMm);
    if(melhor==null || dist<melhor.dist || (dist===melhor.dist && e>melhor.e)) melhor={dist,f,e};
  }
  return melhor?melhor.f:null;
}
/* mesmo padrão de tools.planeamento_entradas._comprimento_do_nome_artigo
   — pedido explícito do Rui, 2026-10-30: "o desperdício tem de aparecer,
   mesmo sem o cálculo automático": uma linha criada/editada à mão não
   tem nenhum comprimento guardado de um PDF, mas se o nome do artigo
   escrito à mão tiver o padrão "CxLxE" (ex: "Barrote 990x52x52"), o
   comprimento lê-se dali na mesma. */
const RE_DIMENSAO_ARTIGO=/(\d+)(?:\/\d+)?\s*[xX]\s*\d+\s*[xX]\s*\d+/;
const RE_DIMENSAO_ARTIGO_G=/(\d+)(?:\/\d+)?\s*[xX]\s*\d+\s*[xX]\s*\d+/g;
function comprimentoDoNomeArtigo(nome){
  const m=RE_DIMENSAO_ARTIGO.exec(nome||"");
  return m ? parseFloat(m[1]) : null;
}
/* como comprimentoDoNomeArtigo, mas apanha TODOS os comprimentos do nome
   (ver tools.planeamento_entradas._todos_comprimentos_do_nome_artigo) —
   uma linha combinada de dois artigos (pedido explícito do Rui,
   2026-10-30) tem nome "Artigo A + Artigo B", logo dois comprimentos. */
function todosComprimentosDoNomeArtigo(nome){
  const resultado=[]; let m;
  RE_DIMENSAO_ARTIGO_G.lastIndex=0;
  while((m=RE_DIMENSAO_ARTIGO_G.exec(nome||""))) resultado.push(parseFloat(m[1]));
  return resultado;
}
/* % de aproveitamento de um WIP/Toro a cortar peças de uma lista de um ou
   mais comprimentos (ver
   tools.planeamento_entradas._aproveitamento_pct_multi, mesma fórmula —
   generalizada para linhas combinadas) — recalculado ao vivo aqui também,
   para continuar certo mesmo que o WIP/Toro Cmp ou o nome do artigo sejam
   corrigidos à mão depois do cálculo automático (pedido explícito do
   Rui, 2026-10-30). */
const TOLERANCIA_CORTE_MM=5;
function aproveitamentoPctMulti(comprimentosMm, presetMm){
  const validos=(comprimentosMm||[]).filter(c=>c!=null && !isNaN(c) && c>0);
  if(!validos.length || !presetMm || isNaN(presetMm)) return null;
  const unidades=validos.map(c=>c+TOLERANCIA_CORTE_MM);
  const maximos=unidades.map(u=>Math.floor(presetMm/u));
  if(maximos.some(m=>m<1)) return null;
  let melhorOcupado=0;
  const combos=(idx, atual)=>{
    if(idx===unidades.length){
      const ocupado=atual.reduce((s,n,i)=>s+n*unidades[i],0);
      if(ocupado<=presetMm && ocupado>melhorOcupado) melhorOcupado=ocupado;
      return;
    }
    for(let n=1;n<=maximos[idx];n++) combos(idx+1,[...atual,n]);
  };
  combos(0,[]);
  if(melhorOcupado<=0) return null;
  return Math.round(melhorOcupado/presetMm*1000)/10;
}
function volumeRealLote(wipCmpMm, wipEspMm){
  const fiadas=fiadasPorEspessura(wipEspMm);
  if(wipCmpMm==null || wipEspMm==null || fiadas==null) return null;
  const v=(wipCmpMm/1000)*((WIP_LAR_FIXA/1000)*0.7)*fiadas*(wipEspMm/1000);
  return v>0 ? v : null;
}
/* inverso de calcularNLotes — usado pelo card "Buffer" (pedido explícito
   do Rui, 2026-10-04): a partir de um Nº de lotes escrito à mão, calcula
   QTD Wip/QTD Toros (m³), tal como tools/planeamento_entradas.
   _calcular_wip_e_toros_de_lotes faz no servidor ao gravar. */
function calcularWipEToroDeLotes(nLotes, wipCmpMm, wipEspMm, indiceWip, indiceToros){
  const volumeLote=volumeRealLote(wipCmpMm, wipEspMm);
  if(nLotes==null || volumeLote==null) return [null,null];
  const qtdWip=Math.round(nLotes*volumeLote*100)/100;
  let qtdToros=null;
  if(indiceWip){
    const volumeImplicito=qtdWip/indiceWip;
    qtdToros=indiceToros ? Math.round(volumeImplicito*indiceToros*100)/100 : null;
  }
  return [qtdWip, qtdToros];
}
function calcularNLotes(qtdWipM3, wipCmpMm, wipEspMm){
  const volumeLote=volumeRealLote(wipCmpMm, wipEspMm);
  if(qtdWipM3==null || volumeLote==null) return null;
  return Math.round(qtdWipM3/volumeLote);
}
/* resumo de Toros/Nº lotes a mostrar na face do card (pedido explícito
   do Rui, 2026-10-23) — um Buffer continua a ter um único WIP+Toro
   (e.qtdToros/e.nLotes); uma OF real pode ter várias linhas (uma por
   artigo/comprimento, "Algoritmo de Comprimentos"), por isso soma-se
   aqui o QTD Toros/Nº de lotes de todas (null só se nenhuma linha tiver
   valor nenhum, nunca 0 por omissão). */
function resumoCardToros(e){
  if(e.buffer) return {qtdToros:e.qtdToros, nLotes:e.nLotes};
  const linhas=e.linhas||[];
  let qtdToros=null, nLotes=null;
  for(const l of linhas){
    if(l.qtdToros!=null) qtdToros=(qtdToros||0)+l.qtdToros;
    if(l.nLotes!=null) nLotes=(nLotes||0)+l.nLotes;
  }
  return {qtdToros: qtdToros!=null?Math.round(qtdToros*100)/100:null, nLotes};
}
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
let view={mode:"semana",start:Math.max(segundaDe(HOJE),INICIO_MIN_SEMANA),len:7};
let logs=[], DAY=92, LANE=78;

const item=id=>entradas.find(e=>e.id===id);
/* uma OF pode estar em vários charriots ao mesmo tempo (pedido explícito
   do Rui, 2026-10-02). Em vez de aparecer repetida num card por cada
   charriot (confuso — pedido explícito do Rui, 2026-10-04: "quero que o
   card fique um só pelos vários charriots, como fazemos horizontalmente"),
   desenha-se UM único card, esticado na vertical da lane de topo até à
   lane de fundo do intervalo (ver minMaxLane) — o mesmo espírito do
   alargar horizontal por dias (.rsz-h/larguraDias), agora na vertical
   (.rsz-v já arrastava uma pré-visualização exatamente assim; só faltava
   o resultado final, depois de largar, desenhar da mesma forma).
   lanesDe devolve todas as lanes onde uma OF está atribuída — [0]
   ("Por atribuir") quando ainda não tem nenhum charriot. */
const lanesDe=e=>{
  if(!e.charriots || !e.charriots.length) return [0];
  return e.charriots.map(c=>CHARRIOTS.indexOf(c)+1);
};
const minMaxLane=e=>{ const ls=lanesDe(e); return [Math.min(...ls),Math.max(...ls)]; };
const numCurto=id=>String(id).slice(-4);

function metrics(){
  const avail=$("#scroll").clientWidth||600;
  // ecrã de telemóvel (pedido explícito do Rui, 2026-10-06): com a largura
  // mínima de sempre (84px/72px), 7 dias nunca cabiam perto de um ecrã
  // estreito, obrigando a arrastar para o lado com os títulos cortados
  // quase por completo — aqui deixa-se cada dia ficar mais estreito (o
  // "dense" abaixo já reduz fonte/espaçamento para continuar legível),
  // em vez de forçar sempre a mesma largura mínima do ecrã largo.
  const movel=window.innerWidth<640;
  if(view.mode==="mes"){ DAY=movel?34:44; LANE=78; }
  else if(view.mode==="duas"){ DAY=Math.max(movel?48:72,Math.floor(avail/14)); LANE=78; }
  else { DAY=Math.max(movel?56:84,Math.floor(avail/7)); LANE=78; }
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
// cascata "em escada" (ver empacotarGlobal/renderLanes) — pedido
// explícito do Rui, 2026-10-06: o passo vertical tem de dar para ler bem
// o título de cada card, mesmo dos que ficam mais atrás; a lane cresce
// em altura o que for preciso para caber todos (ver calcularAlturasLanes),
// já não há limite ao nº de níveis.
const PASSO_CASCATA_H=24, PASSO_CASCATA_V=26;
let ALTURAS_LANE=[], OFFSETS_LANE=[];
/* uma OF pode "alargar" (ver .rsz-h) para ocupar vários dias na mesma lane
   (pedido explícito do Rui, 2026-10-02), e pode também estar esticada por
   várias lanes ao mesmo tempo (ver minMaxLane) — por isso dois cards
   "colidem" (precisam de aparecer em cascata, ver renderLanes) sempre que
   os intervalos de LANE e de DIA de ambos se cruzarem ao mesmo tempo.
   Nunca se tenta fazer um empacotamento perfeito sem nenhuma sobreposição
   (ver histórico: isso exigia que um card esticado ocupasse sempre a
   lane toda por onde passa, o que por sua vez tornava impossível dois
   cards coexistirem em qualquer lane só com uma linha diferente — ex:
   uma OF esticada que termina numa lane onde já está outra OF, mesmo
   lane nenhuma, nunca tinha linha livre nenhuma). Em vez disso, cada
   conflito só desloca o card "um bocado para dentro" em cascata (pedido
   explícito do Rui, 2026-10-06, "como no Google Calendar") — aceitável
   mesmo que ocasionalmente ainda se toquem um pouco, porque nunca faz a
   tabela crescer nem esconde a informação por completo. */
function tamanhoDe(e){
  // só a envergadura em LANES conta como "tamanho" (não os dias de
  // largura) — é o que determina quanto é que um card continua visível
  // na vertical quando outro lhe cai em cima; um card estreito mas muito
  // largo (ex: Moreira, 1 lane × 3 dias) é tão fácil de esconder como
  // qualquer outro de 1 lane, não deve competir com um card realmente
  // esticado por vários charriots (bug real, 2026-10-06: Moreira, 1 lane,
  // empatava com um buffer de 3 lanes só por ser mais largo em dias, e
  // ficava "de trás" — escondido — quando devia ficar à frente).
  const [minLane,maxLane]=minMaxLane(e);
  return maxLane-minLane+1;
}
function empacotarGlobal(lista){
  // os maiores (mais lanes) são colocados PRIMEIRO. Cada novo card fica
  // sempre numa linha estritamente acima de QUALQUER já colocado com que
  // colida (nunca reaproveita uma linha livre "por baixo") — isto
  // garante que, em qualquer par de cards em conflito, o mais pequeno
  // (colocado depois) fica sempre numa linha mais alta, ou seja, mais à
  // frente (mais desvio, mais zIndex, ver renderLanes), nunca escondido
  // atrás de algo maior do que ele. Pedido explícito do Rui, 2026-10-06:
  // "os mais pequenos devem passar para a frente para ficarem mais
  // visíveis", porque um card grande continua bem visível mesmo tapado
  // em parte, mas um card pequeno tapado por um grande desaparece todo.
  const ordenada=[...lista].sort((x,y)=>
    tamanhoDe(y)-tamanhoDe(x) || (y.larguraDias||1)-(x.larguraDias||1) || x.gs-y.gs || x.id-y.id);
  const colocados=[];  // {minLane,maxLane,fim,linha}
  const linhaPorId=new Map();
  ordenada.forEach(e=>{
    const [minLane,maxLane]=minMaxLane(e);
    const fim=e.gs+(e.larguraDias||1)-1;
    let linha=0;
    colocados.forEach(o=>{
      if(o.fim>=e.gs && !(maxLane<o.minLane || minLane>o.maxLane)) linha=Math.max(linha,o.linha+1);
    });
    colocados.push({minLane,maxLane,fim,linha});
    linhaPorId.set(e.id,linha);
  });
  return linhaPorId;
}
/* pedido explícito do Rui (2026-10-06, "como no Google [Calendar]", com
   screenshot): cards em conflito (ver empacotarGlobal) ficam "em escada",
   um bocado para dentro uns dos outros (mais estreitos, deslocados) — mas
   cada lane CRESCE em altura o que for preciso para caber a cascata toda
   dos dias visíveis (pedido explícito do Rui, 2026-10-06: "se isso for
   necessário aumentar o tamanho da linha para caber todos os cards que
   estiverem lá, faz isso") — só pelos dias visíveis, nunca por um
   conflito escondido fora da vista atual (ver days()/view). */
function calcularAlturasLanes(linhaPorId){
  const D=days();
  const diaIni=view.start, diaFim=view.start+view.len-1;
  ALTURAS_LANE=LANES.map((nome,li)=>{
    let maxLinha=0;
    entradas.forEach(e=>{
      const [minLane,maxLane]=minMaxLane(e);
      // conta para TODAS as lanes que o card atravessa, não só onde
      // começa e onde acaba (pedido explícito do Rui, 2026-10-06) — bug
      // real: um card esticado por Charriot 1-3, em cascata por causa de
      // outros dois exatamente na mesma posição, só fazia crescer a
      // altura do Charriot 1 — o fim dele, em Charriot 3, continuava com
      // a altura normal, e a cascata "entrava" visualmente na Multiserra
      // de Toros a seguir, porque o desvio aplicado no topo empurra o
      // fundo do card na mesma medida (ver renderLanes: fundo =
      // OFFSETS_LANE[maxLane]+PAD+ITEM_H+linha*V, independente de onde o
      // card começa).
      if(li<minLane || li>maxLane) return;
      const fim=e.gs+(e.larguraDias||1)-1;
      if(fim<diaIni || e.gs>diaFim) return; // fora da vista
      maxLinha=Math.max(maxLinha, linhaPorId.get(e.id)||0);
    });
    return Math.max(LANE, ITEM_H+ITEM_PAD*2+maxLinha*PASSO_CASCATA_V);
  });
  let acumulado=0;
  OFFSETS_LANE=ALTURAS_LANE.map(alt=>{ const topo=acumulado; acumulado+=alt; return topo; });
}
/* quanto é que o fim das lanes (já todas com a sua própria altura, ver
   calcularAlturasLanes) precisa de crescer, exatamente, para caber o
   card esticado por várias lanes (ver minMaxLane) que mais ultrapasse o
   fim da última — 0 no caso normal (nenhum chega lá), nunca um valor
   fixo "a olho" (ver renderLanes). Um card de uma só lane nunca
   ultrapassa nada: a sua própria lane já cresceu para o conter (ver
   calcularAlturasLanes), por isso só interessam aqui os esticados. */
function alturaExtraParaCardsEsticados(linhaPorId){
  const diaIni=view.start, diaFim=view.start+view.len-1;
  const fimLanes=OFFSETS_LANE[OFFSETS_LANE.length-1]+ALTURAS_LANE[ALTURAS_LANE.length-1];
  let extra=0;
  entradas.forEach(e=>{
    const [minLane,maxLane]=minMaxLane(e);
    if(minLane===maxLane) return;
    const fim=e.gs+(e.larguraDias||1)-1;
    if(fim<diaIni || e.gs>diaFim) return; // fora da vista
    const topoPx=OFFSETS_LANE[minLane]+ITEM_PAD;
    const alturaPx=(OFFSETS_LANE[maxLane]+ITEM_PAD+ITEM_H)-topoPx;
    const fundoCard=topoPx+(linhaPorId.get(e.id)||0)*PASSO_CASCATA_V+alturaPx;
    extra=Math.max(extra, fundoCard-fimLanes);
  });
  return Math.max(0, Math.ceil(extra));
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
  const linhaPorId=empacotarGlobal(entradas);
  calcularAlturasLanes(linhaPorId); renderLabels();
  const D=days();
  let h="";
  LANES.forEach((nome,li)=>{ h+=`<div class="row" style="height:${ALTURAS_LANE[li]}px">`+D.map(d=>{
    const hoje=MASTER.indexOf(d)===HOJE;
    return `<div class="cell${NAO_PRODUZ(d)?" wk":""}${hoje?" hoje":""}${ehFeriado(d)?" feriado":""}"></div>`;
  }).join("")+'</div>'; });
  h+='<div class="blocks" id="blocks"></div>';
  // reserva de espaço invisível no fundo, calculada exatamente (nunca um
  // valor fixo "a olho" — pedido explícito do Rui, 2026-10-06: "no fundo
  // continua a colocar uma linha branca sem necessidade", depois de uma
  // primeira tentativa com 20px fixos) — cada lane já cresce o que for
  // preciso para a sua própria cascata (ver calcularAlturasLanes), mas um
  // card esticado por várias lanes (ver minMaxLane) ainda pode ultrapassar
  // um pouco o fim da última lane, se a cascata começar numa lane
  // anterior; só nesse caso (raro) é que isto dá mais que 0px.
  h+=`<div style="height:${alturaExtraParaCardsEsticados(linhaPorId)}px"></div>`;
  const lanes=$("#lanes"); lanes.innerHTML=h; lanes.style.width=(D.length*DAY)+"px";
  const bl=$("#blocks");
  // um único passo por entrada (não mais por lane): o empacotamento já é
  // global (ver empacotarGlobal), por isso cada card só precisa de ser
  // desenhado uma vez, na sua lane de topo.
  entradas.forEach(e=>{
    const [minLane,maxLane]=minMaxLane(e);
    const largura=e.larguraDias||1;
    const aIni=e.gs-view.start, aFim=e.gs+largura-1-view.start;
    if(aFim<0||aIni>=view.len) return; // completamente fora da vista
    const aVis=Math.max(aIni,0), aFimVis=Math.min(aFim,view.len-1);
    const linha=linhaPorId.get(e.id);
    const el=document.createElement("div");
    const temCharriot=e.charriots && e.charriots.length>0;
    el.className="blk"+(temCharriot?"":" semCharriot")+(e.produzido?" produzido":"")+(e.buffer?" buffer":"");
    el.tabIndex=0; el.dataset.id=e.id; el.dataset.lane=minLane;
    el.style.borderLeftColor=corProduto(e);
    const topoPx=OFFSETS_LANE[minLane]+ITEM_PAD;
    const alturaPx=(maxLane>minLane
      ? (OFFSETS_LANE[maxLane]+ITEM_PAD+ITEM_H)-topoPx
      : ITEM_H);
    // "escada" ao estilo Google Calendar (pedido explícito do Rui,
    // 2026-10-06): um card em conflito com outro (linha>0, ver
    // empacotarGlobal) desloca-se para a direita/baixo e encolhe, para dar
    // para ler bem o título dos que ficam por baixo (pedido explícito do
    // Rui, 2026-10-06) — a lane cresce o que for preciso para caber (ver
    // calcularAlturasLanes), por isso já não há limite ao nº de níveis.
    el.style.left=(aVis*DAY+3+linha*PASSO_CASCATA_H)+"px";
    el.style.top=(topoPx+linha*PASSO_CASCATA_V)+"px";
    el.style.width=Math.max(70,((aFimVis-aVis+1)*DAY-8-linha*PASSO_CASCATA_H))+"px";
    el.style.height=alturaPx+"px";
    if(linha>0){ el.style.zIndex=10+linha; el.style.boxShadow="0 2px 8px rgba(0,0,0,.28)"; }
    // manípulos de arrastar para alargar (pedido explícito do Rui,
    // 2026-10-02): borda direita alarga em dias (só visual — ver
    // definir_largura_dias); borda de baixo marca mais charriots ao
    // mesmo tempo, a partir desta lane (mesmo resultado das caixas de
    // seleção na ficha, mas mais rápido).
    const resumo=resumoCardToros(e);
    el.innerHTML=`<div class="tt"><span class="ttText">${e.titulo}</span></div>
      <div class="of">Toros: ${resumo.qtdToros!=null?resumo.qtdToros+" m³":"—"}</div>
      <div class="of">Nº lotes: ${resumo.nLotes!=null?resumo.nLotes:"—"}</div>
      <div class="rsz rsz-h" title="arrastar para alargar (dias)"></div>
      <div class="rsz rsz-v" title="arrastar para marcar mais charriots"></div>`;
    bl.appendChild(el);
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
    view.start = clamp(segundaDe(anchor), INICIO_MIN_SEMANA, Math.max(MASTER.length-view.len,0));
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
    // pedido explícito do Rui (2026-10-04): as setas avançam/recuam
    // sempre uma semana de cada vez (7 dias), mesmo em "Duas semanas" —
    // nunca o tamanho da janela (view.len), que em "Duas semanas" são 14
    // dias, o que saltava duas semanas de cada vez em vez de uma.
    view.start=clamp(view.start+dir*7,INICIO_MIN_SEMANA,Math.max(MASTER.length-view.len,0));
  }
  render();
}
document.querySelectorAll("#seg button").forEach(b=>b.onclick=()=>setMode(b.dataset.m));
$("#prev").onclick=()=>step(-1);
$("#next").onclick=()=>step(1);
// pedido explícito do Rui (2026-10-04): "Hoje" não deve pôr hoje como
// primeiro dia da tabela — deve mostrar a semana de hoje (alinhada a
// segunda-feira, ver setMode/segundaDe), com hoje algures dentro dela.
$("#hoje").onclick=()=>{ view.start=Math.max(HOJE,0); setMode(view.mode); };
window.addEventListener("resize",render);

/* ---------- arrastar entre lanes/dias ---------- */
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
  drag={el:b,it,x0:e.clientX,y0:e.clientY,gsIni:it.gs,moveu:false};
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
  const dx=e.clientX-drag.x0, dy=e.clientY-drag.y0;
  if(Math.abs(dx)>6||Math.abs(dy)>6){ drag.moveu=true; drag.el.classList.add("drag"); }
  if(drag.moveu) drag.el.style.transform=`translate(${dx}px,${dy}px)`;
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
  const {el,it,moveu,gsIni,x0}=drag;
  el.classList.remove("drag"); el.style.transform="";
  drag=null;
  if(!moveu) return; // clique simples sem arrastar — não faz nada sozinho
  const r=$("#lanes").getBoundingClientRect();
  const novaLane=laneDeY(e.clientY-r.top);
  const novoGs=clamp(gsIni+Math.round((e.clientX-x0)/DAY),0,MASTER.length-1);
  // arrastar substitui sempre por um único charriot (ainda que a OF
  // estivesse em vários ao mesmo tempo — ver caixas de seleção na ficha
  // ou o manípulo rsz-v para marcar mais que um sem substituir).
  const novosCharriots = novaLane===0 ? null : [CHARRIOTS[novaLane-1]];
  const atuais = it.charriots||[];
  const laneMudou = !((novosCharriots===null && atuais.length===0) ||
    (novosCharriots && atuais.length===1 && atuais[0]===novosCharriots[0]));
  const diaMudou = novoGs!==gsIni;
  if(!laneMudou && !diaMudou){ renderLanes(); return; }
  // mudar de dia (pedido explícito do Rui, 2026-10-04): passa a ser
  // possível, mas sempre com confirmação — ver definir_dia_entrada em
  // tools/planeamento_entradas.py (o dia de início da produção, na outra
  // página, nunca é tocado por isto).
  if(diaMudou && !confirm(`Vais mudar o dia de entrada de ${MASTER[gsIni].iso} para ${MASTER[novoGs].iso}. Tens a certeza?`)){
    renderLanes();
    return;
  }
  const anteriorCharriots=it.charriots, gsAnterior=it.gs;
  if(laneMudou) it.charriots=novosCharriots;
  if(diaMudou) it.gs=novoGs;
  renderLanes();
  if(laneMudou) atribuirServidor(it, anteriorCharriots);
  if(diaMudou) definirDiaEntradaServidor(it, gsAnterior);
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
async function definirDiaEntradaServidor(it, gsAnterior){
  try{
    const r=await fetch("/planeamento-entradas/dia-entrada",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({basecamp_card_id:it.id,dia_entrada:MASTER[it.gs].iso})});
    const d=await r.json();
    if(d.erro){ alert(d.erro); it.gs=gsAnterior; renderLanes(); return; }
    log("local",`"${it.titulo}" com entrada movida para ${MASTER[it.gs].iso}`);
  }catch(e){ alert("Falhou a guardar: "+e); it.gs=gsAnterior; renderLanes(); }
}
// usado só pelo botão "Guardar" da ficha (pedido explícito do Rui,
// 2026-10-06: "tem de ser possível alterá-la aí e não apenas movendo o
// card na tabela") — mesmo endpoint do arrasto, mas reportando erros no
// próprio formulário ($fErro), como o resto da ficha, em vez de alert().
// Devolve false (e já escreve o erro) se falhar, para o chamador não
// continuar a guardar o resto da ficha como se tivesse corrido tudo bem.
async function guardarDiaEntradaSeMudou(it){
  const novo=$("#fDiaEntrada").value;
  if(!novo || novo===MASTER[it.gs].iso) return true;
  const r=await fetch("/planeamento-entradas/dia-entrada",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({basecamp_card_id:it.id,dia_entrada:novo})});
  const d=await r.json();
  if(d.erro){ $("#fErro").textContent=d.erro; return false; }
  it.gs=idxOf(novo);
  return true;
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
function wipCmpOptionsHtml(atual){
  const atualTxt = atual==null ? "" : String(atual);
  return `<option value="" ${!atualTxt?"selected":""}>—</option>` +
    WIP_CMP_PRESETS.map(v=>`<option value="${v}"${atualTxt===v?" selected":""}>${v}</option>`).join("");
}
/* uma linha de WIP+Toro (pedido explícito do Rui, 2026-10-23, "Algoritmo
   de Comprimentos") — uma OF real pode ter várias, uma por artigo/
   comprimento identificado no PDF; data-peso guarda o peso relativo (QTD
   do PDF) usado para repartir o volume da OF entre linhas, nunca mostrado
   nem editado diretamente (só o resultado interessa à equipa). */
function linhaWipToroHtml(linha, idx){
  const toroEhPreset = linha.toroCmp!=null && TORO_PRESETS.includes(linha.toroCmp);
  const toroOutroValor = (linha.toroCmp!=null && !toroEhPreset) ? linha.toroCmp : "";
  return `
  <div class="linhaWipToro" data-idx="${idx}" data-peso="${linha.peso??""}"
       data-comprimento-artigo="${linha.comprimentoArtigoMm??""}"
       style="border:1px solid var(--linha,#333);border-radius:8px;padding:10px;margin-bottom:10px">
    <div class="frow" style="margin-top:0">
      <label>Artigo</label>
      <input class="fArtigo" type="text" value="${linha.artigo||""}" placeholder="(identificado automaticamente)">
      <button type="button" class="btn fRemoverLinha" style="flex:0 0 auto">Remover linha</button>
    </div>
    <label class="grupoLbl" style="margin-top:6px">WIP</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><select class="fWipCmp">${wipCmpOptionsHtml(linha.wipCmp)}</select></div>
      <div class="miniField"><label>Lar</label><input type="text" value="${WIP_LAR_FIXA}" disabled></div>
      <div class="miniField"><label>Esp</label><input class="fWipEsp" type="text" inputmode="decimal" value="${linha.wipEsp??""}" placeholder="—"></div>
      <div class="miniField"><label>Fiadas</label><b class="fFiadas mono" style="align-self:center">—</b></div>
      <div class="miniField"><label>Índice</label><input class="fIndiceWip" type="text" inputmode="decimal" value="${linha.indiceWip??INDICE_WIP_DEFAULT}"></div>
      <div class="miniField"><label>QTD Wip</label><b class="fQtdWip mono" style="align-self:center">—</b></div>
      <div class="miniField"><label>Nº lotes</label><b class="fNLotes mono" style="align-self:center">—</b></div>
    </div>
    <label class="grupoLbl">Toro</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><select class="fToroCmp">${toroCmpOptionsHtml(linha.toroCmp)}</select></div>
      <div class="miniField fToroCmpOutroWrap" style="${(toroEhPreset||linha.toroCmp==null)?"display:none":""}">
        <label>Valor</label><input class="fToroCmpOutro" type="text" inputmode="decimal" value="${toroOutroValor}" list="histToroCmp"></div>
      <div class="miniField"><label>Tipo</label><select class="fToroTipo">
        <option value="" ${!linha.toroTipo?"selected":""}>—</option>
        <option value="IN"${linha.toroTipo==="IN"?" selected":""}>IN</option>
        <option value="MT"${linha.toroTipo==="MT"?" selected":""}>MT</option>
      </select></div>
      <div class="miniField"><label>Índice</label><input class="fIndiceToros" type="text" inputmode="decimal" value="${linha.indiceToros??INDICE_TOROS_DEFAULT}"></div>
      <div class="miniField"><label>QTD Toros</label>
        <span style="display:flex;align-items:center;gap:6px;white-space:nowrap">
          <b class="fQtdToros mono">—</b>
          <span class="fAproveitamento mono" style="color:var(--dim);font-size:12px"></span>
        </span></div>
    </div>
  </div>`;
}
function bindLinhaHandlers(container, it){
  const toroCmpEl=container.querySelector(".fToroCmp");
  toroCmpEl.onchange=()=>{
    container.querySelector(".fToroCmpOutroWrap").style.display = toroCmpEl.value==="outro" ? "" : "none";
    recalcularLinha(it,container);
  };
  // pedido explícito do Rui, 2026-10-30: "sempre que se alterar as
  // dimensões do cmp, a percentagem de aproveitamento devia mudar" — Cmp
  // do Toro (preset ou "outro") recalcula tal como o Cmp do WIP já fazia.
  container.querySelector(".fToroCmpOutro").oninput=()=>recalcularLinha(it,container);
  // pedido explícito do Rui, 2026-10-30: o nome do artigo pode trazer o
  // comprimento (padrão "CxLxE") mesmo numa linha sem PDF nenhum — a %
  // tem de aparecer assim que o nome bater com esse padrão.
  container.querySelector(".fArtigo").oninput=()=>recalcularLinha(it,container);
  container.querySelector(".fIndiceWip").oninput=()=>recalcularLinha(it,container);
  container.querySelector(".fIndiceToros").oninput=()=>recalcularLinha(it,container);
  container.querySelector(".fWipCmp").onchange=()=>recalcularLinha(it,container);
  container.querySelector(".fWipEsp").oninput=()=>recalcularLinha(it,container);
  container.querySelector(".fRemoverLinha").onclick=()=>{
    const todas=document.querySelectorAll("#linhasWipToro .linhaWipToro");
    if(todas.length<=1){ alert("Tem de ficar pelo menos uma linha."); return; }
    container.remove();
    recalcularTodasLinhas(it);
  };
  recalcularLinha(it,container);
}
/* usa diretamente data-peso (campo "QTD" do PDF, por artigo, já em m³)
   como o volume dessa linha, tal como
   tools.planeamento_entradas._linhas_com_calculo — só quando uma linha
   não tem peso nenhum (ex: adicionada à mão) é que cai para 1/nº de
   linhas do volume total da OF (it.volume), como valor de recurso. */
function recalcularLinha(it, container){
  const todas=Array.from(document.querySelectorAll("#linhasWipToro .linhaWipToro"));
  const n=todas.length;
  const pesoLinha=parseFloat(container.dataset.peso);
  const volumeLinha=!isNaN(pesoLinha) ? pesoLinha : ((it.volume!=null) ? it.volume/n : null);
  const indiceWip=parseFloat(container.querySelector(".fIndiceWip").value);
  const indiceToros=parseFloat(container.querySelector(".fIndiceToros").value);
  const wipCmpEl=container.querySelector(".fWipCmp"), wipEspEl=container.querySelector(".fWipEsp");
  const wipCmp=wipCmpEl.value?parseFloat(wipCmpEl.value):null;
  const wipEsp=wipEspEl.value?parseFloat(wipEspEl.value):null;
  const fiadas=fiadasPorEspessura(wipEsp);
  container.querySelector(".fFiadas").textContent=fiadas!=null?fiadas:"—";
  const qtdWip=(volumeLinha!=null && !isNaN(indiceWip)) ? Math.round(volumeLinha*indiceWip*100)/100 : null;
  container.querySelector(".fQtdWip").textContent=qtdWip!=null?qtdWip+" m³":"—";
  container.querySelector(".fQtdToros").textContent=(volumeLinha!=null && !isNaN(indiceToros))
    ? (Math.round(volumeLinha*indiceToros*100)/100)+" m³" : "—";
  // pedido explícito do Rui, 2026-10-30: a % mostra-se junto da QTD
  // Toros, por isso segue sempre o Cmp do TORO (não o do WIP) — muda
  // assim que o Cmp do Toro muda, preset ou "outro".
  const toroCmpSel=container.querySelector(".fToroCmp").value;
  const toroCmpTexto=toroCmpSel==="outro" ? container.querySelector(".fToroCmpOutro").value : toroCmpSel;
  const toroCmpMm=toroCmpTexto?parseFloat(toroCmpTexto):null;
  const comprimentoGuardado=parseFloat(container.dataset.comprimentoArtigo);
  const artigoTexto=container.querySelector(".fArtigo").value;
  const comprimentosDoNome=todosComprimentosDoNomeArtigo(artigoTexto);
  const comprimentosParaAproveitamento=comprimentosDoNome.length?comprimentosDoNome:(isNaN(comprimentoGuardado)?[]:[comprimentoGuardado]);
  const pct=aproveitamentoPctMulti(comprimentosParaAproveitamento, toroCmpMm);
  container.querySelector(".fAproveitamento").textContent=pct!=null?`(${pct}%)`:"";
  const nLotes=calcularNLotes(qtdWip,wipCmp,wipEsp);
  container.querySelector(".fNLotes").textContent=nLotes!=null?nLotes:"—";
}
function recalcularTodasLinhas(it){
  document.querySelectorAll("#linhasWipToro .linhaWipToro").forEach(c=>recalcularLinha(it,c));
}
/* lê o estado atual de todas as linhas da ficha, tal como ficaram depois
   de editadas à mão — formato pronto a enviar a /planeamento-entradas/
   linhas (ver tools.planeamento_entradas.guardar_linhas_entrada). */
function coletarLinhas(){
  return Array.from(document.querySelectorAll("#linhasWipToro .linhaWipToro")).map(c=>{
    const toroCmpSel=c.querySelector(".fToroCmp").value;
    let toroCmp=null;
    if(toroCmpSel==="outro"){ toroCmp=c.querySelector(".fToroCmpOutro").value||null; }
    else if(toroCmpSel){ toroCmp=toroCmpSel; }
    const wipCmp=c.querySelector(".fWipCmp").value;
    const wipEsp=c.querySelector(".fWipEsp").value;
    const indiceWip=c.querySelector(".fIndiceWip").value;
    const indiceToros=c.querySelector(".fIndiceToros").value;
    const peso=c.dataset.peso;
    const comprimentoArtigo=c.dataset.comprimentoArtigo;
    return {
      artigo: c.querySelector(".fArtigo").value||null,
      peso: peso!==""?parseFloat(peso):null,
      wip_cmp: wipCmp?parseFloat(wipCmp):null,
      wip_esp: wipEsp?parseFloat(wipEsp):null,
      indice_wip: indiceWip?parseFloat(indiceWip):null,
      toro_cmp: toroCmp,
      toro_tipo: c.querySelector(".fToroTipo").value||null,
      indice_toros: indiceToros?parseFloat(indiceToros):null,
      comprimento_artigo_mm: comprimentoArtigo!==""?parseFloat(comprimentoArtigo):null,
    };
  });
}
function closeSheet(){ $("#veil").classList.remove("on"); $("#sheet").classList.remove("on"); }
/* QTD Toros/QTD Wip nunca se guardam — recalculam-se aqui ao vivo, à
   medida que o índice muda no formulário (pedido explícito do Rui,
   2026-10-01: "por omissão tem de fazer sempre este cálculo"), a partir
   do volume da OF (fixo, vem da produção) × o índice atual no campo. */
function recalcularQtd(it){
  const indiceWip=parseFloat($("#fIndiceWip").value);
  const indiceToros=parseFloat($("#fIndiceToros").value);
  const wipCmp = $("#fWipCmp").value ? parseFloat($("#fWipCmp").value) : null;
  const wipEsp = $("#fWipEsp").value ? parseFloat($("#fWipEsp").value) : null;
  const fiadas = fiadasPorEspessura(wipEsp);
  $("#fFiadas").textContent = fiadas!=null ? fiadas : "—";
  // card "Buffer" (pedido explícito do Rui, 2026-10-04): o Nº de lotes é
  // escrito à mão (ver openSheet) — QTD Wip/QTD Toros recalculam-se a
  // partir dele, ao contrário de um card normal, onde é o oposto.
  if(it.buffer){
    const nLotes = $("#fNLotes").value ? parseFloat($("#fNLotes").value) : null;
    const [qtdWip, qtdToros] = calcularWipEToroDeLotes(nLotes, wipCmp, wipEsp, indiceWip, indiceToros);
    $("#fQtdWip").textContent = qtdWip!=null ? qtdWip+" m³" : "—";
    $("#fQtdToros").textContent = qtdToros!=null ? qtdToros+" m³" : "—";
    return;
  }
  const qtdWip = (it.volume!=null && !isNaN(indiceWip)) ? Math.round(it.volume*indiceWip*100)/100 : null;
  $("#fQtdWip").textContent = qtdWip!=null ? qtdWip+" m³" : "—";
  $("#fQtdToros").textContent = (it.volume!=null && !isNaN(indiceToros)) ? (Math.round(it.volume*indiceToros*100)/100)+" m³" : "—";
  const nLotes = calcularNLotes(qtdWip, wipCmp, wipEsp);
  $("#fNLotes").textContent = nLotes!=null ? nLotes : "—";
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

    <!-- data de entrada editável diretamente aqui (pedido explícito do
         Rui, 2026-10-06: "tem de ser possível alterá-la aí e não apenas
         movendo o card na tabela") — mesmo endpoint do arrasto
         (definir_dia_entrada), com o mesmo aviso antes de confirmar (ver
         guardarDiaEntradaSeMudou abaixo). -->
    <div class="frow"><label class="destaque">Data de entrada</label>
      <input id="fDiaEntrada" type="date" value="${MASTER[it.gs].iso}"></div>

    <!-- todos os campos numéricos desta ficha usam type="text" + inputmode
         (nunca type="number"), pedido explícito do Rui (2026-10-04):
         tablets reais não deixavam escrever nalguns destes campos — bug
         conhecido do Safari/iOS com input[type=number] (sobretudo quando
         combinado com um datalist, mas visto também sem ele, incluindo com
         um teclado físico ligado) — type="text"+inputmode dá o mesmo
         teclado numérico sem esse problema. -->
    <div class="frow"><label>Duração na tabela (dias)</label>
      <input id="fLarguraDias" type="text" inputmode="numeric"
        style="width:70px;flex:0 0 auto" value="${it.larguraDias||1}"></div>

    ${it.buffer ? "" : `
    <div class="frow"><label class="destaque">Em contínuo</label>
      <input id="fEmContinuo" type="checkbox" style="width:auto;flex:0 0 auto;transform:scale(1.3)" ${it.emContinuo?"checked":""}></div>`}

    ${it.buffer ? `
    <label class="grupoLbl">WIP</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><select id="fWipCmp">${wipCmpOptionsHtml(it.wipCmp)}</select></div>
      <div class="miniField"><label>Lar</label><input type="text" value="${WIP_LAR_FIXA}" disabled></div>
      <div class="miniField"><label>Esp</label><input id="fWipEsp" type="text" inputmode="decimal" value="${it.wipEsp??""}" placeholder="—"></div>
      <div class="miniField"><label>Fiadas</label><b id="fFiadas" class="mono" style="align-self:center">—</b></div>
      <div class="miniField"><label>Índice</label><input id="fIndiceWip" type="text" inputmode="decimal" value="${it.indiceWip}"></div>
      <div class="miniField"><label>QTD Wip</label><b id="fQtdWip" class="mono" style="align-self:center">—</b></div>
      <div class="miniField"><label>Nº lotes</label>
        <input id="fNLotes" type="text" inputmode="numeric" style="width:60px" value="${it.nLotes??""}"></div>
    </div>

    <label class="grupoLbl">Toro</label>
    <div class="miniRow">
      <div class="miniField"><label>Cmp</label><select id="fToroCmp">${toroCmpOptionsHtml(it.toroCmp)}</select></div>
      <div class="miniField" id="fToroCmpOutroWrap" style="${(toroEhPreset||it.toroCmp==null)?"display:none":""}">
        <label>Valor</label><input id="fToroCmpOutro" type="text" inputmode="decimal" value="${toroOutroValor}" list="histToroCmp"></div>
      <div class="miniField"><label>Tipo</label><select id="fToroTipo">
        <option value="" ${!it.toroTipo?"selected":""}>—</option>
        <option value="IN"${it.toroTipo==="IN"?" selected":""}>IN</option>
        <option value="MT"${it.toroTipo==="MT"?" selected":""}>MT</option>
      </select></div>
      <div class="miniField"><label>Índice</label><input id="fIndiceToros" type="text" inputmode="decimal" value="${it.indiceToros}"></div>
      <div class="miniField"><label>QTD Toros</label><b id="fQtdToros" class="mono" style="align-self:center">—</b></div>
    </div>` : `
    <label class="grupoLbl" style="margin-top:0">Linhas de WIP+Toro</label>
    <div class="owner" style="font-size:12.5px;color:var(--dim);margin-bottom:6px">
      Uma linha por artigo/comprimento identificado no PDF da OF — "Calcular automaticamente" lê o PDF e
      substitui estas linhas pelo resultado; também podes editar ou adicionar à mão.</div>
    <div id="linhasWipToro">${(it.linhas&&it.linhas.length?it.linhas:[{}]).map((l,i)=>linhaWipToroHtml(l,i)).join("")}</div>
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <button type="button" class="btn" id="btnAdicionarLinha">+ Adicionar linha</button>
      <button type="button" class="btn" id="btnCalcularAuto">Calcular automaticamente</button>
    </div>`}
    ${datalistHtml("histToroCmp",historico.toro_cmp)}

    <div class="kv" style="margin-top:16px"><span>Nome</span><b>${it.titulo}</b></div>
    <div class="kv"><span>Linha</span><b>${it.linha||"—"}</b></div>
    <div class="kv"><span>Início produção</span><b>${it.dataInicioProducao||"—"}</b></div>
    <div class="kv"><span>Volume</span><b>${it.volume?(it.volume+" m³"):"—"}</b></div>
    <div class="kv"><span>Madeira</span><b>${it.madeira||"—"}</b></div>

    <div class="err" id="fErro"></div>
    <div class="acts" style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
      ${it.url?`<a class="btn" target="_blank" rel="noopener" href="${it.url}">Abrir ${it.buffer?"OF de origem":"card"} no Basecamp</a>`:""}
      <button class="btn verde${it.produzido?" ativo":""}" id="btnProduzido">${it.produzido?"Produzido ✓":"Marcar Produzido"}</button>
      ${it.buffer
        ? `<button class="btn" id="btnApagarBuffer">Apagar Buffer</button>`
        : `<button class="btn buffer" id="btnFazerBuffer">Fazer Buffer</button>`}
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
  if(it.buffer){
    $("#fToroCmp").onchange=()=>{
      $("#fToroCmpOutroWrap").style.display = $("#fToroCmp").value==="outro" ? "" : "none";
    };
  }
  // avisar sempre antes de alterar a data de entrada calculada (pedido
  // explícito do Rui, 2026-10-06), tanto aqui como ao desmarcar "Em
  // contínuo" abaixo — mesmo aviso que já existia ao arrastar o card.
  const diaEntradaOriginal = MASTER[it.gs].iso;
  $("#fDiaEntrada").onchange=()=>{
    const novo=$("#fDiaEntrada").value;
    if(novo && novo!==diaEntradaOriginal
        && !confirm(`Vais mudar a data de entrada calculada de ${diaEntradaOriginal} para ${novo}. Tens a certeza?`)){
      $("#fDiaEntrada").value=diaEntradaOriginal;
    }
  };
  if(!it.buffer){
    $("#fEmContinuo").onchange=()=>{
      if(!$("#fEmContinuo").checked && !confirm(
          "Vais desmarcar \"Em contínuo\" — a data de entrada calculada deixa de ser o dia de início da "
          + "produção e passa a ser o dia anterior (ou sábado, se isso cair a domingo). Tens a certeza?")){
        $("#fEmContinuo").checked=true;
      }
    };
  }
  if(it.buffer){
    $("#fIndiceWip").oninput=()=>recalcularQtd(it);
    $("#fIndiceToros").oninput=()=>recalcularQtd(it);
    $("#fWipCmp").onchange=()=>recalcularQtd(it);
    $("#fWipEsp").oninput=()=>recalcularQtd(it);
    $("#fNLotes").oninput=()=>recalcularQtd(it);
    recalcularQtd(it);
  }else{
    document.querySelectorAll("#linhasWipToro .linhaWipToro").forEach(c=>bindLinhaHandlers(c,it));
    $("#btnAdicionarLinha").onclick=()=>{
      const container=document.createElement("div");
      const idx=document.querySelectorAll("#linhasWipToro .linhaWipToro").length;
      const toroTipoDefeito=ehQuadradilho(it.titulo)?tipoMadeiraDoTitulo(it.titulo):null;
      container.innerHTML=linhaWipToroHtml({toroTipo:toroTipoDefeito},idx);
      const novaLinha=container.firstElementChild;
      $("#linhasWipToro").appendChild(novaLinha);
      bindLinhaHandlers(novaLinha,it);
      recalcularTodasLinhas(it);
    };
    $("#btnCalcularAuto").onclick=async()=>{
      $("#fErro").textContent="";
      const btn=$("#btnCalcularAuto");
      btn.disabled=true; btn.textContent="A calcular…";
      try{
        const r=await fetch("/planeamento-entradas/linhas/calcular",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id})});
        const d=await r.json();
        if(d.erro){ $("#fErro").textContent=d.erro; return; }
        log("local",`"${it.titulo}": comprimentos calculados automaticamente (${d.linhas.length} linha(s), de "${d.ficheiro}")`+
          (d.volume_m3!=null?` — volume total retificado para ${d.volume_m3} m³`:""));
        if(d.aviso_volume){ $("#fErro").textContent=d.aviso_volume; }
        await carregar();
        openSheet(it.id);
      }catch(e){ $("#fErro").textContent="Falhou a calcular: "+e; }
      finally{ btn.disabled=false; btn.textContent="Calcular automaticamente"; }
    };
  }
  if(it.buffer){
    $("#btnApagarBuffer").onclick=async()=>{
      if(!confirm(`Apagar o card "${it.titulo}"? Não pode ser desfeito.`)) return;
      try{
        const r=await fetch("/planeamento-entradas/buffer/apagar",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({id:Math.abs(it.id)})});
        const d=await r.json();
        if(d.erro){ $("#fErro").textContent=d.erro; return; }
        entradas=entradas.filter(e=>e.id!==it.id);
        log("local",`"${it.titulo}" apagado`);
        closeSheet();
        render();
      }catch(e){ $("#fErro").textContent="Falhou a apagar: "+e; }
    };
  }else{
    $("#btnFazerBuffer").onclick=async()=>{
      try{
        const r=await fetch("/planeamento-entradas/buffer/criar",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id})});
        const d=await r.json();
        if(d.erro){ $("#fErro").textContent=d.erro; return; }
        const novo={
          id:d.basecamp_card_id, titulo:d.titulo, url:d.url, coluna:d.coluna_basecamp,
          linha:d.linha, dataInicioProducao:d.dia_inicio_producao, volume:d.volume_m3, madeira:d.tipo_madeira, cor:d.cor,
          gs:idxOf(d.dia_entrada), charriots:d.charriots||[], larguraDias:d.largura_dias||1,
          produzido:!!d.produzido, emContinuo:!!d.em_continuo,
          wipCmp:d.wip_cmp, wipLar:d.wip_lar, wipEsp:d.wip_esp,
          indiceWip:d.indice_wip, qtdWip:d.qtd_wip_m3, nLotes:d.n_lotes,
          toroCmp:d.toro_cmp, toroTipo:d.toro_tipo,
          indiceToros:d.indice_toros, qtdToros:d.qtd_toros_m3,
          buffer:!!d.buffer,
        };
        entradas.push(novo);
        log("local",`"${novo.titulo}" criado a partir de "${it.titulo}"`);
        closeSheet();
        render();
        openSheet(novo.id);
      }catch(e){ $("#fErro").textContent="Falhou a criar o buffer: "+e; }
    };
  }
  $("#guardar").onclick=async()=>{
    $("#fErro").textContent="";
    const charriots=Array.from(document.querySelectorAll(".fCharriot:checked")).map(x=>x.value);
    const larguraDias=Math.max(1,Math.min(LARGURA_DIAS_MAX,parseInt($("#fLarguraDias").value)||1));
    // card "Buffer" (pedido explícito do Rui, 2026-10-04): grava tudo de
    // uma vez (não é uma OF real, não há nada para ler de outro lado) —
    // ver tools/planeamento_entradas.guardar_buffer.
    if(it.buffer){
      const wipCmp=$("#fWipCmp").value?parseFloat($("#fWipCmp").value):null;
      const wipEsp=$("#fWipEsp").value?parseFloat($("#fWipEsp").value):null;
      const indiceWip=$("#fIndiceWip").value?parseFloat($("#fIndiceWip").value):null;
      const indiceToros=$("#fIndiceToros").value?parseFloat($("#fIndiceToros").value):null;
      // toroCmp é texto (não número — há opções como "255 ⌀16", ver
      // TORO_PRESETS): nunca usar parseFloat aqui, perderia o "⌀16".
      let toroCmp=null;
      if($("#fToroCmp").value==="outro"){ toroCmp=$("#fToroCmpOutro").value||null; }
      else if($("#fToroCmp").value){ toroCmp=$("#fToroCmp").value; }
      const toroTipo=$("#fToroTipo").value||null;
      const nLotes=$("#fNLotes").value?parseFloat($("#fNLotes").value):null;
      try{
        if(!(await guardarDiaEntradaSeMudou(it))) return;
        const r=await fetch("/planeamento-entradas/buffer/guardar",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({id:Math.abs(it.id),charriots,largura_dias:larguraDias,wip_cmp:wipCmp,wip_esp:wipEsp,
            indice_wip:indiceWip,n_lotes:nLotes,toro_cmp:toroCmp,toro_tipo:toroTipo,indice_toros:indiceToros})});
        const d=await r.json();
        if(d.erro){ $("#fErro").textContent=d.erro; return; }
        log("local",`dados de "${it.titulo}" guardados`);
        closeSheet();
        await carregar();
      }catch(e){ $("#fErro").textContent="Falhou a guardar: "+e; }
      return;
    }
    const emContinuo=$("#fEmContinuo").checked;
    const linhas=coletarLinhas();
    try{
      if(!(await guardarDiaEntradaSeMudou(it))) return;
      if(JSON.stringify(charriots)!==JSON.stringify(it.charriots||[])){
        const r0=await fetch("/planeamento-entradas/atribuir",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id,charriots})});
        const d0=await r0.json();
        if(d0.erro){ $("#fErro").textContent=d0.erro; return; }
      }
      if(larguraDias!==(it.larguraDias||1)){
        const r4=await fetch("/planeamento-entradas/largura-dias",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id,largura_dias:larguraDias})});
        const d4=await r4.json();
        if(d4.erro){ $("#fErro").textContent=d4.erro; return; }
      }
      const r1=await fetch("/planeamento-entradas/linhas",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({basecamp_card_id:it.id,linhas})});
      const d1=await r1.json();
      if(d1.erro){ $("#fErro").textContent=d1.erro; return; }
      if(emContinuo!==it.emContinuo){
        const r3=await fetch("/planeamento-entradas/em-continuo",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({basecamp_card_id:it.id,em_continuo:emContinuo})});
        const d3=await r3.json();
        if(d3.erro){ $("#fErro").textContent=d3.erro; return; }
      }
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
      indiceWip:e.indice_wip, qtdWip:e.qtd_wip_m3, nLotes:e.n_lotes,
      toroCmp:e.toro_cmp, toroTipo:e.toro_tipo,
      indiceToros:e.indice_toros, qtdToros:e.qtd_toros_m3,
      linhas:(e.linhas||[]).map(l=>({
        id:l.id, artigo:l.artigo, peso:l.peso,
        wipCmp:l.wip_cmp, wipEsp:l.wip_esp, indiceWip:l.indice_wip, qtdWip:l.qtd_wip_m3, nLotes:l.n_lotes,
        toroCmp:l.toro_cmp, toroTipo:l.toro_tipo, indiceToros:l.indice_toros, qtdToros:l.qtd_toros_m3,
        comprimentoArtigoMm:l.comprimento_artigo_mm,
      })),
      buffer:!!e.buffer,
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
