"""Configuración leída de variables de entorno (ver server/.env.example)."""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _lista(valor: str) -> list[str]:
    return [v.strip() for v in valor.split(",") if v.strip()]


@dataclass(frozen=True)
class Config:
    # Seguridad: el celular y el panel mandan este token en cada petición.
    token: str = field(default_factory=lambda: os.environ.get("APP_TOKEN", ""))

    # Datos de la persona que usa el asistente.
    nombre_usuario: str = field(default_factory=lambda: os.environ.get("NOMBRE_USUARIO", "Don Pepe"))
    nombre_asistente: str = field(default_factory=lambda: os.environ.get("NOMBRE_ASISTENTE", "Chema"))
    ciudad: str = field(default_factory=lambda: os.environ.get("CIUDAD", "Ciudad de México"))
    latitud: float = field(default_factory=lambda: float(os.environ.get("LATITUD", "19.4326")))
    longitud: float = field(default_factory=lambda: float(os.environ.get("LONGITUD", "-99.1332")))
    zona_horaria: ZoneInfo = field(
        default_factory=lambda: ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Mexico_City"))
    )

    # Modelo de Claude. effort "low" para que conteste rápido en una plática por voz.
    modelo: str = field(default_factory=lambda: os.environ.get("MODELO", "claude-opus-5-5"))
    esfuerzo: str = field(default_factory=lambda: os.environ.get("ESFUERZO", "low"))

    # Base de datos SQLite.
    ruta_db: str = field(default_factory=lambda: os.environ.get("RUTA_DB", "asistente.db"))

    # Avisos a la familia por Telegram (gratis): token del bot + chat ids separados por coma.
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chats: list[str] = field(
        default_factory=lambda: _lista(os.environ.get("TELEGRAM_CHAT_IDS", ""))
    )
    # Avisos por WhatsApp con CallMeBot (gratis): "5215511112222:apikey,5215533334444:apikey".
    callmebot: list[str] = field(default_factory=lambda: _lista(os.environ.get("CALLMEBOT_WHATSAPP", "")))

    # Minutos sin confirmar una medicina antes de avisar a la familia.
    minutos_escalar_medicina: int = field(
        default_factory=lambda: int(os.environ.get("MINUTOS_ESCALAR_MEDICINA", "30"))
    )
    # Minutos sin que el celular se reporte antes de avisar a la familia (0 = desactivado).
    minutos_celular_desconectado: int = field(
        default_factory=lambda: int(os.environ.get("MINUTOS_CELULAR_DESCONECTADO", "60"))
    )
    # Minutos de silencio tras los que se empieza una plática nueva.
    minutos_reiniciar_platica: int = field(
        default_factory=lambda: int(os.environ.get("MINUTOS_REINICIAR_PLATICA", "10"))
    )
