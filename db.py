# db.py — ligação Postgres + schema + memória partilhada
import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversas (
    id SERIAL PRIMARY KEY,
    utilizador TEXT NOT NULL,
    sessao TEXT NOT NULL,
    papel TEXT NOT NULL,          -- 'user' | 'assistant'
    conteudo TEXT NOT NULL,
    agente TEXT,                  -- que agente respondeu (invisível ao utilizador)
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisoes (
    id SERIAL PRIMARY KEY,
    tema TEXT NOT NULL,
    decisao TEXT NOT NULL,
    origem TEXT,                  -- conversa, reunião, Basecamp
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS routing_log (
    id SERIAL PRIMARY KEY,
    pergunta TEXT,
    agente_escolhido TEXT,
    correto BOOLEAN,              -- preenchido na revisão semanal
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS perfis (
    utilizador TEXT PRIMARY KEY,
    papel TEXT,
    estilo_resposta TEXT,
    formato TEXT,
    decisao TEXT,
    dificuldades TEXT,
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memoria_utilizador (
    id SERIAL PRIMARY KEY,
    utilizador TEXT NOT NULL,
    facto TEXT NOT NULL,
    criado_em TIMESTAMPTZ DEFAULT now()
);

-- sugestões de mudança de comportamento da Alma, feitas por qualquer pessoa
-- em qualquer conversa (ver registar_sugestao_comportamento em
-- agents/base.py) — ficam aqui até serem aprovadas ou rejeitadas, porque a
-- aprovação pode vir de outro dia, outra pessoa, ou outro canal, nunca da
-- mesma troca de mensagens que gerou a sugestão.
CREATE TABLE IF NOT EXISTS sugestoes_pendentes (
    id SERIAL PRIMARY KEY,
    sugestao TEXT NOT NULL,
    proposto_por TEXT NOT NULL,
    origem TEXT,                  -- consola, basecamp, reunião
    criado_em TIMESTAMPTZ DEFAULT now()
);

-- memória aprovada (ver sugestoes_pendentes acima) que afeta TODAS as
-- conversas, de qualquer utilizador, em qualquer canal — ao contrário de
-- memoria_utilizador, que só é lida no contexto da própria pessoa.
CREATE TABLE IF NOT EXISTS memoria_global (
    id SERIAL PRIMARY KEY,
    facto TEXT NOT NULL,
    proposto_por TEXT,
    aprovado_por TEXT NOT NULL,
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS basecamp_alertas (
    recording_id BIGINT PRIMARY KEY,
    prazo DATE,                   -- due_on no momento do alerta, para saber se mudou
    comentario TEXT,
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS basecamp_eventos_processados (
    comment_id BIGINT PRIMARY KEY,   -- id do comentário/tarefa/card que mencionou a Alma
    resposta TEXT,
    criado_em TIMESTAMPTZ DEFAULT now()
);

-- mapeamento id da Agenda (Schedule) do Basecamp, projeto Entregas -> id do
-- evento no Google Calendar (ver tools/google_calendar.py e
-- agents/sincronizacao_calendario.py) — sincronização unidirecional
-- (Basecamp -> Google, nunca ao contrário), pedido do Rui (2026-07-29).
-- titulo/inicio/fim guardam o último estado sincronizado, para detetar
-- alterações num próximo ciclo sem ter de voltar a pedir o evento ao
-- Google Calendar só para comparar.
CREATE TABLE IF NOT EXISTS basecamp_google_calendar_sync (
    entry_id BIGINT PRIMARY KEY,     -- id da entrada na Agenda do Basecamp
    google_event_id TEXT NOT NULL,
    titulo TEXT,
    inicio TEXT,
    fim TEXT,
    criado_em TIMESTAMPTZ DEFAULT now(),
    atualizado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reunioes_em_curso (
    sessao TEXT PRIMARY KEY,
    excertos JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"indice": "texto transcrito"}
    processados INTEGER NOT NULL DEFAULT 0,
    criado_em TIMESTAMPTZ DEFAULT now(),
    atualizado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logistica_alertas (
    recording_id BIGINT NOT NULL,
    condicao TEXT NOT NULL,        -- 'A'..'I', ver tools/logistica.py
    criado_em TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (recording_id, condicao)
);

CREATE TABLE IF NOT EXISTS avaliacoes_cargas_toros (
    id SERIAL PRIMARY KEY,
    fornecedor TEXT NOT NULL,
    quantidade TEXT,                -- peso/quantidade da carga, texto livre (as unidades variam)
    data_carga TEXT,                -- data da carga tal como mencionada (texto livre, não normalizada)
    talao TEXT,                     -- número do talão
    avaliacao TEXT NOT NULL,        -- os pontos importantes da avaliação em si
    ano INTEGER NOT NULL,           -- calculado em Python (ver tools/ecos_largos), nunca pelo modelo
    criado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documentos_gerados (
    id SERIAL PRIMARY KEY,
    utilizador TEXT,                -- quem pediu o documento (ver migração abaixo p/ tabelas já existentes)
    titulo TEXT NOT NULL,
    formato TEXT NOT NULL DEFAULT 'pdf',  -- 'pdf' ou 'xlsx' (ver tools/documentos_gerados.py)
    pdf BYTEA NOT NULL,             -- guardado em Postgres, não em disco (Railway não persiste disco entre
                                     -- deploys) — nome histórico da coluna, guarda o ficheiro final em
                                     -- qualquer formato (pdf ou xlsx), não só PDF
    conteudo_markdown TEXT,         -- fonte usada para gerar o documento (markdown p/ pdf, JSON de
                                     -- colunas/linhas p/ xlsx), para a Alma poder reler/reaproveitar depois
    criado_em TIMESTAMPTZ DEFAULT now()
);

-- leitura diária do estado de um projeto do Basecamp (ver
-- agents/mensagem_motivacional_diaria.py) — guardada para se poder comparar
-- a leitura de hoje com a última leitura anterior (evolução real, não só
-- uma fotografia isolada de um dia). Uma linha por (data, projeto); se a
-- corrida repetir no mesmo dia, substitui a leitura desse dia em vez de
-- duplicar.
CREATE TABLE IF NOT EXISTS snapshot_diario_projetos (
    data DATE NOT NULL,
    projeto TEXT NOT NULL,
    total_ativos INTEGER NOT NULL,
    atrasados INTEGER NOT NULL,
    parados INTEGER NOT NULL,
    por_estado JSONB NOT NULL DEFAULT '{}'::jsonb,
    criado_em TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (data, projeto)
);

-- parâmetros numéricos do "Procedimento Tempos de Montagem para Logística"
-- (projeto Alma Data) — ver tools/tempos_montagem.py. Guardados em DB, não
-- hardcoded, precisamente para poderem ser calibrados de 2 em 2 meses (ver
-- agents/estimativa_montagem.py) sem precisar de alteração de código.
CREATE TABLE IF NOT EXISTS parametros_estimativa (
    chave TEXT PRIMARY KEY,
    valor NUMERIC NOT NULL,
    atualizado_em TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS estimativas_montagem (
    recording_id BIGINT PRIMARY KEY,
    titulo TEXT,
    url_api TEXT,                  -- para reler o registo depois (nunca reconstruído à mão)
    comments_url TEXT,              -- para ler comentários e encontrar o "Real" depois da entrega
    publicado_em TIMESTAMPTZ DEFAULT now(),
    estimativa_minutos NUMERIC,
    valor_encomenda NUMERIC,
    decomposicao JSONB,
    confianca TEXT,
    real_minutos NUMERIC,
    real_pessoas INTEGER,
    real_ocorrencias TEXT,
    real_registado_em TIMESTAMPTZ,
    calibrado BOOLEAN NOT NULL DEFAULT false
);

-- pausa das publicações automáticas da Alma (mensagem diária, alertas de
-- atraso, avisos de agendas/logística) — pedido explícito do Rui
-- (2026-08-07, post "Boas férias!" no Mural da Gestão): durante férias ou
-- paragens da equipa, os jobs agendados não devem publicar nem marcar
-- pessoas. `ativa` permite terminar uma pausa antes da data prevista (ver
-- retomar_publicacoes_automaticas) sem apagar o histórico.
CREATE TABLE IF NOT EXISTS pausas_automaticas (
    id SERIAL PRIMARY KEY,
    data_inicio DATE NOT NULL,
    data_fim DATE NOT NULL,
    motivo TEXT,
    criado_por TEXT,
    ativa BOOLEAN NOT NULL DEFAULT true,
    criado_em TIMESTAMPTZ DEFAULT now()
);
"""

# período de férias já anunciado pelo Rui no Mural da Gestão (post "Boas
# férias!", 2026-08-07): "não precisas de publicar as mensagens de início
# de dia, nem tagar durante este período porque estamos de férias". Antes
# desta tabela existir, esse pedido só gerava, no máximo, um comentário
# isolado de resposta — nada persistia para os jobs agendados consultarem,
# por isso a Alma continuava a publicar e a marcar pessoas todos os dias
# durante a própria pausa que lhe tinham pedido (bug real, 2026-08-10).
# Semeado aqui (em vez de só pela ferramenta pausar_publicacoes_automaticas)
# para valer já a partir do próximo arranque, sem depender de a Alma voltar
# a processar aquele post. WHERE NOT EXISTS evita duplicar em cada arranque.
SEED_PAUSA_FERIAS_AGOSTO_2026 = """
INSERT INTO pausas_automaticas (data_inicio, data_fim, motivo, criado_por)
SELECT '2026-08-10', '2026-08-23', 'férias da equipa (post de Rui Rocha no Mural, 2026-08-07)', 'Rui Rocha'
WHERE NOT EXISTS (
    SELECT 1 FROM pausas_automaticas WHERE data_inicio = '2026-08-10' AND data_fim = '2026-08-23'
);
"""

# valores exatamente os do "Procedimento Tempos de Montagem para Logística"
# (Rui/documento, 2026-07-28) — ON CONFLICT DO NOTHING para nunca sobrescrever
# um valor já calibrado manualmente com o valor de origem do documento.
SEED_PARAMETROS_ESTIMATIVA = """
INSERT INTO parametros_estimativa (chave, valor) VALUES
    ('minutos_ligeiro', 10),
    ('minutos_normal', 30),
    ('minutos_pesado', 75),
    ('acrescimo_fixa_parede_min', 40),
    ('acrescimo_candeeiro_teto_min', 30),
    ('acrescimo_desmontado_inesperado_min', 45),
    ('fixo_paragem_min', 40),
    ('fator_equipa_3_pessoas', 0.75),
    ('valor_dia_referencia_eur', 15000),
    ('banda_baixa_eur_hora', 800),
    ('banda_alta_eur_hora', 4000),
    ('fator_sem_elevador', 1.4),
    ('fator_obra', 1.3),
    ('fator_centro_historico', 1.2),
    ('custo_km_combustivel_eur', 0.23),
    ('custo_km_manutencao_eur', 0.12),
    ('custo_hora_pessoa_eur', 15)
ON CONFLICT (chave) DO NOTHING;
"""

# à parte do SCHEMA principal: a tabela perfis já existe em produção com
# dados reais, e CREATE TABLE IF NOT EXISTS não acrescenta colunas novas a
# uma tabela já existente — precisa de um ALTER TABLE explícito, idempotente.
# O mesmo para avaliacoes_cargas_toros: os campos importantes (fornecedor,
# quantidade, data_carga, talao, avaliacao) foram pedidos depois da tabela
# já ter sido criada com um esquema mais simples (cliente/resumo) — estas
# colunas ficam de fora nesse caso até serem acrescentadas aqui.
MIGRACOES = """
ALTER TABLE perfis ADD COLUMN IF NOT EXISTS empresa TEXT;
ALTER TABLE avaliacoes_cargas_toros ADD COLUMN IF NOT EXISTS fornecedor TEXT;
ALTER TABLE avaliacoes_cargas_toros ADD COLUMN IF NOT EXISTS quantidade TEXT;
ALTER TABLE avaliacoes_cargas_toros ADD COLUMN IF NOT EXISTS data_carga TEXT;
ALTER TABLE avaliacoes_cargas_toros ADD COLUMN IF NOT EXISTS talao TEXT;
ALTER TABLE avaliacoes_cargas_toros ADD COLUMN IF NOT EXISTS avaliacao TEXT;
ALTER TABLE documentos_gerados ADD COLUMN IF NOT EXISTS utilizador TEXT;
ALTER TABLE documentos_gerados ADD COLUMN IF NOT EXISTS conteudo_markdown TEXT;
ALTER TABLE documentos_gerados ADD COLUMN IF NOT EXISTS formato TEXT NOT NULL DEFAULT 'pdf';
-- card_id do Basecamp, só usado pelo portal de projeto (ver
-- tools/portal_projeto.py) — permite atualizar o mesmo registo (e por
-- isso manter o mesmo link) em vez de criar um documento novo a cada
-- vez que o portal de um projeto é gerado outra vez. NULL para todos os
-- outros tipos de documento (PDF/Excel gerados por gerar_pdf/gerar_excel).
ALTER TABLE documentos_gerados ADD COLUMN IF NOT EXISTS card_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS documentos_gerados_card_id_idx
    ON documentos_gerados (card_id) WHERE card_id IS NOT NULL;
"""

# bug real, encontrado nos logs do Railway (2026-07-22): a tabela em
# produção foi criada há muito com o esquema antigo (cliente/resumo,
# ambas colunas NOT NULL) — as migrações acima só ACRESCENTARAM colunas
# novas, nunca mexeram nas antigas. Como o INSERT atual (ver
# guardar_avaliacao_carga_toros) nunca preenche "cliente" nem "resumo",
# TODAS as gravações têm falhado desde essa mudança de esquema, sempre
# com NotNullViolation — silenciosamente, do ponto de vista de quem
# pergunta (o erro só aparecia nos logs). "ALTER COLUMN ... DROP NOT
# NULL" não tem uma forma "IF EXISTS" direta, e instalações novas (via
# CREATE TABLE acima) nunca chegam a ter estas colunas — por isso o bloco
# verifica primeiro se a coluna existe, para ser seguro correr sempre,
# em qualquer ambiente.
MIGRACAO_CLIENTE_RESUMO_NULAVEL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'avaliacoes_cargas_toros' AND column_name = 'cliente') THEN
        ALTER TABLE avaliacoes_cargas_toros ALTER COLUMN cliente DROP NOT NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'avaliacoes_cargas_toros' AND column_name = 'resumo') THEN
        ALTER TABLE avaliacoes_cargas_toros ALTER COLUMN resumo DROP NOT NULL;
    END IF;
END $$;
"""

def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def inicializar_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute(MIGRACOES)
            cur.execute(MIGRACAO_CLIENTE_RESUMO_NULAVEL)
            cur.execute(SEED_PARAMETROS_ESTIMATIVA)
            cur.execute(SEED_PAUSA_FERIAS_AGOSTO_2026)
        conn.commit()

def guardar_mensagem(utilizador: str, sessao: str, papel: str, conteudo: str, agente: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversas (utilizador, sessao, papel, conteudo, agente)
                   VALUES (%s, %s, %s, %s, %s)""",
                (utilizador, sessao, papel, conteudo, agente)
            )
        conn.commit()

_LIMITE_CARATERES_RECORTE_ANTERIOR = 6000  # texto, não tokens — não precisa de ser exato

def _recorte_sessao_anterior(utilizador: str, sessao_atual: str, limite_mensagens: int = 12) -> list[dict]:
    """Só chamado por historico_sessao quando a sessão atual está mesmo a
    começar (sem mensagens ainda) — nunca a meio de uma conversa já em
    curso, para não repetir isto a cada troca.

    Pedido do Rui (2026-08-05): a Alma só tinha memória do que decidisse
    guardar explicitamente (memorizar_facto, ver contexto_utilizador) ou
    da sessão ATUAL — nada da conversa anterior a essa, mesmo que tivesse
    sido no próprio dia anterior. Devolve um recorte em BRUTO das últimas
    mensagens da sessão anterior mais recente desta pessoa, como um par
    user/assistant sintético logo no início da conversa — de propósito
    não é um resumo: resumir custava uma chamada extra ao modelo a cada
    sessão nova, e arriscava perder ou deturpar algo que depois fizesse
    falta; o recorte mostra exatamente o que foi dito. [] se não houver
    nenhuma sessão anterior."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sessao, MAX(criado_em) AS ultima_atividade
                   FROM conversas
                   WHERE utilizador = %s AND sessao != %s
                   GROUP BY sessao
                   ORDER BY ultima_atividade DESC
                   LIMIT 1""",
                (utilizador, sessao_atual)
            )
            anterior = cur.fetchone()
            if not anterior:
                return []
            cur.execute(
                """SELECT papel, conteudo FROM conversas
                   WHERE sessao = %s AND utilizador = %s
                   ORDER BY criado_em DESC LIMIT %s""",
                (anterior["sessao"], utilizador, limite_mensagens)
            )
            linhas = list(reversed(cur.fetchall()))
    if not linhas:
        return []
    texto = "\n\n".join(f"{'Tu' if l['papel'] == 'assistant' else 'Eu'}: {l['conteudo']}" for l in linhas)
    # corta pelo início (mantém o fim, mais recente) se ainda assim for
    # muito grande — ex: uma das mensagens tinha uma tabela enorme
    texto = texto[-_LIMITE_CARATERES_RECORTE_ANTERIOR:]
    quando = anterior["ultima_atividade"].strftime("%d/%m às %H:%M")
    return [
        {"role": "user", "content": (
            f"(nota, não é uma pergunta — recorte da nossa conversa anterior, de {quando}, só "
            f"para teres contexto do que já falámos; não respondas a isto, espera pela mensagem "
            f"a seguir)\n\n{texto}"
        )},
        {"role": "assistant", "content": "Entendido, tenho isso em conta."}
    ]

def historico_sessao(sessao: str, utilizador: str, limite: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT papel, conteudo FROM (
                       SELECT papel, conteudo, criado_em FROM conversas
                       WHERE sessao = %s AND utilizador = %s
                       ORDER BY criado_em DESC
                       LIMIT %s
                   ) AS recentes
                   ORDER BY criado_em ASC""",
                (sessao, utilizador, limite)
            )
            linhas = cur.fetchall()
    return [{"role": l["papel"], "content": l["conteudo"]} for l in linhas]

def historico_sessao_para_modelo(sessao: str, utilizador: str, limite: int = 20) -> list[dict]:
    """Como historico_sessao, mas com o recorte da sessão anterior (ver
    _recorte_sessao_anterior) à frente, quando esta sessão está mesmo a
    começar — só para o que é dado ao modelo como contexto, nunca para o
    que fica visível na consola (/historico/{sessao} usa historico_sessao
    diretamente, sem isto, porque essa nota sintética não é uma mensagem
    real da conversa e não deve aparecer no ecrã)."""
    mensagens = historico_sessao(sessao, utilizador, limite)
    if not mensagens:
        mensagens = _recorte_sessao_anterior(utilizador, sessao) + mensagens
    return mensagens

def sessoes_utilizador(utilizador: str, limite: int = 30) -> list[dict]:
    """Sessões recentes de um utilizador, com preview da primeira mensagem — para a barra lateral."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sessao,
                          MAX(criado_em) AS ultima_atividade,
                          (ARRAY_AGG(conteudo ORDER BY criado_em ASC)
                               FILTER (WHERE papel = 'user'))[1] AS preview
                   FROM conversas
                   WHERE utilizador = %s
                   GROUP BY sessao
                   ORDER BY ultima_atividade DESC
                   LIMIT %s""",
                (utilizador, limite)
            )
            return cur.fetchall()

def eliminar_sessao(sessao: str, utilizador: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversas WHERE sessao = %s AND utilizador = %s",
                (sessao, utilizador)
            )
        conn.commit()

def log_routing(pergunta: str, agente_escolhido: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO routing_log (pergunta, agente_escolhido)
                   VALUES (%s, %s)""",
                (pergunta, agente_escolhido)
            )
        conn.commit()

def perfil_existe(utilizador: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM perfis WHERE utilizador = %s", (utilizador,))
            return cur.fetchone() is not None

def guardar_perfil(utilizador: str, papel: str, estilo_resposta: str,
                   formato: str, decisao: str, dificuldades: str, empresa: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO perfis (utilizador, papel, estilo_resposta, formato, decisao, dificuldades, empresa)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (utilizador) DO UPDATE SET
                       papel = EXCLUDED.papel, estilo_resposta = EXCLUDED.estilo_resposta,
                       formato = EXCLUDED.formato, decisao = EXCLUDED.decisao,
                       dificuldades = EXCLUDED.dificuldades, empresa = EXCLUDED.empresa""",
                (utilizador, papel, estilo_resposta, formato, decisao, dificuldades, empresa)
            )
        conn.commit()
    return {"guardado": True}

def obter_perfil(utilizador: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM perfis WHERE utilizador = %s", (utilizador,))
            return cur.fetchone()

def atualizar_empresa(utilizador: str, empresa: str):
    """Corrige só a equipa/empresa registada no perfil, sem repetir todo o
    acolhimento — usado quando alguém já tem perfil mas a Alma não a está a
    reconhecer corretamente como sendo da Ecos Largos (ou da Interior
    Guider), ex: porque nunca lhe foi perguntado isto explicitamente, ou
    porque a deteção automática pela equipa do projeto no Basecamp falhou
    (só funciona para quem tem conta no Basecamp — muita gente da Ecos
    Largos fala só pela consola)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO perfis (utilizador, empresa) VALUES (%s, %s)
                   ON CONFLICT (utilizador) DO UPDATE SET empresa = EXCLUDED.empresa""",
                (utilizador, empresa)
            )
        conn.commit()
    return {"guardado": True, "empresa": empresa}

def memorizar_facto(utilizador: str, facto: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memoria_utilizador (utilizador, facto) VALUES (%s, %s)",
                (utilizador, facto)
            )
        conn.commit()
    return {"memorizado": facto}

def esquecer_factos(utilizador: str, termo: str):
    """Apaga factos que contenham o termo. Devolve quantos apagou."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM memoria_utilizador
                   WHERE utilizador = %s AND facto ILIKE %s""",
                (utilizador, f"%{termo}%")
            )
            apagados = cur.rowcount
        conn.commit()
    return {"apagados": apagados}

def factos_utilizador(utilizador: str, limite: int = 50) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT facto FROM memoria_utilizador
                   WHERE utilizador = %s ORDER BY criado_em DESC LIMIT %s""",
                (utilizador, limite)
            )
            return [l["facto"] for l in cur.fetchall()]

def registar_sugestao_comportamento(sugestao: str, proposto_por: str, origem: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sugestoes_pendentes (sugestao, proposto_por, origem)
                   VALUES (%s, %s, %s) RETURNING id""",
                (sugestao, proposto_por, origem)
            )
            id_gerado = cur.fetchone()["id"]
        conn.commit()
    return {"registado": True, "id": id_gerado}

def sugestoes_pendentes_lista(limite: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, sugestao, proposto_por, origem, criado_em
                   FROM sugestoes_pendentes ORDER BY criado_em ASC LIMIT %s""",
                (limite,)
            )
            return [{
                "id": l["id"], "sugestao": l["sugestao"], "proposto_por": l["proposto_por"],
                "origem": l["origem"], "criado_em": l["criado_em"].isoformat(),
            } for l in cur.fetchall()]

def obter_sugestao_pendente(id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sugestoes_pendentes WHERE id = %s", (id,))
            return cur.fetchone()

def eliminar_sugestao_pendente(id: int) -> bool:
    """Devolve True se havia mesmo uma sugestão pendente com este id (e foi apagada), False se não existia."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sugestoes_pendentes WHERE id = %s", (id,))
            apagou = cur.rowcount > 0
        conn.commit()
    return apagou

def aprovar_sugestao_pendente(id: int, aprovado_por: str) -> dict:
    """Move a sugestão pendente para memória global (visível a todas as
    conversas) e apaga-a da lista de pendentes. Quem está autorizado a
    aprovar é decidido em agents/base.py, não aqui."""
    pendente = obter_sugestao_pendente(id)
    if not pendente:
        return {"erro": f"não encontrei nenhuma sugestão pendente com id {id}"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO memoria_global (facto, proposto_por, aprovado_por)
                   VALUES (%s, %s, %s)""",
                (pendente["sugestao"], pendente["proposto_por"], aprovado_por)
            )
            cur.execute("DELETE FROM sugestoes_pendentes WHERE id = %s", (id,))
        conn.commit()
    return {"aprovado": True, "facto": pendente["sugestao"]}

def factos_globais(limite: int = 50) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT facto FROM memoria_global ORDER BY criado_em DESC LIMIT %s",
                (limite,)
            )
            return [l["facto"] for l in cur.fetchall()]

def memoria_global_lista(limite: int = 50) -> list[dict]:
    """Como factos_globais, mas devolve o registo completo (quem propôs,
    quem aprovou, quando) — para a Alma poder mostrar um histórico
    consultável a quem perguntar, em vez de só usar os factos, em bruto, no
    contexto de sistema (ver contexto_global)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT facto, proposto_por, aprovado_por, criado_em
                   FROM memoria_global ORDER BY criado_em DESC LIMIT %s""",
                (limite,)
            )
            return [{
                "facto": l["facto"], "proposto_por": l["proposto_por"], "aprovado_por": l["aprovado_por"],
                "criado_em": l["criado_em"].isoformat(),
            } for l in cur.fetchall()]

def esquecer_factos_globais(termo: str) -> dict:
    """Apaga da memória global os factos que contenham o termo. Devolve quantos apagou."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memoria_global WHERE facto ILIKE %s", (f"%{termo}%",))
            apagados = cur.rowcount
        conn.commit()
    return {"apagados": apagados}

def marcar_pausa_automatica(data_inicio: str, data_fim: str, motivo: str, criado_por: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pausas_automaticas (data_inicio, data_fim, motivo, criado_por)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (data_inicio, data_fim, motivo, criado_por)
            )
            id_gerado = cur.fetchone()["id"]
        conn.commit()
    return {"registado": True, "id": id_gerado, "data_inicio": data_inicio, "data_fim": data_fim}

def pausa_automatica_ativa(hoje) -> dict:
    """A pausa ativa que cobre `hoje`, se houver uma — consultada pelos
    próprios jobs agendados (ver agents/monitor_basecamp.py e outros) antes
    de publicarem no Mural ou marcarem alguém, para nunca o fazerem durante
    uma pausa pedida (ver marcar_pausa_automatica). None se não houver
    nenhuma a cobrir esta data."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, data_inicio, data_fim, motivo, criado_por
                   FROM pausas_automaticas
                   WHERE ativa AND %s BETWEEN data_inicio AND data_fim
                   ORDER BY data_inicio DESC LIMIT 1""",
                (hoje,)
            )
            l = cur.fetchone()
            if not l:
                return None
            return {"id": l["id"], "data_inicio": l["data_inicio"].isoformat(),
                    "data_fim": l["data_fim"].isoformat(), "motivo": l["motivo"], "criado_por": l["criado_por"]}

def terminar_pausa_automatica(hoje) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE pausas_automaticas SET ativa = false
                   WHERE ativa AND %s BETWEEN data_inicio AND data_fim""",
                (hoje,)
            )
            terminadas = cur.rowcount
        conn.commit()
    return {"terminadas": terminadas}

def pausas_automaticas_lista(limite: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, data_inicio, data_fim, motivo, criado_por, ativa, criado_em
                   FROM pausas_automaticas ORDER BY data_inicio DESC LIMIT %s""",
                (limite,)
            )
            return [{
                "id": l["id"], "data_inicio": l["data_inicio"].isoformat(), "data_fim": l["data_fim"].isoformat(),
                "motivo": l["motivo"], "criado_por": l["criado_por"], "ativa": l["ativa"],
                "criado_em": l["criado_em"].isoformat(),
            } for l in cur.fetchall()]

def contexto_global() -> str:
    """Bloco de texto com a memória global aprovada (ver
    aprovar_sugestao_pendente), para injetar no system prompt de QUALQUER
    conversa, de qualquer utilizador, em qualquer canal — ao contrário de
    contexto_utilizador, que só se aplica à própria pessoa."""
    factos = factos_globais()
    if not factos:
        return ""
    linhas = ["Regras/decisões aprovadas para todas as conversas, seja quem for a falar:"]
    linhas += [f"- {f}" for f in factos]
    return "\n".join(linhas)

def guardar_avaliacao_carga_toros(fornecedor: str, avaliacao: str, ano: int,
                                  quantidade: str = None, data_carga: str = None, talao: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO avaliacoes_cargas_toros
                   (fornecedor, quantidade, data_carga, talao, avaliacao, ano)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (fornecedor, quantidade, data_carga, talao, avaliacao, ano)
            )
        conn.commit()

def avaliacoes_cargas_toros_ano(ano: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT fornecedor, quantidade, data_carga, talao, avaliacao, criado_em
                   FROM avaliacoes_cargas_toros
                   WHERE ano = %s ORDER BY criado_em ASC""",
                (ano,)
            )
            return [{
                "fornecedor": l["fornecedor"],
                "quantidade": l["quantidade"],
                "data_carga": l["data_carga"],
                "talao": l["talao"],
                "avaliacao": l["avaliacao"],
                "registado_em": l["criado_em"].date().isoformat(),
            } for l in cur.fetchall()]

def guardar_documento_gerado(utilizador: str, titulo: str, ficheiro: bytes, conteudo_fonte: str,
                             formato: str = "pdf") -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO documentos_gerados (utilizador, titulo, pdf, conteudo_markdown, formato)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (utilizador, titulo, ficheiro, conteudo_fonte, formato)
            )
            id_gerado = cur.fetchone()["id"]
        conn.commit()
    return id_gerado

def guardar_ou_atualizar_documento_gerado(utilizador: str, titulo: str, ficheiro: bytes, conteudo_fonte: str,
                                          card_id: int, formato: str = "html") -> int:
    """Como guardar_documento_gerado, mas para documentos que representam
    um projeto/card específico (ex: o portal de acompanhamento) e por isso
    têm de manter sempre o mesmo link: se já existir um documento gerado
    para este `card_id`, atualiza esse registo em vez de criar um novo.
    Sem isto, cada vez que o portal era gerado outra vez (ex: depois de
    uma fase ser validada) criava um documento novo com um link novo — o
    link já enviado à cliente ficava congelado no estado antigo, o que
    contradiz a própria ideia de um portal de acompanhamento."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO documentos_gerados (utilizador, titulo, pdf, conteudo_markdown, formato, card_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (card_id) WHERE card_id IS NOT NULL
                   DO UPDATE SET utilizador = EXCLUDED.utilizador, titulo = EXCLUDED.titulo,
                                 pdf = EXCLUDED.pdf, conteudo_markdown = EXCLUDED.conteudo_markdown,
                                 formato = EXCLUDED.formato, criado_em = now()
                   RETURNING id""",
                (utilizador, titulo, ficheiro, conteudo_fonte, formato, card_id)
            )
            id_gerado = cur.fetchone()["id"]
        conn.commit()
    return id_gerado

def obter_documento_gerado(id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT titulo, pdf, formato, card_id, conteudo_markdown FROM documentos_gerados "
                       "WHERE id = %s", (id,))
            linha = cur.fetchone()
            return ({"titulo": linha["titulo"], "pdf": bytes(linha["pdf"]), "formato": linha["formato"],
                    "card_id": linha["card_id"], "conteudo_markdown": linha["conteudo_markdown"]}
                    if linha else None)

def obter_documento_gerado_por_card_id(card_id: int) -> dict:
    """Como obter_documento_gerado, mas pelo card_id do Basecamp — usado
    para a página do portal de projeto encontrar o seu próprio registo a
    partir do card, sem precisar de conhecer o id do documento (ver
    tools/portal_projeto.validar_fase_portal, chamado pelo botão de
    validação de cada fase, cujo link só conhece o card_id)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, titulo, pdf, formato, card_id, conteudo_markdown FROM documentos_gerados "
                       "WHERE card_id = %s", (card_id,))
            linha = cur.fetchone()
            return ({"id": linha["id"], "titulo": linha["titulo"], "pdf": bytes(linha["pdf"]),
                    "formato": linha["formato"], "card_id": linha["card_id"],
                    "conteudo_markdown": linha["conteudo_markdown"]}
                    if linha else None)

def eliminar_documento_gerado(id: int) -> bool:
    """Elimina definitivamente um documento gerado (PDF, Excel ou portal de
    projeto) — usado pelo botão "Eliminar" da página de listagem de
    portais (ver tools/portal_projeto.pagina_lista). Devolve False se não
    existia nenhum documento com este id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documentos_gerados WHERE id = %s", (id,))
            eliminado = cur.rowcount > 0
        conn.commit()
    return eliminado

def listar_portais_projeto() -> list:
    """Todos os portais de acompanhamento de projeto já gerados (ver
    tools/portal_projeto), para a página interna de listagem/pesquisa da
    equipa (ver tools/portal_projeto.pagina_lista) — mais recentes
    primeiro. Extrai só os campos pequenos do JSON guardado (cliente, ref,
    validade, fases, imagem de conceito) diretamente em SQL, para nunca
    ter de ler os PDFs em base64 embutidos em `conteudo_markdown` só para
    montar uma lista — a imagem de conceito é a única imagem trazida
    (para o tile de cada projeto), por já ser a imagem-capa do próprio
    portal."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, card_id, titulo, criado_em,
                          conteudo_markdown::json -> 'projeto' ->> 'cliente' AS cliente,
                          conteudo_markdown::json -> 'projeto' ->> 'ref' AS ref,
                          conteudo_markdown::json -> 'projeto' ->> 'validade' AS validade,
                          conteudo_markdown::json -> 'projeto' -> 'fases' AS fases,
                          conteudo_markdown::json -> 'projeto' -> 'conceito' ->> 'imagem' AS imagem
                   FROM documentos_gerados
                   WHERE formato = 'html' AND card_id IS NOT NULL
                   ORDER BY criado_em DESC"""
            )
            linhas = cur.fetchall()
            return [{"id": l["id"], "card_id": l["card_id"], "titulo": l["titulo"],
                    "criado_em": l["criado_em"].isoformat(), "cliente": l["cliente"], "ref": l["ref"],
                    "validade": l["validade"], "fases": l["fases"] or [], "imagem": l["imagem"]} for l in linhas]

def documentos_gerados_recentes(utilizador: str, limite: int = 5) -> list:
    """Últimos documentos que a Alma gerou para esta pessoa (ver gerar_pdf e
    gerar_excel) — usado para ela saber, sem perguntar, do que já falámos/
    geramos antes, e para dar o `id` que obter_conteudo_documento_gerado
    precisa para reler a fonte de um deles."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, titulo, formato, criado_em FROM documentos_gerados
                   WHERE utilizador = %s ORDER BY criado_em DESC LIMIT %s""",
                (utilizador, limite)
            )
            return [{"id": l["id"], "titulo": l["titulo"], "formato": l["formato"],
                     "criado_em": l["criado_em"].isoformat()}
                    for l in cur.fetchall()]

def obter_conteudo_documento_gerado(utilizador: str, id: int) -> dict:
    """Devolve a fonte de um documento já gerado (markdown p/ pdf, JSON de
    colunas/linhas p/ xlsx — ver formato), para a Alma poder reaproveitá-la
    (ex: gerar noutro formato, atualizar, resumir) sem pedir à pessoa para
    reenviar os dados. Restrito a documentos gerados para o próprio
    utilizador que pergunta — nunca aos de outra pessoa."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT titulo, conteudo_markdown, formato FROM documentos_gerados WHERE id = %s AND utilizador = %s",
                (id, utilizador)
            )
            linha = cur.fetchone()
            if not linha:
                return {"erro": "não encontrei nenhum documento com este id, gerado para ti"}
            if not linha["conteudo_markdown"]:
                return {"erro": "este documento foi gerado antes de guardarmos a fonte — já não está disponível"}
            return {"titulo": linha["titulo"], "formato": linha["formato"], "conteudo": linha["conteudo_markdown"]}

def contexto_utilizador(utilizador: str) -> str:
    """Bloco de texto com perfil + memórias, para injetar no system prompt.

    O perfil (acolhimento) só existe para quem passou pela consola — mas os
    factos memorizados podem existir mesmo sem perfil (ex: alguém só conhecido
    por menções no Basecamp, que nunca fez o acolhimento). Por isso os dois
    são independentes: só devolve vazio se não houver mesmo nada."""
    p = obter_perfil(utilizador)
    factos = factos_utilizador(utilizador)
    documentos = documentos_gerados_recentes(utilizador)
    if not p and not factos and not documentos:
        return ""
    linhas = [f"Estás a falar com: {utilizador}"]
    if p:
        if p.get("empresa"):
            linhas.append(f"Equipa/empresa: {p['empresa']}")
        linhas += [
            f"Papel na equipa: {p['papel']}",
            f"Estilo de resposta preferido: {p['estilo_resposta']}",
            f"Formato preferido: {p['formato']}",
            f"Decisões: {p['decisao']}",
            f"Dificuldades onde a Alma pode ajudar: {p['dificuldades']}",
        ]
    if factos:
        linhas.append("O que sabes sobre o trabalho recente desta pessoa:")
        linhas += [f"- {f}" for f in factos]
    if documentos:
        linhas.append("Documentos que já geraste para esta pessoa (mais recente primeiro) — usa "
                       "obter_conteudo_documento_gerado com o id se precisares de reler/reaproveitar um deles, "
                       "em vez de dizeres que não tens acesso:")
        linhas += [f"- id {d['id']}: \"{d['titulo']}\" ({d['formato']}, {d['criado_em']})" for d in documentos]
    return "\n".join(linhas)

def ja_alertado(recording_id: int, prazo: str) -> bool:
    """Verifica se já foi publicado um alerta para esta tarefa/card com este prazo."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM basecamp_alertas WHERE recording_id = %s AND prazo = %s",
                (recording_id, prazo)
            )
            return cur.fetchone() is not None

def registar_alerta(recording_id: int, prazo: str, comentario: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO basecamp_alertas (recording_id, prazo, comentario)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (recording_id) DO UPDATE SET
                       prazo = EXCLUDED.prazo, comentario = EXCLUDED.comentario,
                       criado_em = now()""",
                (recording_id, prazo, comentario)
            )
        conn.commit()

def logistica_ja_alertado_recente(recording_id: int, condicao: str, dias: int) -> bool:
    """Se já foi publicado um alerta desta condição para este card nos
    últimos `dias` dias — cada condição (A a I) tem a sua própria janela de
    repetição (ver tools/logistica.py), por isso isto não é um simples
    "já alguma vez", é sempre relativo a um período."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM logistica_alertas
                   WHERE recording_id = %s AND condicao = %s
                   AND criado_em > now() - (%s || ' days')::interval""",
                (recording_id, condicao, dias)
            )
            return cur.fetchone() is not None

def logistica_data_ultimo_alerta(recording_id: int, condicao: str):
    """Timestamp do último alerta desta condição para este card, ou None —
    usado pela condição C (sem resposta do fornecedor 48h depois do alerta
    B) para medir o tempo decorrido desde esse alerta em concreto."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT criado_em FROM logistica_alertas
                   WHERE recording_id = %s AND condicao = %s""",
                (recording_id, condicao)
            )
            linha = cur.fetchone()
            return linha["criado_em"] if linha else None

def logistica_primeiro_alerta(recording_id: int):
    """Timestamp do alerta mais antigo (de qualquer condição) para este
    card — usado para escalar para a Isa Moreira quando uma situação está
    em curso há mais de duas semanas, independentemente de qual condição
    a foi sinalizando ao longo do tempo."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(criado_em) AS primeiro FROM logistica_alertas WHERE recording_id = %s",
                (recording_id,)
            )
            linha = cur.fetchone()
            return linha["primeiro"] if linha and linha["primeiro"] else None

def logistica_registar_alerta(recording_id: int, condicao: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO logistica_alertas (recording_id, condicao, criado_em)
                   VALUES (%s, %s, now())
                   ON CONFLICT (recording_id, condicao) DO UPDATE SET criado_em = now()""",
                (recording_id, condicao)
            )
        conn.commit()

def obter_parametros_estimativa() -> dict:
    """Todos os parâmetros do procedimento de tempos de montagem, chave →
    valor numérico (float) — ver tools/tempos_montagem.py para o uso."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chave, valor FROM parametros_estimativa")
            return {linha["chave"]: float(linha["valor"]) for linha in cur.fetchall()}

def atualizar_parametro_estimativa(chave: str, valor: float) -> dict:
    """Atualiza um parâmetro já existente — nunca cria uma chave nova (evita
    um erro de digitação a criar um parâmetro solto que nunca é lido por
    nada); ver agents/base.py para a restrição de quem pode chamar isto."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE parametros_estimativa SET valor = %s, atualizado_em = now()
                   WHERE chave = %s""",
                (valor, chave)
            )
            if cur.rowcount == 0:
                return {"erro": f"parâmetro desconhecido: {chave!r}"}
        conn.commit()
        return {"chave": chave, "novo_valor": valor}

def estimativa_existente(recording_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM estimativas_montagem WHERE recording_id = %s", (recording_id,))
            return cur.fetchone()

def registar_estimativa_montagem(recording_id: int, titulo: str, url_api: str, comments_url: str,
                                 estimativa_minutos: float, valor_encomenda: float,
                                 decomposicao: dict, confianca: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO estimativas_montagem
                   (recording_id, titulo, url_api, comments_url, estimativa_minutos, valor_encomenda,
                    decomposicao, confianca)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (recording_id) DO NOTHING""",
                (recording_id, titulo, url_api, comments_url, estimativa_minutos, valor_encomenda,
                 Json(decomposicao), confianca)
            )
        conn.commit()

def marcar_real_estimativa(recording_id: int, real_minutos: float, real_pessoas: int, real_ocorrencias: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE estimativas_montagem
                   SET real_minutos = %s, real_pessoas = %s, real_ocorrencias = %s,
                       real_registado_em = now()
                   WHERE recording_id = %s""",
                (real_minutos, real_pessoas, real_ocorrencias, recording_id)
            )
        conn.commit()

def estimativas_aguardando_real() -> list:
    """Estimativas já publicadas cuja entrega ainda não foi confirmada como
    concluída (sem "Real" registado) — ver
    agents.estimativa_montagem.verificar_entregas_concluidas_e_ler_real."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM estimativas_montagem WHERE real_registado_em IS NULL")
            return cur.fetchall()

def estimativas_por_calibrar() -> list:
    """Estimativas com "Real" já registado mas ainda não incluídas em nenhum
    relatório de calibração — ver agents.estimativa_montagem.correr_calibracao_estimativa."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM estimativas_montagem WHERE real_registado_em IS NOT NULL AND calibrado = false"
            )
            return cur.fetchall()

def marcar_calibrado(recording_ids: list):
    if not recording_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE estimativas_montagem SET calibrado = true WHERE recording_id = ANY(%s)",
                (recording_ids,)
            )
        conn.commit()

def evento_ja_processado(comment_id: int) -> bool:
    """Evita responder duas vezes à mesma menção (o Basecamp pode reenviar o webhook)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM basecamp_eventos_processados WHERE comment_id = %s", (comment_id,))
            return cur.fetchone() is not None

def registar_evento_processado(comment_id: int, resposta: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO basecamp_eventos_processados (comment_id, resposta)
                   VALUES (%s, %s) ON CONFLICT (comment_id) DO NOTHING""",
                (comment_id, resposta)
            )
        conn.commit()

def mapeamentos_calendario_google() -> dict:
    """Todo o mapeamento atual entrada da Agenda do Basecamp -> evento do
    Google Calendar (ver agents/sincronizacao_calendario.py), indexado por
    entry_id — usado para decidir, a cada ciclo de sincronização, o que é
    novo, alterado ou removido no Basecamp desde o último ciclo."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM basecamp_google_calendar_sync")
            return {linha["entry_id"]: linha for linha in cur.fetchall()}

def registar_mapeamento_calendario_google(entry_id: int, google_event_id: str,
                                          titulo: str, inicio: str, fim: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO basecamp_google_calendar_sync
                       (entry_id, google_event_id, titulo, inicio, fim)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (entry_id) DO UPDATE
                       SET google_event_id = EXCLUDED.google_event_id,
                           titulo = EXCLUDED.titulo, inicio = EXCLUDED.inicio,
                           fim = EXCLUDED.fim, atualizado_em = now()""",
                (entry_id, google_event_id, titulo, inicio, fim)
            )
        conn.commit()

def atualizar_mapeamento_calendario_google(entry_id: int, titulo: str, inicio: str, fim: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE basecamp_google_calendar_sync
                   SET titulo = %s, inicio = %s, fim = %s, atualizado_em = now()
                   WHERE entry_id = %s""",
                (titulo, inicio, fim, entry_id)
            )
        conn.commit()

def remover_mapeamento_calendario_google(entry_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM basecamp_google_calendar_sync WHERE entry_id = %s", (entry_id,))
        conn.commit()

def alertas_recentes(limite: int = 30) -> list[dict]:
    """Últimos alertas publicados no Basecamp — para confirmar corridas sem ir aos logs."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT recording_id, prazo, comentario, criado_em
                   FROM basecamp_alertas ORDER BY criado_em DESC LIMIT %s""",
                (limite,)
            )
            return cur.fetchall()

def guardar_estado_reuniao(sessao: str, excertos: dict, processados: int):
    """Persiste o estado de uma reunião em curso (excertos transcritos por
    índice + contagem de processados) — sem isto, a transcrição acumulada só
    existia em memória do processo do servidor e perdia-se por completo se
    o servidor reiniciasse a meio de uma reunião longa (ex: um deploy novo)."""
    excertos_json = {str(indice): texto for indice, texto in excertos.items()}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reunioes_em_curso (sessao, excertos, processados)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (sessao) DO UPDATE SET
                       excertos = EXCLUDED.excertos, processados = EXCLUDED.processados,
                       atualizado_em = now()""",
                (sessao, Json(excertos_json), processados)
            )
        conn.commit()

def carregar_estado_reuniao(sessao: str):
    """Estado persistido de uma reunião (excertos + processados), ou None se
    não houver nenhum guardado para esta sessão — usado para recuperar uma
    reunião em curso depois de o servidor reiniciar a meio dela."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT excertos, processados FROM reunioes_em_curso WHERE sessao = %s",
                (sessao,)
            )
            linha = cur.fetchone()
    if not linha:
        return None
    # float, não int — uma resposta da Alma regista-se num índice fracionário
    # (ver tools/reuniao.py:registar_resposta_alma), para ficar sempre entre
    # o turno que a desencadeou e o seguinte, sem colidir com índices
    # inteiros futuros atribuídos pelo cliente (que não sabe desta inserção
    # do lado do servidor)
    excertos = {float(indice): texto for indice, texto in linha["excertos"].items()}
    return {"excertos": excertos, "processados": linha["processados"]}

def eliminar_estado_reuniao(sessao: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reunioes_em_curso WHERE sessao = %s", (sessao,))
        conn.commit()

def limpar_reunioes_antigas(dias: int = 3) -> int:
    """Apaga estado de reuniões persistido há mais de `dias` dias (por
    omissão, 3) — isto só existe para sobreviver a um reinício do servidor a
    meio de uma reunião, não é suposto acumular para sempre. Devolve quantas
    foram apagadas."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reunioes_em_curso WHERE atualizado_em < now() - %s * interval '1 day'",
                (dias,)
            )
            apagadas = cur.rowcount
        conn.commit()
    return apagadas

def guardar_snapshot_diario_projeto(data, projeto: str, total_ativos: int, atrasados: int,
                                    parados: int, por_estado: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO snapshot_diario_projetos
                       (data, projeto, total_ativos, atrasados, parados, por_estado)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (data, projeto) DO UPDATE SET
                       total_ativos = EXCLUDED.total_ativos, atrasados = EXCLUDED.atrasados,
                       parados = EXCLUDED.parados, por_estado = EXCLUDED.por_estado""",
                (data, projeto, total_ativos, atrasados, parados, Json(por_estado))
            )
        conn.commit()

def snapshot_diario_projeto_anterior(projeto: str, antes_de) -> dict:
    """A leitura mais recente deste projeto anterior a `antes_de` (tipicamente
    hoje) — para comparar com a leitura de hoje e ter uma evolução real, não
    só uma fotografia isolada. None se nunca houve nenhuma leitura anterior."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT data, total_ativos, atrasados, parados, por_estado
                   FROM snapshot_diario_projetos
                   WHERE projeto = %s AND data < %s
                   ORDER BY data DESC LIMIT 1""",
                (projeto, antes_de)
            )
            return cur.fetchone()
