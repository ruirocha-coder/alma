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

_transcricoes: dict[str, list[str]] = {}

def iniciar(sessao: str) -> None:
    """Começa (ou reinicia) a escuta de uma reunião para esta sessão."""
    _transcricoes[sessao] = []

def em_curso(sessao: str) -> bool:
    return sessao in _transcricoes

def registar(sessao: str, texto: str) -> None:
    """Acrescenta mais um excerto transcrito ao que já se ouviu nesta reunião."""
    if texto.strip():
        _transcricoes.setdefault(sessao, []).append(texto.strip())

def foi_chamada(texto: str) -> bool:
    """Verifica se este excerto menciona a Alma diretamente (ex: "Alma, o que achas...")."""
    return bool(_MENCAO_ALMA.search(texto))

def transcricao_ate_agora(sessao: str) -> str:
    return " ".join(_transcricoes.get(sessao, []))

def terminar(sessao: str) -> str:
    """Termina a reunião e devolve a transcrição completa — a partir daqui a
    Alma já não tem acesso a este áudio/texto, só ao resumo que gerar dele."""
    return " ".join(_transcricoes.pop(sessao, []))
