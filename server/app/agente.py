"""El agente: recibe lo que dijo la persona, llama a Claude con herramientas y regresa qué decir."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anthropic

from .config import Config
from .herramientas import DEFINICIONES, Herramientas

log = logging.getLogger(__name__)

MAX_VUELTAS = 8
RESPUESTA_FALLA = "Perdóneme, no le entendí bien. ¿Me lo puede repetir, por favor?"
RESPUESTA_SIN_CONEXION = "Ay, perdóneme, ahorita no me puedo conectar. Inténtelo en un ratito."


def prompt_sistema(config: Config) -> str:
    # Estable entre peticiones para aprovechar el caché: nada de fechas ni datos que cambien aquí.
    return f"""Eres {config.nombre_asistente}, el asistente de voz de {config.nombre_usuario}, una persona \
mayor que vive en {config.ciudad}. Todo lo que escribes se lee en voz alta por la bocina del celular, y \
todo lo que recibes es lo que él dijo, transcrito por el reconocimiento de voz (puede tener errores; \
interpreta con sentido común y, si no queda claro, pregunta).

Cómo hablar:
- Español de México, cálido, respetuoso y paciente. Háblale de usted y por su nombre de vez en cuando.
- Respuestas cortas: una a tres oraciones. Una sola idea o pregunta a la vez.
- Nada de listas, viñetas, emojis, asteriscos, enlaces ni formato: solo texto que suene natural hablado.
- Fechas y horas como se dicen: "el martes 14 a las cinco de la tarde", no "2026-10-14 17:00".
- Si repite una pregunta, contéstala otra vez con la misma paciencia, sin hacerlo notar.
- Puedes platicar con gusto de lo que quiera: su vida, recuerdos, futbol, música, noticias que conozcas.

Lo que puedes hacer con tus herramientas: avisar o mandar recados a la familia, llamar a sus contactos, \
llevar sus medicinas, recordatorios, citas y notas, y ver el clima. Usa las herramientas en lugar de \
inventar datos; nunca digas que hiciste algo si la herramienta no lo confirmó.

Seguridad, lo más importante:
- Si menciona una caída, dolor de pecho, falta de aire, mareo fuerte, confusión, sangrado, o pide ayuda, \
usa pedir_ayuda de inmediato, sin preguntar antes. Luego dile con calma que ya avisaste a su familia y, \
si suena grave, que marque al 911 o que tú puedes llamar a alguien.
- No eres doctor: no diagnostiques, no cambies dosis ni recomiendes medicinas. Si tiene una duda de \
salud, sugiérele consultarlo con su doctor o su familia, y ofrece avisarles.
- Si te pide algo que no puedes hacer, díselo con sencillez y ofrece avisar a la familia.

Cada mensaje suyo empieza con un bloque [Contexto] que pone el sistema (hora actual, medicinas \
pendientes). Úsalo pero no lo leas en voz alta. Si hay medicinas pendientes de confirmar y él dice que \
ya se la tomó, usa confirmar_medicina."""


@dataclass
class Platica:
    mensajes: list[dict[str, Any]] = field(default_factory=list)
    ultima_actividad: datetime | None = None


@dataclass
class Respuesta:
    texto: str
    acciones: list[dict] = field(default_factory=list)


class Agente:
    def __init__(self, config: Config, herramientas: Herramientas, cliente: Any | None = None):
        self.config = config
        self.herramientas = herramientas
        self.cliente = cliente or anthropic.AsyncAnthropic()
        self.sistema = prompt_sistema(config)
        self.platica = Platica()
        self._lock = asyncio.Lock()

    def _contexto(self) -> str:
        ahora = self.herramientas.ahora()
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        pendientes = [
            f"{m['medicina']} de las {m['hora']}"
            for m in self.herramientas.resumen_medicinas_hoy()
            if m["estado"] == "pendiente"
        ]
        return (
            f"[Contexto: hoy es {dias[ahora.weekday()]} {ahora:%Y-%m-%d}, son las {ahora:%H:%M}. "
            f"Medicinas pendientes de confirmar: {', '.join(pendientes) if pendientes else 'ninguna'}.]"
        )

    async def responder(self, texto_usuario: str) -> Respuesta:
        async with self._lock:
            ahora = self.herramientas.ahora()
            limite = timedelta(minutes=self.config.minutos_reiniciar_platica)
            if self.platica.ultima_actividad is None or ahora - self.platica.ultima_actividad > limite:
                # Plática nueva: el historial solo se agrega, nunca se edita, para no romper el caché.
                self.platica = Platica()
            self.platica.ultima_actividad = ahora
            self.herramientas.db.registrar(ahora, "platica", f"Dijo: {texto_usuario}")

            self.platica.mensajes.append(
                {"role": "user", "content": f"{self._contexto()}\n{texto_usuario}"}
            )
            try:
                respuesta = await self._bucle(self.platica.mensajes)
            except anthropic.APIConnectionError:
                log.exception("Sin conexión con la API de Claude")
                self.platica = Platica()
                return Respuesta(RESPUESTA_SIN_CONEXION)
            except anthropic.APIStatusError:
                log.exception("Error de la API de Claude")
                self.platica = Platica()
                return Respuesta(RESPUESTA_FALLA)
            if self.platica.mensajes[-1]["role"] != "assistant":
                # El bucle se cortó a medias; mejor empezar limpio la próxima vez.
                self.platica = Platica()
            self.herramientas.db.registrar(self.herramientas.ahora(), "platica", f"Respondió: {respuesta.texto}")
            return respuesta

    async def _bucle(self, mensajes: list[dict[str, Any]]) -> Respuesta:
        acciones: list[dict] = []
        for _ in range(MAX_VUELTAS):
            r = await self.cliente.beta.messages.create(
                model=self.config.modelo,
                max_tokens=8000,
                system=self.sistema,
                tools=DEFINICIONES,
                messages=mensajes,
                output_config={"effort": self.config.esfuerzo},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            # Se agrega el contenido completo (incluye bloques de pensamiento) tal cual.
            mensajes.append({"role": "assistant", "content": r.content})

            if r.stop_reason == "refusal":
                return Respuesta(RESPUESTA_FALLA, acciones)
            if r.stop_reason != "tool_use":
                texto = " ".join(b.text for b in r.content if b.type == "text").strip()
                return Respuesta(texto or RESPUESTA_FALLA, acciones)

            resultados = []
            for bloque in r.content:
                if bloque.type != "tool_use":
                    continue
                try:
                    salida = self.herramientas.ejecutar(bloque.name, dict(bloque.input), acciones)
                    resultados.append({"type": "tool_result", "tool_use_id": bloque.id, "content": salida})
                except Exception as e:
                    log.exception("Falló la herramienta %s", bloque.name)
                    resultados.append(
                        {"type": "tool_result", "tool_use_id": bloque.id, "content": f"ERROR: {e}", "is_error": True}
                    )
                log.info("Herramienta %s(%s)", bloque.name, json.dumps(bloque.input, ensure_ascii=False))
            mensajes.append({"role": "user", "content": resultados})

        log.warning("Se acabaron las vueltas del agente")
        return Respuesta(RESPUESTA_FALLA, acciones)
