import anthropic
from agents import ceo, ecos_largos
from tools import basecamp

client = anthropic.Anthropic()

# Agentes da Interior Guider (escolhidos por intenção, classificada por
# Haiku abaixo) — hoje só o CEO, mas a estrutura fica pronta para os
# próximos. Ecos Largos é outra equipa inteiramente à parte (gerida no
# mesmo Basecamp, mas sem relação com a Interior Guider).
AGENTES_INTERIOR_GUIDER = {"ceo": ceo.responder}
AGENTES_INTERIOR_GUIDER_STREAM = {"ceo": ceo.responder_stream}
# semana 5+: "orcamentos": orcamentos.responder, "design": design.responder, ...

AGENTES = {**AGENTES_INTERIOR_GUIDER, "ecos_largos": ecos_largos.responder}
AGENTES_STREAM = {**AGENTES_INTERIOR_GUIDER_STREAM, "ecos_largos": ecos_largos.responder_stream}

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

def encaminhar(pergunta: str, utilizador: str) -> str:
    """Decide primeiro a EMPRESA pela equipa do projeto no Basecamp (quem é a
    pessoa) — é o que faz a mesma consola e o mesmo link adaptarem-se
    sozinhos consoante quem está a falar com a Alma.

    Há quem trabalhe com as duas equipas ao mesmo tempo — para essas
    pessoas, pertencer à Ecos Largos não pode significar perder o acesso à
    Interior Guider (nem o inverso): decide-se então pela própria pergunta,
    não só pela identidade, para nunca lhe negar nenhum dos dois lados."""
    try:
        eh_ecos_largos = basecamp.pertence_a_ecos_largos(utilizador)
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa Ecos Largos, a assumir Interior Guider: {e!r}")
        eh_ecos_largos = False

    if not eh_ecos_largos:
        return _escolher_agente_interior_guider(pergunta)

    try:
        eh_tambem_interior_guider = basecamp.pertence_a_projeto(utilizador, "Gestão")
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa da Gestão, a assumir só Ecos Largos: {e!r}")
        eh_tambem_interior_guider = False

    if not eh_tambem_interior_guider:
        return "ecos_largos"

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
        return "ecos_largos"
    return _escolher_agente_interior_guider(pergunta)
