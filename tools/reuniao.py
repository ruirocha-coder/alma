# tools/reuniao.py — modo reunião: a Alma "ouve" continuamente (em vez de
# push-to-talk) e só entra na conversa quando é chamada pelo nome.
#
# O áudio nunca chega a este servidor — o browser liga-se diretamente à
# Realtime API da OpenAI (por WebRTC, ver /alma/reuniao/iniciar em main.py) e
# só nos envia o texto já transcrito de cada turno. Esse texto fica em
# memória; no fim, o resumo/ata gerado a partir dele é o registo que persiste
# de facto (guardado no histórico da conversa, como qualquer outra resposta
# da Alma).
#
# A transcrição em si vive principalmente em memória de processo (rápido,
# sem ida à BD a cada excerto de poucos segundos), mas é também persistida na
# BD a cada excerto — só para sobreviver a um reinício do servidor a meio de
# uma reunião longa (ex: um deploy novo), não como registo permanente: ver
# RETENCAO_DIAS. Se o estado em memória desaparecer (reinício), é recuperado
# da BD de forma transparente da próxima vez que a sessão for usada.
import re
import db

# quanto tempo o estado de uma reunião persistido na BD sobrevive antes de
# ser considerado obsoleto e apagado (ver db.limpar_reunioes_antigas) — isto
# não é um arquivo de reuniões passadas, é só uma rede de segurança contra
# um reinício do servidor a meio de uma reunião ainda em curso.
RETENCAO_DIAS = 3

_MENCAO_ALMA = re.compile(r"\balma\b", re.IGNORECASE)

# pedido para a Alma só se calar, sem gerar resposta nova — distinto de a
# chamarem outra vez pelo nome (isso já interrompe e responde de novo). Sem
# isto, a única forma de a interromper era chamá-la de novo, o que também
# disparava sempre uma resposta nova, mesmo quando só se queria que ela
# parasse de falar. Exige o nome dela por perto (não só "para" sozinho, que
# é uma palavra comum de mais em português para servir de gatilho sozinha).
_PEDIDO_PARAR = re.compile(
    r"\balma\b.{0,20}\b(para|pára|chega|cala[- ]?te|obrigad[ao])\b"
    r"|\b(para|pára|chega|cala[- ]?te|obrigad[ao])\b.{0,20}\balma\b",
    re.IGNORECASE,
)

# palavras de preenchimento comuns quando se chama a Alma só para lhe pedir
# atenção/pausa, sem uma pergunta a seguir (ex: "Alma, espera", "Alma, calma")
_PREENCHIMENTO = re.compile(
    r"\b(espera|esperas|calma|ok|então|entao|olha|pera[íi]?|desculpa)\b",
    re.IGNORECASE,
)
_PONTUACAO_E_ESPACO = re.compile(r"[\s.,!?…\-–—]+")

def foi_chamada_sem_conteudo(texto: str) -> bool:
    """Distingue chamar a Alma com uma pergunta nova de só lhe chamar a
    atenção/pedir uma pausa (ex: só "Alma", "Alma!", "Alma, espera") — sem
    isto, a mesma palavra "Alma" interrompe sempre E tenta gerar uma
    resposta nova, mesmo quando não há pergunta nenhuma a seguir (pedido do
    Rui, 2026-07-31: "a mesma palavra 'alma' ativa e desativa", queria uma
    forma mais natural de distinguir). Remove o nome e preenchimento comum;
    se não sobrar nada com conteúdo, é só um pedido de atenção/pausa."""
    resto = _MENCAO_ALMA.sub("", texto)
    resto = _PREENCHIMENTO.sub("", resto)
    resto = _PONTUACAO_E_ESPACO.sub("", resto)
    return resto == ""

# quando a Alma responde "ao vivo" a uma chamada, usar a transcrição toda
# desde o início da reunião tornaria cada resposta mais lenta à medida que a
# reunião cresce (mais texto a enviar ao modelo) — o que se sente como
# "bloquear" numa reunião longa ou muito faladora. Só o fim recente da
# transcrição chega para responder com contexto; a transcrição completa
# continua a ser usada no resumo final.
#
# 32000 carateres cobre, na prática, uma reunião de 30 minutos com conversa
# intensa (a ~150 palavras/minuto, com várias pessoas a falar, isso são umas
# dezenas de milhares de carateres) — o suficiente para a Alma responder
# tendo em conta praticamente tudo o que já se disse, não só os últimos
# minutos, sem deixar de ter um limite para reuniões muito mais longas.
_LIMITE_CONTEXTO_AO_VIVO = 32000

_transcricoes: dict[str, dict[int, str]] = {}
_processados: dict[str, int] = {}
# número da "geração" de resposta em curso nesta sessão — sempre que uma
# nova chamada chega, a geração avança, e qualquer resposta ainda a decorrer
# de uma geração anterior sabe que deve parar (interrompida): é assim que a
# Alma para de falar assim que é chamada de novo, em vez de acabar a frase
# toda primeiro.
_geracao: dict[str, int] = {}

def iniciar(sessao: str) -> None:
    """Começa (ou reinicia) a escuta de uma reunião para esta sessão — limpa
    qualquer estado persistido antigo com o mesmo nome de sessão, para não
    herdar a transcrição de uma reunião anterior."""
    _transcricoes[sessao] = {}
    _processados[sessao] = 0
    _geracao[sessao] = 0
    db.eliminar_estado_reuniao(sessao)

def em_curso(sessao: str) -> bool:
    if sessao in _transcricoes:
        return True
    # o estado em memória pode ter desaparecido (ex: reinício do servidor a
    # meio da reunião) — recupera-o da BD antes de assumir que a reunião
    # acabou, para a pessoa nem chegar a notar que o servidor reiniciou
    estado = db.carregar_estado_reuniao(sessao)
    if estado is None:
        return False
    _transcricoes[sessao] = estado["excertos"]
    _processados[sessao] = estado["processados"]
    _geracao.setdefault(sessao, 0)
    return True

def registar(sessao: str, indice: int, texto: str) -> None:
    """Acrescenta mais um excerto transcrito, na posição indicada pelo
    índice (atribuído no cliente pela ordem de gravação) — assim a
    transcrição fica na ordem certa mesmo que os pedidos de rede cheguem
    trocados (ex: um excerto demorou mais a transcrever que o seguinte).
    Persiste o novo estado na BD, para sobreviver a um reinício do
    servidor a meio da reunião."""
    if texto.strip():
        _transcricoes.setdefault(sessao, {})[indice] = texto.strip()
    _processados[sessao] = _processados.get(sessao, 0) + 1
    db.guardar_estado_reuniao(sessao, _transcricoes.get(sessao, {}), _processados[sessao])

def excertos_processados(sessao: str) -> int:
    """Contagem de excertos já respondidos pelo servidor — serve de sinal de
    vida para a consola (para se perceber que a Alma continua ativa, mesmo
    quando não há nada para responder)."""
    return _processados.get(sessao, 0)

def nova_geracao(sessao: str) -> int:
    """Chamar sempre que se vai começar a responder a uma nova chamada —
    invalida (interrompe) qualquer resposta anterior ainda em curso nesta
    sessão. Devolve o número que a nova resposta deve usar para, por sua
    vez, saber se foi interrompida por uma chamada ainda mais recente."""
    nova = _geracao.get(sessao, 0) + 1
    _geracao[sessao] = nova
    return nova

def foi_interrompida(sessao: str, minha_geracao: int) -> bool:
    return _geracao.get(sessao, 0) != minha_geracao

def foi_chamada(texto: str) -> bool:
    """Verifica se este excerto menciona a Alma diretamente (ex: "Alma, o que achas...")."""
    return bool(_MENCAO_ALMA.search(texto))

def foi_pedido_para_parar(texto: str) -> bool:
    """Verifica se este excerto pede à Alma para se calar (ex: "Alma, para",
    "chega, Alma") — só deve interromper, nunca gerar uma resposta nova."""
    return bool(_PEDIDO_PARAR.search(texto))

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
    _geracao.pop(sessao, None)
    db.eliminar_estado_reuniao(sessao)
    return texto

def limpar_reunioes_antigas() -> None:
    """Apaga estado de reuniões persistido há mais de RETENCAO_DIAS dias —
    pensado para correr periodicamente (agendado), não para arquivo; uma
    reunião com este estado tão antigo já terminou há muito ou nunca foi
    encerrada corretamente."""
    try:
        apagadas = db.limpar_reunioes_antigas(RETENCAO_DIAS)
        if apagadas:
            print(f"[reuniao] limpeza: {apagadas} reunião(ões) com mais de {RETENCAO_DIAS} dias apagada(s)")
    except Exception as e:
        print(f"[reuniao] falha na limpeza de reuniões antigas: {e!r}")
