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
        "description": "Guarda um facto relevante e duradouro sobre o trabalho deste utilizador (projeto em curso, preferência expressa, contexto que ajudará em conversas futuras). Não guardar trivialidades nem informação sensível.",
        "input_schema": {
            "type": "object",
            "properties": {"facto": {"type": "string"}},
            "required": ["facto"]
        }
    },
    {
        "name": "esquecer",
        "description": "Apaga da memória os factos que contenham o termo indicado. Usar quando o utilizador pedir para esqueceres algo.",
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
# ou à Isa; o responder_basecamp identifica a origem acrescentando
# " (Basecamp)" ao nome do utilizador.
TOOLS_MURAL = [
    {
        "name": "publicar_mural",
        "description": "Publica uma mensagem no Mural do Basecamp, visível a toda a equipa. Na consola de chat qualquer pessoa pode pedir. Vindo de uma menção no Basecamp, só podes usar isto quando o Rui, a Beatriz ou a Isa pedirem explicitamente — qualquer outra pessoa a pedir isso a partir do Basecamp, recusa e explica que só eles podem pedir por ali.",
        "input_schema": {
            "type": "object",
            "properties": {"assunto": {"type": "string"}, "mensagem": {"type": "string"}},
            "required": ["assunto", "mensagem"]
        }
    }
]

_AUTORIZADOS_MURAL = ("rui", "beatriz", "isa")

def _publicar_mural_restrito(utilizador: str, assunto: str, mensagem: str):
    veio_do_basecamp = utilizador.strip().endswith("(Basecamp)")
    if veio_do_basecamp and not any(nome in utilizador.lower() for nome in _AUTORIZADOS_MURAL):
        return {"erro": "só o Rui, a Beatriz ou a Isa podem pedir uma publicação no mural a partir do Basecamp"}
    return basecamp.publicar_mural(assunto, mensagem)

def correr_agente(system_prompt: str, tools: list, mensagens: list,
                  utilizador: str, modelo: str = "claude-sonnet-4-6") -> str:
    """Loop de agente com memória por utilizador: chama o modelo, executa tools até haver resposta final."""
    funcoes_utilizador = {
        "memorizar_facto": lambda facto: db.memorizar_facto(utilizador, facto),
        "esquecer": lambda termo: db.esquecer_factos(utilizador, termo),
        "publicar_mural": lambda assunto, mensagem: _publicar_mural_restrito(utilizador, assunto, mensagem),
    }
    contexto = db.contexto_utilizador(utilizador)
    system = system_prompt + ("\n\n" + contexto if contexto else "")
    tools_completas = tools + TOOLS_MEMORIA + TOOLS_MURAL

    while True:
        resposta = client.messages.create(
            model=modelo, max_tokens=2000,
            system=system, tools=tools_completas, messages=mensagens
        )
        if resposta.stop_reason != "tool_use":
            return "".join(b.text for b in resposta.content if b.type == "text")

        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados = []
        for bloco in resposta.content:
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
        mensagens.append({"role": "user", "content": resultados})
