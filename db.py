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

def historico_sessao(sessao: str, limite: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT papel, conteudo FROM conversas
                   WHERE sessao = %s
                   ORDER BY criado_em ASC
                   LIMIT %s""",
                (sessao, limite)
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
