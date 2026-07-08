import anthropic, json
from tools import bigcommerce, site

client = anthropic.Anthropic()

# Tools que qualquer agente pode incluir — quem adicionar um agente novo só
# precisa de fazer TOOLS_X = TOOLS_COMUNS + [tools específicas do agente].
TOOLS_COMUNS = bigcommerce.TOOLS_COMUNS + site.TOOLS_SITE

FUNCOES = {
    "procurar_produtos": bigcommerce.procurar_produtos,
    "procurar_paginas": bigcommerce.procurar_paginas,
    "procurar_posts_blog": bigcommerce.procurar_posts_blog,
    "resumo_vendas": bigcommerce.resumo_vendas,
    "listar_paginas_site": site.listar_paginas_site,
    "ler_pagina_site": site.ler_pagina_site,
}

def correr_agente(system_prompt: str, tools: list, mensagens: list,
                  modelo: str = "claude-sonnet-4-6") -> str:
    """Loop de agente: chama o modelo, executa tools até haver resposta final."""
    while True:
        resposta = client.messages.create(
            model=modelo, max_tokens=2000,
            system=system_prompt, tools=tools, messages=mensagens
        )
        if resposta.stop_reason != "tool_use":
            return "".join(b.text for b in resposta.content if b.type == "text")

        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados = []
        for bloco in resposta.content:
            if bloco.type == "tool_use":
                try:
                    out = FUNCOES[bloco.name](**bloco.input)
                except Exception as e:
                    out = {"erro": str(e)}
                resultados.append({
                    "type": "tool_result",
                    "tool_use_id": bloco.id,
                    "content": json.dumps(out, ensure_ascii=False, default=str)
                })
        mensagens.append({"role": "user", "content": resultados})
