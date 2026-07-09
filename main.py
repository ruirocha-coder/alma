from dotenv import load_dotenv
load_dotenv()

import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from orchestrator import encaminhar, AGENTES
from db import (guardar_mensagem, historico_sessao, log_routing,
                sessoes_utilizador, eliminar_sessao, perfil_existe, alertas_recentes)
from agents import acolhimento, monitor_basecamp
from db import inicializar_schema
inicializar_schema()

app = FastAPI(title="ALMA")

# monitorização do Basecamp: todos os dias às 8h (hora de Lisboa)
scheduler = BackgroundScheduler(timezone="Europe/Lisbon")
scheduler.add_job(monitor_basecamp.correr_monitorizacao, "cron", hour=8, minute=0)
scheduler.start()

class Pedido(BaseModel):
    utilizador: str
    sessao: str
    mensagem: str

@app.post("/alma")
def alma(p: Pedido):
    mensagens = historico_sessao(p.sessao, p.utilizador)   # memória por utilizador
    mensagens.append({"role": "user", "content": p.mensagem})

    try:
        if not perfil_existe(p.utilizador):
            resposta = acolhimento.responder(p.utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(p.mensagem)
            log_routing(p.mensagem, agente)
            resposta = AGENTES[agente](p.utilizador, mensagens)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao obter resposta do agente: {e}")

    guardar_mensagem(p.utilizador, p.sessao, "user", p.mensagem)
    guardar_mensagem(p.utilizador, p.sessao, "assistant", resposta, agente)
    return {"resposta": resposta}                    # o agente nunca é exposto

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/health/config")
def health_config():
    """Diz quais as variáveis de ambiente necessárias que estão definidas (nunca os valores) — para diagnosticar sem ir ao painel do Railway."""
    variaveis = ["DATABASE_URL", "ANTHROPIC_API_KEY", "BIGCOMMERCE_STORE_HASH",
                 "BIGCOMMERCE_ACCESS_TOKEN", "SITE_URL",
                 "BASECAMP_ACCOUNT_ID", "BASECAMP_CLIENT_ID", "BASECAMP_CLIENT_SECRET",
                 "BASECAMP_REFRESH_TOKEN", "PROCEDIMENTOS_DOC_ID"]
    return {v: bool(os.environ.get(v)) for v in variaveis}

@app.get("/sessoes")
def sessoes(utilizador: str):
    return sessoes_utilizador(utilizador)

@app.get("/historico/{sessao}")
def historico(sessao: str, utilizador: str):
    return historico_sessao(sessao, utilizador, limite=200)

@app.delete("/sessoes/{sessao}")
def apagar_sessao(sessao: str, utilizador: str):
    eliminar_sessao(sessao, utilizador)
    return {"ok": True}

@app.post("/basecamp/monitorizar")
def monitorizar_basecamp_agora():
    """Dispara a monitorização do Basecamp já, em segundo plano — contas com
    muito histórico podem demorar vários minutos, por isso não bloqueia o
    pedido; os resultados/erros ficam nos logs do servidor."""
    scheduler.add_job(monitor_basecamp.correr_monitorizacao, "date", run_date=datetime.now())
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.get("/basecamp/alertas")
def alertas_basecamp_recentes(limite: int = 30):
    """Últimos alertas publicados no Basecamp — para confirmar corridas sem ir aos logs do Railway."""
    return alertas_recentes(limite)

# consola de chat de teste, servida em "/"
app.mount("/", StaticFiles(directory="static", html=True), name="static")
