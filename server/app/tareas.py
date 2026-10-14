"""Revisión periódica: horas de medicina, recordatorios, avisos a la familia si algo no cuadra."""

import logging
from datetime import datetime, timedelta

from .avisos import Avisador
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

# Cuánto tiempo después de la hora aún vale la pena dar un recordatorio (p. ej. si el servidor se reinició).
VENTANA = timedelta(minutes=20)


class Revisor:
    def __init__(self, db: DB, config: Config, avisador: Avisador):
        self.db = db
        self.config = config
        self.avisador = avisador

    def _mensaje(self, ahora: datetime, texto: str, toma_id: int | None = None) -> None:
        self.db.ejecutar(
            "INSERT INTO mensajes (texto, creado, toma_id) VALUES (?, ?, ?)",
            (texto, ahora.isoformat(timespec="seconds"), toma_id),
        )

    def revisar(self, ahora: datetime) -> None:
        self._medicinas(ahora)
        self._recordatorios(ahora)
        self._escalar_medicinas(ahora)
        self._celular_desconectado(ahora)

    def _medicinas(self, ahora: datetime) -> None:
        hoy = ahora.date().isoformat()
        for m in self.db.todos("SELECT * FROM medicinas WHERE activa = 1"):
            for hora in (h.strip() for h in m["horas"].split(",") if h.strip()):
                momento = datetime.combine(ahora.date(), datetime.strptime(hora, "%H:%M").time())
                if not (momento <= ahora < momento + VENTANA):
                    continue
                if self.db.uno(
                    "SELECT id FROM tomas WHERE medicina_id = ? AND fecha = ? AND hora = ?", (m["id"], hoy, hora)
                ):
                    continue
                toma_id = self.db.ejecutar(
                    "INSERT INTO tomas (medicina_id, fecha, hora, creada) VALUES (?, ?, ?, ?)",
                    (m["id"], hoy, hora, ahora.isoformat(timespec="seconds")),
                )
                texto = f"{self.config.nombre_usuario}, ya es hora de su {m['nombre']}"
                if m["dosis"]:
                    texto += f", {m['dosis']}"
                if m["indicaciones"]:
                    texto += f". {m['indicaciones']}"
                texto += ". ¿Ya se la tomó?"
                self._mensaje(ahora, texto, toma_id)
                self.db.registrar(ahora, "medicina", f"Recordatorio de {m['nombre']} ({hora})")

    def _recordatorios(self, ahora: datetime) -> None:
        for r in self.db.todos(
            "SELECT * FROM recordatorios WHERE disparado = 0 AND cuando <= ?", (ahora.isoformat(),)
        ):
            self.db.ejecutar("UPDATE recordatorios SET disparado = 1 WHERE id = ?", (r["id"],))
            if ahora - datetime.fromisoformat(r["cuando"]) <= VENTANA:
                self._mensaje(ahora, r["texto"])

    def _escalar_medicinas(self, ahora: datetime) -> None:
        limite = (ahora - timedelta(minutes=self.config.minutos_escalar_medicina)).isoformat(timespec="seconds")
        for t in self.db.todos(
            "SELECT t.id, t.hora, m.nombre FROM tomas t JOIN medicinas m ON m.id = t.medicina_id "
            "WHERE t.estado = 'pendiente' AND t.escalada = 0 AND t.creada <= ?",
            (limite,),
        ):
            self.db.ejecutar("UPDATE tomas SET escalada = 1 WHERE id = ?", (t["id"],))
            self.avisador.enviar(
                f"💊 {self.config.nombre_usuario} no ha confirmado su {t['nombre']} de las {t['hora']}. "
                "¿Le pueden llamar para recordarle?"
            )
            self.db.registrar(ahora, "aviso", f"Familia avisada: {t['nombre']} de las {t['hora']} sin confirmar")

    def _celular_desconectado(self, ahora: datetime) -> None:
        if self.config.minutos_celular_desconectado <= 0:
            return
        ultima = self.db.obtener_estado("celular_ultima_vez")
        if ultima is None or self.db.obtener_estado("celular_aviso_desconexion") == ultima:
            return
        if ahora - datetime.fromisoformat(ultima) > timedelta(minutes=self.config.minutos_celular_desconectado):
            self.db.guardar_estado("celular_aviso_desconexion", ultima)
            self.avisador.enviar(
                f"📵 El celular del asistente de {self.config.nombre_usuario} no se reporta desde las "
                f"{datetime.fromisoformat(ultima):%H:%M}. Puede estar apagado, sin batería o sin internet."
            )
            self.db.registrar(ahora, "sistema", "Celular desconectado; familia avisada")

    def latido(self, ahora: datetime) -> None:
        """El celular se reportó."""
        self.db.guardar_estado("celular_ultima_vez", ahora.isoformat(timespec="seconds"))
