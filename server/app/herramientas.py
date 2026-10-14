"""Herramientas que Claude puede usar, y su implementación."""

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

from .avisos import Avisador
from .config import Config
from .db import DB

log = logging.getLogger(__name__)


def _herramienta(nombre: str, descripcion: str, propiedades: dict[str, dict]) -> dict:
    return {
        "name": nombre,
        "description": descripcion,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": propiedades,
            "required": list(propiedades),
            "additionalProperties": False,
        },
    }


_TEXTO = {"type": "string"}

DEFINICIONES: list[dict] = [
    _herramienta(
        "pedir_ayuda",
        "Avisa DE INMEDIATO a toda la familia que hay una emergencia. Úsala sin dudar si dice que se "
        "cayó, que le duele el pecho, que no puede respirar, que se siente muy mal, que pide ayuda, "
        "o si algo de lo que dice suena a peligro.",
        {"motivo": {**_TEXTO, "description": "Qué pasó, en pocas palabras, tal como lo dijo."}},
    ),
    _herramienta(
        "avisar_familia",
        "Manda un mensaje NO urgente a la familia (por ejemplo: 'dile a Lupita que venga el domingo', "
        "'avísale a mi sobrino que ya llegué').",
        {"mensaje": {**_TEXTO, "description": "El mensaje a mandar, redactado en tercera persona."}},
    ),
    _herramienta(
        "llamar",
        "Hace una llamada telefónica a uno de sus contactos guardados. Usa ver_contactos si no estás "
        "seguro del nombre.",
        {"nombre": {**_TEXTO, "description": "Nombre o parentesco del contacto (ej. 'Lupita', 'mi sobrino')."}},
    ),
    _herramienta("ver_contactos", "Lista los contactos guardados con nombre y parentesco.", {}),
    _herramienta(
        "ver_medicinas",
        "Muestra sus medicinas, a qué horas le tocan y cuáles ya se tomó hoy.",
        {},
    ),
    _herramienta(
        "confirmar_medicina",
        "Registra que ya se tomó una medicina. Úsala cuando diga que ya se la tomó. Si no dice cuál, "
        "usa 'todas' para confirmar todas las pendientes de hoy.",
        {"nombre": {**_TEXTO, "description": "Nombre de la medicina, o 'todas'."}},
    ),
    _herramienta(
        "crear_recordatorio",
        "Programa un recordatorio que se le dirá en voz alta a la hora indicada.",
        {
            "texto": {**_TEXTO, "description": "Qué hay que recordarle, dicho de forma amable."},
            "cuando": {**_TEXTO, "description": "Fecha y hora local en formato AAAA-MM-DDTHH:MM."},
        },
    ),
    _herramienta(
        "agregar_cita",
        "Guarda una cita (doctor, banco, visita...). Se le recordará un día antes y dos horas antes.",
        {
            "descripcion": _TEXTO,
            "cuando": {**_TEXTO, "description": "Fecha y hora local en formato AAAA-MM-DDTHH:MM."},
        },
    ),
    _herramienta("ver_citas", "Lista las próximas citas.", {}),
    _herramienta(
        "guardar_nota",
        "Guarda algo para que no se le olvide (dónde dejó algo, un dato, un teléfono, un pendiente).",
        {"texto": _TEXTO},
    ),
    _herramienta("ver_notas", "Lista todas las notas guardadas, de la más reciente a la más vieja.", {}),
    _herramienta(
        "borrar_nota",
        "Borra una nota que ya no sirve. Obtén el id con ver_notas.",
        {"id": {"type": "integer"}},
    ),
    _herramienta("ver_clima", "Da el clima de hoy y mañana en su ciudad.", {}),
]


def _parsear_fecha(valor: str) -> datetime:
    return datetime.fromisoformat(valor.strip()).replace(second=0, microsecond=0, tzinfo=None)


class Herramientas:
    """Ejecuta herramientas. `acciones` junta lo que el celular debe hacer (p. ej. llamar)."""

    def __init__(self, db: DB, config: Config, avisador: Avisador, ahora: Callable[[], datetime]):
        self.db = db
        self.config = config
        self.avisador = avisador
        self.ahora = ahora  # hora local sin tzinfo

    def ejecutar(self, nombre: str, entrada: dict[str, Any], acciones: list[dict]) -> str:
        metodo = getattr(self, f"_h_{nombre}", None)
        if metodo is None:
            raise ValueError(f"Herramienta desconocida: {nombre}")
        return metodo(acciones=acciones, **entrada)

    # --- emergencias y familia ---

    def pedir_ayuda(self, motivo: str) -> int:
        """También la usa el endpoint de emergencia del celular, sin pasar por Claude."""
        ahora = self.ahora()
        self.db.registrar(ahora, "emergencia", motivo)
        # El celular avisa directo y luego Claude puede volver a pedir ayuda por lo mismo:
        # si ya salió un aviso hace menos de 2 minutos, no se repite.
        anterior = self.db.obtener_estado("ultima_emergencia")
        if anterior:
            momento, _, enviados = anterior.partition("|")
            if int(enviados) > 0 and ahora - datetime.fromisoformat(momento) < timedelta(minutes=2):
                return int(enviados)
        texto = (
            f"🚨 EMERGENCIA – {self.config.nombre_usuario} pidió ayuda "
            f"({ahora:%H:%M}): {motivo}. Llámenle o vayan a verlo ya."
        )
        enviados = self.avisador.enviar(texto)
        self.db.guardar_estado("ultima_emergencia", f"{ahora.isoformat(timespec='seconds')}|{enviados}")
        return enviados

    def _h_pedir_ayuda(self, acciones: list[dict], motivo: str) -> str:
        enviados = self.pedir_ayuda(motivo)
        acciones.append({"tipo": "emergencia", "motivo": motivo, "enviados": enviados})
        if enviados:
            return "Aviso de emergencia enviado a la familia."
        return (
            "ERROR: no se pudo avisar por internet. El celular intentará mandar un SMS de emergencia. "
            "Dile que marque al 911 si es grave."
        )

    def _h_avisar_familia(self, acciones: list[dict], mensaje: str) -> str:
        self.db.registrar(self.ahora(), "aviso", mensaje)
        enviados = self.avisador.enviar(f"💬 Mensaje de {self.config.nombre_usuario}: {mensaje}")
        return "Mensaje enviado." if enviados else "ERROR: no se pudo mandar el mensaje."

    def _buscar_contacto(self, nombre: str) -> dict | None:
        nombre = nombre.lower().strip()
        for c in self.db.todos("SELECT * FROM contactos"):
            if nombre in c["nombre"].lower() or (c["parentesco"] and nombre in c["parentesco"].lower()):
                return c
            if c["nombre"].lower() in nombre or (c["parentesco"] and c["parentesco"].lower() in nombre):
                return c
        return None

    def _h_llamar(self, acciones: list[dict], nombre: str) -> str:
        contacto = self._buscar_contacto(nombre)
        if contacto is None:
            return f"No hay ningún contacto que se llame '{nombre}'. Contactos: {self._h_ver_contactos(acciones)}"
        acciones.append({"tipo": "llamar", "nombre": contacto["nombre"], "telefono": contacto["telefono"]})
        self.db.registrar(self.ahora(), "platica", f"Llamada a {contacto['nombre']}")
        return f"Marcando a {contacto['nombre']} en cuanto termines de hablar."

    def _h_ver_contactos(self, acciones: list[dict]) -> str:
        contactos = self.db.todos("SELECT nombre, parentesco FROM contactos ORDER BY nombre")
        if not contactos:
            return "No hay contactos guardados. La familia los agrega desde el panel."
        return "; ".join(f"{c['nombre']} ({c['parentesco']})" if c["parentesco"] else c["nombre"] for c in contactos)

    # --- medicinas ---

    def resumen_medicinas_hoy(self) -> list[dict]:
        hoy = self.ahora().date().isoformat()
        resumen = []
        for m in self.db.todos("SELECT * FROM medicinas WHERE activa = 1 ORDER BY nombre"):
            for hora in sorted(h.strip() for h in m["horas"].split(",") if h.strip()):
                toma = self.db.uno(
                    "SELECT estado FROM tomas WHERE medicina_id = ? AND fecha = ? AND hora = ?",
                    (m["id"], hoy, hora),
                )
                resumen.append(
                    {
                        "medicina": m["nombre"],
                        "dosis": m["dosis"],
                        "indicaciones": m["indicaciones"],
                        "hora": hora,
                        "estado": toma["estado"] if toma else "todavía no le toca",
                    }
                )
        return resumen

    def _h_ver_medicinas(self, acciones: list[dict]) -> str:
        resumen = self.resumen_medicinas_hoy()
        if not resumen:
            return "No hay medicinas registradas. La familia las agrega desde el panel."
        return json.dumps(resumen, ensure_ascii=False)

    def _h_confirmar_medicina(self, acciones: list[dict], nombre: str) -> str:
        ahora = self.ahora()
        pendientes = self.db.todos(
            "SELECT t.id, m.nombre, t.hora FROM tomas t JOIN medicinas m ON m.id = t.medicina_id "
            "WHERE t.estado = 'pendiente' AND t.fecha = ? ORDER BY t.hora",
            (ahora.date().isoformat(),),
        )
        if nombre.strip().lower() != "todas":
            pendientes = [p for p in pendientes if nombre.lower() in p["nombre"].lower() or p["nombre"].lower() in nombre.lower()]
        if not pendientes:
            return "No había ninguna toma pendiente con ese nombre para confirmar hoy."
        for p in pendientes:
            self.db.ejecutar(
                "UPDATE tomas SET estado = 'tomada', confirmada = ? WHERE id = ?",
                (ahora.isoformat(timespec="seconds"), p["id"]),
            )
            self.db.registrar(ahora, "medicina", f"Tomó {p['nombre']} ({p['hora']})")
        return "Confirmado: " + ", ".join(f"{p['nombre']} de las {p['hora']}" for p in pendientes)

    # --- recordatorios y citas ---

    def _h_crear_recordatorio(self, acciones: list[dict], texto: str, cuando: str) -> str:
        momento = _parsear_fecha(cuando)
        if momento < self.ahora() - timedelta(minutes=1):
            return "ERROR: esa hora ya pasó. Pregúntale de nuevo para cuándo."
        self.db.ejecutar("INSERT INTO recordatorios (texto, cuando) VALUES (?, ?)", (texto, momento.isoformat()))
        return f"Recordatorio guardado para {momento:%Y-%m-%d %H:%M}."

    def _h_agregar_cita(self, acciones: list[dict], descripcion: str, cuando: str) -> str:
        momento = _parsear_fecha(cuando)
        cita_id = self.db.ejecutar(
            "INSERT INTO citas (cuando, descripcion) VALUES (?, ?)", (momento.isoformat(), descripcion)
        )
        ahora = self.ahora()
        for antes, texto in (
            (timedelta(days=1), f"Le recuerdo que mañana a las {momento:%H:%M} tiene: {descripcion}."),
            (timedelta(hours=2), f"En dos horas, a las {momento:%H:%M}, tiene: {descripcion}."),
        ):
            aviso = momento - antes
            if aviso > ahora:
                self.db.ejecutar(
                    "INSERT INTO recordatorios (texto, cuando, cita_id) VALUES (?, ?, ?)",
                    (texto, aviso.isoformat(), cita_id),
                )
        return f"Cita guardada para {momento:%Y-%m-%d %H:%M}."

    def _h_ver_citas(self, acciones: list[dict]) -> str:
        citas = self.db.todos(
            "SELECT cuando, descripcion FROM citas WHERE cuando >= ? ORDER BY cuando LIMIT 10",
            (self.ahora().date().isoformat(),),
        )
        if not citas:
            return "No hay citas próximas."
        return "; ".join(f"{c['cuando'].replace('T', ' ')}: {c['descripcion']}" for c in citas)

    # --- notas ---

    def _h_guardar_nota(self, acciones: list[dict], texto: str) -> str:
        nota_id = self.db.ejecutar(
            "INSERT INTO notas (texto, creada) VALUES (?, ?)", (texto, self.ahora().isoformat(timespec="minutes"))
        )
        return f"Nota guardada (id {nota_id})."

    def _h_ver_notas(self, acciones: list[dict]) -> str:
        notas = self.db.todos("SELECT id, texto, creada FROM notas ORDER BY id DESC LIMIT 100")
        return json.dumps(notas, ensure_ascii=False) if notas else "No hay notas guardadas."

    def _h_borrar_nota(self, acciones: list[dict], id: int) -> str:
        borradas = self.db.ejecutar("DELETE FROM notas WHERE id = ?", (id,))
        return "Nota borrada." if borradas else "No existe una nota con ese id."

    # --- clima ---

    def _h_ver_clima(self, acciones: list[dict]) -> str:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.config.latitud}&longitude={self.config.longitud}"
            "&current=temperature_2m,precipitation,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&forecast_days=2&timezone={self.config.zona_horaria.key}"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                datos = json.load(r)
        except Exception:
            log.exception("Fallo al consultar el clima")
            return "ERROR: no se pudo consultar el clima ahorita."
        return json.dumps(
            {"ciudad": self.config.ciudad, "ahora": datos.get("current"), "pronostico": datos.get("daily")},
            ensure_ascii=False,
        )
