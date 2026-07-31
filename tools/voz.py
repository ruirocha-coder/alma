# tools/voz.py — voz da Alma no modo reunião: uma sessão de conversação
# completa da Realtime API da OpenAI (ouve, fala, decide quando responder e
# gere as suas próprias interrupções) — o browser liga-se-lhe diretamente
# por WebRTC, sem o áudio passar por este servidor. Quando a pergunta for
# sobre a empresa (Basecamp, produção, calendário, documentos, equipas), a
# Alma chama a função "perguntar_dados_empresa" em vez de responder sozinha;
# o browser intercepta essa chamada e envia a pergunta a
# /alma/reuniao/pergunta_empresa (main.py), que corre o Claude com as
# ferramentas de sempre e devolve o texto para a sessão dizer.
#
# Histórico (2026-07-31): esta era antes uma sessão só de transcrição
# (gpt-4o-mini-transcribe) + síntese de voz nossa por frase (OpenAI TTS,
# `/v1/audio/speech`, voz "marin") + deteção de chamada/interrupção em
# código (regex sobre o texto transcrito, ver histórico de
# tools/reuniao.py). Essa arquitetura mostrou-se frágil: a voz da própria
# Alma, apanhada de volta pelo microfone (eco imperfeito, sem a mesma
# ligação WebRTC a servir de referência), por vezes continha "Alma" e
# disparava uma resposta nova sobre a que estava em curso — um ciclo de
# arrancar/parar. Numa sessão de conversação a sério, o cancelamento de eco
# e a deteção de turno/interrupção são geridos nativamente pela OpenAI
# sobre a mesma ligação que reproduz a voz da Alma, em vez de recriados à
# mão a partir de texto transcrito.
#
# Trade-off aceite (Rui, 2026-07-31): a Alma pode reformular por palavras
# próprias o que o Claude respondeu (a Realtime API fala com a sua própria
# voz, não lê o texto do Claude à letra), e a regra "só responde quando
# chamada pelo nome" passa a ser uma instrução dada ao modelo, não uma
# verificação de código determinística.
import os, re
import httpx

# a resposta do Claude vem em markdown (para a consola em texto), mas isso
# não deve chegar tal e qual à voz — a Realtime API, ao receber isto como
# resultado da função perguntar_dados_empresa, ou tentaria "ler" os
# marcadores (asteriscos, pipes de tabela) ou parafraseava-os de forma
# estranha. Isto limpa a formatação antes de devolver o texto para a Alma
# dizer; a consola em texto continua a mostrar o markdown original (ver
# main.py:reuniao_pergunta_empresa, que devolve os dois separadamente).
_MD_BLOCO_CODIGO = re.compile(r"```.*?```", re.DOTALL)
_MD_CODIGO_LINHA = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_ENFASE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1")
_MD_TITULO = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LISTA = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_LISTA_NUM = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_MD_CITACAO = re.compile(r"^>\s?", re.MULTILINE)
_MD_LINHA_HORIZONTAL = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
_MD_PIPE = re.compile(r"\|")

def limpar_para_fala(texto: str) -> str:
    """Remove marcação markdown (incluindo tabelas) de um texto antes de o
    devolver para a Alma dizer em voz — a consola em texto continua a
    receber o markdown original, sem passar por aqui."""
    texto = _MD_BLOCO_CODIGO.sub(" ", texto)
    texto = _MD_LINHA_HORIZONTAL.sub(" ", texto)
    texto = _MD_LINK.sub(r"\1", texto)
    texto = _MD_CODIGO_LINHA.sub(r"\1", texto)
    texto = _MD_ENFASE.sub(r"\2", texto)
    texto = _MD_TITULO.sub("", texto)
    texto = _MD_LISTA.sub("", texto)
    texto = _MD_LISTA_NUM.sub("", texto)
    texto = _MD_CITACAO.sub("", texto)
    texto = _MD_PIPE.sub(" ", texto)
    return re.sub(r"\s+", " ", texto).strip()

# a Alma só deve chamar esta função para assuntos concretos da empresa —
# para conversa geral responde diretamente, com a sua própria voz
TOOL_PERGUNTAR_EMPRESA = {
    "type": "function",
    "name": "perguntar_dados_empresa",
    "description": (
        "Usa esta função sempre que te perguntarem sobre assuntos concretos "
        "da empresa: tarefas ou cards do Basecamp, produção, encomendas, "
        "entregas, logística, calendário, documentos internos, ou "
        "informação sobre a Interior Guider, a Ecos Largos ou as equipas. "
        "NUNCA inventes nem adivinhes estes dados sozinha — na dúvida, "
        "chama sempre esta função em vez de arriscar uma resposta. Para "
        "conversa geral, opiniões, ou conhecimento do dia a dia, responde "
        "tu diretamente, sem a usar."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pergunta": {
                "type": "string",
                "description": "A pergunta da pessoa, tal como foi feita.",
            },
        },
        "required": ["pergunta"],
    },
}

INSTRUCOES_MODO_REUNIAO = (
    "Português europeu, nunca do Brasil. És a Alma, assistente da Boa Safra "
    "/ Interior Guider, numa reunião de trabalho em modo de escuta contínua "
    "— várias pessoas podem estar a falar entre si, não contigo.\n\n"
    "REGRA MAIS IMPORTANTE, acima de tudo o resto: só respondes quando "
    "alguém te chama pelo nome diretamente, a dirigir-se a ti. Na dúvida, "
    "o mais seguro é sempre ficar calada — nunca o oposto. É preferível "
    "ficar calada uma vez em que eras mesmo chamada do que responder uma "
    "vez em que não eras: uma resposta fora de contexto interrompe a "
    "reunião das pessoas.\n\n"
    "Exemplos do que É uma chamada direta (respondes):\n"
    "- \"Alma, que dia é hoje?\"\n"
    "- \"Ó Alma, ajuda-me aqui.\"\n"
    "- \"Diz-me lá, Alma, qual é o estado da encomenda X.\"\n\n"
    "Exemplos do que NÃO é uma chamada, é o teu nome só a passar na "
    "conversa entre as pessoas (NÃO respondas nem interrompas):\n"
    "- \"A Alma disse ontem que...\"\n"
    "- \"Depois perguntamos à Alma.\"\n"
    "- \"Ainda bem que temos a Alma para isto.\"\n"
    "- Qualquer frase em que \"Alma\" não é a pessoa a quem se fala, mas "
    "sim de quem se fala.\n\n"
    "Quando fores mesmo chamada: se for conversa geral, responde tu mesma, "
    "breve e direta. Se for sobre a empresa (Basecamp, produção, "
    "encomendas, entregas, calendário, documentos, equipas), usa sempre a "
    "função perguntar_dados_empresa — nunca inventes esses dados.\n\n"
    "Se alguém te chamar de novo enquanto ainda estás a falar, pára "
    "imediatamente e ouve o que disserem a seguir.\n\n"
    "Repetindo, porque é a regra mais importante: se não foste chamada "
    "diretamente pelo nome, fica calada. Não respondas."
)

def emprestar_token_conversa() -> dict:
    """Pede à OpenAI um token efémero (dura poucos minutos/horas, nunca é a
    chave principal da API) para o browser abrir, ele próprio, uma sessão de
    conversação completa da Realtime API por WebRTC — sem o nosso servidor
    ter de reencaminhar áudio. Devolve o token e a hora a que expira, para o
    browser saber quando pedir um novo (ver reconexão no modo reunião).

    Ao contrário de uma sessão só de transcrição, esta fala com a sua
    própria voz e decide, pelas instruções acima, quando responder e quando
    chamar perguntar_dados_empresa — ver o módulo para o porquê desta troca.

    noise_reduction "far_field" (mic de sala) e o "prompt" de transcrição a
    nomear "Alma" explicitamente: mesma sintonia já testada ao vivo na
    versão anterior (só transcrição) — sem o nome no prompt, o modelo por
    vezes ouvia "Alba" ou "Alberto" em vez de "Alma"."""
    r = httpx.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "instructions": INSTRUCOES_MODO_REUNIAO,
                # por omissão já seria ["audio"], mas explícito não deixa
                # nada a depender de um valor por omissão não confirmado
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "transcription": {
                            "model": "gpt-4o-mini-transcribe",
                            "language": "pt",
                            "prompt": ("Reunião de trabalho em português europeu, na empresa "
                                       "Boa Safra / Interior Guider, com a assistente Alma a "
                                       "participar por voz. As pessoas dizem o nome \"Alma\" com "
                                       "frequência para lhe chamar a atenção — nunca transcrever "
                                       "\"Alma\" como um nome parecido (ex: Alba, Alberto, Ana). "
                                       "Assuntos comuns: produção, encomendas, entregas, logística, "
                                       "Basecamp, calendário."),
                        },
                        "turn_detection": {
                            "type": "semantic_vad",
                            # "low" em vez de "high" (Rui, 2026-07-31): menos
                            # ávido a fechar um turno como "completo" dá ao
                            # modelo menos oportunidades por reunião de errar
                            # se deve responder — sem ligar a isso, uma
                            # eagerness alta multiplicava o número de vezes
                            # que a regra "só respondes se chamada" tinha de
                            # ser acertada.
                            "eagerness": "low",
                            # create_response: true — voltámos atrás de
                            # false (Rui, 2026-07-31): com false, o browser
                            # decidia por regex (/\balma\b/i no texto
                            # transcrito) quando disparar response.create, o
                            # que forçava sempre uma resposta em voz mesmo
                            # quando "Alma" só passava numa frase que não lhe
                            # era dirigida (ex: "a Alma disse que..."), já
                            # que response.create não dá à sessão a opção de
                            # decidir ficar calada — só de decidir o quê
                            # dizer. Isso produzia respostas incoerentes /
                            # fora de contexto, pior do que confiar no juízo
                            # do modelo. Com true, é a própria sessão que
                            # decide, a cada turno, se responde — a garantia
                            # passa a vir de INSTRUCOES_MODO_REUNIAO (regra
                            # reforçada com exemplos positivos/negativos e
                            # repetida) em vez de código. interrupt_response
                            # mantém-se true: uma fala nova continua a cortar
                            # uma resposta em curso.
                            "create_response": True,
                            "interrupt_response": True,
                        },
                        "noise_reduction": {"type": "far_field"},
                    },
                    "output": {"voice": "marin"},
                },
                "tools": [TOOL_PERGUNTAR_EMPRESA],
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    dados = r.json()
    return {"token": dados["value"], "expira_em": dados["expires_at"]}
