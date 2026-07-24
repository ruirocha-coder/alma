import anthropic
from agents import ceo, ecos_largos, qualidade_toros_ecos_largos
from tools import basecamp
import db

client = anthropic.Anthropic()

# Agentes da Interior Guider (escolhidos por intenção, classificada por
# Haiku abaixo) — hoje só o CEO, mas a estrutura fica pronta para os
# próximos. Ecos Largos é outra equipa inteiramente à parte (gerida no
# mesmo Basecamp, mas sem relação com a Interior Guider).
AGENTES_INTERIOR_GUIDER = {"ceo": ceo.responder}
AGENTES_INTERIOR_GUIDER_STREAM = {"ceo": ceo.responder_stream}
# semana 5+: "orcamentos": orcamentos.responder, "design": design.responder, ...

AGENTES = {**AGENTES_INTERIOR_GUIDER, "ecos_largos": ecos_largos.responder,
           "qualidade_toros_ecos_largos": qualidade_toros_ecos_largos.responder}
AGENTES_STREAM = {**AGENTES_INTERIOR_GUIDER_STREAM, "ecos_largos": ecos_largos.responder_stream,
                  "qualidade_toros_ecos_largos": qualidade_toros_ecos_largos.responder_stream}

# jargão específico do dashboard de produção da Ecos Largos — termos que
# um classificador genérico (Haiku, ver _escolher_entre_empresas) pode
# não reconhecer como sinal de produção industrial, e por omissão
# manda para a Interior Guider (a regra de "se não estiver claro, escolhe
# interior_guider"). Bug real (Rui, 2026-07-24): perguntar sobre
# "charriots" (carrinhos/lotes que percorrem a linha de produção, ver
# tools.ecos_largos._resumo_charriots) foi parar ao CEO, que não tem
# nenhuma ferramenta da Ecos Largos — mesmo com o perfil em "ambas".
_PALAVRAS_PRODUCAO_ECOS_LARGOS = ("charriot", "carriot", "chariot", "tronco", "cubicador", "patela", "oee", "takt")

def _pergunta_sobre_producao_ecos_largos(pergunta: str) -> bool:
    """Deteta perguntas sobre o dashboard de produção da Ecos Largos que
    usam jargão específico deste dashboard, difícil de reconhecer por um
    classificador genérico sem o contexto todo — ver
    _PALAVRAS_PRODUCAO_ECOS_LARGOS."""
    termo = pergunta.lower()
    return any(p in termo for p in _PALAVRAS_PRODUCAO_ECOS_LARGOS)

# cobre não só "avalia..." mas qualquer forma natural de pedir o histórico
# já guardado — "regista o registo", "histórico", "resumo" — a Beatriz
# pediu "dá-me o registo das cargas de toros hoje", sem a palavra
# "avalia" nenhuma, e isso tem de chegar ao mesmo sítio.
_PALAVRAS_AVALIACAO = ("avalia", "registo", "registos", "histórico", "historico", "resumo")
_PALAVRAS_CARGA_TOROS = ("carga", "toros", "fornecedor", "talão", "talao", "talões", "taloes")

def _pergunta_sobre_avaliacoes_cargas(pergunta: str) -> bool:
    """Deteta perguntas sobre o histórico de avaliações de cargas de toros
    já feitas (ex: "quantas cargas foram avaliadas este ano", "resume as
    avaliações do fornecedor X", "dá-me o registo das cargas de toros
    hoje") — pedido explícito do Rui para que qualquer pessoa da equipa
    consiga consultar isto a qualquer momento, mesmo sem foto anexada
    (nesse caso a decisão já é sempre determinística por tem_anexos). Sem
    uma foto, quem pergunta pode facilmente não usar a palavra "qualidade"
    nem "avalia" (o que o classificador por Haiku procura), e confiar só
    nessa classificação já falhou antes com frases curtas."""
    termo = pergunta.lower()
    return (any(p in termo for p in _PALAVRAS_AVALIACAO)
            and any(p in termo for p in _PALAVRAS_CARGA_TOROS))

def escolher_agente_ecos_largos(pergunta: str, tem_anexos: bool = False) -> str:
    """Dentro da Ecos Largos, decide entre o apoio geral (produção, tarefas/
    cards do projeto) e o subagente dedicado às regras de qualidade de
    cargas de toros (documento "Manual Qualidade de Cargas - Toros") —
    pedido explicitamente pelo Rui para não se misturar com o resto. Exposta (sem
    "_" no nome) porque agents/responder_basecamp.py também precisa desta
    mesma decisão para menções no Basecamp do projeto Ecos Largos.

    `tem_anexos`: a mensagem trouxe ficheiros/fotos anexados — nesse caso
    salta a classificação por Haiku e vai sempre para o subagente de
    qualidade. Enviar fotos (a carga de madeira, o talão) para a Ecos
    Largos é sempre um pedido de avaliação de qualidade — não há outro
    uso estabelecido para anexar ficheiros nesta equipa. Confiar só na
    classificação por texto falhava sempre que a legenda era curta ou
    genérica (ex: "analisa a carga", sem a palavra "qualidade"), mandando
    o pedido para o agente geral, que nem conhece o manual. Ver também
    _pergunta_sobre_avaliacoes_cargas, para o mesmo problema numa consulta
    ao histórico de avaliações, sem foto anexada."""
    if tem_anexos or _pergunta_sobre_avaliacoes_cargas(pergunta):
        return "qualidade_toros_ecos_largos"
    r = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system="Esta pergunta é da equipa Ecos Largos. Classifica-a como "
               "'qualidade_toros' se for sobre regras, critérios ou avaliação "
               "de qualidade de cargas de toros (o documento \"Manual "
               "Qualidade de Cargas - Toros\"), ou 'geral' para qualquer "
               "outra coisa (produção, dashboard, tarefas/cards do "
               "Basecamp). Responde só com uma das duas palavras.",
        messages=[{"role": "user", "content": pergunta}]
    )
    escolha = r.content[0].text.strip().lower()
    return "qualidade_toros_ecos_largos" if escolha == "qualidade_toros" else "ecos_largos"

def _escolher_agente_interior_guider(pergunta: str) -> str:
    if len(AGENTES_INTERIOR_GUIDER) == 1:
        return "ceo"
    r = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system="Classifica a pergunta num destes agentes: "
               + ", ".join(AGENTES_INTERIOR_GUIDER) + ". Responde só com o nome do agente.",
        messages=[{"role": "user", "content": pergunta}]
    )
    escolha = r.content[0].text.strip().lower()
    return escolha if escolha in AGENTES_INTERIOR_GUIDER else "ceo"  # fallback: CEO

def _escolher_entre_empresas(pergunta: str, tem_anexos: bool = False) -> str:
    """Para quem trabalha com as duas equipas: decide pela própria pergunta,
    não só pela identidade, para nunca lhe negar acesso a nenhum dos dois
    lados."""
    if tem_anexos:
        # anexar ficheiros é sempre coisa da Ecos Largos (avaliação de
        # cargas) nesta aplicação — não há um uso equivalente para a
        # Interior Guider, por isso nem vale a pena perguntar ao Haiku.
        return escolher_agente_ecos_largos(pergunta, tem_anexos=True)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system=("Esta pessoa trabalha tanto com a Interior Guider como com a Ecos Largos "
                "(duas equipas geridas no mesmo Basecamp, sem relação entre si). Classifica "
                "esta mensagem como 'ecos_largos' (produção industrial — entrada/receção "
                "de madeira, m3, troncos, charriots, takt, OEE, linhas de produção — ou o "
                "projeto Ecos Largos em geral) ou 'interior_guider' (vendas de mobiliário, "
                "produtos, site, encomendas de clientes, projetos da Interior Guider). "
                "Bug real já visto: \"entradas\" sozinho é ambíguo entre as duas — só "
                "escolhe 'interior_guider' se for claramente sobre receita/vendas em "
                "euros; uma pergunta sobre quantidade, m3, ou sem unidade monetária "
                "explícita é 'ecos_largos' (entrada de madeira). Se mesmo assim não "
                "estiver claro, escolhe 'interior_guider'. Responde só com uma das duas "
                "palavras."),
        messages=[{"role": "user", "content": pergunta}]
    )
    escolha = r.content[0].text.strip().lower()
    if escolha == "ecos_largos":
        return escolher_agente_ecos_largos(pergunta)
    return _escolher_agente_interior_guider(pergunta)

def contexto_para_encaminhar(mensagens: list, max_chars: int = 800) -> str:
    """Junta as últimas trocas da conversa (a mensagem atual já vem incluída
    no fim de `mensagens`, ver main.py) num único texto para o encaminhamento
    — deteção de palavras-chave e classificação por Haiku — em vez de olhar
    só para a mensagem isolada.

    Bug real (Rui, 2026-07-24): uma conversa sobre as avaliações de
    qualidade de cargas de toros (guardadas na memória da Alma, não no
    Basecamp) foi parar a meio a um agente sem essa ferramenta nenhuma —
    a mensagem seguinte ("Não esta guardado no basecamp, está na memoria
    da alma após a receção do talão e foto da carga") já não tinha
    nenhuma palavra-chave de toros/avaliação nem de produção, por isso
    foi reclassificada do zero, sem saber do que a conversa já estava a
    falar, e a Alma acabou por negar ter qualquer memória persistente.
    `encaminhar` decide de novo a cada mensagem (não há agente "preso" à
    sessão) — juntar o contexto recente é a forma mais simples de dar à
    deteção/classificação a mesma informação que um humano teria lendo a
    conversa toda, sem mudar essa decisão a cada vez."""
    recentes = [m["content"] for m in mensagens[-4:] if isinstance(m.get("content"), str) and m["content"]]
    return " ".join(recentes)[-max_chars:]

def encaminhar(pergunta: str, utilizador: str, tem_anexos: bool = False) -> str:
    """Decide primeiro a EMPRESA (quem é a pessoa, não do que fala) — é o que
    faz a mesma consola e o mesmo link adaptarem-se sozinhos.

    O sinal principal é o campo 'empresa' do perfil, respondido logo no
    acolhimento — funciona para toda a gente que fala com a Alma pela
    consola, mesmo quem não tem conta própria no Basecamp (a maioria da
    Ecos Largos, por exemplo). Só quando o perfil não tem essa resposta
    (perfis antigos, de antes desta pergunta existir) é que se recorre à
    deteção pela equipa do projeto no Basecamp, que só funciona para quem
    lá tem acesso.

    Há quem trabalhe com as duas equipas ao mesmo tempo — para essas
    pessoas, pertencer à Ecos Largos não pode significar perder o acesso à
    Interior Guider (nem o inverso): decide-se então pela própria pergunta.

    Bug real (Beatriz, 2026-07-22): o perfil dela tem 'empresa' =
    "interior_guider", por isso uma pergunta sobre o histórico de
    avaliações de cargas de toros ia direta a _escolher_agente_interior_
    guider, que nem sabe que a Ecos Largos existe — nunca chegava a
    _pergunta_sobre_avaliacoes_cargas (essa verificação vivia só dentro de
    escolher_agente_ecos_largos, tarde demais). Pedido explícito do Rui:
    qualquer utilizador, a qualquer momento, tem de conseguir consultar
    isto — por isso esta deteção acontece aqui, antes de qualquer decisão
    por empresa.

    Bug real (Rui, 2026-07-24), mesmo padrão: mesmo com o perfil em
    "ambas" (que já decide pelo conteúdo da pergunta, ver
    _escolher_entre_empresas), o classificador genérico não reconheceu
    "charriots" como termo de produção e, por omissão, escolheu Interior
    Guider — por isso _pergunta_sobre_producao_ecos_largos intercepta
    aqui também, antes de qualquer classificação por IA."""
    if _pergunta_sobre_avaliacoes_cargas(pergunta) or _pergunta_sobre_producao_ecos_largos(pergunta):
        return escolher_agente_ecos_largos(pergunta, tem_anexos=tem_anexos)

    empresa = None
    try:
        perfil = db.obter_perfil(utilizador)
        empresa = (perfil or {}).get("empresa")
    except Exception as e:
        print(f"[orchestrator] não consegui ler o perfil para saber a empresa: {e!r}")

    if empresa == "ecos_largos":
        return escolher_agente_ecos_largos(pergunta, tem_anexos=tem_anexos)
    if empresa == "interior_guider":
        return _escolher_agente_interior_guider(pergunta)
    if empresa == "ambas":
        return _escolher_entre_empresas(pergunta, tem_anexos=tem_anexos)

    # perfil sem 'empresa' definida — recorre à deteção pela equipa do
    # projeto no Basecamp (comportamento anterior a esta pergunta existir).
    # Isto só funciona para quem tem conta própria no Basecamp — a maioria
    # da Ecos Largos não tem, por isso "não encontrado" aqui não é prova de
    # que a pessoa é da Interior Guider, só que não a conseguimos confirmar
    # pela conta. Em vez de assumir logo Interior Guider, decide-se também
    # pelo conteúdo da própria pergunta (a mesma lógica de quem trabalha com
    # as duas equipas) — assim uma pergunta claramente sobre produção/
    # dashboard da Ecos Largos não fica presa no agente errado, sem
    # ferramentas para lhe responder.
    try:
        eh_ecos_largos = basecamp.pertence_a_ecos_largos(utilizador)
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa Ecos Largos, a decidir pela pergunta: {e!r}")
        eh_ecos_largos = False

    if not eh_ecos_largos:
        return _escolher_entre_empresas(pergunta, tem_anexos=tem_anexos)

    try:
        eh_tambem_interior_guider = basecamp.pertence_a_projeto(utilizador, "Gestão")
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa da Gestão, a assumir só Ecos Largos: {e!r}")
        eh_tambem_interior_guider = False

    if not eh_tambem_interior_guider:
        return escolher_agente_ecos_largos(pergunta, tem_anexos=tem_anexos)

    return _escolher_entre_empresas(pergunta, tem_anexos=tem_anexos)
