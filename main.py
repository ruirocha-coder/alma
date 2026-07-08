from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from orchestrator import encaminhar, AGENTES
from db import guardar_mensagem, historico_sessao, log_routing, sessoes_utilizador, eliminar_sessao
from db import inicializar_schema
inicializar_schema()

app = FastAPI(title="ALMA")

class Pedido(BaseModel):
    utilizador: str
    sessao: str
    mensagem: str

@app.post("/alma")
def alma(p: Pedido):
    agente = encaminhar(p.mensagem)
    log_routing(p.mensagem, agente)

    mensagens = historico_sessao(p.sessao)          # memória partilhada
    mensagens.append({"role": "user", "content": p.mensagem})

    try:
        resposta = AGENTES[agente](mensagens)
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
                 "BIGCOMMERCE_ACCESS_TOKEN", "SITE_URL"]
    return {v: bool(os.environ.get(v)) for v in variaveis}

@app.get("/sessoes")
def sessoes(utilizador: str):
    return sessoes_utilizador(utilizador)

@app.get("/historico/{sessao}")
def historico(sessao: str):
    return historico_sessao(sessao, limite=200)

@app.delete("/sessoes/{sessao}")
def apagar_sessao(sessao: str, utilizador: str):
    eliminar_sessao(sessao, utilizador)
    return {"ok": True}

# consola de chat de teste, servida em "/"
app.mount("/", StaticFiles(directory="static", html=True), name="static")
