from dotenv import load_dotenv
load_dotenv()

import base64, json, os
import threading
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from orchestrator import encaminhar, AGENTES, AGENTES_STREAM
from db import (guardar_mensagem, historico_sessao, log_routing,
                sessoes_utilizador, eliminar_sessao, perfil_existe, alertas_recentes)
from agents import acolhimento, monitor_basecamp, responder_basecamp, resumo_semanal_basecamp
from tools import basecamp, ficheiros, voz
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

def _fluxo_resposta_agente(utilizador: str, sessao: str, mensagem_agente: str, mensagem_visivel: str = None):
    """Generator SSE: transmite a resposta do agente à medida que o modelo a
    gera (rondas de tool-use são resolvidas em silêncio antes disso — só o
    texto final visível é transmitido), e no fim guarda a troca completa no
    histórico, tal como _responder_e_guardar faz na versão não-streaming."""
    mensagens = historico_sessao(sessao, utilizador)
    mensagens.append({"role": "user", "content": mensagem_agente})

    try:
        if not perfil_existe(utilizador):
            gerador = acolhimento.responder_stream(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem_agente[:500])
            log_routing(mensagem_agente[:500], agente)
            gerador = AGENTES_STREAM[agente](utilizador, mensagens)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    partes = []
    try:
        for pedaco in gerador:
            partes.append(pedaco)
            yield f"data: {json.dumps({'delta': pedaco}, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    resposta = "".join(partes)
    guardar_mensagem(utilizador, sessao, "user", mensagem_visivel or mensagem_agente)
    guardar_mensagem(utilizador, sessao, "assistant", resposta, agente)
    yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"

@app.post("/alma/stream")
def alma_stream(p: Pedido):
    return StreamingResponse(
        _fluxo_resposta_agente(p.utilizador, p.sessao, p.mensagem),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

def _evento_audio(frase: str) -> str:
    """Sintetiza uma frase já fechada em voz e devolve-a como evento SSE — se
    falhar, não interrompe a resposta, só fica sem áudio para essa frase (o
    texto já chegou por 'delta' de qualquer forma)."""
    try:
        audio_b64 = base64.b64encode(voz.sintetizar(frase)).decode()
        return f"data: {json.dumps({'audio': audio_b64}, ensure_ascii=False)}\n\n"
    except Exception as e:
        print(f"[voz] falha ao sintetizar frase: {e!r}")
        return ""

def _fluxo_resposta_por_voz(utilizador: str, sessao: str, mensagem: str):
    """Como _fluxo_resposta_agente, mas sintetiza voz frase a frase à medida
    que o texto da resposta vai chegando — a Alma começa a falar sem esperar
    pela resposta toda estar pronta."""
    yield f"data: {json.dumps({'transcricao': mensagem}, ensure_ascii=False)}\n\n"

    mensagens = historico_sessao(sessao, utilizador)
    mensagens.append({"role": "user", "content": mensagem})

    try:
        if not perfil_existe(utilizador):
            gerador = acolhimento.responder_stream(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem[:500])
            log_routing(mensagem[:500], agente)
            gerador = AGENTES_STREAM[agente](utilizador, mensagens)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    partes, buffer_frase = [], ""
    try:
        for pedaco in gerador:
            partes.append(pedaco)
            yield f"data: {json.dumps({'delta': pedaco}, ensure_ascii=False)}\n\n"
            buffer_frase += pedaco
            frases_prontas, buffer_frase = voz.dividir_em_frases_prontas(buffer_frase)
            for frase in frases_prontas:
                if frase.strip():
                    yield _evento_audio(frase)
        if buffer_frase.strip():
            yield _evento_audio(buffer_frase)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    resposta = "".join(partes)
    guardar_mensagem(utilizador, sessao, "user", mensagem)
    guardar_mensagem(utilizador, sessao, "assistant", resposta, agente)
    yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"

@app.post("/alma/voz")
async def alma_por_voz(utilizador: str = Form(...), sessao: str = Form(...),
                       audio: UploadFile = File(...)):
    """Pergunta à Alma por voz: transcreve a gravação, pergunta como de
    costume, e devolve a resposta em stream (texto + voz sintetizada frase a
    frase, à medida que a resposta vai sendo gerada)."""
    bruto = await audio.read()
    if len(bruto) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Áudio demasiado grande (máx. 15 MB)")

    try:
        mensagem = voz.transcrever(bruto, audio.filename or "audio.webm", audio.content_type or "audio/webm")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao transcrever o áudio: {e}")
    if not mensagem:
        raise HTTPException(status_code=422,
                            detail="Não consegui perceber o áudio — tenta falar mais perto do microfone.")

    return StreamingResponse(
        _fluxo_resposta_por_voz(utilizador, sessao, mensagem),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

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
                 "ALMA_APP_URL", "BASECAMP_WEBHOOK_SECRET", "OPENAI_API_KEY",
                 "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
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
