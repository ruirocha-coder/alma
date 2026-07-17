import anthropic
from agents import ceo, ecos_largos
from tools import basecamp

client = anthropic.Anthropic()

# Agentes da Interior Guider (escolhidos por intenção, classificada por
# Haiku abaixo) — hoje só o CEO, mas a estrutura fica pronta para os
# próximos. Ecos Largos é outra equipa inteiramente à parte (gerida no
# mesmo Basecamp, mas sem relação com a Interior Guider), por isso não entra
# nessa classificação por intenção: é decidida por identidade, antes disso.
AGENTES_INTERIOR_GUIDER = {"ceo": ceo.responder}
AGENTES_INTERIOR_GUIDER_STREAM = {"ceo": ceo.responder_stream}
# semana 5+: "orcamentos": orcamentos.responder, "design": design.responder, ...

AGENTES = {**AGENTES_INTERIOR_GUIDER, "ecos_largos": ecos_largos.responder}
AGENTES_STREAM = {**AGENTES_INTERIOR_GUIDER_STREAM, "ecos_largos": ecos_largos.responder_stream}

def encaminhar(pergunta: str, utilizador: str) -> str:
    """Primeiro decide a EMPRESA pela equipa do projeto no Basecamp (quem é
    a pessoa, não do que fala) — isto é o que faz a mesma consola e o mesmo
    link adaptarem-se sozinhos consoante quem está a falar com a Alma. Só
    depois, dentro da Interior Guider, classifica a intenção com Haiku entre
    os agentes dessa equipa (hoje, trivial: só há o CEO)."""
    try:
        if basecamp.pertence_a_ecos_largos(utilizador):
            return "ecos_largos"
    except Exception as e:
        print(f"[orchestrator] não consegui verificar a equipa Ecos Largos, a assumir Interior Guider: {e!r}")

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
