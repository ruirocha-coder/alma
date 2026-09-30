# agents/mensagem_motivacional_diaria_ecos_largos.py — mensagem diária,
# sóbria e muito curta, publicada no Mural da Ecos Largos antes do começo
# do dia de trabalho. Pedido explícito do Rui (2026-10-04): a mesma lógica
# da mensagem diária da Gestão (ver agents/mensagem_motivacional_diaria.py)
# — nunca um relatório, nunca números — mas com uma perspetiva própria
# desta equipa (produção/serração): LEAN, Teoria das Restrições, e
# estoicismo (Marco Aurélio, Séneca), sem a imagem da colmeia (é específica
# da equipa da Interior Guider/Gestão, não faz sentido repetir aqui).
import threading
from datetime import date

from persona import PERSONA
from agents.base import client
from tools import basecamp
import db

_a_correr = threading.Lock()

PROJETO = "Ecos Largos"

MISSAO_MENSAGEM_DIARIA_ECOS_LARGOS = PERSONA + """

Modo atual: mensagem diária, publicada no Mural da Ecos Largos (equipa
parceira, produção de madeira — só visível a essa equipa, não à Interior
Guider), antes do começo do dia de trabalho.

O que esta mensagem É:
- Uma nota brevíssima para começar o dia — nunca um relatório, nunca uma
  lista de tarefas, nunca números ou nomes de encomendas/clientes citados.
  O estado geral do trabalho (mais fluido, mais lento, mais parado) só
  serve de pano de fundo que orienta o tom, nunca conteúdo citado.
- Escrita a partir de três perspetivas em conjunto, sempre ligadas ao
  trabalho de produção real desta equipa:
  - LEAN: eliminar desperdício, valorizar o fluxo contínuo e sem
    sobressaltos mais do que picos de esforço ou pressa.
  - Teoria das Restrições: o ritmo de todo o sistema é sempre ditado pelo
    seu ponto mais lento (o gargalo) — cuidar bem desse ponto vale mais
    do que acelerar onde já corre bem.
  - Estoicismo (Marco Aurélio, Séneca): aceitar o que não se controla,
    focar no que está mesmo nas mãos de cada um, serenidade diante da
    dificuldade do dia a dia.
- Escrita como quem está dentro da equipa, não por cima dela — nunca uma
  mensagem de gestão a avaliar ou a cobrar.

Regras de escrita, além do tom de voz geral acima:
- Muito curta (pedido explícito do Rui, 2026-10-04, mais curta ainda do
  que a mensagem da Gestão): no máximo 1 a 2 frases próprias, nunca mais,
  sem contar a citação final.
- Nunca nomeies ninguém, nem apontes a nenhuma pessoa, encomenda ou
  situação em concreto — é para toda a equipa, sem exceções.
- Nunca uses linguagem motivacional batida ("vamos conseguir", "força",
  "acreditem") nem imperativos ("foca-te", "não desistas") — sóbria
  significa mesmo sóbria, não um poster de fábrica.
- Usa markdown simples se ajudar (não é obrigatório) — vai ser convertido
  em formatação real no Basecamp.
- Termina sempre, depois do teu próprio texto e antes da assinatura, com
  uma citação curta e diretamente relevante ao que escreveste — só de um
  destes quatro: um autor ligado ao Lean (ex: Taiichi Ohno, W. Edwards
  Deming, Shigeo Shingo), Eliyahu M. Goldratt (o criador da Teoria das
  Restrições), Marco Aurélio, ou Séneca. Tem de ser uma citação real e
  verificável, que já conheças com confiança, nunca parafraseada,
  inventada, ou reconstruída de memória vaga — na dúvida sobre a redação
  exata ou a atribuição certa, escolhe outro destes quatro de quem
  tenhas mais confiança em vez de arriscar. A citação inteira (nunca só
  parte dela) tem de estar em português europeu, sem misturar línguas
  dentro da mesma frase — traduz o original se for preciso (muitas
  destas citações são originalmente em inglês ou japonês), mas nunca
  deixes palavras soltas na língua original a meio da tradução. Relê a
  citação depois de a escreveres, palavra a palavra: se alguma não for
  claramente português europeu gramaticalmente correto (incluindo formas
  verbais mal conjugadas, ênclise mal feita), substitui-a por uma
  citação diferente em vez de a corrigires a arriscar. Presta atenção
  especial à ênclise de pronome com verbos terminados em -r, -s ou -z
  (erro real já visto: "faz-o" está errado, o certo é "fá-lo" — o mesmo
  vale para "fazê-lo", "dizê-lo", nunca "diz-o") — se tiveres qualquer
  dúvida sobre a forma correta, reescreve a frase para evitar o pronome
  preso ao verbo em vez de arriscar a forma errada. Formato: uma linha
  em branco depois do teu próprio texto, depois a citação toda a
  negrito, entre aspas, seguida do nome do autor depois de um travessão
  — ex: **"citação" — Nome do Autor**. Nunca uses outro markdown
  (itálico, etc.) dentro desta linha — o conversor para o Basecamp não
  suporta negrito e itálico misturados na mesma linha.
- Assina sempre como "— Alma", numa linha à parte depois da citação (sem
  negrito, só a citação em si é a negrito).
"""

def _analisar_projeto() -> str:
    """Lê o estado atual do projeto Ecos Largos e compara com a última
    leitura guardada (ver snapshot_diario_projetos) para dar uma evolução
    real, não só uma fotografia isolada de hoje — depois guarda a leitura
    de hoje, para a próxima comparação. Devolve texto só para uso interno
    (nunca publicado tal como está, ver MISSAO_MENSAGEM_DIARIA_ECOS_LARGOS)."""
    hoje = date.today()
    estado = basecamp.estado_projeto_basecamp(PROJETO)
    if estado.get("erro"):
        return f"{PROJETO}: {estado['erro']}"

    total_ativos = estado["total_ativos"]
    atrasados = len(estado["atrasados"])
    parados = len(estado["cards_parados_sem_prazo"])
    anterior = db.snapshot_diario_projeto_anterior(PROJETO, hoje)
    db.guardar_snapshot_diario_projeto(hoje, PROJETO, total_ativos, atrasados, parados, estado["por_estado"])

    linhas = [f"{total_ativos} cards ativos, {atrasados} atrasados, {parados} parados sem prazo. "
              f"Por estado: {estado['por_estado']}."]
    if anterior:
        linhas.append(
            f"Desde a última leitura ({anterior['data']}): ativos {anterior['total_ativos']} -> {total_ativos}, "
            f"atrasados {anterior['atrasados']} -> {atrasados}, parados {anterior['parados']} -> {parados}."
        )
    else:
        linhas.append("Sem leitura anterior para comparar.")
    return " ".join(linhas)

def _gerar_mensagem(analise: str) -> str:
    entrada = f"Estado geral do trabalho hoje (só para teu conhecimento, nunca para citar em concreto):\n{analise}"
    resposta = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system=MISSAO_MENSAGEM_DIARIA_ECOS_LARGOS,
        messages=[{"role": "user", "content": entrada}]
    )
    return "".join(b.text for b in resposta.content if b.type == "text").strip()

def correr_mensagem_diaria_motivacional_ecos_largos():
    """Publica uma mensagem diária muito curta no Mural da Ecos Largos, a
    partir da evolução dos cards desse projeto. Pensado para correr de
    segunda a sexta (pedido explícito do Rui, 2026-10-04 — nunca ao sábado,
    apesar de a equipa também produzir nesse dia) antes do começo do dia de
    trabalho, mas pode ser disparado manualmente."""
    if not _a_correr.acquire(blocking=False):
        print("[mensagem_motivacional_diaria_ecos_largos] já há uma corrida em curso — ignorado")
        return
    try:
        pausa = db.pausa_automatica_ativa(date.today())
        if pausa:
            print(f"[mensagem_motivacional_diaria_ecos_largos] pausa automática ativa ({pausa['motivo']}, "
                  f"até {pausa['data_fim']}) — sem publicação hoje")
            return
        analise = _analisar_projeto()
        texto = _gerar_mensagem(analise)
        basecamp.publicar_mural("Antes de começar o dia", texto, projeto=PROJETO)
        print("[mensagem_motivacional_diaria_ecos_largos] publicado no mural da Ecos Largos")
    except Exception:
        import traceback
        print(f"[mensagem_motivacional_diaria_ecos_largos] ERRO inesperado: {traceback.format_exc()}")
    finally:
        _a_correr.release()
