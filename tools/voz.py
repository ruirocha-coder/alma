# tools/voz.py — voz para a Alma: síntese de fala (TTS) e o token efémero que
# dá acesso ao browser à Realtime API da OpenAI para transcrever ao vivo (a
# transcrição em si já não passa por aqui — ver tools/reuniao.py e o modo
# reunião em main.py: o browser liga-se diretamente à OpenAI por WebRTC e só
# envia o texto já transcrito para o servidor).
#
# A Anthropic não tem síntese de fala, por isso recorre-se à ElevenLabs —
# medido ao vivo (pedido do Rui, 2026-07-30): a API de TTS da OpenAI demora
# 2-4s por frase, a ElevenLabs Flash (escolhida por ser a mais rápida do seu
# catálogo) bem menos de 1s — a diferença sente-se muito numa conversa em
# tempo real, onde cada frase da resposta espera por isto antes de tocar.
# Por pedido HTTP simples (sem SDK nem streaming do lado do fornecedor: uma
# chamada por frase chega, porque quem faz o "streaming" percebido é o
# troceamento em frases feito aqui, não a API externa).
import os, re
import httpx

_FIM_DE_FRASE = re.compile(r"(?<=[.!?…])\s+")

# a resposta do modelo vem em markdown (para a consola em texto), mas isso
# não deve ser lido em voz alta tal e qual — senão a Alma diz literalmente
# "asterisco asterisco", lê o alvo de um link, ou faz pausas estranhas nos
# marcadores de lista/título. Isto limpa a formatação antes de sintetizar.
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
    """Remove marcação markdown de um excerto de texto antes de o sintetizar
    em voz — a consola em texto continua a receber o markdown original."""
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

def sintetizar(texto: str) -> bytes:
    """Sintetiza texto em voz (mp3), com a voz personalizada da empresa."""
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}",
        headers={
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
            "Content-Type": "application/json",
        },
        json={
            "text": texto,
            "model_id": "eleven_flash_v2_5",  # multilingue (inclui português) e o mais rápido
            "voice_settings": {
                # stability mais baixa dá mais variação de entoação (menos "monótono
                # e sério"); style acrescenta um pouco de exagero expressivo — o
                # resultado é um tom mais alegre, sem exagerar ao ponto de soar instável.
                "stability": 0.35,
                "similarity_boost": 0.8,
                "style": 0.45,
                "use_speaker_boost": True,
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.content

def emprestar_token_transcricao() -> dict:
    """Pede à OpenAI um token efémero (dura poucos minutos/horas, nunca é a
    chave principal da API) para o browser abrir, ele próprio, uma sessão de
    transcrição contínua da Realtime API por WebRTC — sem o nosso servidor
    ter de reencaminhar áudio. Devolve o token e a hora a que expira, para o
    browser saber quando pedir um novo (ver reconexão no modo reunião).

    O modelo "gpt-live-transcribe" (mais indicado para legendas em contínuo)
    não suporta turn_detection — a API rejeita o pedido com "Turn detection
    is not supported for this transcription model" — e sem turn_detection o
    servidor nunca deteta o fim de uma frase, por isso nunca chega nenhuma
    transcrição (era o bug de "não ouve nada"). "gpt-4o-mini-transcribe"
    suporta-o.

    semantic_vad (não server_vad): testado ao vivo (pedido do Rui,
    2026-07-30) — server_vad fecha o turno logo após um breve silêncio fixo
    (~200ms por omissão), o que na prática cortava frases inteiras em
    pedaços minúsculos ("Hm.", "OK.", "Alma" sozinha sem a pergunta a
    seguir) sempre que havia a mínima pausa a meio de uma frase. Cada
    fragmento com "Alma" disparava uma resposta nova e cortava a anterior a
    meio (ver nova_geracao em tools/reuniao.py), por isso a Alma nunca
    chegava a terminar uma resposta completa — sentia-se pior e mais lenta,
    não melhor. semantic_vad só fecha o turno quando entende que a pessoa
    terminou mesmo o pensamento, a um custo de latência que, na prática,
    compensa por dar turnos inteiros e coerentes em vez de fragmentados."""
    r = httpx.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "transcription": {"model": "gpt-4o-mini-transcribe", "language": "pt"},
                    "turn_detection": {"type": "semantic_vad"},
                }},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    dados = r.json()
    return {"token": dados["value"], "expira_em": dados["expires_at"]}

# quando a primeira frase da resposta é muito longa (uma introdução sem
# pontuação a fechar tão cedo), esperar por ela tal e qual faz a voz demorar
# demasiado a começar a falar — ao fim deste comprimento, corta-se na última
# vírgula ou espaço disponível e sintetiza-se esse pedaço na mesma.
_TAMANHO_MAXIMO_SEM_PONTUACAO = 180

def dividir_em_frases_prontas(buffer_texto: str) -> tuple[list[str], str]:
    """Dado o texto acumulado até agora (ex: enquanto a resposta ainda está a
    chegar em stream), separa as frases já fechadas (terminadas em . ! ? …)
    do resto, que ainda pode crescer. Cada frase pronta pode ser sintetizada
    e tocada de imediato, sem esperar pela resposta toda."""
    partes = _FIM_DE_FRASE.split(buffer_texto)
    if len(partes) > 1:
        return partes[:-1], partes[-1]

    if len(buffer_texto) > _TAMANHO_MAXIMO_SEM_PONTUACAO:
        corte = buffer_texto.rfind(", ", 0, _TAMANHO_MAXIMO_SEM_PONTUACAO)
        if corte == -1:
            corte = buffer_texto.rfind(" ", 0, _TAMANHO_MAXIMO_SEM_PONTUACAO)
        if corte != -1:
            return [buffer_texto[:corte + 1]], buffer_texto[corte + 1:]

    return [], buffer_texto
