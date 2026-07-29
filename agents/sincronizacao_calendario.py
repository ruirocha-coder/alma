# agents/sincronizacao_calendario.py — sincronização unidirecional e quase
# em tempo real da Agenda (Schedule) do projeto "Entregas" do Basecamp para
# um calendário específico do Google Calendar, pedido do Rui (2026-07-29):
# "criar uma sincronização unidireccional e em tempo quase real da Agenda
# do projeto 'Entregas' do Basecamp para um calendário específico do
# Google, com criação, actualização e eliminação, guardando os IDs
# correspondentes numa base de dados".
#
# Unidirecional só: Basecamp -> Google, NUNCA ao contrário — nunca lê nem
# escreve no Google Calendar para decidir nada sobre o Basecamp.
#
# "Quase em tempo real" aqui é feito por polling (ver main.py, job de 2 em
# 2 minutos), não por webhook: confirmado (2026-07-29) que o Basecamp não
# tem hoje nenhum webhook do tipo Schedule::Entry registado (só Comment,
# Todo e Kanban::Card, ver main.py::/basecamp/webhooks/registar) — mesmo
# que passasse a estar, a entrada não tem informação de "isto foi
# alterado", por isso a lista completa de entradas ativas a cada ciclo
# (ver tools.basecamp.entradas_agenda) é a única forma fiável de detetar
# criação, alteração E eliminação sem depender de eventos que o Basecamp
# não garante.
#
# Deteção de cada tipo de mudança, por comparação entre o que está agora
# no Basecamp e o último estado guardado em basecamp_google_calendar_sync
# (ver db.mapeamentos_calendario_google):
# - entrada no Basecamp sem mapeamento -> nova -> cria no Google Calendar.
# - entrada com mapeamento mas título/início/fim diferentes do guardado ->
#   alterada -> atualiza o evento no Google Calendar.
# - mapeamento sem entrada correspondente no Basecamp -> a entrada foi
#   apagada (Basecamp deixa simplesmente de a listar como ativa) -> apaga
#   o evento no Google Calendar e remove o mapeamento.
#
# Bug real, encontrado ao testar ao vivo (2026-07-29) contra a conta real:
# a Agenda do projeto Entregas tem 1180 entradas ativas, algumas desde
# 2016 — sem filtro, o primeiro ciclo de sincronização criaria 1180
# eventos históricos de uma só vez no Google Calendar. JANELA_DIAS_PASSADO
# faz com que só entradas recentes/futuras sejam candidatas a CRIAR pela
# primeira vez; história antiga, nunca antes sincronizada, fica de fora
# para sempre (nunca ganha mapeamento, por isso nunca é reavaliada). Uma
# entrada já mapeada continua a ser verificada para atualização/eliminação
# independentemente da idade — só a criação inicial é filtrada por data.
import threading
from datetime import date, timedelta
from tools import basecamp, google_calendar
import db

_a_correr = threading.Lock()

PROJETO_ENTREGAS = "Entregas"
JANELA_DIAS_PASSADO = 7


def _estado_atual(entrada: dict) -> tuple:
    return (entrada.get("summary") or "", entrada.get("starts_at") or "", entrada.get("ends_at") or "")


def _cor_do_evento(titulo: str) -> str:
    """Os eventos de deslocação (título "Viagem: X -> Y", ver
    agents/ceo.py e a tool criar_eventos_calendario_entregas) ficam a azul
    no Google Calendar, para se distinguirem visualmente das entregas em
    si — pedido do Rui (2026-07-29)."""
    if titulo.startswith("Viagem:"):
        return google_calendar.COR_AZUL_VIAGEM
    return None


def _dentro_da_janela_de_sincronizacao(entrada: dict, hoje: date) -> bool:
    inicio = entrada.get("starts_at")
    if not inicio:
        return True  # sem data — não há forma de filtrar, mais vale sincronizar do que perder a entrada
    try:
        data_inicio = date.fromisoformat(inicio[:10])
    except ValueError:
        return True
    return data_inicio >= hoje - timedelta(days=JANELA_DIAS_PASSADO)


def correr_sincronizacao_calendario() -> dict:
    """Um ciclo de sincronização: lê as entradas ativas da Agenda do
    Basecamp (projeto Entregas), compara contra o mapeamento guardado, e
    espelha no Google Calendar tudo o que for novo, alterado ou removido.
    Sem custo evitável: se nada mudou desde o ciclo anterior, não chama o
    Google Calendar API nenhuma vez. Nunca cria no Google Calendar entradas
    históricas nunca antes sincronizadas — só as dos últimos
    JANELA_DIAS_PASSADO dias (ou futuras) — ver
    _dentro_da_janela_de_sincronizacao."""
    if not _a_correr.acquire(blocking=False):
        print("[sincronizacao_calendario] já há um ciclo em curso — ignorado")
        return {"erro": "já está a correr um ciclo de sincronização"}
    try:
        try:
            entradas = basecamp.entradas_agenda(PROJETO_ENTREGAS)
        except Exception as e:
            print(f"[sincronizacao_calendario] não foi possível obter a Agenda do Basecamp: {e!r}")
            return {"erro": str(e)}

        mapeamentos = db.mapeamentos_calendario_google()
        entradas_por_id = {e["id"]: e for e in entradas}
        hoje = date.today()

        criados = atualizados = eliminados = 0
        ignorados_historico = 0

        for entry_id, entrada in entradas_por_id.items():
            titulo, inicio, fim = _estado_atual(entrada)
            descricao = entrada.get("description") or ""
            mapeamento = mapeamentos.get(entry_id)
            try:
                if mapeamento is None:
                    if not _dentro_da_janela_de_sincronizacao(entrada, hoje):
                        ignorados_historico += 1
                        continue
                    evento = google_calendar.criar_evento(titulo, inicio, fim, descricao, _cor_do_evento(titulo))
                    db.registar_mapeamento_calendario_google(entry_id, evento["id"], titulo, inicio, fim)
                    criados += 1
                elif (mapeamento["titulo"], mapeamento["inicio"], mapeamento["fim"]) != (titulo, inicio, fim):
                    google_calendar.atualizar_evento(mapeamento["google_event_id"], titulo, inicio, fim, descricao,
                                                     _cor_do_evento(titulo))
                    db.atualizar_mapeamento_calendario_google(entry_id, titulo, inicio, fim)
                    atualizados += 1
            except Exception as e:
                print(f"[sincronizacao_calendario] falhou a sincronizar a entrada {entry_id}: {e!r}")

        for entry_id, mapeamento in mapeamentos.items():
            if entry_id in entradas_por_id:
                continue
            try:
                google_calendar.eliminar_evento(mapeamento["google_event_id"])
            except Exception as e:
                print(f"[sincronizacao_calendario] falhou a eliminar o evento da entrada {entry_id}: {e!r}")
                continue
            db.remover_mapeamento_calendario_google(entry_id)
            eliminados += 1

        if criados or atualizados or eliminados:
            print(f"[sincronizacao_calendario] {criados} criado(s), {atualizados} atualizado(s), "
                  f"{eliminados} eliminado(s), {ignorados_historico} histórico(s) ignorado(s)")
        return {"criados": criados, "atualizados": atualizados, "eliminados": eliminados,
                "ignorados_historico": ignorados_historico}
    finally:
        _a_correr.release()
