# agents/verificar_portais_projeto.py — reforço diário (pedido explícito do
# Rui, 2026-10-02) ao webhook que já tenta abrir sozinho uma fase do portal
# de acompanhamento sempre que o documento certo aparece num comentário novo
# (ver agents/responder_basecamp._tentar_atualizar_portal). Os webhooks às
# vezes perdem-se (já aconteceu nesta conta, durante um deploy instável) —
# sem esta corrida, uma fase podia ficar presa à espera de alguém reparar à
# mão que o documento já estava no card, exatamente o problema que isto
# existe para resolver.
import re
import threading
import traceback

import db
from agents.responder_basecamp import _tentar_atualizar_portal
from tools import basecamp

_a_correr = threading.Lock()

_PROJETO_INTERIOR_GUIDER = "@ Interior Guider"


def _comment_id_de_url(url: str):
    """Extrai o id numérico de um "comentario_url" (ex:
    ".../comments/10361299298.json" -> 10361299298) — para usar a mesma
    chave de dedup (db.portal_documento_ja_processado) que o webhook já
    usa para o mesmo comentário, e nunca tentar duas vezes à toa."""
    m = re.search(r"/comments/(\d+)\.json", url or "")
    return int(m.group(1)) if m else None


def correr_verificacao_diaria_portais():
    """Para cada portal já gerado, olha para os PDFs mais recentes
    anexados ao respetivo card e tenta reabrir fases "prevista" se
    algum corresponder — mesma lógica partilhada do webhook, só que
    corre sozinha uma vez por dia, como rede de segurança."""
    if not _a_correr.acquire(blocking=False):
        print("[verificar_portais_projeto] já há uma corrida em curso — ignorado")
        return
    try:
        portais = db.listar_portais_projeto()
        print(f"[verificar_portais_projeto] a verificar {len(portais)} portal(is)")
        for p in portais:
            card_id = p.get("card_id")
            if not card_id:
                continue
            try:
                comments_url = f"{basecamp._base_url()}/recordings/{card_id}/comments.json"
                pdfs = basecamp.listar_pdfs_anexados_por_data(comments_url)
                # só os primeiros (mais recentes) — o dedup por comentário já
                # evita reprocessar os antigos, isto é só para não varrer uma
                # centena de comentários por card, todos os dias, à toa.
                for pdf in pdfs[:5]:
                    comment_id = _comment_id_de_url(pdf.get("comentario_url"))
                    if comment_id is None:
                        continue
                    _tentar_atualizar_portal(card_id, p.get("titulo"), _PROJETO_INTERIOR_GUIDER,
                                             [pdf], comment_id)
            except Exception:
                print(f"[verificar_portais_projeto] falhou a verificar o card {card_id}: "
                     f"{traceback.format_exc()}")
    finally:
        _a_correr.release()
