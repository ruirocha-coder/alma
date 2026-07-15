from dotenv import load_dotenv
load_dotenv()

import os
import threading
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from orchestrator import encaminhar, AGENTES
from db import (guardar_mensagem, historico_sessao, log_routing,
                sessoes_utilizador, eliminar_sessao, perfil_existe, alertas_recentes)
from agents import acolhimento, monitor_basecamp, responder_basecamp, resumo_semanal_basecamp
from tools import basecamp, ficheiros
from db import inicializar_schema
inicializar_schema()

app = FastAPI(title="ALMA")

# monitorização do Basecamp: todos os dias às 8h (hora de Lisboa)
scheduler = BackgroundScheduler(timezone="Europe/Lisbon")
scheduler.add_job(monitor_basecamp.correr_monitorizacao, "cron", hour=8, minute=0)
# resumo semanal no Mural: segundas-feiras às 9h (hora de Lisboa)
scheduler.add_job(resumo_semanal_basecamp.correr_resumo_semanal, "cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

class Pedido(BaseModel):
    utilizador: str
    sessao: str
    mensagem: str

def _responder_e_guardar(utilizador: str, sessao: str, mensagem_agente: str, mensagem_visivel: str = None):
    """Núcleo partilhado por /alma e /alma/ficheiro: o que é enviado ao agente
    (mensagem_agente) pode ser maior do que o que fica guardado no histórico
    (mensagem_visivel) — ex: um ficheiro anexado não deve inchar todas as
    chamadas futuras à API com o texto extraído inteiro outra vez."""
    mensagens = historico_sessao(sessao, utilizador)   # memória por utilizador
    mensagens.append({"role": "user", "content": mensagem_agente})

    try:
        if not perfil_existe(utilizador):
            resposta = acolhimento.responder(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem_agente[:500])
            log_routing(mensagem_agente[:500], agente)
            resposta = AGENTES[agente](utilizador, mensagens)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao obter resposta do agente: {e}")

    guardar_mensagem(utilizador, sessao, "user", mensagem_visivel or mensagem_agente)
    guardar_mensagem(utilizador, sessao, "assistant", resposta, agente)
    return {"resposta": resposta}                    # o agente nunca é exposto

@app.post("/alma")
def alma(p: Pedido):
    return _responder_e_guardar(p.utilizador, p.sessao, p.mensagem)

@app.post("/alma/ficheiro")
async def alma_com_ficheiro(utilizador: str = Form(...), sessao: str = Form(...),
                            mensagem: str = Form(""), ficheiro: UploadFile = File(...)):
    """Recebe um ficheiro anexado na consola de chat (PDF, Word, imagem, texto)
    e responde com o seu conteúdo já disponível ao agente."""
    bruto = await ficheiro.read()
    if len(bruto) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Ficheiro demasiado grande (máx. 15 MB)")

    texto = ficheiros.extrair_texto(bruto, ficheiro.content_type, ficheiro.filename)
    if texto is None:
        raise HTTPException(status_code=415,
                            detail=f"Não consigo ler ficheiros do tipo {ficheiro.content_type or '(desconhecido)'}")

    mensagem_visivel = f"📎 {ficheiro.filename}" + (f"\n{mensagem}" if mensagem else "")
    mensagem_agente = (f'Ficheiro anexado ("{ficheiro.filename}"):\n\n{texto[:8000]}\n\n'
                       f'{mensagem or "O que achas deste ficheiro?"}')
    return _responder_e_guardar(utilizador, sessao, mensagem_agente, mensagem_visivel)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/health/config")
def health_config():
    """Diz quais as variáveis de ambiente necessárias que estão definidas (nunca os valores) — para diagnosticar sem ir ao painel do Railway."""
    variaveis = ["DATABASE_URL", "ANTHROPIC_API_KEY", "BIGCOMMERCE_STORE_HASH",
                 "BIGCOMMERCE_ACCESS_TOKEN", "SITE_URL",
                 "BASECAMP_ACCOUNT_ID", "BASECAMP_CLIENT_ID", "BASECAMP_CLIENT_SECRET",
                 "BASECAMP_REFRESH_TOKEN", "PROCEDIMENTOS_DOC_ID",
                 "ALMA_APP_URL", "BASECAMP_WEBHOOK_SECRET"]
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
    pedido; os resultados/erros ficam nos logs do servidor.

    Uma thread simples em vez de agendar via scheduler.add_job(..., "date",
    run_date=...): esse caminho exige uma data com fuso horário coerente com
    o do BackgroundScheduler (Europe/Lisbon) — um datetime.now() "nu" foi
    interpretado como já sendo hora de Lisboa e disparou sempre um misfire
    silencioso (a corrida nunca chegava a arrancar)."""
    threading.Thread(target=monitor_basecamp.correr_monitorizacao, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.get("/basecamp/alertas")
def alertas_basecamp_recentes(limite: int = 30):
    """Últimos alertas publicados no Basecamp — para confirmar corridas sem ir aos logs do Railway."""
    return alertas_recentes(limite)

@app.post("/basecamp/resumo-semanal")
def resumo_semanal_basecamp_agora():
    """Dispara já o resumo semanal de atividade no Mural, em segundo plano."""
    threading.Thread(target=resumo_semanal_basecamp.correr_resumo_semanal, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/basecamp/webhooks/registar")
def registar_webhooks_basecamp():
    """Cria (de forma idempotente) um webhook de comentários/tarefas/cards em
    cada projeto a que a Alma tem acesso, para ela poder reagir a menções em
    tempo real. Podes correr isto outra vez sempre que houver projetos novos."""
    payload_url = f"{os.environ['ALMA_APP_URL'].rstrip('/')}/basecamp/webhook?chave={os.environ['BASECAMP_WEBHOOK_SECRET']}"
    resultado = []
    for projeto in basecamp.listar_projetos():
        bucket_id = projeto["id"]
        ja_existe = any(w.get("payload_url", "").split("?")[0] == payload_url.split("?")[0]
                       for w in basecamp.listar_webhooks(bucket_id))
        if ja_existe:
            resultado.append({"projeto": projeto["name"], "estado": "já existia"})
            continue
        try:
            basecamp.criar_webhook(bucket_id, payload_url, tipos=["Comment", "Todo", "Kanban::Card"])
            resultado.append({"projeto": projeto["name"], "estado": "criado"})
        except Exception as e:
            resultado.append({"projeto": projeto["name"], "estado": f"falhou: {e}"})
    return resultado

@app.post("/basecamp/webhook")
async def receber_webhook_basecamp(request: Request, chave: str = ""):
    """Recebe eventos do Basecamp (comentário/tarefa/card criado ou atualizado).
    Responde já com 200 e processa em segundo plano — o Basecamp espera uma
    resposta rápida, e ler o contexto + gerar a resposta pode demorar alguns
    segundos."""
    if chave != os.environ.get("BASECAMP_WEBHOOK_SECRET"):
        raise HTTPException(status_code=403, detail="chave inválida")
    payload = await request.json()
    threading.Thread(target=responder_basecamp.processar_evento_webhook, args=(payload,), daemon=True).start()
    return {"ok": True}

# consola de chat de teste, servida em "/"
app.mount("/", StaticFiles(directory="static", html=True), name="static")
