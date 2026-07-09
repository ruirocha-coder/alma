# db.py — ligação Postgres + schema + memória partilhada
import os
import psycopg
from psycopg.rows import dict_row

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
"""

def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def inicializar_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
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
                   formato: str, decisao: str, dificuldades: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO perfis (utilizador, papel, estilo_resposta, formato, decisao, dificuldades)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (utilizador) DO UPDATE SET
                       papel = EXCLUDED.papel, estilo_resposta = EXCLUDED.estilo_resposta,
                       formato = EXCLUDED.formato, decisao = EXCLUDED.decisao,
                       dificuldades = EXCLUDED.dificuldades""",
                (utilizador, papel, estilo_resposta, formato, decisao, dificuldades)
            )
        conn.commit()
    return {"guardado": True}

def obter_perfil(utilizador: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM perfis WHERE utilizador = %s", (utilizador,))
            return cur.fetchone()

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

def factos_utilizador(utilizador: str, limite: int = 30) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT facto FROM memoria_utilizador
                   WHERE utilizador = %s ORDER BY criado_em DESC LIMIT %s""",
                (utilizador, limite)
            )
            return [l["facto"] for l in cur.fetchall()]

def contexto_utilizador(utilizador: str) -> str:
    """Bloco de texto com perfil + memórias, para injetar no system prompt."""
    p = obter_perfil(utilizador)
    if not p:
        return ""
    linhas = [
        f"Estás a falar com: {utilizador}",
        f"Papel na Interior Guider: {p['papel']}",
        f"Estilo de resposta preferido: {p['estilo_resposta']}",
        f"Formato preferido: {p['formato']}",
        f"Decisões: {p['decisao']}",
        f"Dificuldades onde a Alma pode ajudar: {p['dificuldades']}",
    ]
    factos = factos_utilizador(utilizador)
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
