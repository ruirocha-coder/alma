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
    titulo TEXT NOT NULL,
    pdf BYTEA NOT NULL,             -- guardado em Postgres, não em disco (Railway não persiste disco entre deploys)
    criado_em TIMESTAMPTZ DEFAULT now()
);
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

def historico_sessao(sessao: str, utilizador: str, limite: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT papel, conteudo FROM conversas
                   WHERE sessao = %s AND utilizador = %s
                   ORDER BY criado_em ASC
                   LIMIT %s""",
                (sessao, utilizador, limite)
            )
            linhas = cur.fetchall()
    return [{"role": l["papel"], "content": l["conteudo"]} for l in linhas]

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

def guardar_documento_gerado(titulo: str, pdf: bytes) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documentos_gerados (titulo, pdf) VALUES (%s, %s) RETURNING id",
                (titulo, pdf)
            )
            id_gerado = cur.fetchone()["id"]
        conn.commit()
    return id_gerado

def obter_documento_gerado(id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT titulo, pdf FROM documentos_gerados WHERE id = %s", (id,))
            linha = cur.fetchone()
            return {"titulo": linha["titulo"], "pdf": bytes(linha["pdf"])} if linha else None

def contexto_utilizador(utilizador: str) -> str:
    """Bloco de texto com perfil + memórias, para injetar no system prompt.

    O perfil (acolhimento) só existe para quem passou pela consola — mas os
    factos memorizados podem existir mesmo sem perfil (ex: alguém só conhecido
    por menções no Basecamp, que nunca fez o acolhimento). Por isso os dois
    são independentes: só devolve vazio se não houver mesmo nada."""
    p = obter_perfil(utilizador)
    factos = factos_utilizador(utilizador)
    if not p and not factos:
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
    excertos = {int(indice): texto for indice, texto in linha["excertos"].items()}
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
