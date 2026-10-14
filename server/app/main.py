"""Servidor HTTP: lo usa el celular (hablar, mensajes, emergencia) y la familia (panel)."""

import asyncio
import contextlib
import hmac
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .agente import Agente
from .avisos import Avisador
from .config import Config
from .db import DB
from .herramientas import Herramientas
from .tareas import Revisor

log = logging.getLogger(__name__)
PANEL = Path(__file__).with_name("panel.html")


class Hablar(BaseModel):
    texto: str = Field(min_length=1, max_length=2000)


class Emergencia(BaseModel):
    motivo: str = Field(default="Pidió ayuda desde el celular", max_length=500)


class Medicina(BaseModel):
    nombre: str = Field(min_length=1)
    dosis: str = ""
    horas: str = Field(pattern=r"^\s*([01]\d|2[0-3]):[0-5]\d(\s*,\s*([01]\d|2[0-3]):[0-5]\d)*\s*$")
    indicaciones: str = ""


class Contacto(BaseModel):
    nombre: str = Field(min_length=1)
    parentesco: str = ""
    telefono: str = Field(min_length=3)


class Cita(BaseModel):
    descripcion: str = Field(min_length=1)
    cuando: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def crear_app(
    config: Config | None = None,
    cliente: Any | None = None,
    ahora: Callable[[], datetime] | None = None,
    revisar_cada: float = 30.0,
) -> FastAPI:
    config = config or Config()
    if not config.token:
        raise RuntimeError("Falta APP_TOKEN: ponle una contraseña larga al servidor.")
    reloj = ahora or (lambda: datetime.now(config.zona_horaria).replace(tzinfo=None))

    db = DB(config.ruta_db)
    avisador = Avisador(config)
    herramientas = Herramientas(db, config, avisador, reloj)
    agente = Agente(config, herramientas, cliente)
    revisor = Revisor(db, config, avisador)

    @contextlib.asynccontextmanager
    async def ciclo(app: FastAPI):
        async def revisar_siempre():
            while True:
                try:
                    await asyncio.to_thread(revisor.revisar, reloj())
                except Exception:
                    log.exception("Falló la revisión periódica")
                await asyncio.sleep(revisar_cada)

        tarea = asyncio.create_task(revisar_siempre()) if revisar_cada > 0 else None
        if not avisador.configurado:
            log.warning("No hay Telegram ni WhatsApp configurado: la familia NO recibirá avisos.")
        yield
        if tarea:
            tarea.cancel()

    app = FastAPI(title="Asistente de voz", lifespan=ciclo)
    app.state.db = db
    app.state.revisor = revisor
    app.state.agente = agente

    def autorizar(authorization: str = Header(default="")) -> None:
        recibido = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(recibido.encode(), config.token.encode()):
            raise HTTPException(status_code=401, detail="Token incorrecto")

    seguro = [Depends(autorizar)]

    @app.get("/salud")
    def salud():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/panel", response_class=HTMLResponse)
    def panel():
        return PANEL.read_text(encoding="utf-8")

    # --- celular ---

    @app.post("/api/hablar", dependencies=seguro)
    async def hablar(datos: Hablar):
        revisor.latido(reloj())
        respuesta = await agente.responder(datos.texto)
        return {"texto": respuesta.texto, "acciones": respuesta.acciones}

    @app.get("/api/mensajes", dependencies=seguro)
    def mensajes():
        revisor.latido(reloj())
        return db.todos("SELECT id, texto FROM mensajes WHERE entregado = 0 ORDER BY id")

    @app.post("/api/mensajes/{mensaje_id}/entregado", dependencies=seguro)
    def entregado(mensaje_id: int):
        db.ejecutar("UPDATE mensajes SET entregado = 1 WHERE id = ?", (mensaje_id,))
        return {"ok": True}

    @app.post("/api/emergencia", dependencies=seguro)
    def emergencia(datos: Emergencia):
        enviados = herramientas.pedir_ayuda(datos.motivo)
        return {"enviados": enviados}

    # --- panel de la familia ---

    @app.get("/api/resumen", dependencies=seguro)
    def resumen():
        return {
            "nombre": config.nombre_usuario,
            "celular_ultima_vez": db.obtener_estado("celular_ultima_vez"),
            "avisos_configurados": avisador.configurado,
            "medicinas_hoy": herramientas.resumen_medicinas_hoy(),
        }

    @app.get("/api/medicinas", dependencies=seguro)
    def ver_medicinas():
        return db.todos("SELECT * FROM medicinas WHERE activa = 1 ORDER BY nombre")

    @app.post("/api/medicinas", dependencies=seguro)
    def crear_medicina(m: Medicina):
        horas = ",".join(sorted(h.strip() for h in m.horas.split(",")))
        nuevo = db.ejecutar(
            "INSERT INTO medicinas (nombre, dosis, horas, indicaciones) VALUES (?, ?, ?, ?)",
            (m.nombre, m.dosis, horas, m.indicaciones),
        )
        return {"id": nuevo}

    @app.delete("/api/medicinas/{item_id}", dependencies=seguro)
    def borrar_medicina(item_id: int):
        # Se desactiva en lugar de borrar para conservar el historial de tomas.
        db.ejecutar("UPDATE medicinas SET activa = 0 WHERE id = ?", (item_id,))
        return {"ok": True}

    @app.get("/api/contactos", dependencies=seguro)
    def ver_contactos():
        return db.todos("SELECT * FROM contactos ORDER BY nombre")

    @app.post("/api/contactos", dependencies=seguro)
    def crear_contacto(c: Contacto):
        return {
            "id": db.ejecutar(
                "INSERT INTO contactos (nombre, parentesco, telefono) VALUES (?, ?, ?)",
                (c.nombre, c.parentesco, c.telefono),
            )
        }

    @app.delete("/api/contactos/{item_id}", dependencies=seguro)
    def borrar_contacto(item_id: int):
        db.ejecutar("DELETE FROM contactos WHERE id = ?", (item_id,))
        return {"ok": True}

    @app.get("/api/citas", dependencies=seguro)
    def ver_citas():
        return db.todos("SELECT * FROM citas WHERE cuando >= ? ORDER BY cuando", (reloj().date().isoformat(),))

    @app.post("/api/citas", dependencies=seguro)
    def crear_cita(c: Cita):
        herramientas.ejecutar("agregar_cita", {"descripcion": c.descripcion, "cuando": c.cuando}, [])
        return {"ok": True}

    @app.delete("/api/citas/{item_id}", dependencies=seguro)
    def borrar_cita(item_id: int):
        db.ejecutar("DELETE FROM citas WHERE id = ?", (item_id,))
        return {"ok": True}

    @app.get("/api/notas", dependencies=seguro)
    def ver_notas():
        return db.todos("SELECT * FROM notas ORDER BY id DESC")

    @app.delete("/api/notas/{item_id}", dependencies=seguro)
    def borrar_nota(item_id: int):
        db.ejecutar("DELETE FROM notas WHERE id = ?", (item_id,))
        return {"ok": True}

    @app.get("/api/historial", dependencies=seguro)
    def historial(limite: int = 200):
        return db.todos("SELECT * FROM historial ORDER BY id DESC LIMIT ?", (min(limite, 1000),))

    return app


def app() -> FastAPI:
    """Punto de entrada para uvicorn: `uvicorn app.main:app --factory`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return crear_app()
