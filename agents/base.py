import anthropic, json
from tools import bigcommerce, site, documentos_empresa
import db

client = anthropic.Anthropic()

# Tools que qualquer agente pode incluir — quem adicionar um agente novo só
# precisa de fazer TOOLS_X = TOOLS_COMUNS + [tools específicas do agente].
TOOLS_COMUNS = bigcommerce.TOOLS_COMUNS + site.TOOLS_SITE + documentos_empresa.TOOLS_DOCUMENTOS_EMPRESA

FUNCOES = {
    "procurar_produtos": bigcommerce.procurar_produtos,
    "procurar_paginas": bigcommerce.procurar_paginas,
    "procurar_posts_blog": bigcommerce.procurar_posts_blog,
    "resumo_vendas": bigcommerce.resumo_vendas,
    "listar_paginas_site": site.listar_paginas_site,
    "ler_pagina_site": site.ler_pagina_site,
    "procurar_documentos_empresa": documentos_empresa.procurar_documentos_empresa,
    "ler_documento_empresa": documentos_empresa.ler_documento_empresa,
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

def correr_agente(system_prompt: str, tools: list, mensagens: list,
                  utilizador: str, modelo: str = "claude-sonnet-4-6") -> str:
    """Loop de agente com memória por utilizador: chama o modelo, executa tools até haver resposta final."""
    funcoes_utilizador = {
        "memorizar_facto": lambda facto: db.memorizar_facto(utilizador, facto),
        "esquecer": lambda termo: db.esquecer_factos(utilizador, termo),
    }
    contexto = db.contexto_utilizador(utilizador)
    system = system_prompt + ("\n\n" + contexto if contexto else "")
    tools_completas = tools + TOOLS_MEMORIA

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
