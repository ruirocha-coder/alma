from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from orchestrator import encaminhar, AGENTES
from db import guardar_mensagem, historico_sessao, log_routing
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

# consola de chat de teste, servida em "/"
app.mount("/", StaticFiles(directory="static", html=True), name="static")
