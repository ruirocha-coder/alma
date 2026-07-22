from dotenv import load_dotenv
load_dotenv()

import asyncio, base64, json, os
import threading
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from orchestrator import encaminhar, AGENTES, AGENTES_STREAM
from db import (guardar_mensagem, historico_sessao, log_routing,
                sessoes_utilizador, eliminar_sessao, perfil_existe, alertas_recentes,
                obter_documento_gerado, avaliacoes_cargas_toros_ano)
from agents import (acolhimento, monitor_basecamp, responder_basecamp,
                    resumo_semanal_basecamp, resumo_diario_ecos_largos,
                    resumo_anual_cargas_toros, logistica_entregas)
from tools import basecamp, ficheiros as ficheiros_tool, voz, reuniao, logistica, documentos_empresa, ecos_largos
from db import inicializar_schema
inicializar_schema()

app = FastAPI(title="ALMA")

# monitorização do Basecamp: todos os dias às 8h (hora de Lisboa)
scheduler = BackgroundScheduler(timezone="Europe/Lisbon")
scheduler.add_job(monitor_basecamp.correr_monitorizacao, "cron", hour=8, minute=0)
# resumo semanal no Mural (Gestão, Interior Guider): segundas-feiras às 9h
scheduler.add_job(resumo_semanal_basecamp.correr_resumo_semanal, "cron", day_of_week="mon", hour=9, minute=0)
# resumo semanal no Mural da Ecos Largos: separado do da Interior Guider,
# mesmo dia mas a horas diferentes para não publicarem os dois em simultâneo
scheduler.add_job(resumo_semanal_basecamp.correr_resumo_semanal_ecos_largos, "cron",
                  day_of_week="mon", hour=9, minute=15)
# análise diária do dashboard de produção, no Mural da Ecos Largos: às 19h, de segunda a sábado (não há produção aos domingos)
scheduler.add_job(resumo_diario_ecos_largos.correr_resumo_diario_ecos_largos, "cron",
                  day_of_week="mon-sat", hour=19, minute=0)
# limpeza do estado de reuniões persistido (rede de segurança contra um
# reinício do servidor a meio de uma reunião) — todos os dias às 4h
scheduler.add_job(reuniao.limpar_reunioes_antigas, "cron", hour=4, minute=0)
# resumo anual das avaliações de cargas de toros (Ecos Largos): 31 de
# dezembro às 22h — bastante antes da meia-noite, para "o ano corrente" no
# momento em que corre ser sempre o ano que está mesmo a terminar
scheduler.add_job(resumo_anual_cargas_toros.correr_resumo_anual_cargas_toros, "cron",
                  month=12, day=31, hour=22, minute=0)
# monitorização de logística (projeto Entregas): todos os dias antes das 9h
scheduler.add_job(logistica_entregas.correr_monitorizacao_logistica, "cron", hour=7, minute=30)
scheduler.start()

class Pedido(BaseModel):
    utilizador: str
    sessao: str
    mensagem: str

def _responder_e_guardar(utilizador: str, sessao: str, mensagem_agente: str, mensagem_visivel: str = None,
                         tem_anexos: bool = False):
    """Núcleo partilhado por /alma e /alma/ficheiro: o que é enviado ao agente
    (mensagem_agente) pode ser maior do que o que fica guardado no histórico
    (mensagem_visivel) — ex: um ficheiro anexado não deve inchar todas as
    chamadas futuras à API com o texto extraído inteiro outra vez.

    `tem_anexos`: se esta mensagem trouxe ficheiros/fotos anexados — usado
    para o encaminhamento nunca depender só da classificação por texto
    (ver orchestrator.escolher_agente_ecos_largos)."""
    mensagens = historico_sessao(sessao, utilizador)   # memória por utilizador
    mensagens.append({"role": "user", "content": mensagem_agente})

    try:
        if not perfil_existe(utilizador):
            resposta = acolhimento.responder(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem_agente[:500], utilizador, tem_anexos=tem_anexos)
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

def _fluxo_resposta_agente(utilizador: str, sessao: str, mensagem_agente: str, mensagem_visivel: str = None,
                           tem_anexos: bool = False):
    """Generator SSE: transmite a resposta do agente à medida que o modelo a
    gera (rondas de tool-use são resolvidas em silêncio antes disso — só o
    texto final visível é transmitido), e no fim guarda a troca completa no
    histórico, tal como _responder_e_guardar faz na versão não-streaming.

    `tem_anexos`: ver orchestrator.escolher_agente_ecos_largos — usado
    também aqui (não só na versão não-streaming) porque uma avaliação de
    carga com fotos passou a vir sempre por este caminho (ver
    alma_com_ficheiro), para beneficiar do sinal de vida durante chamadas
    a ferramentas demoradas (ex: ler o manual, consultar o Basecamp)."""
    mensagens = historico_sessao(sessao, utilizador)
    mensagens.append({"role": "user", "content": mensagem_agente})

    try:
        if not perfil_existe(utilizador):
            gerador = acolhimento.responder_stream(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem_agente[:500], utilizador, tem_anexos=tem_anexos)
            log_routing(mensagem_agente[:500], agente)
            gerador = AGENTES_STREAM[agente](utilizador, mensagens)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    partes = []
    try:
        for pedaco in gerador:
            if pedaco is None:
                # sinal de vida (ex: a meio de uma tool a demorar) — não é texto
                yield f"data: {json.dumps({'a_processar': True})}\n\n"
                continue
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
    frase_limpa = voz.limpar_para_fala(frase)
    if not frase_limpa:
        return ""
    try:
        audio_b64 = base64.b64encode(voz.sintetizar(frase_limpa)).decode()
        return f"data: {json.dumps({'audio': audio_b64}, ensure_ascii=False)}\n\n"
    except Exception as e:
        print(f"[voz] falha ao sintetizar frase: {e!r}")
        return ""

def _fluxo_resposta_por_voz(utilizador: str, sessao: str, mensagem_agente: str,
                            mensagem_visivel: str = None, texto_transcricao: str = None,
                            minha_geracao: int = None):
    """Como _fluxo_resposta_agente, mas sintetiza voz frase a frase à medida
    que o texto da resposta vai chegando — a Alma começa a falar sem esperar
    pela resposta toda estar pronta.

    mensagem_agente é o que o modelo recebe (pode incluir contexto extra, ex:
    a transcrição de uma reunião em curso); mensagem_visivel e
    texto_transcricao (por omissão, iguais a mensagem_agente) são o que fica
    no histórico e o que é mostrado como "ouvi isto", respetivamente — úteis
    quando o que se ouviu não deve ser, ao mesmo tempo, o texto todo enviado
    ao modelo.

    minha_geracao (modo reunião): se dado, a resposta para assim que reuniao
    marcar uma geração mais recente para esta sessão — é o que permite
    interromper a Alma a meio quando alguém a chama de novo antes de ela
    acabar de responder."""
    mensagem_visivel = mensagem_visivel or mensagem_agente
    texto_transcricao = texto_transcricao if texto_transcricao is not None else mensagem_agente
    yield f"data: {json.dumps({'transcricao': texto_transcricao}, ensure_ascii=False)}\n\n"

    mensagens = historico_sessao(sessao, utilizador)
    mensagens.append({"role": "user", "content": mensagem_agente})

    try:
        if not perfil_existe(utilizador):
            gerador = acolhimento.responder_stream(utilizador, mensagens)
            agente = "acolhimento"
        else:
            agente = encaminhar(mensagem_agente[:500], utilizador)
            log_routing(mensagem_agente[:500], agente)
            gerador = AGENTES_STREAM[agente](utilizador, mensagens)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    def _interrompida() -> bool:
        return minha_geracao is not None and reuniao.foi_interrompida(sessao, minha_geracao)

    partes, buffer_frase, interrompida = [], "", False
    try:
        for pedaco in gerador:
            if _interrompida():
                interrompida = True
                gerador.close()
                break
            if pedaco is None:
                # sinal de vida (ex: a meio de uma tool a demorar) — não é texto
                yield f"data: {json.dumps({'a_processar': True})}\n\n"
                continue
            partes.append(pedaco)
            yield f"data: {json.dumps({'delta': pedaco}, ensure_ascii=False)}\n\n"
            buffer_frase += pedaco
            frases_prontas, buffer_frase = voz.dividir_em_frases_prontas(buffer_frase)
            for frase in frases_prontas:
                if _interrompida():
                    interrompida = True
                    break
                if frase.strip():
                    yield _evento_audio(frase)
            if interrompida:
                gerador.close()
                break
        if not interrompida and buffer_frase.strip():
            yield _evento_audio(buffer_frase)
    except Exception as e:
        yield f"data: {json.dumps({'erro': str(e)}, ensure_ascii=False)}\n\n"
        return

    if interrompida:
        nota = "\n\n_(interrompida — foi chamada de novo)_"
        partes.append(nota)
        yield f"data: {json.dumps({'delta': nota}, ensure_ascii=False)}\n\n"

    resposta = "".join(partes)
    guardar_mensagem(utilizador, sessao, "user", mensagem_visivel)
    guardar_mensagem(utilizador, sessao, "assistant", resposta, agente)
    yield f"data: {json.dumps({'done': True, 'interrompida': interrompida}, ensure_ascii=False)}\n\n"

@app.post("/alma/reuniao/iniciar")
def reuniao_iniciar(sessao: str = Form(...)):
    """Começa o modo reunião: a Alma passa a ouvir em contínuo (excertos
    curtos, um após o outro) e só responde quando for chamada pelo nome."""
    reuniao.iniciar(sessao)
    return {"ok": True}

@app.post("/alma/reuniao/chunk")
async def reuniao_chunk(utilizador: str = Form(...), sessao: str = Form(...),
                        indice: int = Form(...), audio: UploadFile = File(...)):
    """Recebe mais um excerto curto da reunião em curso (indice identifica a
    posição deste excerto na ordem de gravação, para a transcrição acumulada
    ficar sempre correta mesmo que os pedidos cheguem trocados). Transcreve-o
    e acrescenta-o ao que já se ouviu; o áudio em si nunca é guardado. Se o
    excerto não mencionar a Alma, devolve só a transcrição (para uma legenda
    ao vivo, se a consola quiser mostrar). Se mencionar, devolve um stream
    com a resposta (texto + voz) — e se já havia uma resposta anterior em
    curso nesta sessão, essa é interrompida na hora (a Alma para de falar a
    meio para ouvir e responder à nova chamada)."""
    if not reuniao.em_curso(sessao):
        raise HTTPException(status_code=409, detail="Não há nenhuma reunião em curso nesta sessão.")

    bruto = await audio.read()
    if len(bruto) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Áudio demasiado grande (máx. 15 MB)")

    try:
        texto = voz.transcrever(bruto, audio.filename or "audio.webm", audio.content_type or "audio/webm")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao transcrever o áudio: {e}")

    if not texto:
        return {"transcricao": "", "acionado": False,
                "processados": reuniao.excertos_processados(sessao)}

    reuniao.registar(sessao, indice, texto)
    processados = reuniao.excertos_processados(sessao)

    pendente = reuniao.chamada_pendente(sessao)
    if pendente is not None:
        # já estávamos à espera da continuação de uma chamada anterior (o
        # nome apareceu perto do fim de um excerto, a meio de frase) — este
        # excerto é essa continuação; responde já com os dois juntos, sem
        # voltar a esperar mais um (nunca mais do que um excerto de atraso)
        reuniao.limpar_chamada_pendente(sessao)
        texto_chamada = f"{pendente} {texto}".strip()
    elif reuniao.foi_chamada(texto) and not reuniao.parece_completa(texto):
        # foi chamada, mas o excerto acaba a meio de frase — o bloco de
        # gravação de duração fixa cortou-a, não foi uma pausa real da
        # pessoa. Espera pelo excerto seguinte antes de responder, em vez
        # de reagir já só ao bocado da pergunta que apanhou (era isto que
        # fazia a Alma dizer que não tinha ouvido).
        reuniao.registar_chamada_pendente(sessao, texto)
        return {"transcricao": texto, "acionado": False, "processados": processados}
    elif reuniao.foi_chamada(texto):
        texto_chamada = texto
    else:
        return {"transcricao": texto, "acionado": False, "processados": processados}

    # nova chamada: avança a geração já (antes de gerar a resposta) — é isto
    # que interrompe, de imediato, qualquer resposta anterior ainda em curso
    minha_geracao = reuniao.nova_geracao(sessao)
    contexto = reuniao.contexto_ao_vivo(sessao)
    mensagem_agente = (
        "Estás numa reunião em curso, a ouvir em modo contínuo (não é uma pergunta "
        "direta como de costume). Isto é o mais recente que se disse, transcrito "
        f"automaticamente (pode ter erros ou sobreposição de vozes):\n\n{contexto}\n\n"
        f'Alguém acabou de te chamar pelo nome. O que disseram foi: "{texto_chamada}"\n\n'
        "Responde diretamente a essa pessoa, como se estivesses presente na sala."
    )
    return StreamingResponse(
        _fluxo_resposta_por_voz(utilizador, sessao, mensagem_agente,
                                mensagem_visivel=f"🎙️ (reunião) {texto_chamada}",
                                texto_transcricao=texto_chamada, minha_geracao=minha_geracao),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.post("/alma/reuniao/terminar")
def reuniao_terminar(utilizador: str = Form(...), sessao: str = Form(...)):
    """Termina o modo reunião e gera um resumo/ata a partir de tudo o que foi
    ouvido — esse resumo é o único registo que fica guardado; a transcrição
    bruta é descartada a partir daqui."""
    transcricao = reuniao.terminar(sessao)
    if not transcricao.strip():
        return {"resumo": "Não ouvi conversa suficiente para gerar um resumo desta reunião."}

    mensagem_agente = (
        "Acabaste de ouvir esta reunião do início ao fim, em modo contínuo "
        "(transcrição automática — pode ter erros e alguma sobreposição de vozes):\n\n"
        f"{transcricao}\n\n"
        "Escreve um resumo/ata claro e conciso: principais pontos discutidos, decisões "
        "tomadas e ações com responsável, se forem identificáveis."
    )
    resultado = _responder_e_guardar(
        utilizador, sessao, mensagem_agente,
        mensagem_visivel="🎙️ (fim da reunião) Gera o resumo desta reunião."
    )
    return {"resumo": resultado["resposta"]}

async def _processar_ficheiro_anexado(ficheiro: UploadFile) -> str:
    """Lê um ficheiro anexado e devolve o texto/descrição já formatado para o
    agente, ou uma nota de erro — nunca levanta exceção, para uma falha num
    ficheiro não impedir os outros de serem processados (ver
    alma_com_ficheiro, que corre vários destes em paralelo)."""
    bruto = await ficheiro.read()
    if len(bruto) > 15 * 1024 * 1024:
        return f'Ficheiro anexado ("{ficheiro.filename}"): demasiado grande (máx. 15 MB), não foi lido.'
    try:
        # extrair_texto é síncrona (chama a API da Anthropic para imagens/
        # PDFs escaneados) — corre em thread para vários ficheiros
        # avançarem ao mesmo tempo, em vez de um de cada vez à vez (é o que
        # tornava lenta uma avaliação de carga com várias fotos anexadas).
        texto = await asyncio.to_thread(
            ficheiros_tool.extrair_texto, bruto, ficheiro.content_type, ficheiro.filename)
    except Exception as e:
        return f'Ficheiro anexado ("{ficheiro.filename}"): erro ao ler ({e}).'
    if texto is None:
        return (f'Ficheiro anexado ("{ficheiro.filename}"): não consigo ler ficheiros do tipo '
                f'{ficheiro.content_type or "(desconhecido)"}.')
    return f'Ficheiro anexado ("{ficheiro.filename}"):\n\n{texto[:8000]}'

_EXTENSOES_IMAGEM = (".jpg", ".jpeg", ".png", ".gif", ".webp")

def _e_imagem(ficheiro: UploadFile) -> bool:
    if ficheiro.content_type and ficheiro.content_type.startswith("image/"):
        return True
    return (ficheiro.filename or "").lower().endswith(_EXTENSOES_IMAGEM)

@app.post("/alma/ficheiro")
async def alma_com_ficheiro(utilizador: str = Form(...), sessao: str = Form(...),
                            mensagem: str = Form(""), ficheiros: list[UploadFile] = File(...)):
    """Recebe um ou mais ficheiros anexados na consola de chat (PDF, Word,
    imagem, texto) e responde com o seu conteúdo já disponível ao agente,
    por SSE (tal como /alma/stream) — não em bloco. Uma avaliação de carga
    (fotos + leitura do manual + Basecamp) pode demorar bastante mais do
    que um pedido de texto simples; sem o sinal de vida periódico da versão
    em stream, um pedido destes já ultrapassou o limite de um proxy
    intermediário e voltou "Erro ao contactar a Alma: 502" antes de a Alma
    sequer ter acabado de responder. Cada ficheiro é lido em paralelo com
    os outros (nunca um de cada vez) — um demasiado grande ou de um tipo
    não suportado não impede os outros de serem lidos, só fica assinalado
    para o agente saber que não conseguiu ler esse em concreto."""
    nomes = [ficheiro.filename for ficheiro in ficheiros]
    tem_imagem = any(_e_imagem(f) for f in ficheiros)
    partes = await asyncio.gather(*(_processar_ficheiro_anexado(f) for f in ficheiros))

    mensagem_visivel = "\n".join(f"📎 {nome}" for nome in nomes) + (f"\n{mensagem}" if mensagem else "")
    # o pedido em si vem sempre primeiro, antes do conteúdo dos ficheiros —
    # main.py trunca esta mensagem a 500 carateres só para escolher o
    # agente/subagente certo (ver orchestrator.encaminhar); com fotos
    # grandes ou várias, o conteúdo delas sozinho já passa dos 500
    # carateres, e se o pedido viesse depois nunca chegava a entrar nesse
    # excerto — a classificação via só descrições de imagem, sem saber que
    # a pessoa pediu uma avaliação, e escolhia o agente errado.
    mensagem_agente = ((mensagem or ("O que achas deste ficheiro?" if len(nomes) == 1
                                     else "O que achas destes ficheiros?"))
                       + "\n\n" + "\n\n---\n\n".join(partes))
    # mesmo com o pedido em primeiro lugar, uma legenda curta/genérica (ex:
    # "analisa a carga", sem a palavra "qualidade") podia continuar a ser
    # classificada como pergunta "geral" da Ecos Largos em vez de uma
    # avaliação de qualidade — anexar uma foto é por si só um sinal forte
    # e determinístico de pedido de avaliação, não vale a pena arriscar a
    # classificação por texto quando este sinal já existe (ver
    # orchestrator.escolher_agente_ecos_largos).
    return StreamingResponse(
        _fluxo_resposta_agente(utilizador, sessao, mensagem_agente, mensagem_visivel, tem_anexos=tem_imagem),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.get("/documentos-gerados/{id}")
def documento_gerado(id: int):
    """Serve um PDF gerado pela Alma (ver tools/documentos_gerados.gerar_pdf)
    — o link que ela partilha na conversa aponta para aqui. Guardado em
    Postgres, não em disco (o Railway não persiste ficheiros locais entre
    deploys), por isso o link continua válido mesmo depois de um deploy."""
    documento = obter_documento_gerado(id)
    if not documento:
        raise HTTPException(status_code=404, detail="documento não encontrado")
    # um título com acentos (normal em português) não é um valor de header
    # HTTP válido tal e qual — precisa do formato filename*= (RFC 6266),
    # com uma reserva em ASCII simples para browsers/clientes antigos
    titulo = documento["titulo"]
    nome_ascii = titulo.encode("ascii", errors="ignore").decode().strip() or "documento"
    nome_utf8 = quote(f"{titulo}.pdf")
    return Response(
        content=documento["pdf"], media_type="application/pdf",
        headers={"Content-Disposition":
                f'inline; filename="{nome_ascii}.pdf"; filename*=UTF-8\'\'{nome_utf8}'}
    )

@app.get("/health")
def health():
    """Inclui o commit em produção (Railway define isto automaticamente) —
    para confirmar de imediato se um deploy já terminou, sem adivinhar
    pelo tempo passado desde o merge nem ir ao painel do Railway."""
    return {
        "status": "ok",
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "desconhecido")[:12],
    }

@app.get("/health/config")
def health_config():
    """Diz quais as variáveis de ambiente necessárias que estão definidas (nunca os valores) — para diagnosticar sem ir ao painel do Railway."""
    variaveis = ["DATABASE_URL", "ANTHROPIC_API_KEY", "BIGCOMMERCE_STORE_HASH",
                 "BIGCOMMERCE_ACCESS_TOKEN", "SITE_URL",
                 "BASECAMP_ACCOUNT_ID", "BASECAMP_CLIENT_ID", "BASECAMP_CLIENT_SECRET",
                 "BASECAMP_REFRESH_TOKEN", "PROCEDIMENTOS_DOC_ID",
                 "ALMA_APP_URL", "BASECAMP_WEBHOOK_SECRET", "OPENAI_API_KEY",
                 "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ECOS_LARGOS_DASHBOARD_API_URL"]
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

@app.get("/basecamp/pessoas")
def diagnostico_pessoas_basecamp(projeto: str = "Gestão"):
    """Diagnóstico: mostra os campos brutos que o Basecamp devolve para uma
    pessoa de um projeto — usado para confirmar que o campo attachable_sgid
    (necessário para as menções reais em comentários) existe mesmo e tem
    este nome, sem precisar de ir aos logs do Railway."""
    pessoas = basecamp.pessoas_projeto(projeto)
    if not pessoas:
        return {"projeto": projeto, "total": 0, "aviso": "nenhuma pessoa encontrada para este projeto"}
    return {
        "projeto": projeto,
        "total": len(pessoas),
        "campos_disponiveis": sorted(pessoas[0].keys()),
        "tem_attachable_sgid": "attachable_sgid" in pessoas[0],
        "exemplo": pessoas[0],
    }

@app.post("/basecamp/resumo-semanal")
def resumo_semanal_basecamp_agora():
    """Dispara já o resumo semanal de atividade no Mural, em segundo plano."""
    threading.Thread(target=resumo_semanal_basecamp.correr_resumo_semanal, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.get("/logistica/diagnostico")
def diagnostico_logistica_entregas():
    """Diagnóstico: mostra os campos brutos de um card ativo do projeto
    "Entregas" — usado para confirmar, contra dados reais, o nome exato do
    campo que diz se um card está "On Hold" (nunca verificado ao vivo até
    agora, ver tools.logistica.esta_em_on_hold), sem precisar de ir aos
    logs do Railway."""
    itens = [i for i in basecamp._itens_ativos()
            if i.get("type") == "Kanban::Card"
            and logistica.PROJETO_ENTREGAS.lower() in ((i.get("bucket") or {}).get("name") or "").lower()]
    if not itens:
        return {"aviso": "nenhum card ativo encontrado no projeto Entregas"}
    return {
        "total": len(itens),
        "campos_disponiveis": sorted(itens[0].keys()),
        "tem_on_hold_at": "on_hold_at" in itens[0],
        "exemplo": itens[0],
    }

@app.get("/ecos-largos/diagnostico-manual")
def diagnostico_manual_qualidade_toros():
    """Diagnóstico: mostra exatamente o que a conta da Alma vê no Basecamp
    quando procura o manual de qualidade de cargas de toros — usado para
    perceber, contra dados reais, porque é que a procura por vezes não
    encontra o documento (título diferente do esperado? documento não
    partilhado com a conta da Alma? projeto errado?), sem precisar de ir
    aos logs do Railway. `lista_completa` (bruto, sem tentar casar com o
    manual) vem sempre com forcar=True, para nunca mostrar uma lista em
    cache desatualizada."""
    lista_completa = documentos_empresa._listar_bruto(forcar=True)
    candidatos_parecidos = [
        {k: item.get(k) for k in ("id", "tipo", "titulo", "projeto", "pasta")}
        for item in lista_completa
        if any(termo in ecos_largos._normalizar_titulo(item["titulo"])
               for termo in ("ecos", "toros", "carga", "qualidade", "regras", "analise"))
    ]
    resultado_leitura = ecos_largos.ler_manual_qualidade_cargas_toros()
    if "conteudo" in resultado_leitura:
        resultado_leitura = {**resultado_leitura, "conteudo": resultado_leitura["conteudo"][:500] + "..."}
    return {
        "total_documentos_e_ficheiros_visiveis": len(lista_completa),
        "candidatos_com_termo_parecido": candidatos_parecidos,
        "resultado_ler_manual_qualidade_cargas_toros": resultado_leitura,
    }

@app.get("/ecos-largos/diagnostico-avaliacoes")
def diagnostico_avaliacoes_cargas_toros(ano: int = None):
    """Diagnóstico: lê diretamente da base de dados as avaliações de cargas
    de toros guardadas (sem passar pela Alma) — usado para confirmar, com
    dados reais, se as gravações estão mesmo a acontecer, sem depender do
    que a Alma diz na conversa (ela pode dizer "guardado" mesmo quando uma
    gravação falhou, ou o inverso). Por omissão usa o ano corrente."""
    from datetime import date
    ano_resolvido = ano or date.today().year
    avaliacoes = avaliacoes_cargas_toros_ano(ano_resolvido)
    return {"ano": ano_resolvido, "total": len(avaliacoes), "avaliacoes": avaliacoes}

@app.post("/logistica/monitorizar")
def monitorizar_logistica_agora():
    """Dispara já a monitorização de logística (projeto Entregas), em
    segundo plano — os resultados/erros ficam nos logs do servidor."""
    threading.Thread(target=logistica_entregas.correr_monitorizacao_logistica, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/ecos-largos/resumo-diario")
def resumo_diario_ecos_largos_agora():
    """Dispara já a análise diária do dashboard de produção, no Mural da Ecos Largos, em segundo plano."""
    threading.Thread(target=resumo_diario_ecos_largos.correr_resumo_diario_ecos_largos, daemon=True).start()
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
