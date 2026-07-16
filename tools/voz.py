# tools/voz.py — voz para a Alma: transcrição (STT) e síntese de fala (TTS).
#
# A Anthropic não tem estas capacidades, por isso esta é a única integração
# desta aplicação que depende de outro fornecedor — usa a OpenAI (Whisper
# para transcrever, TTS para sintetizar), via pedidos HTTP simples (sem SDK
# nem streaming do lado do fornecedor: uma chamada por gravação/frase chega,
# porque quem faz o "streaming" percebido é o troceamento em frases feito
# aqui, não a API externa).
import os, re
import httpx

_FIM_DE_FRASE = re.compile(r"(?<=[.!?…])\s+")

def _headers():
    return {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}

def transcrever(bruto: bytes, filename: str = "audio.webm", content_type: str = "audio/webm") -> str:
    """Transcreve uma gravação (ex: da consola de chat) para texto, via Whisper."""
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers=_headers(),
        data={"model": "whisper-1", "language": "pt"},
        files={"file": (filename, bruto, content_type)},
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()

def sintetizar(texto: str, voz: str = "alloy") -> bytes:
    """Sintetiza texto em voz (mp3)."""
    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"model": "tts-1", "voice": voz, "input": texto},
        timeout=60,
    )
    r.raise_for_status()
    return r.content

def dividir_em_frases_prontas(buffer_texto: str) -> tuple[list[str], str]:
    """Dado o texto acumulado até agora (ex: enquanto a resposta ainda está a
    chegar em stream), separa as frases já fechadas (terminadas em . ! ? …)
    do resto, que ainda pode crescer. Cada frase pronta pode ser sintetizada
    e tocada de imediato, sem esperar pela resposta toda."""
    partes = _FIM_DE_FRASE.split(buffer_texto)
    if len(partes) <= 1:
        return [], buffer_texto
    return partes[:-1], partes[-1]
