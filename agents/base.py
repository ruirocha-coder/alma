import anthropic, json, threading
from tools import (bigcommerce, site, documentos_empresa, documentos_referencia, basecamp, ecos_largos,
                   documentos_gerados, portal_projeto, tempo, calculadora)
from agents import agendamento_entregas
import db

# entre rondas de tool-use (ex: a consultar o Basecamp, que pode demorar
# bastante numa conta com muito histórico) o stream fica sem nada para
# transmitir — sem isto, a consola ficava sem sinal de que a Alma continua a
# tratar do pedido durante esse tempo.
_INTERVALO_SINAL_DE_VIDA = 8

# 2000 tokens (~1500 palavras) cortava respostas longas a meio — insuficiente
# para um documento formal ou uma resposta corrida bem desenvolvida. 8192 é o
# limite de saída sem precisar de flags beta especiais; para um documento de
# várias dezenas de páginas (dezenas de milhares de palavras) isto ainda não
# chega numa única resposta — usa gerar_pdf com o conteúdo que couber, e se
# for pedido para continuar/expandir, gera mais conteúdo a seguir.
MAX_TOKENS_RESPOSTA = 8192

client = anthropic.Anthropic()

# Pesquisa e leitura da internet: tools do lado do servidor da Anthropic — a
# própria Claude executa a pesquisa/leitura, sem nenhuma função nossa a
# correr (por isso não aparecem em FUNCOES). Pedido do Rui (2026-07-31):
# questões gerais, fora do que a Alma já sabe pelas suas ferramentas da
# empresa. Nunca declarar "code_execution" ao lado destas — a filtragem
# dinâmica já corre por baixo destas duas, e uma segunda ferramenta de
# execução de código só confundia o modelo.
TOOLS_INTERNET = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

# Tools que qualquer agente pode incluir — quem adicionar um agente novo só
# precisa de fazer TOOLS_X = TOOLS_COMUNS + [tools específicas do agente].
TOOLS_COMUNS = (bigcommerce.TOOLS_COMUNS + site.TOOLS_SITE
                + documentos_empresa.TOOLS_DOCUMENTOS_EMPRESA
                + documentos_referencia.TOOLS_DOCUMENTOS_REFERENCIA
                + basecamp.TOOLS_ESTADO_PROJETO
                + TOOLS_INTERNET)

def _disparar_sugestao_semanal_logistica():
    # import feito aqui dentro (não no topo do módulo) de propósito:
    # agents/sugestao_logistica_semanal.py importa `client` deste ficheiro
    # (agents/base.py) — um import direto no topo criava um ciclo
    # (base -> sugestao_logistica_semanal -> base). Adiado até à chamada,
    # já com os dois módulos totalmente carregados, evita o ciclo.
    from agents import sugestao_logistica_semanal
    return sugestao_logistica_semanal.correr_sugestao_semanal_logistica()

def _diagnosticar_logistica_on_hold():
    # mesma razão do import adiado acima: agents/logistica_entregas.py
    # também importa `client` deste ficheiro.
    from agents import logistica_entregas
    return logistica_entregas.diagnostico_cards_regiao()

def _trajetos_logistica_entregas():
    # mesma razão do import adiado acima.
    from agents import sugestao_logistica_semanal
    return sugestao_logistica_semanal.trajetos_logistica_entregas()

def _disparar_avisos_gestao_agendas():
    # mesma razão do import adiado acima.
    from agents import avisos_gestao_agendas
    return avisos_gestao_agendas.correr_avisos_gestao_agendas()

FUNCOES = {
    "agora": lambda: tempo.agora(),
    "dia_da_semana": tempo.dia_da_semana,
    "calcular": calculadora.calcular,
    "procurar_produtos": bigcommerce.procurar_produtos,
    "procurar_paginas": bigcommerce.procurar_paginas,
    "procurar_posts_blog": bigcommerce.procurar_posts_blog,
    "resumo_vendas": bigcommerce.resumo_vendas,
    "listar_paginas_site": site.listar_paginas_site,
    "ler_pagina_site": site.ler_pagina_site,
    "procurar_documentos_empresa": documentos_empresa.procurar_documentos_empresa,
    "ler_documento_empresa": documentos_empresa.ler_documento_empresa,
    "ler_anexos_registo_basecamp": documentos_empresa.ler_anexos_registo_basecamp,
    "ler_folha_excel_anexo": documentos_empresa.ler_folha_excel_anexo,
    "documentos_referencia_empresa": documentos_referencia.documentos_referencia_empresa,
    "estado_projeto_basecamp": basecamp.estado_projeto_basecamp,
    "resumo_pessoa_basecamp": basecamp.resumo_pessoa_basecamp,
    "procurar_cards_basecamp": basecamp.procurar_cards_basecamp,
    "cards_de_card_table": basecamp.cards_de_card_table,
    "procurar_anexo_em_comentarios": basecamp.procurar_anexo_em_comentarios,
    "listar_pdfs_anexados_por_data": basecamp.listar_pdfs_anexados_por_data,
    "dashboard_producao_ecos_largos": ecos_largos.ler_dashboard_producao,
    "dashboard_producao_ecos_largos_intervalo": ecos_largos.ler_dashboard_producao_intervalo,
    "ler_manual_qualidade_cargas_toros": ecos_largos.ler_manual_qualidade_cargas_toros,
    "guardar_avaliacao_carga_toros": ecos_largos.guardar_avaliacao_carga_toros,
    "resumo_avaliacoes_cargas_toros": ecos_largos.resumo_avaliacoes_cargas_toros,
    "disparar_sugestao_semanal_logistica": _disparar_sugestao_semanal_logistica,
    "diagnosticar_logistica_on_hold": _diagnosticar_logistica_on_hold,
    "trajetos_logistica_entregas": _trajetos_logistica_entregas,
    "disparar_avisos_gestao_agendas": _disparar_avisos_gestao_agendas,
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
    },
    {
        "name": "definir_empresa",
        "description": "Corrige a equipa/empresa registada no perfil desta pessoa (Interior Guider, Ecos Largos, ou ambas). USA ISTO sempre que a pessoa disser explicitamente qual é a sua equipa e isso contradisser o que a conversa sugere (ex: perguntaste algo da Ecos Largos e disseste que não tinhas acesso, e ela corrigiu-te dizendo que trabalha lá) — a deteção automática pode falhar para quem não tem conta própria no Basecamp. Depois de corrigido, continua a responder já assumindo a equipa certa, sem pedir para repetir a pergunta.",
        "input_schema": {
            "type": "object",
            "properties": {"empresa": {"type": "string", "enum": ["interior_guider", "ecos_largos", "ambas"]}},
            "required": ["empresa"]
        }
    },
    {
        "name": "atualizar_empresa_pessoa",
        "description": "Corrige a equipa/empresa registada no perfil de OUTRA pessoa (não o de quem está a falar contigo agora — para isso usa definir_empresa) — usa isto só quando um administrador pedir explicitamente para corrigir ou registar a empresa de alguém (ex: \"a empresa da Beatriz passou a ser só Ecos Largos\", \"regista a Maria como Interior Guider\"). `nome` tem de ser o nome exato da pessoa, tal como já está registado no perfil dela — só quem está autorizado consegue usar esta ferramenta, e ela recusa se não encontrar já um perfil guardado com esse nome exato (para nunca criar por engano um perfil novo/duplicado por causa de um nome mal escrito) — nesse caso, confirma o nome exato com quem pediu antes de tentares outra vez.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string", "description": "nome exato da pessoa cujo perfil vai ser corrigido, tal como já está registado"},
                "empresa": {"type": "string", "enum": ["interior_guider", "ecos_largos", "ambas"]}
            },
            "required": ["nome", "empresa"]
        }
    },
    {
        "name": "atualizar_parametro_estimativa",
        "description": "Ajusta um parâmetro numérico do \"Procedimento Tempos de Montagem para Logística\" (minutos por artigo, acréscimos, bandas de rendimento, custos de deslocação) — usa isto só quando pedirem explicitamente para mudar um valor deste procedimento (ex: \"muda os minutos de montagem normal para 35\"), tipicamente depois de um relatório de calibração mostrar um desvio consistente entre a estimativa e o real. Só quem está autorizado consegue usar esta ferramenta. Ajusta só o parâmetro pedido, um de cada vez — nunca vários ao mesmo tempo, para se perceber o efeito de cada mudança isoladamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string", "enum": [
                    "minutos_ligeiro", "minutos_normal", "minutos_pesado",
                    "acrescimo_fixa_parede_min", "acrescimo_candeeiro_teto_min",
                    "acrescimo_desmontado_inesperado_min", "fixo_paragem_min",
                    "fator_equipa_3_pessoas", "valor_dia_referencia_eur",
                    "banda_baixa_eur_hora", "banda_alta_eur_hora",
                    "fator_sem_elevador", "fator_obra", "fator_centro_historico",
                    "custo_km_combustivel_eur", "custo_km_manutencao_eur", "custo_hora_pessoa_eur"
                ]},
                "valor": {"type": "number"}
            },
            "required": ["nome", "valor"]
        }
    },
    {
        "name": "criar_eventos_calendario_entregas",
        "description": "Cria eventos reais na Agenda (calendário) do projeto \"Entregas\" no Basecamp — usa isto SÓ depois de a Conceição ou a Isa confirmarem explicitamente a proposta de agendamento (a que vem no fim da sugestão semanal de logística, ou discutida na conversa), com ou sem ajustes pedidos antes. Cria SEMPRE um evento por entrega E TAMBÉM um evento por cada viagem entre paragens (Armazém → 1ª paragem, paragem → paragem seguinte, última paragem → Armazém) — nunca só as entregas, os tempos de viagem fazem sempre parte da agenda, tal como já aparecem discriminados na \"Tabela preparatória de agendamento\" no fim da sugestão semanal (colunas evento/tempo estimado/custo). NUNCA chames isto só com base na proposta inicial sem confirmação explícita, e nunca inventes ou recalcules data/hora — usa sempre os valores exatos já confirmados na conversa ou já mostrados nessa tabela (nunca uma aritmética tua). Cada evento precisa de título, data (AAAA-MM-DD), hora de início e hora de fim (HH:MM) — para uma entrega usa a morada/cliente/produtos como descrição; para uma viagem, título \"Viagem: X → Y\" (tal como na tabela) chega, sem descrição obrigatória. Só quem está autorizado consegue usar esta ferramenta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "eventos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string"},
                            "data": {"type": "string", "description": "AAAA-MM-DD"},
                            "hora_inicio": {"type": "string", "description": "HH:MM"},
                            "hora_fim": {"type": "string", "description": "HH:MM"},
                            "descricao": {"type": "string", "description": "cliente, morada, produtos encomendados, e qualquer nota relevante para a equipa de entrega"}
                        },
                        "required": ["titulo", "data", "hora_inicio", "hora_fim"]
                    }
                }
            },
            "required": ["eventos"]
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
        "description": "Publica uma mensagem no Mural do Basecamp do teu projeto/equipa (o da Gestão, ou o da Ecos Largos se for alguém dessa equipa) — visível a quem lá está. USA ISTO SÓ quando o pedido for estrita e explicitamente para publicares no mural (ex: \"publica isto no mural\") — nunca por iniciativa própria, por achares um assunto importante, ou como forma de responder a uma pergunta geral. Qualquer outra situação (incluindo responder a uma menção numa tarefa/card) é sempre um comentário normal, nunca isto. Na consola de chat qualquer pessoa pode pedir. Vindo de uma menção no Basecamp, só podes usar isto quando o Rui, a Beatriz ou a Isa pedirem explicitamente — qualquer outra pessoa a pedir isso a partir do Basecamp, recusa e explica que só eles podem pedir por ali. Se quiseres notificar alguém em concreto na mensagem, escreve o nome como \"@Nome Completo\" — se corresponder a alguém com acesso a este projeto, vira uma menção a sério (notifica a pessoa), não só o nome em texto.",
        "input_schema": {
            "type": "object",
            "properties": {"assunto": {"type": "string"}, "mensagem": {"type": "string"}},
            "required": ["assunto", "mensagem"]
        }
    },
    {
        "name": "listar_mural_basecamp",
        "description": "Lista as mensagens mais recentes do Mural do Basecamp de um projeto (assunto, autor, data, quantos comentários tem) — usa isto sempre que precisares de encontrar um post anterior (ex: um resumo semanal/diário antigo, para comparar com o de agora, ou para ver se alguém já comentou algo lá). Por omissão lista o mural da Gestão; passa `projeto` para o mural de outra equipa (ex: \"Ecos Largos\"). Depois de encontrares o post certo, usa ler_mensagem_mural_basecamp com o `url` devolvido para leres o conteúdo e os comentários na íntegra.",
        "input_schema": {
            "type": "object",
            "properties": {
                "projeto": {"type": "string", "description": "Por omissão \"Gestão\" — passa o nome de outro projeto para o mural dele"},
                "limite": {"type": "integer", "description": "Quantas mensagens recentes listar — por omissão 20"}
            }
        }
    },
    {
        "name": "ler_mensagem_mural_basecamp",
        "description": "Lê o conteúdo completo e os comentários de uma mensagem do Mural, pelo `url` devolvido por listar_mural_basecamp. Usa isto para ver o que foi dito nos comentários de um post anterior — ex: pedidos de alteração que alguém tenha deixado num resumo automático.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    }
]

# Sugestões de mudança de comportamento: pedido do Rui e da Beatriz
# (2026-08-05) — quando alguém propõe uma mudança à forma como a Alma se
# comporta (não um facto sobre uma pessoa, isso é memorizar_facto), fica
# pendente até uma pessoa autorizada aprovar; só aí passa a memória global,
# visível em todas as conversas. Mesmo padrão do modo mudo em tools/voz.py:
# é sempre juízo da Alma reconhecer a intenção, nunca uma palavra-chave.
TOOLS_SUGESTAO = [
    {
        "name": "registar_sugestao_comportamento",
        "description": "Chama esta função sempre que reconheceres, pelo sentido da conversa (nunca por uma palavra-chave em concreto), que alguém está a propor uma mudança de comportamento ou de regra tua — ex: \"passa a responder sempre em tópicos\", \"não precisas de perguntar X, assume sempre Y\", \"a partir de agora, quando isto acontecer, faz aquilo\" — mesmo que a pessoa não use a palavra \"sugestão\" nem peça explicitamente para guardares nada, e em qualquer canal (consola, Basecamp, reunião). Fica registada como pendente, com um id. Diz sempre à pessoa, depois de chamares esta função, o id atribuído e que a aprovação só pode ser pedida no Basecamp, pelo Rui ou pela Beatriz (basta um dos dois) — só depois disso passa a aplicar-se de verdade, a todas as conversas, de qualquer pessoa. Nunca uses isto para um facto sobre uma pessoa em concreto (para isso usa memorizar_facto) nem para algo já cobertos por outra função restrita (ex: atualizar_parametro_estimativa) — só para mudanças de comportamento gerais, sem função própria.",
        "input_schema": {
            "type": "object",
            "properties": {"sugestao": {"type": "string", "description": "a mudança proposta, resumida com clareza suficiente para alguém aprovar sem ambiguidade"}},
            "required": ["sugestao"]
        }
    },
    {
        "name": "listar_sugestoes_pendentes",
        "description": "Lista as sugestões de mudança de comportamento ainda por aprovar (ver registar_sugestao_comportamento), com id, quem propôs, quando e em que canal — usa isto sempre que precisares de encontrar o id certo (ex: alguém no Basecamp diz \"aprovo a sugestão sobre X\" sem dizer o id — encontra aqui qual corresponde pelo conteúdo antes de chamares aprovar_sugestao/rejeitar_sugestao; só perguntas o id explicitamente se houver mesmo ambiguidade entre duas pendentes parecidas), ou quando alguém perguntar o que está pendente. Disponível em qualquer canal.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "aprovar_sugestao",
        "description": "Aprova uma sugestão de mudança de comportamento pendente (ver registar_sugestao_comportamento), pelo `id` — passa a valer para todas as conversas a partir daqui. Só funciona a partir do Basecamp, e só para o Rui ou a Beatriz (basta um dos dois, nunca aprovação dupla) — se o pedido vier da consola ou de outra pessoa, recusa e explica que a aprovação só pode ser pedida no Basecamp. Se não tiveres já o id exato da conversa, usa primeiro listar_sugestoes_pendentes para o encontrares pelo conteúdo.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"]
        }
    },
    {
        "name": "rejeitar_sugestao",
        "description": "Rejeita e apaga uma sugestão de mudança de comportamento pendente (ver registar_sugestao_comportamento), pelo `id`, sem a tornar memória. Mesma restrição de aprovar_sugestao: só a partir do Basecamp, só o Rui ou a Beatriz. Se não tiveres já o id exato, usa primeiro listar_sugestoes_pendentes.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"]
        }
    },
    {
        "name": "esquecer_regra_global",
        "description": "Apaga da memória global (ver aprovar_sugestao) as regras/decisões já aprovadas que contenham o termo indicado — usa quando pedirem para deixares de aplicar algo já aprovado antes, em todas as conversas. Mesma restrição de aprovar_sugestao: só a partir do Basecamp, só o Rui ou a Beatriz.",
        "input_schema": {
            "type": "object",
            "properties": {"termo": {"type": "string"}},
            "required": ["termo"]
        }
    },
    {
        "name": "listar_memoria_global",
        "description": "Lista as regras/decisões já aprovadas, ativas em todas as conversas (o facto, quem propôs, quem aprovou, quando) — usa isto sempre que alguém perguntar o que já foi decidido/aprovado sobre o teu comportamento, para responderes com a lista real em vez de tentares recordar de cabeça. Disponível em qualquer canal, sem restrição — é só consulta, não uma alteração.",
        "input_schema": {"type": "object", "properties": {}}
    }
]

# pedido explícito do Rui (2026-07-27): corrigir a empresa registada no
# perfil de OUTRA pessoa (não o de quem está a falar) afeta o routing dela
# em toda a aplicação — só o Rui e a Beatriz podem fazer isto, em qualquer
# canal (consola ou Basecamp), ao contrário de publicar_mural (que só
# restringe vindo do Basecamp). Ajustar esta lista se mais alguém precisar
# de poder fazer isto.
_AUTORIZADOS_ATUALIZAR_EMPRESA_ALHEIA = ("rui", "beatriz")

def _atualizar_empresa_pessoa_restrito(utilizador: str, nome: str, empresa: str) -> dict:
    if not any(autorizado in utilizador.lower() for autorizado in _AUTORIZADOS_ATUALIZAR_EMPRESA_ALHEIA):
        return {"erro": f"{utilizador} não tem autorização para alterar a empresa registada de outra pessoa"}
    if not db.perfil_existe(nome):
        return {"erro": f"não encontrei nenhum perfil guardado exatamente com o nome {nome!r} — "
                        "confirma o nome exato (como está registado) antes de tentar outra vez"}
    return db.atualizar_empresa(nome, empresa)

# pedido explícito do Rui (2026-07-28): ajustar um parâmetro do procedimento
# de tempos de montagem (ver tools/tempos_montagem.py) é uma decisão de
# negócio, não uma ação de logística do dia a dia — mesma restrição de quem
# pode corrigir a empresa de outra pessoa (só o Rui e a Beatriz), nunca só
# por instrução no texto da missão.
def _atualizar_parametro_estimativa_restrito(utilizador: str, nome: str, valor: float) -> dict:
    if not any(autorizado in utilizador.lower() for autorizado in _AUTORIZADOS_ATUALIZAR_EMPRESA_ALHEIA):
        return {"erro": f"{utilizador} não tem autorização para alterar parâmetros da estimativa de montagem"}
    return db.atualizar_parametro_estimativa(nome, valor)

# Sugestões de mudança de comportamento (ver TOOLS_SUGESTAO): qualquer
# pessoa, em qualquer canal, pode propor — mas só passa a valer para todas
# as conversas depois de aprovada por alguém autorizado, e basta UMA pessoa
# desta lista (nunca aprovação dupla). Pedido explícito do Rui (2026-08-05):
# ao contrário da proposta (aberta a qualquer canal), a aprovação/rejeição/
# remoção só pode ser pedida a partir do Basecamp — nunca da consola, mesmo
# que seja o Rui ou a Beatriz a pedir.
def _registar_sugestao_comportamento(utilizador: str, sugestao: str, origem: str) -> dict:
    return db.registar_sugestao_comportamento(sugestao, utilizador, origem)

def _alteracao_memoria_global_autorizada(utilizador: str, origem: str) -> dict:
    """Devolve o erro comum às três operações abaixo, ou None se autorizado."""
    if origem != "basecamp":
        return {"erro": "alterações à memória global só podem ser pedidas a partir do Basecamp"}
    if not any(autorizado in utilizador.lower() for autorizado in _AUTORIZADOS_ATUALIZAR_EMPRESA_ALHEIA):
        return {"erro": f"{utilizador} não tem autorização para alterar a memória global"}
    return None

def _aprovar_sugestao_restrito(utilizador: str, id: int, origem: str) -> dict:
    return _alteracao_memoria_global_autorizada(utilizador, origem) or db.aprovar_sugestao_pendente(id, utilizador)

def _rejeitar_sugestao_restrito(utilizador: str, id: int, origem: str) -> dict:
    erro = _alteracao_memoria_global_autorizada(utilizador, origem)
    if erro:
        return erro
    if not db.eliminar_sugestao_pendente(id):
        return {"erro": f"não encontrei nenhuma sugestão pendente com id {id}"}
    return {"rejeitado": True, "id": id}

def _esquecer_regra_global_restrito(utilizador: str, termo: str, origem: str) -> dict:
    return _alteracao_memoria_global_autorizada(utilizador, origem) or db.esquecer_factos_globais(termo)

_AUTORIZADOS_MURAL = ("rui", "beatriz", "isa")

def _publicar_mural_restrito(utilizador: str, assunto: str, mensagem: str, origem: str, projeto: str):
    if origem == "basecamp" and not any(nome in utilizador.lower() for nome in _AUTORIZADOS_MURAL):
        return {"erro": "só o Rui, a Beatriz ou a Isa podem pedir uma publicação no mural a partir do Basecamp"}
    return basecamp.publicar_mural(assunto, mensagem, projeto=projeto)

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

# Algumas tools recebem, num argumento, um texto que TEM de aparecer na
# resposta visível — mas confiar no modelo para o repetir na sua própria
# resposta falhava sistematicamente (ver guardar_avaliacao_carga_toros:
# mesmo depois de 3 reforços sucessivos na missão, o modelo continuava a
# escrever a avaliação detalhada inteira SÓ dentro deste argumento —
# invisível à pessoa — e a deixar um resumo curto como resposta visível).
# Em vez de continuar a tentar convencer por instrução, o conteúdo deste
# campo passa a ser sempre forçado para a resposta, por código — nome da
# tool -> nome do argumento a forçar.
CAMPOS_FORCADOS_NA_RESPOSTA = {
    "guardar_avaliacao_carga_toros": "avaliacao",
}

def _executar_tool_uses(blocos: list, funcoes_utilizador: dict) -> tuple:
    resultados = []
    texto_forcado = ""
    for bloco in blocos:
        if bloco.type == "tool_use":
            try:
                funcao = funcoes_utilizador.get(bloco.name) or FUNCOES[bloco.name]
                out = funcao(**bloco.input)
            except Exception as e:
                print(f"[ferramenta] {bloco.name}({bloco.input}) falhou: {e!r}")
                out = {"erro": str(e)}
            else:
                campo = CAMPOS_FORCADOS_NA_RESPOSTA.get(bloco.name)
                valor = bloco.input.get(campo) if campo else None
                if valor:
                    texto_forcado += "\n\n" + valor
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloco.id,
                "content": json.dumps(out, ensure_ascii=False, default=str)
            })
    return resultados, texto_forcado

def _preparar(system_prompt: str, tools: list, utilizador: str, origem: str, projeto_mural: str):
    # memória global (ver TOOLS_SUGESTAO/contexto_global) aplica-se a
    # qualquer conversa, por isso vem sempre antes da memória desta pessoa
    # em concreto, nunca condicionada a ela.
    # a data de hoje vem sempre aqui, fora do bloco de system prompt em
    # cache (que nunca é reprocessado entre pedidos) — nunca dentro dele,
    # ou ficaria presa à data do dia em que a cache foi criada.
    contexto = "\n\n".join(c for c in (tempo.contexto_data_atual(), db.contexto_global(),
                                       db.contexto_utilizador(utilizador)) if c)
    system = _system_com_cache(system_prompt, contexto)
    tools_completas = _tools_com_cache(
        tools + TOOLS_MEMORIA + TOOLS_MURAL + TOOLS_SUGESTAO + documentos_gerados.TOOLS_DOCUMENTOS_GERADOS
        + portal_projeto.TOOLS_PORTAL_PROJETO + tempo.TOOLS_TEMPO + calculadora.TOOLS_CALCULADORA)
    funcoes_utilizador = {
        "memorizar_facto": lambda facto: db.memorizar_facto(utilizador, facto),
        "esquecer": lambda termo: db.esquecer_factos(utilizador, termo),
        "registar_sugestao_comportamento": lambda sugestao: _registar_sugestao_comportamento(
            utilizador, sugestao, origem),
        "listar_sugestoes_pendentes": lambda: db.sugestoes_pendentes_lista(),
        "aprovar_sugestao": lambda id: _aprovar_sugestao_restrito(utilizador, id, origem),
        "rejeitar_sugestao": lambda id: _rejeitar_sugestao_restrito(utilizador, id, origem),
        "esquecer_regra_global": lambda termo: _esquecer_regra_global_restrito(utilizador, termo, origem),
        "listar_memoria_global": lambda: db.memoria_global_lista(),
        "definir_empresa": lambda empresa: db.atualizar_empresa(utilizador, empresa),
        "atualizar_empresa_pessoa": lambda nome, empresa: _atualizar_empresa_pessoa_restrito(
            utilizador, nome, empresa),
        "atualizar_parametro_estimativa": lambda nome, valor: _atualizar_parametro_estimativa_restrito(
            utilizador, nome, valor),
        "criar_eventos_calendario_entregas": lambda eventos: agendamento_entregas.criar_eventos_calendario_entregas_restrito(
            utilizador, eventos),
        "publicar_mural": lambda assunto, mensagem: _publicar_mural_restrito(
            utilizador, assunto, mensagem, origem, projeto_mural),
        "listar_mural_basecamp": lambda projeto="Gestão", limite=20: basecamp.listar_mural(projeto, limite),
        "ler_mensagem_mural_basecamp": lambda url: basecamp.ler_mensagem_mural(url),
        "gerar_pdf": lambda titulo, conteudo_markdown: documentos_gerados.gerar_pdf(
            utilizador, titulo, conteudo_markdown),
        "gerar_excel": lambda titulo, colunas, linhas, subtitulo=None, linhas_destacadas=None: documentos_gerados.gerar_excel(
            utilizador, titulo, colunas, linhas, subtitulo, linhas_destacadas),
        "obter_conteudo_documento_gerado": lambda id: documentos_gerados.obter_conteudo_documento_gerado(
            utilizador, id),
        "gerar_portal_projeto": lambda **kwargs: portal_projeto.gerar_portal_projeto(utilizador, **kwargs),
    }
    return system, tools_completas, funcoes_utilizador

def correr_agente(system_prompt: str, tools: list, mensagens: list, utilizador: str,
                  modelo: str = "claude-sonnet-4-6", origem: str = "consola",
                  projeto_mural: str = "Gestão") -> str:
    """Loop de agente com memória por utilizador: chama o modelo, executa tools até haver resposta final.

    `utilizador` deve ser o identificador real da pessoa (o mesmo em qualquer
    canal), para o perfil e a memória de longo prazo serem partilhados —
    `origem` ("consola" ou "basecamp") é só para decidir permissões
    (ex: quem pode pedir uma publicação no mural), nunca para identificar
    quem é a pessoa. `projeto_mural` é o mural onde publicar_mural publica —
    cada agente declara o seu (ex: o da Ecos Largos usa "Ecos Largos"),
    porque é o agente escolhido para esta mensagem que sabe isso, não o
    utilizador em si (alguém pode trabalhar com as duas equipas)."""
    system, tools_completas, funcoes_utilizador = _preparar(system_prompt, tools, utilizador, origem, projeto_mural)

    # acumula o texto de TODAS as rondas, não só da última — uma ronda com
    # texto antes de uma tool_use (ex: a Alma escreve a avaliação e só
    # depois chama guardar_avaliacao_carga_toros) não pode perder-se; só
    # devolver o texto da ronda final ignorava por completo o que tivesse
    # sido escrito antes de qualquer chamada a uma tool.
    partes_resposta = []
    while True:
        resposta = client.messages.create(
            model=modelo, max_tokens=MAX_TOKENS_RESPOSTA,
            system=system, tools=tools_completas, messages=mensagens
        )
        texto_ronda = "".join(b.text for b in resposta.content if b.type == "text")
        if texto_ronda:
            partes_resposta.append(texto_ronda)

        if resposta.stop_reason == "pause_turn":
            # o próprio servidor atingiu o limite de rondas de uma tool sua
            # (ex: várias pesquisas na internet seguidas, ver TOOLS_INTERNET)
            # — reenviar a mesma conversa retoma sozinho, sem repetir nada
            mensagens.append({"role": "assistant", "content": resposta.content})
            continue

        if resposta.stop_reason != "tool_use":
            return "".join(partes_resposta)

        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados, texto_forcado = _executar_tool_uses(resposta.content, funcoes_utilizador)
        if texto_forcado:
            partes_resposta.append(texto_forcado)
        mensagens.append({"role": "user", "content": resultados})

def correr_agente_stream(system_prompt: str, tools: list, mensagens: list, utilizador: str,
                         modelo: str = "claude-sonnet-4-6", origem: str = "consola",
                         projeto_mural: str = "Gestão"):
    """Generator: dá 'yield' a pedaços de texto da resposta final, à medida
    que chegam do modelo. Rondas de tool-use são resolvidas por completo (sem
    stream) antes disso — só a resposta final visível à pessoa é transmitida
    em tempo real.

    Enquanto uma tool está a correr (pode demorar bastante, ex: uma consulta
    ao Basecamp numa conta com muito histórico), o generator dá 'yield' a
    None de vez em quando — um sinal de vida, não texto real — para quem
    consome o stream saber que a Alma continua a tratar do pedido, em vez de
    parecer parada."""
    system, tools_completas, funcoes_utilizador = _preparar(system_prompt, tools, utilizador, origem, projeto_mural)

    while True:
        with client.messages.stream(
            model=modelo, max_tokens=MAX_TOKENS_RESPOSTA,
            system=system, tools=tools_completas, messages=mensagens
        ) as stream:
            for texto in stream.text_stream:
                yield texto
            resposta = stream.get_final_message()

        if resposta.stop_reason == "pause_turn":
            # ver correr_agente: o servidor atingiu o limite de rondas de uma
            # tool sua — reenviar a mesma conversa retoma sozinho
            mensagens.append({"role": "assistant", "content": resposta.content})
            continue

        if resposta.stop_reason != "tool_use":
            return

        mensagens.append({"role": "assistant", "content": resposta.content})

        resultado = {}
        def _correr_tools():
            resultado["saida"], resultado["forcado"] = _executar_tool_uses(resposta.content, funcoes_utilizador)
        tarefa = threading.Thread(target=_correr_tools, daemon=True)
        tarefa.start()
        while tarefa.is_alive():
            tarefa.join(timeout=_INTERVALO_SINAL_DE_VIDA)
            if tarefa.is_alive():
                yield None

        # ver CAMPOS_FORCADOS_NA_RESPOSTA: o texto de certos argumentos (ex:
        # a avaliação detalhada passada a guardar_avaliacao_carga_toros) é
        # sempre transmitido, mesmo que o modelo não o repita por si.
        if resultado.get("forcado"):
            yield resultado["forcado"]

        mensagens.append({"role": "user", "content": resultado["saida"]})
