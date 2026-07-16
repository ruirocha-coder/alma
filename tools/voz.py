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

def transcrever(bruto: bytes, filename: str = "audio.webm", content_type: str = "audio/webm") -> str:
    """Transcreve uma gravação (ex: da consola de chat) para texto, via Whisper."""
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        data={"model": "whisper-1", "language": "pt"},
        files={"file": (filename, bruto, content_type)},
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()

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
        },
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
