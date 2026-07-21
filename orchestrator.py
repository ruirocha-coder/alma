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

def escolher_agente_ecos_largos(pergunta: str) -> str:
    """Dentro da Ecos Largos, decide entre o apoio geral (produção, tarefas/
    cards do projeto) e o subagente dedicado às regras de qualidade de
    cargas de toros (Manual Qualidade de Cargas - Toros) — pedido
    explicitamente pelo Rui para não se misturar com o resto. Exposta (sem
    "_" no nome) porque agents/responder_basecamp.py também precisa desta
    mesma decisão para menções no Basecamp do projeto Ecos Largos."""
    r = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system="Esta pergunta é da equipa Ecos Largos. Classifica-a como "
               "'qualidade_toros' se for sobre regras, critérios ou avaliação "
               "de qualidade de cargas de toros (o Manual Qualidade de Cargas "
               "- Toros), ou 'geral' para qualquer outra coisa (produção, "
               "dashboard, tarefas/cards do Basecamp). Responde só com uma "
               "das duas palavras.",
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

def _escolher_entre_empresas(pergunta: str) -> str:
    """Para quem trabalha com as duas equipas: decide pela própria pergunta,
    não só pela identidade, para nunca lhe negar acesso a nenhum dos dois
    lados."""
    r = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        system=("Esta pessoa trabalha tanto com a Interior Guider como com a Ecos Largos "
                "(duas equipas geridas no mesmo Basecamp, sem relação entre si). Classifica "
                "esta mensagem como 'ecos_largos' (produção, dashboard de produção, o "
                "projeto Ecos Largos) ou 'interior_guider' (vendas, produtos, site, "
                "projetos da Interior Guider). Se não estiver claro, escolhe "
                "'interior_guider'. Responde só com uma das duas palavras."),
        messages=[{"role": "user", "content": pergunta}]
    )
    escolha = r.content[0].text.strip().lower()
    if escolha == "ecos_largos":
        return escolher_agente_ecos_largos(pergunta)
    return _escolher_agente_interior_guider(pergunta)

def encaminhar(pergunta: str, utilizador: str) -> str:
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
    Interior Guider (nem o inverso): decide-se então pela própria pergunta."""
    empresa = None
    try:
        perfil = db.obter_perfil(utilizador)
        empresa = (perfil or {}).get("empresa")
    except Exception as e:
        print(f"[orchestrator] não consegui ler o perfil para saber a empresa: {e!r}")

    if empresa == "ecos_largos":
        return escolher_agente_ecos_largos(pergunta)
    if empresa == "interior_guider":
        return _escolher_agente_interior_guider(pergunta)
    if empresa == "ambas":
        return _escolher_entre_empresas(pergunta)

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
        return _escolher_entre_empresas(pergunta)

    try:
        eh_tambem_interior_guider = basecamp.pertence_a_projeto(utilizador, "Gestão")
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa da Gestão, a assumir só Ecos Largos: {e!r}")
        eh_tambem_interior_guider = False

    if not eh_tambem_interior_guider:
        return escolher_agente_ecos_largos(pergunta)

    return _escolher_entre_empresas(pergunta)
