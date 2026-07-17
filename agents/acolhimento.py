from persona import PERSONA
from agents.base import client, _system_com_cache, _tools_com_cache, _executar_tool_uses
from tools import basecamp
import db

MISSAO_ACOLHIMENTO = PERSONA + """

Modo atual: acolhimento. É a primeira vez que falas com esta pessoa.

Objetivo: conhece-la através de uma conversa natural e curta — nunca um
questionário. Faz UMA pergunta de cada vez, reage ao que a pessoa diz antes
de passar à seguinte. As perguntas-chave, por esta ordem:

1. Trabalhas na Interior Guider, na Ecos Largos, ou com as duas equipas?
   (são duas equipas geridas no mesmo Basecamp, sem relação entre si — só
   precisas de saber qual para te adaptares bem, não expliques isto à
   pessoa, é só contexto teu)
2. Qual o teu papel na equipa?
3. Quando me pedires ajuda, preferes que vá direta ao essencial ou que
   explique o raciocínio?
4. Preferes que te dê uma recomendação fechada ou opções para escolheres?
5. Que dificuldades consideras que eu posso complementar e ajudar a resolver?
6. O que te rouba mais tempo na semana?

Quando tiveres as respostas, usa a ferramenta guardar_perfil UMA vez. No
campo empresa, usa exatamente um destes valores: "interior_guider",
"ecos_largos" ou "ambas" — nunca o texto literal que a pessoa disse.

Depois de guardar, resume à pessoa o que ficaste a saber e diz-lhe que pode
pedir-te para alterar ou esquecer qualquer parte, quando quiser. Termina
perguntando em que podes ser útil agora.

Não voltes a fazer estas perguntas no futuro — a partir daqui adaptas-te
pela memória."""

TOOLS_ACOLHIMENTO = [
    {
        "name": "guardar_perfil",
        "description": "Guarda o perfil do utilizador. Usar uma única vez, quando as seis respostas estiverem recolhidas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "empresa": {"type": "string", "enum": ["interior_guider", "ecos_largos", "ambas"],
                           "description": "Com que equipa/empresa a pessoa trabalha"},
                "papel": {"type": "string", "description": "Papel na equipa"},
                "estilo_resposta": {"type": "string", "description": "Direto ao essencial ou com raciocínio explicado"},
                "formato": {"type": "string", "description": "Preferências de formato (listas, texto corrido, visual)"},
                "decisao": {"type": "string", "description": "Recomendação fechada ou opções"},
                "dificuldades": {"type": "string", "description": "Dificuldades onde a Alma pode complementar e ajudar"},
            },
            "required": ["empresa", "papel", "estilo_resposta", "formato", "decisao", "dificuldades"]
        }
    }
]

def _preparar(utilizador: str):
    contexto = ""
    try:
        if basecamp.pertence_a_ecos_largos(utilizador):
            contexto = ("Esta pessoa foi identificada como parte da equipa da Ecos Largos "
                       "(a equipa industrial parceira, não a Interior Guider) — trata-a com "
                       "total normalidade, sem sugerir que não devia ter acesso.")
    except Exception as e:
        print(f"[acolhimento] não consegui verificar a equipa Ecos Largos, a continuar sem essa nota: {e!r}")
    system = _system_com_cache(MISSAO_ACOLHIMENTO, contexto)
    tools = _tools_com_cache(TOOLS_ACOLHIMENTO)
    funcoes = {"guardar_perfil": lambda **kwargs: db.guardar_perfil(utilizador=utilizador, **kwargs)}
    return system, tools, funcoes

def responder(utilizador: str, mensagens: list) -> str:
    system, tools, funcoes = _preparar(utilizador)
    while True:
        resposta = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            system=system, tools=tools, messages=mensagens
        )
        if resposta.stop_reason != "tool_use":
            return "".join(b.text for b in resposta.content if b.type == "text")

        mensagens.append({"role": "assistant", "content": resposta.content})
        mensagens.append({"role": "user", "content": _executar_tool_uses(resposta.content, funcoes)})

def responder_stream(utilizador: str, mensagens: list):
    system, tools, funcoes = _preparar(utilizador)
    while True:
        with client.messages.stream(
            model="claude-sonnet-4-6", max_tokens=1500,
            system=system, tools=tools, messages=mensagens
        ) as stream:
            for texto in stream.text_stream:
                yield texto
            resposta = stream.get_final_message()

        if resposta.stop_reason != "tool_use":
            return

        mensagens.append({"role": "assistant", "content": resposta.content})
        mensagens.append({"role": "user", "content": _executar_tool_uses(resposta.content, funcoes)})
