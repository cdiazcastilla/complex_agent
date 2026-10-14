"""Avisos a la familia por Telegram y/o WhatsApp (CallMeBot). Solo usa la librería estándar."""

import json
import logging
import urllib.parse
import urllib.request

from .config import Config

log = logging.getLogger(__name__)


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=15) as r:
        r.read()


def _post_json(url: str, datos: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(datos).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


class Avisador:
    def __init__(self, config: Config):
        self.config = config

    @property
    def configurado(self) -> bool:
        return bool((self.config.telegram_token and self.config.telegram_chats) or self.config.callmebot)

    def enviar(self, texto: str) -> int:
        """Manda el texto a todos los canales configurados. Regresa cuántos envíos salieron bien."""
        enviados = 0
        if self.config.telegram_token:
            url = f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage"
            for chat in self.config.telegram_chats:
                try:
                    _post_json(url, {"chat_id": chat, "text": texto})
                    enviados += 1
                except Exception:
                    log.exception("No se pudo mandar Telegram a %s", chat)
        for destino in self.config.callmebot:
            telefono, _, apikey = destino.partition(":")
            query = urllib.parse.urlencode({"phone": telefono, "text": texto, "apikey": apikey})
            try:
                _get(f"https://api.callmebot.com/whatsapp.php?{query}")
                enviados += 1
            except Exception:
                log.exception("No se pudo mandar WhatsApp a %s", telefono)
        if not enviados:
            log.warning("Aviso a la familia sin enviar (¿canales configurados?): %s", texto)
        return enviados
