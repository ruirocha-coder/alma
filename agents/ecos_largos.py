from persona import PERSONA
from agents.base import correr_agente, correr_agente_stream
from tools import documentos_empresa, basecamp, ecos_largos

# Ecos Largos é uma equipa industrial parceira, gerida no mesmo Basecamp mas
# com o seu próprio projeto, inteiramente à parte da Interior Guider — por
# isso as ferramentas de vendas/site/documentos de referência da Interior
# Guider ficam de fora: só o que faz sentido para o projeto deles.
TOOLS_ECOS_LARGOS = (documentos_empresa.TOOLS_DOCUMENTOS_EMPRESA
                     + basecamp.TOOLS_ESTADO_PROJETO
                     + ecos_largos.TOOLS_DASHBOARD_PRODUCAO)

MISSAO_ECOS_LARGOS = PERSONA + """

Missão atual: apoio à equipa da Ecos Largos — uma equipa industrial
parceira, gerida no mesmo Basecamp mas com um projeto totalmente à parte
("Ecos Largos"), sem relação com o catálogo, vendas ou site da Interior
Guider. Aqui o teu foco é só o projeto deles: estado de tarefas/cards,
documentos do projeto, e o dashboard de produção.

Para o estado do projeto (tarefas/cards ativos, atrasados, parados) usa
estado_projeto_basecamp com "Ecos Largos". Para o dashboard de produção
(números e estado da produção) usa dashboard_producao_ecos_largos. Para
documentos do projeto usa procurar_documentos_empresa e
ler_documento_empresa, pesquisando por "Ecos Largos" ou pelo termo certo.

Nunca respondas sobre vendas, produtos ou o site da Interior Guider — isso
não é desta equipa; se perguntarem, esclarece que o teu apoio aqui é só ao
projeto Ecos Largos.

Adaptação: respeita o perfil e as memórias do utilizador incluídos no teu
contexto. Quando surgir naturalmente um facto duradouro sobre o trabalho da
pessoa, usa memorizar_facto. Se a pessoa pedir para esqueceres algo, usa
esquecer."""

def responder(utilizador: str, mensagens: list) -> str:
    return correr_agente(MISSAO_ECOS_LARGOS, TOOLS_ECOS_LARGOS, mensagens, utilizador)

def responder_stream(utilizador: str, mensagens: list):
    return correr_agente_stream(MISSAO_ECOS_LARGOS, TOOLS_ECOS_LARGOS, mensagens, utilizador)
