from dotenv import load_dotenv
load_dotenv()

import asyncio, json, os
import threading
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from orchestrator import encaminhar, contexto_para_encaminhar, AGENTES, AGENTES_STREAM
from db import (guardar_mensagem, historico_sessao, log_routing,
                sessoes_utilizador, eliminar_sessao, perfil_existe, alertas_recentes,
                obter_documento_gerado, avaliacoes_cargas_toros_ano)
from agents import (acolhimento, monitor_basecamp, responder_basecamp,
                    resumo_semanal_basecamp, resumo_diario_ecos_largos,
                    resumo_anual_cargas_toros, logistica_entregas,
                    sugestao_logistica_semanal, estimativa_montagem,
                    avisos_gestao_agendas, sincronizacao_calendario)
from tools import basecamp, ficheiros as ficheiros_tool, voz, reuniao, documentos_empresa, ecos_largos
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
# sugestão semanal de logística (Mural "Programação", projeto Entregas),
# dirigida à Conceição Costa — pedido explícito do Rui (2026-07-23):
# segundas-feiras às 8h30, depois da monitorização diária das 7h30 (para
# refletir o estado mais recente possível) e antes dos outros resumos
# semanais das 9h/9h15, para nunca publicarem em simultâneo
scheduler.add_job(sugestao_logistica_semanal.correr_sugestao_semanal_logistica, "cron",
                  day_of_week="mon", hour=8, minute=30)
# verificar entregas concluídas e ler o "Real" registado pela equipa (ver
# "Procedimento Tempos de Montagem para Logística"): todos os dias às 18h,
# fim do dia — depois de qualquer entrega feita nesse dia
scheduler.add_job(estimativa_montagem.verificar_entregas_concluidas_e_ler_real, "cron", hour=18, minute=0)
# calibração da estimativa de tempos de montagem: de 2 em 2 meses, dia 1 às
# 8h — compara estimativa vs. real acumulado e publica um relatório de
# desvio (nunca ajusta parâmetros sozinha, ver agents/estimativa_montagem.py)
scheduler.add_job(estimativa_montagem.correr_calibracao_estimativa, "cron",
                  month="1,3,5,7,9,11", day=1, hour=8, minute=0)
# avisos do documento "GESTÃO DAS AGENDAS" (projeto Alma Data), dirigidos
# à Conceição Costa: todos os dias às 8h, depois da monitorização de
# logística das 7h30 — a própria função só publica algo nos dias em que
# um marco do documento cai mesmo nesse dia (ver
# agents/avisos_gestao_agendas.py), por isso corre sozinha todos os dias
# sem custo extra na maioria deles.
scheduler.add_job(avisos_gestao_agendas.correr_avisos_gestao_agendas, "cron", hour=8, minute=0)
# sincronização unidirecional Basecamp (Agenda do projeto Entregas) ->
# Google Calendar: de 2 em 2 minutos, pedido do Rui (2026-07-29) — o único
# job por intervalo (não "cron") desta aplicação, porque aqui o objetivo é
# mesmo "quase em tempo real" e não um horário fixo do dia.
scheduler.add_job(sincronizacao_calendario.correr_sincronizacao_calendario, "interval", minutes=2)
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
            agente = encaminhar(contexto_para_encaminhar(mensagens), utilizador, tem_anexos=tem_anexos)
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
            agente = encaminhar(contexto_para_encaminhar(mensagens), utilizador, tem_anexos=tem_anexos)
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

@app.post("/alma/reuniao/iniciar")
def reuniao_iniciar(sessao: str = Form(...)):
    """Começa o modo reunião: o browser liga-se diretamente a uma sessão de
    conversação completa da Realtime API da OpenAI (ouve, fala e decide por
    si quando responder — ver tools/voz.py:emprestar_token_conversa)."""
    reuniao.iniciar(sessao)
    try:
        token = voz.emprestar_token_conversa()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao ligar à conversação: {e}")
    return {"ok": True, **token}

@app.post("/alma/reuniao/token")
def reuniao_token(sessao: str = Form(...)):
    """Empresta um novo token efémero de conversação para uma reunião já em
    curso (ex: depois de uma queda de rede a meio) — ao contrário de
    /alma/reuniao/iniciar, não reinicia a transcrição acumulada até agora."""
    if not reuniao.em_curso(sessao):
        raise HTTPException(status_code=409, detail="Não há nenhuma reunião em curso nesta sessão.")
    try:
        return voz.emprestar_token_conversa()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao ligar à conversação: {e}")

@app.post("/alma/reuniao/chunk")
def reuniao_chunk(sessao: str = Form(...), indice: int = Form(...), texto: str = Form(...)):
    """Recebe mais um turno já transcrito da reunião em curso pelo browser
    (indice identifica a posição deste turno na ordem em que foi dito, para a
    transcrição acumulada ficar sempre correta mesmo que os pedidos cheguem
    trocados) — só para alimentar o resumo/ata final e o contexto dado ao
    Claude quando for preciso (ver /alma/reuniao/pergunta_empresa). A
    decisão de responder ou não já não é tomada aqui: é da própria sessão
    de conversação da Realtime API, pelas instruções em
    tools/voz.py:INSTRUCOES_MODO_REUNIAO."""
    if not reuniao.em_curso(sessao):
        raise HTTPException(status_code=409, detail="Não há nenhuma reunião em curso nesta sessão.")
    texto = texto.strip()
    if texto:
        reuniao.registar(sessao, indice, texto)
    return {"processados": reuniao.excertos_processados(sessao)}

@app.post("/alma/reuniao/pergunta_empresa")
def reuniao_pergunta_empresa(utilizador: str = Form(...), sessao: str = Form(...), pergunta: str = Form(...),
                             indice_recente: int = Form(0)):
    """Chamado pelo browser quando a sessão de conversação decide que uma
    pergunta é sobre a empresa (função perguntar_dados_empresa definida em
    tools/voz.py) — corre o Claude, com as ferramentas de sempre (Basecamp,
    calendário, documentos, dashboards), e devolve a resposta em texto para
    a Alma dizer com a sua própria voz. Ao contrário do resto do modo
    reunião, isto não decide SE deve responder — isso já foi decidido pela
    sessão de conversação; aqui só se obtém a resposta.

    indice_recente: o próximo índice que o cliente vai atribuir a um turno
    transcrito, no momento desta chamada — usado só para posicionar a
    resposta da Alma no sítio certo da transcrição acumulada (ver
    reuniao.registar_resposta_alma)."""
    if not reuniao.em_curso(sessao):
        raise HTTPException(status_code=409, detail="Não há nenhuma reunião em curso nesta sessão.")
    pergunta = pergunta.strip()
    if not pergunta:
        raise HTTPException(status_code=400, detail="Pergunta vazia.")

    print(f"[reuniao] (sessao={sessao}): pergunta_empresa {pergunta!r}")
    contexto = reuniao.contexto_ao_vivo(sessao)
    # a pergunta em si fica no fim, sem mais nada a seguir — o encaminhamento
    # (orchestrator.contexto_para_encaminhar) só olha para os últimos 800
    # carateres das mensagens recentes, e esta mensagem pode ter dezenas de
    # milhares de carateres de transcrição de reunião antes da pergunta. Bug
    # real (Rui, 2026-07-31): com a pergunta a meio e instruções de formatação
    # a seguir, a Alma respondeu que só tinha acesso ao Basecamp, não ao
    # dashboard — sinal de ter sido encaminhada para o agente errado, sem ver
    # a pergunta com clareza suficiente no excerto usado para decidir.
    mensagem_agente = (
        "Estás numa reunião em curso, em modo de conversação por voz. "
        "Responde diretamente a quem te perguntar a seguir, como se "
        "estivesses presente na sala. Podes usar a formatação (tabelas, "
        "listas, etc.) que fizer sentido para os dados — a versão dita em "
        "voz é limpa à parte (ver tools/voz.py:limpar_para_fala), a consola "
        "em texto mostra esta resposta tal como a escreveres.\n\n"
        "Isto é o mais recente que se disse na reunião, transcrito "
        f"automaticamente (pode ter erros ou sobreposição de vozes):\n\n{contexto}\n\n"
        f'Pergunta (sobre a empresa): "{pergunta}"'
    )
    try:
        resultado = _responder_e_guardar(
            utilizador, sessao, mensagem_agente,
            mensagem_visivel=f"🎙️ (reunião) {pergunta}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao obter resposta do agente: {e}")
    # "resposta" fica com o markdown original, para a consola em texto
    # mostrar formatado (ex: tabelas do Basecamp); "resposta_falada" é o
    # texto limpo que a sessão de conversação recebe para dizer — sem isto,
    # tabelas/marcadores de markdown chegavam à Alma tal e qual e ou eram
    # lidos à letra (pipes, asteriscos) ou parafraseados de forma estranha.
    resposta = resultado["resposta"]
    # entra também na transcrição acumulada, para a ata final refletir o que
    # a Alma respondeu, não só o que lhe perguntaram (pedido do Rui, 2026-07-31)
    reuniao.registar_resposta_alma(sessao, resposta, indice_recente)
    return {"resposta": resposta, "resposta_falada": voz.limpar_para_fala(resposta)}

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

_MEDIA_TYPE_POR_FORMATO = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

@app.get("/documentos-gerados/{id}")
def documento_gerado(id: int):
    """Serve um documento gerado pela Alma (PDF ou Excel — ver
    tools/documentos_gerados.gerar_pdf e gerar_excel) — o link que ela
    partilha na conversa aponta para aqui. Guardado em Postgres, não em
    disco (o Railway não persiste ficheiros locais entre deploys), por
    isso o link continua válido mesmo depois de um deploy."""
    documento = obter_documento_gerado(id)
    if not documento:
        raise HTTPException(status_code=404, detail="documento não encontrado")
    formato = documento["formato"]
    media_type = _MEDIA_TYPE_POR_FORMATO.get(formato, "application/octet-stream")
    # um título com acentos (normal em português) não é um valor de header
    # HTTP válido tal e qual — precisa do formato filename*= (RFC 6266),
    # com uma reserva em ASCII simples para browsers/clientes antigos
    titulo = documento["titulo"]
    nome_ascii = titulo.encode("ascii", errors="ignore").decode().strip() or "documento"
    nome_utf8 = quote(f"{titulo}.{formato}")
    # inline só faz sentido para o PDF (o browser sabe mostrá-lo); um .xlsx
    # inline costuma só descarregar de qualquer forma, mas "attachment" é o
    # comportamento correto e explícito para esse caso
    disposicao = "inline" if formato == "pdf" else "attachment"
    return Response(
        content=documento["pdf"], media_type=media_type,
        headers={"Content-Disposition":
                f'{disposicao}; filename="{nome_ascii}.{formato}"; filename*=UTF-8\'\'{nome_utf8}'}
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
                 "ECOS_LARGOS_DASHBOARD_API_URL"]
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
    """Diagnóstico: mostra as colunas reais vistas no projeto "Entregas" e
    os cards já em "On Hold" (prontos a entregar — ver
    tools.logistica.fase_encomenda), sem precisar de ir aos logs do
    Railway. A mesma informação também está disponível diretamente na
    conversa com a Alma (ver agents.logistica_entregas.diagnostico_cards_regiao,
    a mesma função usada aqui, para nunca haver duas versões desta lógica
    a divergir)."""
    return logistica_entregas.diagnostico_cards_regiao()

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

@app.post("/logistica/sugestao-semanal")
def sugestao_semanal_logistica_agora():
    """Dispara já a sugestão semanal de logística (Mural "Programação",
    projeto Entregas, dirigida à Conceição Costa), em segundo plano — os
    resultados/erros ficam nos logs do servidor. Útil para testar sem
    esperar pela segunda-feira."""
    threading.Thread(target=sugestao_logistica_semanal.correr_sugestao_semanal_logistica, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/logistica/verificar-real")
def verificar_entregas_concluidas_agora():
    """Dispara já a verificação de entregas concluídas e leitura do "Real"
    registado pela equipa (ver "Procedimento Tempos de Montagem para
    Logística"), em segundo plano — útil para testar sem esperar pelas 18h."""
    threading.Thread(target=estimativa_montagem.verificar_entregas_concluidas_e_ler_real, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/logistica/calibrar-estimativa")
def calibrar_estimativa_agora():
    """Dispara já o relatório de calibração da estimativa de tempos de
    montagem (estimativa vs. real, publicado no Mural do projeto Entregas),
    em segundo plano — útil para testar sem esperar pelo ciclo bimestral."""
    threading.Thread(target=estimativa_montagem.correr_calibracao_estimativa, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/logistica/avisos-gestao-agendas")
def avisos_gestao_agendas_agora():
    """Dispara já a verificação dos marcos do documento "GESTÃO DAS
    AGENDAS" (confirmação com a Sede, informação da previsão ao cliente,
    confirmação final, por região), em segundo plano — só publica algo
    no Mural do projeto Entregas se hoje for mesmo um desses dias. Útil
    para testar sem esperar pelo dia certo da semana."""
    threading.Thread(target=avisos_gestao_agendas.correr_avisos_gestao_agendas, daemon=True).start()
    return {"iniciado": True, "nota": "a correr em segundo plano — acompanha nos logs"}

@app.post("/logistica/sincronizar-calendario")
def sincronizar_calendario_agora():
    """Dispara já um ciclo da sincronização unidirecional da Agenda do
    projeto Entregas (Basecamp) para o Google Calendar, em segundo plano —
    útil para testar sem esperar pelo próximo ciclo automático (a cada 2
    minutos)."""
    threading.Thread(target=sincronizacao_calendario.correr_sincronizacao_calendario, daemon=True).start()
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

@app.middleware("http")
async def sem_cache_para_html(request: Request, call_next):
    """StaticFiles não define Cache-Control, e os browsers guardam o
    index.html com cache heurística — já aconteceu um deploy com correção
    de um bug (exportação Excel) parecer não ter tido efeito nenhum,
    porque a pessoa continuava a receber a versão antiga em cache. Força
    sempre revalidação do HTML, para cada deploy ter efeito imediato."""
    response = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# consola de chat de teste, servida em "/"
app.mount("/", StaticFiles(directory="static", html=True), name="static")
