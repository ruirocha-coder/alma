# tools/voz.py — voz para a Alma: transcrição (STT) e síntese de fala (TTS).
#
# A Anthropic não tem estas capacidades, por isso é preciso recorrer a
# fornecedores externos: OpenAI (Whisper) para transcrever, e a ElevenLabs
# para sintetizar — com a voz personalizada da empresa, em vez de uma voz
# genérica. Ambos por pedidos HTTP simples (sem SDK nem streaming do lado do
# fornecedor: uma chamada por gravação/frase chega, porque quem faz o
# "streaming" percebido é o troceamento em frases feito aqui, não a API
# externa).
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

# limiares típicos para detetar um segmento "alucinado" pelo Whisper — texto
# inventado (ex: "subscreve o canal", créditos de legendagem tipo "Amara.org")
# quando o áudio é só silêncio/ruído, em vez de devolver vazio. Os dois
# sinais têm de aparecer juntos: no_speech_prob alto sozinho também acontece
# em fala real mas muito baixa/curta, por isso exige-se também pouca
# confiança no texto gerado (avg_logprob baixo).
_LIMIAR_SEM_FALA = 0.6
_LIMIAR_CONFIANCA = -1.0

def _parece_alucinacao(segmento: dict) -> bool:
    return (segmento.get("no_speech_prob", 0) > _LIMIAR_SEM_FALA
            and segmento.get("avg_logprob", 0) < _LIMIAR_CONFIANCA)

def transcrever(bruto: bytes, filename: str = "audio.webm", content_type: str = "audio/webm") -> str:
    """Transcreve uma gravação (ex: da consola de chat, ou um excerto de
    reunião) para texto, via Whisper. Pede o resultado em segmentos
    (verbose_json) e descarta os que parecem alucinações sobre
    silêncio/ruído — sem isto, um excerto sem fala real por vezes volta com
    texto inventado (frases comuns nos vídeos com que o Whisper foi
    treinado) em vez de vazio, poluindo a transcrição com conteúdo que
    ninguém disse."""
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        data={"model": "whisper-1", "language": "pt", "response_format": "verbose_json"},
        files={"file": (filename, bruto, content_type)},
        timeout=60,
    )
    r.raise_for_status()
    dados = r.json()
    segmentos = dados.get("segments")
    if segmentos is None:
        return (dados.get("text") or "").strip()
    partes = [s["text"].strip() for s in segmentos if s.get("text") and not _parece_alucinacao(s)]
    return " ".join(partes).strip()

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
