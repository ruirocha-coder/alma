import anthropic, json
from tools import bigcommerce, site, documentos_empresa, documentos_referencia, basecamp
import db

client = anthropic.Anthropic()

# Tools que qualquer agente pode incluir — quem adicionar um agente novo só
# precisa de fazer TOOLS_X = TOOLS_COMUNS + [tools específicas do agente].
TOOLS_COMUNS = (bigcommerce.TOOLS_COMUNS + site.TOOLS_SITE
                + documentos_empresa.TOOLS_DOCUMENTOS_EMPRESA
                + documentos_referencia.TOOLS_DOCUMENTOS_REFERENCIA
                + basecamp.TOOLS_ESTADO_PROJETO)

FUNCOES = {
    "procurar_produtos": bigcommerce.procurar_produtos,
    "procurar_paginas": bigcommerce.procurar_paginas,
    "procurar_posts_blog": bigcommerce.procurar_posts_blog,
    "resumo_vendas": bigcommerce.resumo_vendas,
    "listar_paginas_site": site.listar_paginas_site,
    "ler_pagina_site": site.ler_pagina_site,
    "procurar_documentos_empresa": documentos_empresa.procurar_documentos_empresa,
    "ler_documento_empresa": documentos_empresa.ler_documento_empresa,
    "documentos_referencia_empresa": documentos_referencia.documentos_referencia_empresa,
    "estado_projeto_basecamp": basecamp.estado_projeto_basecamp,
}

# Memória de longo prazo por utilizador — disponível a qualquer agente,
# tal como TOOLS_COMUNS, mas fica de fora desse tuplo porque as funções
# precisam de saber quem é o utilizador (só se sabe dentro de correr_agente).
TOOLS_MEMORIA = [
    {
        "name": "memorizar_facto",
        "description": "Guarda um facto relevante e duradouro sobre o trabalho deste utilizador (projeto em curso, preferência expressa, contexto que ajudará em conversas futuras). Não guardar trivialidades nem informação sensível. Só se guardam os factos mais recentes de cada pessoa — se o que vais guardar atualiza ou substitui um facto que já vês na tua lista de contexto (ex: mudou de projeto, deixou de ter uma preferência), usa esquecer nesse facto antigo primeiro, para não ficarem os dois a ocupar espaço; se for um facto novo e distinto, guarda-o sem mais.",
        "input_schema": {
            "type": "object",
            "properties": {"facto": {"type": "string"}},
            "required": ["facto"]
        }
    },
    {
        "name": "esquecer",
        "description": "Apaga da memória os factos que contenham o termo indicado. Usar quando o utilizador pedir para esqueceres algo, ou quando um facto novo tornar um facto antigo desatualizado (ver memorizar_facto).",
        "input_schema": {
            "type": "object",
            "properties": {"termo": {"type": "string"}},
            "required": ["termo"]
        }
    }
]

# Publicar no Mural: disponível a qualquer agente, tal como a memória. Na
# consola de chat qualquer utilizador pode pedir — quem lá está já é alguém
# de confiança da equipa. Vindo do Basecamp (onde qualquer pessoa com acesso
# a um projeto pode comentar/mencionar) mantém-se restrito ao Rui, à Beatriz
# ou à Isa. A origem chega como parâmetro explícito (não como sufixo no nome
# do utilizador) precisamente para o utilizador poder ser a mesma pessoa/
# identificador em ambos os canais — assim o perfil e a memória são
# partilhados, só a autorização do mural distingue o canal.
TOOLS_MURAL = [
    {
        "name": "publicar_mural",
        "description": "Publica uma mensagem no Mural do Basecamp, visível a toda a equipa. USA ISTO SÓ quando o pedido for estrita e explicitamente para publicares no mural (ex: \"publica isto no mural\") — nunca por iniciativa própria, por achares um assunto importante, ou como forma de responder a uma pergunta geral. Qualquer outra situação (incluindo responder a uma menção numa tarefa/card) é sempre um comentário normal, nunca isto. Na consola de chat qualquer pessoa pode pedir. Vindo de uma menção no Basecamp, só podes usar isto quando o Rui, a Beatriz ou a Isa pedirem explicitamente — qualquer outra pessoa a pedir isso a partir do Basecamp, recusa e explica que só eles podem pedir por ali.",
        "input_schema": {
            "type": "object",
            "properties": {"assunto": {"type": "string"}, "mensagem": {"type": "string"}},
            "required": ["assunto", "mensagem"]
        }
    }
]

_AUTORIZADOS_MURAL = ("rui", "beatriz", "isa")

def _publicar_mural_restrito(utilizador: str, assunto: str, mensagem: str, origem: str):
    if origem == "basecamp" and not any(nome in utilizador.lower() for nome in _AUTORIZADOS_MURAL):
        return {"erro": "só o Rui, a Beatriz ou a Isa podem pedir uma publicação no mural a partir do Basecamp"}
    return basecamp.publicar_mural(assunto, mensagem)

def _system_com_cache(system_prompt: str, contexto: str) -> list:
    """A parte fixa do system prompt (persona + missão do agente) é sempre a
    mesma entre pedidos — marcá-la para cache poupa reprocessar os mesmos
    milhares de tokens em cada chamada. O contexto do utilizador (perfil +
    memória) muda por pessoa, por isso fica depois, fora do bloco cacheado."""
    blocos = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    if contexto:
        blocos.append({"type": "text", "text": contexto})
    return blocos

def _tools_com_cache(tools: list) -> list:
    """As ferramentas de um agente são sempre as mesmas entre pedidos — marca
    a última para cache (a API cacheia tudo até esse bloco, inclusive)."""
    if not tools:
        return tools
    return [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]

def _executar_tool_uses(blocos: list, funcoes_utilizador: dict) -> list:
    resultados = []
    for bloco in blocos:
        if bloco.type == "tool_use":
            try:
                funcao = funcoes_utilizador.get(bloco.name) or FUNCOES[bloco.name]
                out = funcao(**bloco.input)
            except Exception as e:
                print(f"[ferramenta] {bloco.name}({bloco.input}) falhou: {e!r}")
                out = {"erro": str(e)}
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloco.id,
                "content": json.dumps(out, ensure_ascii=False, default=str)
            })
    return resultados

def _preparar(system_prompt: str, tools: list, utilizador: str, origem: str):
    contexto = db.contexto_utilizador(utilizador)
    system = _system_com_cache(system_prompt, contexto)
    tools_completas = _tools_com_cache(tools + TOOLS_MEMORIA + TOOLS_MURAL)
    funcoes_utilizador = {
        "memorizar_facto": lambda facto: db.memorizar_facto(utilizador, facto),
        "esquecer": lambda termo: db.esquecer_factos(utilizador, termo),
        "publicar_mural": lambda assunto, mensagem: _publicar_mural_restrito(utilizador, assunto, mensagem, origem),
    }
    return system, tools_completas, funcoes_utilizador

def correr_agente(system_prompt: str, tools: list, mensagens: list,
                  utilizador: str, modelo: str = "claude-sonnet-4-6", origem: str = "consola") -> str:
    """Loop de agente com memória por utilizador: chama o modelo, executa tools até haver resposta final.

    `utilizador` deve ser o identificador real da pessoa (o mesmo em qualquer
    canal), para o perfil e a memória de longo prazo serem partilhados —
    `origem` ("consola" ou "basecamp") é só para decidir permissões
    (ex: quem pode pedir uma publicação no mural), nunca para identificar
    quem é a pessoa."""
    system, tools_completas, funcoes_utilizador = _preparar(system_prompt, tools, utilizador, origem)

    while True:
        resposta = client.messages.create(
            model=modelo, max_tokens=2000,
            system=system, tools=tools_completas, messages=mensagens
        )
        if resposta.stop_reason != "tool_use":
            return "".join(b.text for b in resposta.content if b.type == "text")

        mensagens.append({"role": "assistant", "content": resposta.content})
        mensagens.append({"role": "user", "content": _executar_tool_uses(resposta.content, funcoes_utilizador)})

def correr_agente_stream(system_prompt: str, tools: list, mensagens: list,
                         utilizador: str, modelo: str = "claude-sonnet-4-6", origem: str = "consola"):
    """Generator: dá 'yield' a pedaços de texto da resposta final, à medida
    que chegam do modelo. Rondas de tool-use são resolvidas por completo (sem
    stream) antes disso — só a resposta final visível à pessoa é transmitida
    em tempo real."""
    system, tools_completas, funcoes_utilizador = _preparar(system_prompt, tools, utilizador, origem)

    while True:
        with client.messages.stream(
            model=modelo, max_tokens=2000,
            system=system, tools=tools_completas, messages=mensagens
        ) as stream:
            for texto in stream.text_stream:
                yield texto
            resposta = stream.get_final_message()

        if resposta.stop_reason != "tool_use":
            return

        mensagens.append({"role": "assistant", "content": resposta.content})
        mensagens.append({"role": "user", "content": _executar_tool_uses(resposta.content, funcoes_utilizador)})
