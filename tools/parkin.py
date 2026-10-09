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
import base64, io, re, unicodedata
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


_ARTIGO_DESCONHECIDO = "desconhecido"


def _chave_artigo(tipo: str, comprimento: float, espessura: str) -> str:
    """"desconhecido" quando falta algum dos três — caso real do histórico
    importado (ver importar_historico_avaliacoes), nunca de um registo
    feito pela foto do talão (aí os três vêm sempre preenchidos ou a
    entrada nem chega a gravar-se, ver registar_entrada)."""
    if tipo is None or comprimento is None or espessura is None:
        return _ARTIGO_DESCONHECIDO
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
    "ABAIXO" ou nenhum sufixo) é normal.

    Tipo (pedido explícito do Rui, 2026-10-07): o talão às vezes diz "IN"
    no campo Produto, às vezes não diz nada — "sempre que não diz IN
    significa que a madeira é MT". Nunca se procura "MT" explicitamente:
    a sua AUSÊNCIA de "IN" já É o sinal de MT, por isso o tipo nunca fica
    por reconhecer (antes disto, um talão sem "IN" nem "MT" escritos
    literalmente falhava aqui com "não consegui reconhecer o tipo")."""
    tipo = "IN" if re.search(r"\bIN\b", produto, re.IGNORECASE) else "MT"

    m_comp = re.search(r"(\d+,\d+)", produto)
    comprimento = float(m_comp.group(1).replace(",", ".")) if m_comp else None

    espessura = "fina" if "ACIMA" in produto.upper() else "normal"

    if comprimento is None:
        return {"tipo": None, "comprimento": None, "espessura": None,
               "erro": f"não consegui reconhecer o comprimento no campo \"Produto\" deste talão: {produto!r}"}
    return {"tipo": tipo, "comprimento": comprimento, "espessura": espessura, "erro": None}


# faixas do manual (ver LIMIAR_BOA/LIMIAR_MEDIA) tal como os nomes que a
# própria avaliação já escreve ("Classificação Final: Aceitável (60-74%)")
# — ler o NOME da faixa é mais fiável do que tentar ler só um número, e é
# a ÚNICA fonte usada para decidir a categoria (nunca um número sozinho,
# depois do bug abaixo). Para o índice (só um número aproximado, usado só
# para "média de qualidade" por fornecedor — ver
# top_qualidade_fornecedores_parkin, nunca para decidir a categoria),
# procura-se uma percentagem perto do próprio local onde o nome da faixa
# foi encontrado, e só se aceita esse número SE for consistente com a
# categoria já determinada — caso contrário fica None, nunca um valor
# que contradiga a categoria.
#
# Bug real, 2026-10-02 (primeira versão desta função): usava
# `.find("classifica")` para situar a janela de busca — mas a palavra
# "classifica" também aparece em frases soltas do corpo da avaliação
# (ex: "Classifica como 'Fresca'"), antes da verdadeira secção de
# classificação final; a janela calculada a partir daí apanhava a
# percentagem ERRADA de uma linha anterior da tabela de critérios (ex:
# "Retidão | 10%"), dando 10% de qualidade a uma carga real "Aceitável"
# (62%) — abaixo do mínimo matematicamente possível do IGQC (20%).
_RE_BANDA_CLASSIFICACAO = re.compile(
    r"Classifica[cç][aã]o\s*(?:[Ff]inal)?[:\*\s]*(?:\d[\d,.\-–>\s%→*]*)?\**\s*"
    r"(Excelente|Boa|Aceit[aá]vel|Fraca|Rejei[cç][aã]o)",
    re.IGNORECASE)
_RE_FAIXA_INTERVALO = re.compile(r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*%")
_RE_PERCENTAGEM_GENERICA = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_BANDA_PARA_CATEGORIA = {"excelente": "Boa", "boa": "Boa", "aceitavel": "Media", "fraca": "Fraca", "rejeicao": "Fraca"}


def _normalizar_banda(banda: str) -> str:
    return banda.lower().replace("á", "a").replace("ã", "a").replace("ç", "c")


def _indice_e_categoria_de_texto(avaliacao_texto: str):
    """Deduz (indice, categoria) de uma avaliação de qualidade já escrita
    (ver agents/qualidade_toros_ecos_largos) — usado como fallback para
    avaliações antigas sem indice_igqc numérico guardado (ou nunca
    guardado, no caso do histórico anterior ao Park In — ver
    importar_historico_avaliacoes), em vez de tentar reavaliar a carga
    sem as fotos originais da madeira (que o Park In não tem aqui, só a
    foto do talão).

    `categoria` vem sempre do nome da faixa (mais fiável), com o
    intervalo numérico como única reserva; `indice` é só um número
    aproximado para "média de qualidade" — só aceite se bater com a
    categoria já determinada, nunca um número que a contradiga (ver bug
    acima). Best-effort: devolve (None, None) se não conseguir ler nada,
    nunca inventa um valor."""
    if not avaliacao_texto:
        return None, None

    categoria = None
    candidato = None

    m_banda = _RE_BANDA_CLASSIFICACAO.search(avaliacao_texto)
    if m_banda:
        categoria = _BANDA_PARA_CATEGORIA.get(_normalizar_banda(m_banda.group(1)))
        # procura uma percentagem mesmo à volta de onde o nome da faixa
        # foi encontrado (ex: "Aceitável (62%)" a seguir, ou "62% →
        # Aceitável" antes) — nunca a partir de .find("classifica"), que
        # pode apanhar uma ocorrência solta da palavra noutro sítio.
        janela_perto = avaliacao_texto[max(0, m_banda.start() - 60):m_banda.end() + 60]
        m_pct = _RE_PERCENTAGEM_GENERICA.search(janela_perto)
        if m_pct:
            try:
                candidato = float(m_pct.group(1).replace(",", "."))
            except ValueError:
                pass

    if categoria is None:
        idx_faixa = avaliacao_texto.lower().rfind("classifica")
        if idx_faixa < 0:
            idx_faixa = avaliacao_texto.lower().rfind("faixa")
        janela_faixa = avaliacao_texto[max(0, idx_faixa - 50):idx_faixa + 250] if idx_faixa >= 0 else avaliacao_texto
        m_faixa = _RE_FAIXA_INTERVALO.search(janela_faixa) or _RE_FAIXA_INTERVALO.search(avaliacao_texto)
        if m_faixa:
            try:
                categoria = _categoria_de_indice(float(m_faixa.group(1)))
            except ValueError:
                pass

    if candidato is None:
        idx = avaliacao_texto.lower().rfind("classifica")
        if idx < 0:
            idx = avaliacao_texto.lower().rfind("igqc")
        janela = avaliacao_texto[idx:idx + 200] if idx >= 0 else avaliacao_texto
        m_pct2 = _RE_PERCENTAGEM_GENERICA.search(janela) or _RE_PERCENTAGEM_GENERICA.search(avaliacao_texto)
        if m_pct2:
            try:
                candidato = float(m_pct2.group(1).replace(",", "."))
            except ValueError:
                pass

    if categoria is None and candidato is not None:
        categoria = _categoria_de_indice(candidato)

    indice = candidato if (candidato is not None and _categoria_de_indice(candidato) == categoria) else None
    return indice, categoria


def _resolver_data(data_str: str):
    try:
        return datetime.strptime((data_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return date.today()


UTILIZADOR_CHAT_PARKIN = "Balança Ecos Largos"


def garantir_perfil_chat_parkin():
    """Garante que UTILIZADOR_CHAT_PARKIN já tem perfil guardado (upsert
    idempotente, seguro a chamar sempre no arranque) — sem isto, a
    primeira mensagem desta identidade cairia no acolhimento (perguntas
    de boas-vindas) em vez de ir direta ao agente de qualidade de toros,
    porque main.py só salta o acolhimento depois de perfil_existe() dar
    True. `empresa="ecos_largos"` é o que faz orchestrator.encaminhar
    decidir por escolher_agente_ecos_largos, que por sua vez escolhe
    sempre qualidade_toros_ecos_largos quando a mensagem traz fotos
    anexadas — exatamente o que o popup "Nova entrada" do Park In faz."""
    db.guardar_perfil(
        UTILIZADOR_CHAT_PARKIN, papel="Operador de balança (Park In)",
        estilo_resposta="direto ao essencial", formato="texto corrido",
        decisao="recomendação fechada",
        dificuldades="avaliar a qualidade de cargas de toros a partir do talão de pesagem",
        empresa="ecos_largos")


def registar_entrada_a_partir_de_avaliacao(talao: str, fornecedor: str, data_carga: str,
                                           tipo: str, comprimento: float, espessura: str,
                                           peso_liquido_kg: float, matricula: str = None,
                                           guia_req: str = None, peso_bruto_kg: float = None,
                                           tara_kg: float = None, indice_igqc: float = None,
                                           registado_por: str = None) -> dict:
    """Regista uma entrada no Park In diretamente a partir dos campos já
    extraídos por uma avaliação de qualidade concluída (ver
    tools/ecos_largos.guardar_avaliacao_carga_toros, chamada
    automaticamente de lá) — ao contrário do antigo fluxo de "Nova
    entrada" do Park In, não lê nenhuma foto aqui: os valores já vêm
    todos prontos da avaliação, que usa a mesma missão/ferramentas do
    chat normal (agora também o próprio popup "Nova entrada" do Park In,
    pedido explícito do Rui, 2026-10-02). Nunca duplica: se este talão já
    tiver uma entrada registada, não faz nada (devolve ok=False)."""
    if talao in db.talaoes_parkin_existentes():
        return {"ok": False, "motivo": f"já existe uma entrada no Park In para o talão {talao}"}
    categoria = _categoria_de_indice(indice_igqc) if indice_igqc is not None else None
    id_gerado = db.guardar_entrada_parkin(
        talao=talao, fornecedor=fornecedor, data=_resolver_data(data_carga),
        tipo=tipo, comprimento=comprimento, espessura=espessura,
        peso_liquido_kg=float(peso_liquido_kg), matricula=matricula, guia_req=guia_req,
        peso_bruto_kg=peso_bruto_kg, tara_kg=tara_kg,
        indice_igqc=indice_igqc, categoria_qualidade=categoria, registado_por=registado_por)
    return {"ok": True, "id": id_gerado, "talao": talao, "categoria_qualidade": categoria}


_RE_PRODUTO_LINHA = re.compile(r"Produto:\**\s*([^\n]+)", re.IGNORECASE)
_RE_PESO_LIQUIDO_TEXTO = re.compile(r"Peso l[ií]quido:?\**\s*([\d.,]+)\s*k?g", re.IGNORECASE)
_RE_MATRICULA_TEXTO = re.compile(r"Matr[ií]cula:?\**\s*([A-Z0-9\-]+)", re.IGNORECASE)


def _parse_peso_kg(texto: str):
    """Lê um peso em kg de texto livre, tolerando tanto "11850 kg" como
    "13.000 kg" (separador de milhares em pt-PT) — nunca inventa um
    valor, devolve None se não encontrar nada parecido com um peso."""
    if not texto:
        return None
    m = re.search(r"([\d.,]+)\s*k?g", texto, re.IGNORECASE)
    if not m:
        return None
    bruto = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(bruto)
    except ValueError:
        return None


def _analisar_avaliacao_historica(avaliacao_texto: str) -> dict:
    """Tenta reconstruir os campos do Park In a partir do texto de uma
    avaliação de qualidade já guardada — usado só por
    importar_historico_avaliacoes, nunca para uma entrada nova (essa lê
    sempre a foto do talão diretamente, muito mais fiável). tipo/
    comprimento/espessura ficam None quando o texto não os preserva — a
    maioria dos casos antigos, porque o resumo de qualidade normalmente
    não guardava o código completo do produto do talão (ex: ficava só
    "Madeira Pinho 2,35" em vez de "006-IN - MADEIRA PINHO 2,35 16
    ACIMA") — nunca inventados aqui."""
    m_prod = _RE_PRODUTO_LINHA.search(avaliacao_texto)
    produto_linha = m_prod.group(1) if m_prod else ""
    interpretado = _interpretar_produto(produto_linha) if produto_linha else {
        "tipo": None, "comprimento": None, "espessura": None}

    m_peso = _RE_PESO_LIQUIDO_TEXTO.search(avaliacao_texto)
    peso_kg = _parse_peso_kg(m_peso.group(1)) if m_peso else None

    m_matricula = _RE_MATRICULA_TEXTO.search(avaliacao_texto)
    matricula = m_matricula.group(1) if m_matricula else None

    return {"tipo": interpretado["tipo"], "comprimento": interpretado["comprimento"],
           "espessura": interpretado["espessura"], "peso_kg": peso_kg, "matricula": matricula}


def importar_historico_avaliacoes(anos: list = None) -> dict:
    """Importa para o Park In todas as avaliações de qualidade já
    guardadas (ver agents/qualidade_toros_ecos_largos) — pedido explícito
    do Rui (2026-10-02): o stock não devia começar vazio, devia refletir
    logo todos os talões já dados à Alma. tipo/comprimento/espessura
    ficam "desconhecido" (ver _chave_artigo) quando o texto da avaliação
    não os preserva — nunca inventados; mesmo assim entram no stock total
    e nas categorias de qualidade, só não entram corretamente
    classificados em "stock por artigo" (decisão explícita do Rui,
    2026-10-02: confirmado contra os dados reais, isto acontece em ~95%
    dos registos antigos, porque o resumo de qualidade não preservava o
    código completo do produto do talão). Nunca duplica um talão já
    existente no Park In, por omissão procura em todos os anos desde
    2025."""
    anos = anos or list(range(2025, date.today().year + 1))
    ja_existentes = db.talaoes_parkin_existentes()
    processados_agora = set()
    importados, sem_peso, sem_qualidade, ja_existiam = [], [], [], []

    for ano in anos:
        for a in db.avaliacoes_cargas_toros_ano(ano):
            talao = (a.get("talao") or "").strip()
            if not talao:
                continue
            if talao in ja_existentes or talao in processados_agora:
                ja_existiam.append(talao)
                continue

            avaliacao_texto = a.get("avaliacao") or ""
            peso_kg = _parse_peso_kg(a.get("quantidade"))
            # pedido do Rui (2026-10-02): a avaliação já guarda o artigo
            # de forma estruturada desde que isto foi corrigido (ver
            # agents/qualidade_toros_ecos_largos) — usa sempre essas
            # colunas quando vierem preenchidas, só cai no parsing de
            # texto (menos fiável) para registos anteriores a essa
            # correção, onde ainda não existem.
            if a.get("tipo") and a.get("comprimento") is not None and a.get("espessura"):
                info = {"tipo": a["tipo"], "comprimento": a["comprimento"], "espessura": a["espessura"],
                       "peso_kg": None, "matricula": None}
            else:
                info = _analisar_avaliacao_historica(avaliacao_texto)
            if not peso_kg:
                peso_kg = info["peso_kg"]
            if not peso_kg:
                sem_peso.append(talao)
                continue

            indice = float(a["indice_igqc"]) if a.get("indice_igqc") is not None else None
            categoria = _categoria_de_indice(indice) if indice is not None else None
            if categoria is None:
                indice_texto, categoria = _indice_e_categoria_de_texto(avaliacao_texto)
                if indice is None and indice_texto is not None:
                    indice = indice_texto
                    db.definir_indice_igqc_avaliacao(a["id"], indice)
            if categoria is None:
                sem_qualidade.append(talao)
                continue

            try:
                data_resolvida = datetime.strptime((a.get("data_carga") or "").strip(), "%Y-%m-%d").date()
            except ValueError:
                data_resolvida = date.today()

            db.guardar_entrada_parkin(
                talao=talao, fornecedor=a.get("fornecedor") or "(fornecedor não identificado)",
                data=data_resolvida, tipo=info["tipo"], comprimento=info["comprimento"],
                espessura=info["espessura"], peso_liquido_kg=peso_kg, matricula=info["matricula"],
                indice_igqc=indice, categoria_qualidade=categoria, registado_por="importação do histórico")
            processados_agora.add(talao)
            importados.append(talao)

    return {"importados": len(importados), "sem_peso_legivel": sem_peso,
           "sem_qualidade_legivel": sem_qualidade, "ja_existiam": len(ja_existiam)}


def _entradas_com_saldo_do_artigo(tipo: str, comprimento: float, espessura: str,
                                  categoria_qualidade: str = None) -> list[dict]:
    """Entradas com saldo para o mesmo artigo que `_chave_artigo` usa para
    agrupar o "stock por artigo" (ver stock_por_artigo) — quando tipo ou
    comprimento vêm None ("N.D.", ver registar_correcao), isso é o próprio
    artigo "desconhecido" como um todo (db.entradas_parkin_desconhecidas_com_saldo),
    nunca uma correspondência exata que nunca bateria certo com NULL.
    `categoria_qualidade`, se vier preenchida, restringe às entradas dessa
    categoria (pedido explícito do Rui, 2026-10-09, para escolher de que
    categoria retirar numa correção negativa, em vez de ser sempre pela
    mais antiga de qualquer categoria)."""
    if _chave_artigo(tipo, comprimento, espessura) == _ARTIGO_DESCONHECIDO:
        entradas = db.entradas_parkin_desconhecidas_com_saldo()
        if categoria_qualidade:
            entradas = [e for e in entradas if e["categoria_qualidade"] == categoria_qualidade]
        return entradas
    return db.entradas_parkin_com_saldo(tipo, comprimento, espessura, categoria_qualidade)


def _saldo_disponivel(tipo: str, comprimento: float, espessura: str, categoria_qualidade: str = None) -> float:
    return sum(float(e["saldo_kg"])
              for e in _entradas_com_saldo_do_artigo(tipo, comprimento, espessura, categoria_qualidade))


def _criar_entrada_negativa(tipo: str, comprimento: float, espessura: str, categoria_qualidade: str,
                            quantidade_kg: float, correcao_id: int, registado_por: str = None) -> dict:
    """Cria a entrada "fantasma" que representa a parte de uma correção
    negativa que excedeu o stock real disponível — pedido explícito do
    Rui (2026-10-09): "fazendo correções tem de deixar ir a negativo", em
    vez de bloquear com um erro quando o artigo (ou a categoria escolhida)
    não tem o suficiente. Mesma lógica da entrada que já se cria para uma
    correção POSITIVA (ver registar_correcao) — só que aqui com peso
    negativo, para o saldo do artigo no dashboard refletir mesmo o
    défice. Usa o mesmo prefixo "CORR-" no talão para ficar sempre fora
    das listagens de Top Entradas/Top Qualidade/listar_movimentos, tal
    como as outras entradas vindas de correções."""
    entrada_id = db.guardar_entrada_parkin(
        talao=f"CORR-{correcao_id}", fornecedor="(correção de inventário)", data=date.today(),
        tipo=tipo, comprimento=comprimento, espessura=espessura, peso_liquido_kg=-quantidade_kg,
        categoria_qualidade=categoria_qualidade, registado_por=registado_por)
    return {"entrada_id": entrada_id, "talao": f"CORR-{correcao_id}", "quantidade_kg": round(quantidade_kg, 1),
           "categoria_qualidade": categoria_qualidade, "negativo": True}


def _aplicar_fifo(tipo: str, comprimento: float, espessura: str, quantidade_kg: float,
                  saida_id: int = None, correcao_id: int = None, categoria_qualidade: str = None,
                  permitir_negativo: bool = False, registado_por: str = None) -> dict:
    """Deplete `quantidade_kg` das entradas mais antigas deste artigo
    exato com saldo (FIFO), gravando cada depleção para auditoria (ver
    db.guardar_depletion_parkin). Assume que o chamador já confirmou que
    há saldo suficiente (ver _saldo_disponivel) — mesmo assim devolve
    {"erro": ...} sem tocar em nada se não houver, como rede de segurança,
    A MENOS que `permitir_negativo` venha True (só as correções, nunca as
    saídas — pedido explícito do Rui, 2026-10-09): nesse caso, deplete o
    que houver e cria uma entrada negativa para o resto (ver
    _criar_entrada_negativa), em vez de falhar.
    `categoria_qualidade` restringe a depleção só a essa categoria (ver
    _entradas_com_saldo_do_artigo)."""
    entradas = _entradas_com_saldo_do_artigo(tipo, comprimento, espessura, categoria_qualidade)
    disponivel = sum(float(e["saldo_kg"]) for e in entradas)
    if disponivel + 1e-6 < quantidade_kg and not permitir_negativo:
        rotulo = "artigo desconhecido" if _chave_artigo(tipo, comprimento, espessura) == _ARTIGO_DESCONHECIDO \
            else f"artigo {tipo} {comprimento} {espessura}"
        if categoria_qualidade:
            rotulo += f" (categoria {categoria_qualidade})"
        return {"erro": (f"só há {disponivel:.0f} kg em stock para o {rotulo} "
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
    if restante > 1e-9:
        depletadas.append(_criar_entrada_negativa(tipo, comprimento, espessura, categoria_qualidade,
                                                   restante, correcao_id, registado_por))
    return {"depletadas": depletadas}


def _aplicar_fifo_com_preferencia(entradas: list[dict], quantidade_kg: float, categoria_preferida: str,
                                  tipo: str, comprimento: float, espessura: str,
                                  saida_id: int = None, correcao_id: int = None,
                                  registado_por: str = None) -> dict:
    """Deplete `quantidade_kg` de `entradas` (já com saldo, de um artigo),
    preferindo a categoria `categoria_preferida` — pedido explícito do
    Rui (2026-10-09): tira o máximo possível dessa categoria primeiro; o
    que faltar reparte pelas OUTRAS categorias com saldo, proporcional ao
    que cada uma tem (nunca em partes iguais — uma categoria com o dobro
    do saldo de outra perde o dobro). Nunca falha uma correção: se mesmo
    assim faltar (o artigo, no total, não tem o suficiente), o resto fica
    a descoberto na própria categoria preferida — ver _criar_entrada_
    negativa (pedido explícito do Rui, 2026-10-09: "fazendo correções tem
    de deixar ir a negativo"). Dentro de cada categoria, continua a tirar
    sempre da entrada mais antiga primeiro (FIFO) — `entradas` já vem
    ordenada por data, por isso cada sub-lista por categoria também fica
    ordenada, sem precisar de reordenar."""
    por_categoria = {}
    for e in entradas:
        por_categoria.setdefault(e["categoria_qualidade"], []).append(e)

    alvo = {}
    preferida = por_categoria.get(categoria_preferida, [])
    saldo_preferida = sum(float(e["saldo_kg"]) for e in preferida)
    tirar_preferida = min(saldo_preferida, quantidade_kg)
    if tirar_preferida > 1e-9:
        alvo[categoria_preferida] = tirar_preferida

    restante = quantidade_kg - tirar_preferida
    if restante > 1e-9:
        outras = {c: sum(float(e["saldo_kg"]) for e in lst) for c, lst in por_categoria.items()
                 if c != categoria_preferida}
        soma_outras = sum(outras.values())
        for c, saldo_c in outras.items():
            if saldo_c > 1e-9:
                alvo[c] = restante * (saldo_c / soma_outras)

    depletadas = []
    for categoria, quantidade in alvo.items():
        restante_cat = quantidade
        for e in por_categoria[categoria]:
            if restante_cat <= 1e-9:
                break
            tirar = min(float(e["saldo_kg"]), restante_cat)
            db.descontar_saldo_entrada_parkin(e["id"], tirar)
            db.guardar_depletion_parkin(e["id"], tirar, saida_id=saida_id, correcao_id=correcao_id)
            depletadas.append({"entrada_id": e["id"], "talao": e["talao"], "quantidade_kg": round(tirar, 1),
                              "categoria_qualidade": categoria})
            restante_cat -= tirar

    faltante = quantidade_kg - sum(alvo.values())
    if faltante > 1e-9:
        depletadas.append(_criar_entrada_negativa(tipo, comprimento, espessura, categoria_preferida,
                                                   faltante, correcao_id, registado_por))
    return {"depletadas": depletadas}


def registar_saida(bruto: bytes, content_type: str, data_saida: str = None, registado_por: str = None) -> dict:
    """Regista uma saída (consumo) a partir da foto de um talão — extrai o
    artigo e a quantidade, e deplete por FIFO as entradas mais antigas
    desse artigo com saldo.

    `data_saida` ("AAAA-MM-DD") é opcional e só serve para FORÇAR o dia do
    movimento (pedido explícito do Rui, 2026-10-07: "por defeito o dia
    será sempre o dia em que estamos a fazer esse movimento... mas caso
    seja necessário... poder colocar a data certa", ex: o talão só chegou
    com um dia de atraso) — por omissão (None/vazio) usa-se sempre a data
    do próprio talão fotografado, tal como já acontecia (ver
    _resolver_data), nunca "hoje" à partida."""
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

    data_resolvida = _resolver_data(data_saida or campos.get("data"))
    saida_id = db.guardar_saida_parkin(tipo, comprimento, espessura, quantidade, data_resolvida,
                                       talao=campos.get("talao"), registado_por=registado_por)
    resultado = _aplicar_fifo(tipo, comprimento, espessura, quantidade, saida_id=saida_id)
    return {"ok": True, "id": saida_id, "artigo": _chave_artigo(tipo, comprimento, espessura),
           "quantidade_kg": quantidade, **resultado}


def info_entrada_por_talao(talao: str) -> dict:
    """Informação do lote (entrada) com este nº de talão — pré-visualização
    antes de confirmar uma saída "só pelo número" (ver
    registar_saida_por_talao), para o Rui ver logo que é mesmo a carga
    certa (fornecedor, artigo, saldo atual) antes de guardar."""
    talao = (talao or "").strip()
    if not talao:
        return {"erro": "o número do talão é obrigatório"}
    entrada = db.entrada_parkin_por_talao(talao)
    if not entrada:
        return {"erro": f"não encontrei nenhuma entrada no Park In com o talão {talao!r}"}
    comprimento = float(entrada["comprimento"]) if entrada["comprimento"] is not None else None
    return {
        "talao": entrada["talao"], "fornecedor": entrada["fornecedor"],
        "tipo": entrada["tipo"], "comprimento": comprimento, "espessura": entrada["espessura"],
        "artigo": _chave_artigo(entrada["tipo"], comprimento, entrada["espessura"]),
        "saldo_kg": round(float(entrada["saldo_kg"] or 0), 1),
    }


def registar_saida_por_talao(talao: str, quantidade_kg: float = None, data_saida: str = None,
                             registado_por: str = None) -> dict:
    """Saída identificada só pelo nº de talão do LOTE de entrada que está a
    sair (pedido explícito do Rui, 2026-10-06, alternativa a fotografar um
    novo talão de consumo) — tipo, comprimento, espessura e saldo vêm
    todos da própria entrada já registada com esse talão (ver
    db.entrada_parkin_por_talao), nunca pedidos outra vez à mão.

    Desconta sempre exatamente ESSE lote (nunca o FIFO genérico do artigo,
    ver _aplicar_fifo) — é precisamente a carga identificada pelo talão,
    não "uma qualquer com o mesmo tipo/comprimento/espessura".
    `quantidade_kg` é opcional: por omissão sai o saldo inteiro do lote —
    o pedido foi literalmente "saber só pelo número... calcular
    automaticamente quanto está a sair", sem indicar peso nenhum à mão; se
    vier preenchida, sai só essa parte, desde que não exceda o saldo.

    `data_saida` ("AAAA-MM-DD") também é opcional: por omissão é sempre
    hoje, o dia do movimento (pedido explícito do Rui, 2026-10-07: "o
    normal é não definir data... é no dia em que estamos que está a ser
    feito corretamente") — só se define à mão quando o talão chega
    atrasado ou foi esquecido."""
    talao = (talao or "").strip()
    if not talao:
        return {"erro": "o número do talão é obrigatório"}
    entrada = db.entrada_parkin_por_talao(talao)
    if not entrada:
        return {"erro": f"não encontrei nenhuma entrada no Park In com o talão {talao!r}"}

    saldo = float(entrada["saldo_kg"] or 0)
    if saldo <= 1e-6:
        return {"erro": f"o lote do talão {talao} já está esgotado (saldo 0 kg)"}

    if quantidade_kg is None:
        quantidade = saldo
    else:
        quantidade = float(quantidade_kg)
        if quantidade <= 0:
            return {"erro": "a quantidade tem de ser maior que zero"}
        if quantidade > saldo + 1e-6:
            return {"erro": (f"o lote do talão {talao} só tem {saldo:.0f} kg de saldo "
                             f"— não é possível tirar {quantidade:.0f} kg")}

    if data_saida:
        try:
            data_resolvida = date.fromisoformat(data_saida)
        except ValueError:
            return {"erro": "data de saída inválida — tem de ser AAAA-MM-DD"}
    else:
        data_resolvida = date.today()
    saida_id = db.guardar_saida_parkin(entrada["tipo"], entrada["comprimento"], entrada["espessura"],
                                       quantidade, data_resolvida, talao=talao, registado_por=registado_por)
    db.descontar_saldo_entrada_parkin(entrada["id"], quantidade)
    db.guardar_depletion_parkin(entrada["id"], quantidade, saida_id=saida_id)
    return {"ok": True, "id": saida_id,
           "artigo": _chave_artigo(entrada["tipo"], entrada["comprimento"], entrada["espessura"]),
           "quantidade_kg": round(quantidade, 1), "fornecedor": entrada["fornecedor"],
           "depletadas": [{"entrada_id": entrada["id"], "talao": entrada["talao"],
                           "quantidade_kg": round(quantidade, 1)}]}


def registar_correcao(tipo: str, comprimento: float, espessura: str, quantidade_kg: float, motivo: str,
                      categoria_qualidade: str = None, registado_por: str = None) -> dict:
    """Lançamento manual de uma correção de stock — `quantidade_kg` com
    sinal: positivo é uma sobra/ajuste de inventário (pede
    `categoria_qualidade` à mão, porque não vem de nenhuma entrada);
    negativo é uma remoção, depletada por FIFO como uma saída normal.

    `tipo` e `comprimento` podem vir `None` ("N.D." — não definido, pedido
    explícito do Rui, 2026-10-06): não sabemos o artigo exato, por isso a
    correção sai/entra no "Artigo desconhecido" do dashboard (ver
    _chave_artigo/stock_por_artigo) em vez de um artigo específico — ex:
    -3000 kg sem tipo nem comprimento remove 3000 kg do stock
    "desconhecido" (as entradas mais antigas sem essa informação, ver
    _entradas_com_saldo_do_artigo), nunca de um artigo real."""
    if tipo is not None and tipo not in TIPOS_VALIDOS:
        return {"erro": f"tipo inválido: {tipo!r} — tem de ser N.D. ou um de {TIPOS_VALIDOS}"}
    if espessura is not None and espessura not in ESPESSURAS_VALIDAS:
        return {"erro": f"espessura inválida: {espessura!r} — tem de ser uma de {ESPESSURAS_VALIDAS}"}
    if categoria_qualidade is not None and categoria_qualidade not in CATEGORIAS_QUALIDADE:
        return {"erro": f"categoria inválida: {categoria_qualidade!r} — tem de ser N.D. ou uma de {CATEGORIAS_QUALIDADE}"}
    if not quantidade_kg:
        return {"erro": "a quantidade não pode ser zero"}
    if not motivo or not motivo.strip():
        return {"erro": "o motivo é obrigatório"}

    artigo = _chave_artigo(tipo, comprimento, espessura)
    data_hoje = date.today()
    if quantidade_kg > 0:
        id_gerado = db.guardar_correcao_parkin(tipo, comprimento, espessura, quantidade_kg, motivo, data_hoje,
                                               categoria_qualidade=categoria_qualidade, registado_por=registado_por)
        # Bug real reportado pelo Rui (2026-10-08): sem uma entrada a
        # suportar esta sobra, ela entrava no stock do dashboard mas nunca
        # mais podia ser retirada (nem por saída nem por correção negativa
        # — ambas só descontam o saldo_kg de uma entrada). Cria sempre essa
        # entrada agora, com um "talão" próprio só para a identificar como
        # vinda de uma correção, nunca de um talão real.
        entrada_id = db.guardar_entrada_parkin(
            talao=f"CORR-{id_gerado}", fornecedor="(correção de inventário)", data=data_hoje,
            tipo=tipo, comprimento=comprimento, espessura=espessura, peso_liquido_kg=quantidade_kg,
            categoria_qualidade=categoria_qualidade, registado_por=registado_por)
        db.ligar_entrada_a_correcao(id_gerado, entrada_id)
        return {"ok": True, "id": id_gerado, "artigo": artigo, "quantidade_kg": quantidade_kg,
               "categoria_qualidade": categoria_qualidade}

    # categoria_qualidade aqui é uma PREFERÊNCIA, não um filtro rígido
    # (pedido explícito do Rui, 2026-10-09): tira o máximo possível dessa
    # categoria primeiro; se não chegar (ou não tiver nada), reparte o
    # que falta pelas outras categorias com saldo, proporcional ao que
    # cada uma tem. N.D. (None) continua sem preferência nenhuma, sempre
    # pela entrada mais antiga de qualquer categoria (ver _aplicar_fifo).
    # Nunca falha por falta de stock — pedido explícito do Rui (2026-10-09):
    # "fazendo correções tem de deixar ir a negativo" — o que não houver em
    # stock real fica como défice (ver _criar_entrada_negativa).
    entradas_tudo = _entradas_com_saldo_do_artigo(tipo, comprimento, espessura)
    id_gerado = db.guardar_correcao_parkin(tipo, comprimento, espessura, quantidade_kg, motivo, data_hoje,
                                           registado_por=registado_por)
    if categoria_qualidade:
        resultado = _aplicar_fifo_com_preferencia(entradas_tudo, abs(quantidade_kg), categoria_qualidade,
                                                  tipo, comprimento, espessura,
                                                  correcao_id=id_gerado, registado_por=registado_por)
    else:
        resultado = _aplicar_fifo(tipo, comprimento, espessura, abs(quantidade_kg), correcao_id=id_gerado,
                                  permitir_negativo=True, registado_por=registado_por)
    return {"ok": True, "id": id_gerado, "artigo": artigo, "quantidade_kg": quantidade_kg,
           "categoria_qualidade": categoria_qualidade, **resultado}


def stock_total() -> dict:
    """Stock total atual, por categoria de qualidade — para a barra
    empilhada principal do dashboard. Inclui o saldo das entradas e as
    correções positivas; as negativas já estão refletidas no saldo das
    entradas que depletaram (ver _aplicar_fifo). Só ignora saldo
    praticamente zero — um saldo negativo (défice de uma correção que foi
    além do stock real, pedido explícito do Rui, 2026-10-09) tem de
    continuar a contar, para o total refletir mesmo o défice em vez de o
    esconder."""
    por_categoria = {c: 0.0 for c in CATEGORIAS_QUALIDADE}
    sem_categoria = 0.0
    for e in db.entradas_parkin_todas():
        saldo = float(e["saldo_kg"] or 0)
        if abs(saldo) <= 1e-6:
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
    artigo" do dashboard, ordenadas da maior para a menor quantidade. Um
    artigo com saldo negativo (défice de uma correção, pedido explícito
    do Rui, 2026-10-09) continua a aparecer, nunca é escondido — só se
    ignora um artigo cujo total fique mesmo a zero."""
    artigos = {}

    def _bucket(chave, tipo, comprimento, espessura):
        return artigos.setdefault(chave, {
            "tipo": tipo, "comprimento": comprimento, "espessura": espessura,
            "por_categoria_kg": {c: 0.0 for c in CATEGORIAS_QUALIDADE}, "sem_categoria_kg": 0.0,
        })

    for e in db.entradas_parkin_todas():
        saldo = float(e["saldo_kg"] or 0)
        if abs(saldo) <= 1e-6:
            continue
        comprimento = float(e["comprimento"]) if e["comprimento"] is not None else None
        chave = _chave_artigo(e["tipo"], comprimento, e["espessura"])
        a = _bucket(chave, e["tipo"], comprimento, e["espessura"])
        if e["categoria_qualidade"] in a["por_categoria_kg"]:
            a["por_categoria_kg"][e["categoria_qualidade"]] += saldo
        else:
            a["sem_categoria_kg"] += saldo

    for c in db.correcoes_parkin_positivas():
        comprimento = float(c["comprimento"]) if c["comprimento"] is not None else None
        chave = _chave_artigo(c["tipo"], comprimento, c["espessura"])
        a = _bucket(chave, c["tipo"], comprimento, c["espessura"])
        if c["categoria_qualidade"] in a["por_categoria_kg"]:
            a["por_categoria_kg"][c["categoria_qualidade"]] += float(c["quantidade_kg"])

    limites = db.limites_parkin()
    resultado = []
    for chave, a in artigos.items():
        total = sum(a["por_categoria_kg"].values()) + a["sem_categoria_kg"]
        if abs(total) <= 1e-6:
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


_RE_CODIGO_FORNECEDOR = re.compile(r"^\s*\d+\s*[-–]\s*")


def _normalizar_fornecedor(nome: str) -> str:
    """Chave de agrupamento tolerante a diferenças de como o mesmo
    fornecedor foi escrito ao longo do tempo (com/sem código à frente,
    maiúsculas, acentos) — ex: "018 - UNIMADEIRAS" e "Unimadeiras" têm de
    contar como o mesmo fornecedor. Só para AGRUPAR, nunca para mostrar —
    ver top_qualidade/top_entradas, que escolhem sempre o nome mais
    usado para mostrar."""
    sem_codigo = _RE_CODIGO_FORNECEDOR.sub("", nome or "")
    sem_acentos = unicodedata.normalize("NFKD", sem_codigo).encode("ascii", "ignore").decode()
    return sem_acentos.strip().lower()


def _agrupar_por_fornecedor(linhas: list, campo: str) -> dict:
    """Agrupa `linhas` (cada uma com "fornecedor" e `campo`) pelo nome
    normalizado — devolve {chave: {"nome": nome_mais_frequente,
    "valores": [...]}}."""
    grupos = {}
    for l in linhas:
        chave = _normalizar_fornecedor(l["fornecedor"])
        g = grupos.setdefault(chave, {"nomes": {}, "valores": []})
        g["nomes"][l["fornecedor"]] = g["nomes"].get(l["fornecedor"], 0) + 1
        g["valores"].append(l[campo])
    return grupos


def top_qualidade(limite: int = 3) -> dict:
    """Média do índice IGQC por fornecedor (só entradas com índice
    conhecido), agrupado por nome normalizado (ver _normalizar_fornecedor)
    — {"melhores": [...], "piores": [...]}. Com poucos fornecedores no
    total, os dois grupos podem repetir nomes — é o retrato real, não um
    bug."""
    grupos = _agrupar_por_fornecedor(db.entradas_parkin_qualidade_e_fornecedor(), "indice_igqc")
    linhas = []
    for g in grupos.values():
        nome = max(g["nomes"], key=g["nomes"].get)
        media = sum(g["valores"]) / len(g["valores"])
        linhas.append({"fornecedor": nome, "media": round(media, 1), "n": len(g["valores"])})
    linhas.sort(key=lambda x: -x["media"])
    return {"melhores": linhas[:limite], "piores": list(reversed(linhas[-limite:]))}


def top_entradas(dias: int = 30, limite: int = 5) -> list[dict]:
    """Fornecedores com mais entregas nos últimos `dias` dias, agrupado
    por nome normalizado — quantidade total e nº de entregas, ordenado
    por quantidade desc."""
    grupos = _agrupar_por_fornecedor(db.entradas_parkin_desde(dias), "peso_liquido_kg")
    linhas = []
    for g in grupos.values():
        nome = max(g["nomes"], key=g["nomes"].get)
        linhas.append({"fornecedor": nome, "entregas": len(g["valores"]), "total_kg": round(sum(g["valores"]), 1)})
    linhas.sort(key=lambda x: -x["total_kg"])
    return linhas[:limite]


def dados_dashboard(dias_top_entradas: int = 30) -> dict:
    return {
        "stock_total": stock_total(),
        "stock_por_artigo": stock_por_artigo(),
        "top_qualidade": top_qualidade(3),
        "top_entradas": top_entradas(dias_top_entradas, 5),
    }


def listar_movimentos(dias: int = 30) -> list[dict]:
    """Lista cronológica (mais recente primeiro) de todos os movimentos de
    stock do Park In nos últimos `dias` dias — entradas, saídas e
    correções — pedido explícito do Rui (2026-10-09): "uma listagem com os
    movimentos que foram feitos no Park In", para a Alma poder responder
    isto diretamente em vez de tentar (sem sucesso) ler a página através
    de pesquisa/leitura genérica da internet, que não funciona numa
    aplicação dinâmica como esta.

    Uma correção positiva já aparece aqui como a sua própria entrada (ver
    registar_correcao) — por isso ignora sempre correções com `entrada_id`
    preenchido, para nunca listar o mesmo movimento duas vezes."""
    movimentos = []
    for e in db.entradas_parkin_movimentos_desde(dias):
        if (e["talao"] or "").startswith("CORR-"):
            continue
        comprimento = float(e["comprimento"]) if e["comprimento"] is not None else None
        movimentos.append({
            "data": str(e["data"]), "tipo_movimento": "entrada",
            "artigo": _chave_artigo(e["tipo"], comprimento, e["espessura"]),
            "quantidade_kg": float(e["peso_liquido_kg"]),
            "fornecedor": e["fornecedor"], "talao": e["talao"],
        })
    for s in db.saidas_parkin_desde(dias):
        comprimento = float(s["comprimento"]) if s["comprimento"] is not None else None
        movimentos.append({
            "data": str(s["data"]), "tipo_movimento": "saida",
            "artigo": _chave_artigo(s["tipo"], comprimento, s["espessura"]),
            "quantidade_kg": -float(s["quantidade_kg"]),
            "talao": s["talao"],
        })
    for c in db.correcoes_parkin_desde(dias):
        if c["entrada_id"] is not None:
            continue
        comprimento = float(c["comprimento"]) if c["comprimento"] is not None else None
        movimentos.append({
            "data": str(c["data"]), "tipo_movimento": "correcao",
            "artigo": _chave_artigo(c["tipo"], comprimento, c["espessura"]),
            "quantidade_kg": float(c["quantidade_kg"]),
            "motivo": c["motivo"],
        })
    movimentos.sort(key=lambda m: m["data"], reverse=True)
    return movimentos


def definir_limite(chave: str, minimo_kg: float = None, maximo_kg: float = None) -> dict:
    db.definir_limite_parkin(chave, minimo_kg, maximo_kg)
    return {"ok": True}


# Ferramentas do Park In para a Alma (pedido explícito do Rui, 2026-10-09):
# a página é dinâmica, não indexada, e o teu tool genérico de internet
# (web_fetch/web_search) não a consegue ler — usa sempre estas, nunca
# tentes ler https://alma-ia.up.railway.app/park-in pela internet.
TOOLS_PARK_IN = [
    {
        "name": "stock_atual_parkin",
        "description": "Stock atual de toros do Park In (Ecos Largos) — total, por categoria de qualidade e por artigo (tipo/comprimento/espessura), mais os fornecedores com melhor/pior qualidade média e com mais entregas recentes. Usa isto sempre que perguntarem pelo stock/quantidade atual de toros, nunca tentes ler a página do Park In pela internet (é uma aplicação dinâmica, não indexada — isso nunca funciona).",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "listar_movimentos_parkin",
        "description": "Lista cronológica (mais recente primeiro) dos movimentos de stock do Park In — entradas, saídas e correções, cada um com data, artigo e quantidade (kg, negativo numa saída/correção de remoção). Usa isto sempre que pedirem os movimentos/histórico do Park In num período, nunca tentes ler a página pela internet.",
        "input_schema": {
            "type": "object",
            "properties": {"dias": {"type": "integer", "description": "Quantos dias para trás — por omissão 30"}}
        }
    }
]


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

<div class="sheet" id="sheetEntrada" style="width:min(560px,calc(100vw - 32px));display:flex;flex-direction:column;max-height:min(680px,calc(100vh - 32px))">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Nova entrada</h3>
  <p class="sub" style="margin:0 0 10px">É mesmo a Alma, tal como no chat — anexa a foto do talão de pesagem e da própria carga (uma de cada vez ou várias juntas) e envia.</p>
  <div id="entradaChat" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;margin-bottom:10px;min-height:100px"></div>
  <div id="entradaAnexos" style="display:flex;flex-wrap:wrap;gap:6px"></div>
  <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
    <input type="file" id="fEntradaFotos" accept="image/*" multiple style="display:none">
    <input type="file" id="fEntradaCamera" accept="image/*" capture="environment" style="display:none">
    <button class="btn" type="button" id="bEntradaAnexar" title="Escolher fotos" style="padding:8px 12px">📎</button>
    <button class="btn" type="button" id="bEntradaCamera" title="Tirar foto" style="padding:8px 12px">📷</button>
    <input type="text" id="entradaTexto" placeholder="(opcional) alguma nota…" style="flex:1;min-width:0">
    <button class="btn primary" id="bEntradaGuardar">Enviar</button>
  </div>
  <div id="entradaMsg"></div>
</div>

<div class="sheet" id="sheetSaida">
  <button class="close" data-close aria-label="Fechar">×</button>
  <h3>Nova saída</h3>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn primary" type="button" id="bSaidaModoFoto">Foto do talão</button>
    <button class="btn" type="button" id="bSaidaModoTalao">Nº do talão</button>
  </div>
  <!-- data do movimento opcional (pedido explícito do Rui, 2026-10-07):
       por omissão fica em branco = hoje, o dia em que o movimento está a
       ser feito; só se preenche quando o talão chega atrasado ou foi
       esquecido. -->
  <div class="frow"><label>Data de saída</label>
    <input type="date" id="cSaidaData">
  </div>
  <p class="sub" style="margin:2px 0 12px">Deixa em branco para usar a data do talão (foto) ou hoje (nº do talão) — só preenche para forçar outro dia (ex: talão chegado atrasado).</p>
  <div id="saidaFotoBloco">
    <p class="sub" style="margin:0 0 12px">Fotos dos talões de consumo — escolhe várias de uma vez.</p>
    <input type="file" id="fSaidaFotos" accept="image/*" multiple>
  </div>
  <div id="saidaTalaoBloco" style="display:none">
    <p class="sub" style="margin:0 0 12px">Escreve o nº do talão do lote que está a sair — o artigo e o saldo vêm automaticamente desse lote, sem foto nova.</p>
    <div class="frow"><label>Nº do talão</label>
      <input type="text" id="cSaidaTalao" placeholder="ex: 11293">
    </div>
    <div id="saidaTalaoInfo" class="sub" style="margin:2px 0 10px"></div>
    <div class="frow"><label>Quantidade (kg)</label>
      <input type="text" inputmode="decimal" id="cSaidaQtd" placeholder="deixa vazio para sair o lote todo">
    </div>
  </div>
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
    <select id="cTipo"><option value="IN">IN</option><option value="MT">MT</option>
      <option value="">N.D. — não definido</option></select>
  </div>
  <div class="frow"><label>Comprimento (m)</label>
    <input type="text" inputmode="decimal" id="cComprimento" placeholder="ex: 2,35 — ou N.D.">
  </div>
  <div class="frow"><label>Espessura</label>
    <select id="cEspessura"><option value="normal">Normal</option><option value="fina">Fina</option></select>
  </div>
  <div class="frow"><label>Quantidade (kg)</label>
    <input type="text" inputmode="decimal" id="cQuantidade" placeholder="+ adiciona, − remove">
  </div>
  <div class="frow" id="cCategoriaRow"><label>Categoria</label>
    <select id="cCategoria"><option value="Boa">Boa</option><option value="Media">Média</option><option value="Fraca">Fraca</option><option value="">N.D. — não definido</option></select>
  </div>
  <p class="sub" id="cCategoriaSub" style="margin:2px 0 12px"></p>
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

function renderGauge(porCategoria, semCategoria, totalKg, minimo, maximo, escalaForcada){
  if(totalKg<=0) return "";
  const escala = escalaForcada || (Math.max(totalKg, maximo||0, 1) * 1.15);
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
  // Escala PARTILHADA por todas as barras (pedido explícito do Rui,
  // 2026-10-09: "as linhas das cores deviam estar... maiores segundo a
  // quantidade que cada artigo tem" — cada renderGauge calculava a sua
  // própria escala a partir do seu próprio total, por isso todas ficavam
  // à mesma largura (~87%) independentemente da quantidade real. Usando
  // o maior total/máximo da lista para todas, o comprimento de cada
  // barra passa a refletir mesmo a quantidade relativa entre artigos.
  const escalaGlobal = Math.max(...lista.map(a => Math.max(a.total_kg, a.maximo_kg || 0)), 1) * 1.15;
  el.innerHTML = lista.map(a => `
    <div class="artigo-row">
      <div class="titulo"><span>${a.artigo==="desconhecido" ? "Artigo desconhecido (histórico sem essa informação)"
        : `${a.tipo} · ${a.comprimento.toFixed(2)}m · ${a.espessura==="fina"?"Fina":"Normal"}`}</span>
        <span class="tot mono">${t(a.total_kg)}</span></div>
      ${renderGauge(a.por_categoria_kg, a.sem_categoria_kg, a.total_kg, a.minimo_kg, a.maximo_kg, escalaGlobal)}
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

let entradaAnexos = [];
let entradaSessao = null;
function renderEntradaAnexos(){
  $("#entradaAnexos").innerHTML = "";
  entradaAnexos.forEach((f, i) => {
    const chip = document.createElement("div");
    chip.style.cssText = "display:flex;align-items:center;gap:4px;background:#f1f1f1;border-radius:14px;padding:4px 8px;font-size:13px";
    chip.innerHTML = `<span>📎 ${f.name}</span>`;
    const rm = document.createElement("button");
    rm.type = "button"; rm.textContent = "✕"; rm.style.cssText = "border:none;background:none;cursor:pointer";
    rm.onclick = () => { entradaAnexos.splice(i,1); renderEntradaAnexos(); };
    chip.appendChild(rm);
    $("#entradaAnexos").appendChild(chip);
  });
}
function receberFotosEntrada(lista, input){
  for(const f of lista) entradaAnexos.push(f);
  input.value = "";
  renderEntradaAnexos();
}
$("#bEntradaAnexar").onclick = () => $("#fEntradaFotos").click();
$("#bEntradaCamera").onclick = () => $("#fEntradaCamera").click();
$("#fEntradaFotos").addEventListener("change", () => receberFotosEntrada($("#fEntradaFotos").files, $("#fEntradaFotos")));
$("#fEntradaCamera").addEventListener("change", () => receberFotosEntrada($("#fEntradaCamera").files, $("#fEntradaCamera")));

function bolhaEntrada(papel){
  const b = document.createElement("div");
  b.style.cssText = papel === "user"
    ? "align-self:flex-end;background:#eef2ff;border-radius:12px;padding:8px 12px;max-width:85%;white-space:pre-wrap"
    : "align-self:flex-start;background:#f5f5f5;border-radius:12px;padding:8px 12px;max-width:95%;white-space:pre-wrap";
  $("#entradaChat").appendChild(b);
  $("#entradaChat").scrollTop = $("#entradaChat").scrollHeight;
  return b;
}

$("#bAbrirEntrada").onclick = () => {
  $("#entradaMsg").innerHTML = ""; $("#entradaChat").innerHTML = ""; $("#entradaTexto").value = "";
  entradaAnexos = []; entradaSessao = "park-in-" + Date.now().toString(36) + Math.random().toString(36).slice(2,8);
  renderEntradaAnexos();
  abrirSheet("#sheetEntrada");
};
let saidaModo = "foto";
function definirSaidaModo(modo){
  saidaModo = modo;
  $("#bSaidaModoFoto").className = "btn" + (modo==="foto" ? " primary" : "");
  $("#bSaidaModoTalao").className = "btn" + (modo==="talao" ? " primary" : "");
  $("#saidaFotoBloco").style.display = modo==="foto" ? "" : "none";
  $("#saidaTalaoBloco").style.display = modo==="talao" ? "" : "none";
}
$("#bSaidaModoFoto").onclick = () => definirSaidaModo("foto");
$("#bSaidaModoTalao").onclick = () => definirSaidaModo("talao");

function rotuloArtigo(info){
  return info.artigo==="desconhecido" ? "Artigo desconhecido"
    : `${info.tipo} · ${info.comprimento.toFixed(2)}m · ${info.espessura==="fina"?"Fina":"Normal"}`;
}
// pré-visualização automática ao escrever o nº do talão (pedido explícito
// do Rui, 2026-10-06: "ir buscar a informação sobre essa carga") — o Rui
// vê logo o artigo/fornecedor/saldo encontrados, antes de confirmar.
let saidaTalaoDebounce = null;
$("#cSaidaTalao").addEventListener("input", () => {
  clearTimeout(saidaTalaoDebounce);
  const talao = $("#cSaidaTalao").value.trim();
  if(!talao){ $("#saidaTalaoInfo").innerHTML = ""; return; }
  saidaTalaoDebounce = setTimeout(async () => {
    try{
      const r = await fetch("/park-in/entrada-por-talao?talao=" + encodeURIComponent(talao));
      const j = await r.json();
      if(j.erro){ $("#saidaTalaoInfo").innerHTML = `<span class="err">${j.erro}</span>`; return; }
      $("#saidaTalaoInfo").innerHTML =
        `${j.fornecedor} · ${rotuloArtigo(j)} · saldo atual: ${t(j.saldo_kg)}`;
      $("#cSaidaQtd").placeholder = `deixa vazio para sair o lote todo (${t(j.saldo_kg)})`;
    } catch(e){ $("#saidaTalaoInfo").innerHTML = `<span class="err">Falhou: ${e}</span>`; }
  }, 400);
});

$("#bAbrirSaida").onclick = () => {
  $("#saidaMsg").innerHTML=""; $("#fSaidaFotos").value=""; $("#cSaidaData").value="";
  $("#cSaidaTalao").value=""; $("#cSaidaQtd").value=""; $("#saidaTalaoInfo").innerHTML="";
  definirSaidaModo("foto");
  abrirSheet("#sheetSaida");
};
$("#bAbrirCorrecao").onclick = () => { $("#correcaoMsg").innerHTML=""; abrirSheet("#sheetCorrecao"); };
$("#bAbrirLimites").onclick = () => { renderLimites(); abrirSheet("#sheetLimites"); };

function atualizarSubCategoria(){
  const v = parseFloat($("#cQuantidade").value.replace(",","."));
  $("#cCategoriaSub").textContent = (v<0)
    ? "Numa remoção: N.D. tira da mais antiga de qualquer categoria; escolhendo uma, prefere essa, e só reparte pelas outras (proporcional ao que têm) se não chegar."
    : "";
}
$("#cQuantidade").addEventListener("input", atualizarSubCategoria);
atualizarSubCategoria();

$("#bEntradaGuardar").onclick = async () => {
  if(!entradaAnexos.length){ $("#entradaMsg").innerHTML = '<div class="err">Anexa pelo menos a foto do talão.</div>'; return; }
  const texto = $("#entradaTexto").value;
  $("#entradaMsg").innerHTML = "";
  $("#bEntradaGuardar").disabled = true;

  const userBolha = bolhaEntrada("user");
  userBolha.textContent = (texto ? texto + "\n" : "") + entradaAnexos.map(f => "📎 " + f.name).join("\n");

  const fd = new FormData();
  fd.append("mensagem", texto); fd.append("sessao", entradaSessao);
  for(const f of entradaAnexos) fd.append("ficheiros", f);
  entradaAnexos = []; $("#entradaTexto").value = ""; renderEntradaAnexos();

  // consome a resposta por SSE tal como o chat normal (ver static/index.html,
  // consumirStreamSSE) — é mesmo o chat da Alma, com a mesma missão de
  // avaliação de qualidade, só com a identidade fixa "Balança Ecos Largos"
  // (ver tools/parkin.UTILIZADOR_CHAT_PARKIN) — pedido explícito do Rui
  // (2026-10-02).
  let assistenteBolha = null, texto2 = "";
  try{
    const r = await fetch("/park-in/entrada", {method:"POST", body:fd});
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while(true){
      const {value, done} = await reader.read();
      if(done) break;
      buffer += decoder.decode(value, {stream:true});
      const linhas = buffer.split("\n\n");
      buffer = linhas.pop();
      for(const linha of linhas){
        if(!linha.startsWith("data: ")) continue;
        let dados;
        try{ dados = JSON.parse(linha.slice(6)); } catch(_) { continue; }
        if(dados.delta){
          if(!assistenteBolha) assistenteBolha = bolhaEntrada("assistant");
          texto2 += dados.delta;
          assistenteBolha.textContent = texto2;
          $("#entradaChat").scrollTop = $("#entradaChat").scrollHeight;
        } else if(dados.a_processar){
          if(!assistenteBolha){ assistenteBolha = bolhaEntrada("assistant"); assistenteBolha.textContent = "a escrever…"; }
        } else if(dados.erro){
          $("#entradaMsg").innerHTML = `<div class="err">${dados.erro}</div>`;
        } else if(dados.done){
          carregar();
        }
      }
    }
  } catch(e){ $("#entradaMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bEntradaGuardar").disabled = false;
};

$("#bSaidaGuardar").onclick = async () => {
  if(saidaModo==="talao"){ await guardarSaidaPorTalao(); return; }
  const fs = $("#fSaidaFotos").files;
  if(!fs.length){ $("#saidaMsg").innerHTML = '<div class="err">Escolhe pelo menos uma foto.</div>'; return; }
  $("#bSaidaGuardar").disabled = true;
  $("#saidaMsg").innerHTML = '<div class="sub">A processar…</div>';
  const fd = new FormData();
  for(const f of fs) fd.append("ficheiros", f);
  const dataSaida = $("#cSaidaData").value;
  if(dataSaida) fd.append("data_saida", dataSaida);
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

// saída "só pelo número do talão" (pedido explícito do Rui, 2026-10-06):
// sem foto nova — o artigo e, por omissão, a quantidade inteira a sair
// vêm automaticamente do próprio lote já registado com esse talão (ver
// tools/parkin.registar_saida_por_talao).
async function guardarSaidaPorTalao(){
  const talao = $("#cSaidaTalao").value.trim();
  if(!talao){ $("#saidaMsg").innerHTML = '<div class="err">Escreve o nº do talão.</div>'; return; }
  const qtdTxt = $("#cSaidaQtd").value.trim();
  const dataSaida = $("#cSaidaData").value;
  const corpo = { talao, quantidade_kg: qtdTxt ? parseFloat(qtdTxt.replace(",",".")) : null,
    data_saida: dataSaida || null };
  $("#bSaidaGuardar").disabled = true;
  $("#saidaMsg").innerHTML = '<div class="sub">A processar…</div>';
  try{
    const r = await fetch("/park-in/saida-por-talao", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(corpo)});
    const j = await r.json();
    if(j.erro){ $("#saidaMsg").innerHTML = `<div class="err">${j.erro}</div>`; }
    else { $("#saidaMsg").innerHTML = `<div class="ok">Saída registada: ${t(j.quantidade_kg)} (${j.artigo}) — ${j.fornecedor}</div>`; carregar(); }
  } catch(e){ $("#saidaMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bSaidaGuardar").disabled = false;
}

$("#bCorrecaoGuardar").onclick = async () => {
  // Tipo e Comprimento aceitam "N.D." (não definido) — pedido explícito
  // do Rui, 2026-10-06: uma correção sem tipo/comprimento definidos tem
  // de sair do "Artigo desconhecido" (ver tools/parkin._chave_artigo),
  // não de um artigo específico.
  const compTxt = $("#cComprimento").value.trim();
  const compND = /^n\.?d\.?$/i.test(compTxt);
  const comprimento = compND ? null : parseFloat(compTxt.replace(",","."));
  const corpo = {
    tipo: $("#cTipo").value || null,
    comprimento: comprimento,
    espessura: $("#cEspessura").value,
    quantidade_kg: parseFloat($("#cQuantidade").value.replace(",",".")),
    motivo: $("#cMotivo").value,
    categoria_qualidade: $("#cCategoria").value,
  };
  if((!compND && (!comprimento || Number.isNaN(comprimento))) || !corpo.quantidade_kg || !corpo.motivo){
    $("#correcaoMsg").innerHTML = '<div class="err">Preenche comprimento (um número, ou "N.D."), quantidade e motivo.</div>'; return;
  }
  $("#bCorrecaoGuardar").disabled = true;
  try{
    const r = await fetch("/park-in/correcao", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(corpo)});
    const j = await r.json();
    if(j.erro){ $("#correcaoMsg").innerHTML = `<div class="err">${j.erro}</div>`; }
    else {
      const sinal = j.quantidade_kg > 0 ? "+" : "−";
      const artigoTxt = rotuloArtigo({artigo: j.artigo, tipo: corpo.tipo, comprimento: corpo.comprimento, espessura: corpo.espessura});
      const categoriaTxt = j.categoria_qualidade ? ` (categoria ${j.categoria_qualidade})`
        : (j.quantidade_kg > 0 ? " (sem categoria)" : "");
      let detalhe = "", aviso = "";
      if(j.depletadas && j.depletadas.length){
        const porCategoria = {};
        j.depletadas.forEach(d => { porCategoria[d.categoria_qualidade] = (porCategoria[d.categoria_qualidade]||0) + d.quantidade_kg; });
        const categorias = Object.keys(porCategoria);
        if(categorias.length > 1 || (categorias.length === 1 && categorias[0] !== j.categoria_qualidade)){
          detalhe = " — " + categorias.map(c => `${c||"sem categoria"}: ${t(porCategoria[c])}`).join(", ");
        }
        const negativa = j.depletadas.find(d => d.negativo);
        if(negativa){
          aviso = ` <span class="err">Aviso: não havia stock suficiente — ficaram ${t(negativa.quantidade_kg)} em défice`
            + (negativa.categoria_qualidade ? ` na categoria ${negativa.categoria_qualidade}` : "") + ".</span>";
        }
      }
      $("#correcaoMsg").innerHTML = `<div class="ok">Correção registada: ${sinal}${t(Math.abs(j.quantidade_kg))} — ${artigoTxt}${categoriaTxt}${detalhe}.${aviso}</div>`;
      carregar();
    }
  } catch(e){ $("#correcaoMsg").innerHTML = `<div class="err">Falhou: ${e}</div>`; }
  $("#bCorrecaoGuardar").disabled = false;
};

function renderLimites(){
  const linhas = [{chave:"total", rotulo:"Stock total", lim:DADOS.stock_total}]
    .concat(DADOS.stock_por_artigo.map(a => ({chave:a.artigo,
      rotulo: a.artigo==="desconhecido" ? "Artigo desconhecido"
        : `${a.tipo} · ${a.comprimento.toFixed(2)}m · ${a.espessura==="fina"?"Fina":"Normal"}`, lim:a})));
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
