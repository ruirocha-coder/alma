# tools/reuniao.py — modo reunião: a Alma "ouve" e conversa em contínuo por
# voz, através de uma sessão de conversação completa da Realtime API da
# OpenAI (ver tools/voz.py:emprestar_token_conversa) — é essa sessão que
# decide, pelas suas próprias instruções, quando responder (só quando
# chamada pelo nome) e quando chamar de volta ao Claude para assuntos da
# empresa (ver a função "perguntar_dados_empresa" e o endpoint
# /alma/reuniao/pergunta_empresa em main.py). Este módulo já não decide se
# a Alma deve responder — só acumula a transcrição de tudo o que foi dito,
# para servir de contexto a essas respostas e para o resumo/ata final.
#
# Histórico (2026-07-31): esta lógica de "chamada"/interrupção existiu aqui
# em código (regex a detetar "Alma" no texto, um contador de "geração" para
# saber se uma resposta anterior devia parar a meio) enquanto a sessão da
# OpenAI só transcrevia, sem falar nem decidir nada. Essa arquitetura
# revelou-se frágil: a voz da própria Alma, apanhada de volta pelo
# microfone (eco imperfeito, sem a mesma ligação WebRTC a servir de
# referência para o cancelamento), por vezes continha "Alma" e disparava
# uma resposta nova sobre a que estava em curso — um ciclo de arrancar/
# parar. Numa sessão de conversação a sério, o cancelamento de eco e a
# deteção de turno/interrupção são geridos nativamente pela própria OpenAI
# sobre a mesma ligação que reproduz a voz da Alma, em vez de recriados à
# mão a partir de texto transcrito.
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
import db

# quanto tempo o estado de uma reunião persistido na BD sobrevive antes de
# ser considerado obsoleto e apagado (ver db.limpar_reunioes_antigas) — isto
# não é um arquivo de reuniões passadas, é só uma rede de segurança contra
# um reinício do servidor a meio de uma reunião ainda em curso.
RETENCAO_DIAS = 3

# quando a Alma responde "ao vivo" a uma pergunta, usar a transcrição toda
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

def iniciar(sessao: str) -> None:
    """Começa (ou reinicia) a escuta de uma reunião para esta sessão — limpa
    qualquer estado persistido antigo com o mesmo nome de sessão, para não
    herdar a transcrição de uma reunião anterior."""
    _transcricoes[sessao] = {}
    _processados[sessao] = 0
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
    """Contagem de excertos já registados — serve de sinal de vida para a
    consola (para se perceber que a transcrição continua ativa)."""
    return _processados.get(sessao, 0)

def registar_resposta_alma(sessao: str, texto: str, apos_indice: float) -> None:
    """Regista o que a própria Alma respondeu (ex: a uma pergunta sobre a
    empresa, via perguntar_dados_empresa) na transcrição acumulada — pedido
    do Rui (2026-07-31): a ata final só refletia o que as pessoas diziam,
    nunca o que ela respondia.

    apos_indice é o índice mais recente conhecido do lado do cliente nesse
    momento (o próximo índice que ele vai atribuir a um turno humano) —
    regista-se em apos_indice - 0.5, um índice fracionário que fica sempre
    entre o turno mais recente e o seguinte. Bug evitado: usar só
    "o índice mais alto + 1" colide mais cedo ou mais tarde com um índice
    inteiro que o cliente ainda vai atribuir (ele não sabe desta inserção
    do lado do servidor), sobrescrevendo essa resposta silenciosamente."""
    mapa = _transcricoes.setdefault(sessao, {})
    mapa[apos_indice - 0.5] = f"[Alma respondeu] {texto.strip()}"
    _processados[sessao] = _processados.get(sessao, 0) + 1
    db.guardar_estado_reuniao(sessao, mapa, _processados[sessao])

def _ordenada(sessao: str) -> list[str]:
    mapa = _transcricoes.get(sessao, {})
    return [mapa[i] for i in sorted(mapa)]

def transcricao_ate_agora(sessao: str) -> str:
    return " ".join(_ordenada(sessao))

def contexto_ao_vivo(sessao: str) -> str:
    """Como transcricao_ate_agora, mas limitado ao fim mais recente — usado
    para responder a uma pergunta sem a resposta ficar cada vez mais lenta
    numa reunião longa."""
    return transcricao_ate_agora(sessao)[-_LIMITE_CONTEXTO_AO_VIVO:]

def terminar(sessao: str) -> str:
    """Termina a reunião e devolve a transcrição completa — a partir daqui a
    Alma já não tem acesso a este áudio/texto, só ao resumo que gerar dele."""
    texto = transcricao_ate_agora(sessao)
    _transcricoes.pop(sessao, None)
    _processados.pop(sessao, None)
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
