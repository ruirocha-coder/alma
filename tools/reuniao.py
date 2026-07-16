# tools/reuniao.py — modo reunião: a Alma "ouve" continuamente (em vez de
# push-to-talk) e só entra na conversa quando é chamada pelo nome.
#
# O áudio de cada excerto é transcrito e imediatamente descartado — só o
# texto fica em memória, e apenas enquanto a reunião está a decorrer (nunca
# em disco/BD). No fim, o resumo/ata gerado a partir dessa transcrição é o
# único registo que persiste (guardado no histórico da conversa, como
# qualquer outra resposta da Alma); a transcrição bruta é descartada nesse
# momento.
#
# Estado em memória de processo, não na BD: um único servidor (Railway,
# uma instância) chega para o caso de uso — uma reunião em curso por sessão.
import re

_MENCAO_ALMA = re.compile(r"\balma\b", re.IGNORECASE)

# quando a Alma responde "ao vivo" a uma chamada, usar a transcrição toda
# desde o início da reunião tornaria cada resposta mais lenta à medida que a
# reunião cresce (mais texto a enviar ao modelo) — o que se sente como
# "bloquear" numa reunião longa ou muito faladora. Só o fim recente da
# transcrição chega para responder com contexto; a transcrição completa
# continua a ser usada no resumo final.
_LIMITE_CONTEXTO_AO_VIVO = 6000

_transcricoes: dict[str, dict[int, str]] = {}
_processados: dict[str, int] = {}
_a_responder: dict[str, bool] = {}

def iniciar(sessao: str) -> None:
    """Começa (ou reinicia) a escuta de uma reunião para esta sessão."""
    _transcricoes[sessao] = {}
    _processados[sessao] = 0
    _a_responder[sessao] = False

def em_curso(sessao: str) -> bool:
    return sessao in _transcricoes

def registar(sessao: str, indice: int, texto: str) -> None:
    """Acrescenta mais um excerto transcrito, na posição indicada pelo
    índice (atribuído no cliente pela ordem de gravação) — assim a
    transcrição fica na ordem certa mesmo que os pedidos de rede cheguem
    trocados (ex: um excerto demorou mais a transcrever que o seguinte)."""
    if texto.strip():
        _transcricoes.setdefault(sessao, {})[indice] = texto.strip()
    _processados[sessao] = _processados.get(sessao, 0) + 1

def excertos_processados(sessao: str) -> int:
    """Contagem de excertos já respondidos pelo servidor — serve de sinal de
    vida para a consola (para se perceber que a Alma continua ativa, mesmo
    quando não há nada para responder)."""
    return _processados.get(sessao, 0)

def esta_a_responder(sessao: str) -> bool:
    return _a_responder.get(sessao, False)

def marcar_a_responder(sessao: str, valor: bool) -> None:
    _a_responder[sessao] = valor

def foi_chamada(texto: str) -> bool:
    """Verifica se este excerto menciona a Alma diretamente (ex: "Alma, o que achas...")."""
    return bool(_MENCAO_ALMA.search(texto))

def _ordenada(sessao: str) -> list[str]:
    mapa = _transcricoes.get(sessao, {})
    return [mapa[i] for i in sorted(mapa)]

def transcricao_ate_agora(sessao: str) -> str:
    return " ".join(_ordenada(sessao))

def contexto_ao_vivo(sessao: str) -> str:
    """Como transcricao_ate_agora, mas limitado ao fim mais recente — usado
    para responder a uma chamada sem a resposta ficar cada vez mais lenta
    numa reunião longa."""
    return transcricao_ate_agora(sessao)[-_LIMITE_CONTEXTO_AO_VIVO:]

def terminar(sessao: str) -> str:
    """Termina a reunião e devolve a transcrição completa — a partir daqui a
    Alma já não tem acesso a este áudio/texto, só ao resumo que gerar dele."""
    texto = transcricao_ate_agora(sessao)
    _transcricoes.pop(sessao, None)
    _processados.pop(sessao, None)
    _a_responder.pop(sessao, None)
    return texto
