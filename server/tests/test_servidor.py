from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import crear_app

TOKEN = "secreto-de-prueba"
CABECERAS = {"Authorization": f"Bearer {TOKEN}"}


class Reloj:
    def __init__(self):
        self.ahora = datetime(2026, 10, 14, 7, 55)

    def __call__(self):
        return self.ahora


def texto(t):
    return SimpleNamespace(type="text", text=t)


def herramienta(nombre, entrada, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=nombre, input=entrada, id=id_)


class ClaudeFalso:
    """Devuelve respuestas guionizadas y guarda lo que se le pidió."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.llamadas = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._crear))

    async def _crear(self, **kwargs):
        self.llamadas.append({**kwargs, "messages": list(kwargs["messages"])})
        contenido, motivo = self.guion.pop(0)
        return SimpleNamespace(content=contenido, stop_reason=motivo)


@pytest.fixture
def armar(tmp_path, monkeypatch):
    enviados = []
    monkeypatch.setattr("app.avisos.Avisador.enviar", lambda self, t: enviados.append(t) or 1)

    def _armar(guion=()):
        config = Config(token=TOKEN, ruta_db=str(tmp_path / "db.sqlite"), nombre_usuario="Don Pepe")
        reloj = Reloj()
        claude = ClaudeFalso(guion)
        app = crear_app(config, cliente=claude, ahora=reloj, revisar_cada=0)
        return TestClient(app), app, reloj, claude, enviados

    return _armar


def test_pide_token(armar):
    cliente, *_ = armar()
    assert cliente.post("/api/hablar", json={"texto": "hola"}).status_code == 401
    assert cliente.get("/api/mensajes", headers={"Authorization": "Bearer otro"}).status_code == 401
    assert cliente.get("/salud").json() == {"ok": True}


def test_platica_simple_con_contexto(armar):
    cliente, _, _, claude, _ = armar([([texto("¡Buenos días, Don Pepe!")], "end_turn")])
    r = cliente.post("/api/hablar", json={"texto": "buenos días"}, headers=CABECERAS).json()
    assert r == {"texto": "¡Buenos días, Don Pepe!", "acciones": []}
    llamada = claude.llamadas[0]
    assert llamada["model"] == "claude-opus-5-5"
    assert llamada["fallbacks"] == "default"
    primer = llamada["messages"][0]["content"]
    assert primer.startswith("[Contexto: hoy es miércoles 2026-10-14, son las 07:55.")
    assert primer.endswith("buenos días")


def test_ciclo_de_medicina_y_escalamiento(armar):
    cliente, app, reloj, _, enviados = armar(
        [
            ([herramienta("confirmar_medicina", {"nombre": "losartán"})], "tool_use"),
            ([texto("Listo, ya quedó anotado.")], "end_turn"),
        ]
    )
    cliente.post(
        "/api/medicinas",
        json={"nombre": "Losartán", "dosis": "una pastilla", "horas": "20:00, 08:00"},
        headers=CABECERAS,
    )
    revisor = app.state.revisor

    revisor.revisar(reloj())  # 7:55, todavía no
    assert cliente.get("/api/mensajes", headers=CABECERAS).json() == []

    reloj.ahora = datetime(2026, 10, 14, 8, 1)
    revisor.revisar(reloj())
    revisor.revisar(reloj())  # no se duplica
    msgs = cliente.get("/api/mensajes", headers=CABECERAS).json()
    assert len(msgs) == 1 and "Losartán" in msgs[0]["texto"]
    cliente.post(f"/api/mensajes/{msgs[0]['id']}/entregado", headers=CABECERAS)
    assert cliente.get("/api/mensajes", headers=CABECERAS).json() == []

    hoy = cliente.get("/api/resumen", headers=CABECERAS).json()["medicinas_hoy"]
    assert [m["estado"] for m in hoy] == ["pendiente", "todavía no le toca"]

    # 31 minutos sin confirmar: se avisa a la familia una sola vez
    reloj.ahora += timedelta(minutes=31)
    revisor.revisar(reloj())
    revisor.revisar(reloj())
    assert len(enviados) == 1 and "Losartán" in enviados[0]

    r = cliente.post("/api/hablar", json={"texto": "ya me la tomé"}, headers=CABECERAS).json()
    assert r["texto"] == "Listo, ya quedó anotado."
    hoy = cliente.get("/api/resumen", headers=CABECERAS).json()["medicinas_hoy"]
    assert hoy[0]["estado"] == "tomada"


def test_emergencia_por_claude_y_directa(armar):
    cliente, _, reloj, claude, enviados = armar(
        [
            ([herramienta("pedir_ayuda", {"motivo": "me caí en el baño"})], "tool_use"),
            ([texto("Ya avisé a su familia, tranquilo.")], "end_turn"),
        ]
    )
    r = cliente.post("/api/hablar", json={"texto": "me caí en el baño"}, headers=CABECERAS).json()
    assert r["acciones"] == [{"tipo": "emergencia", "motivo": "me caí en el baño", "enviados": 1}]
    assert "EMERGENCIA" in enviados[0] and "me caí en el baño" in enviados[0]
    # El resultado de la herramienta regresó a Claude en el mismo turno
    resultado = claude.llamadas[1]["messages"][-1]["content"][0]
    assert resultado["type"] == "tool_result" and "enviado" in resultado["content"]

    # Un segundo aviso inmediato no se duplica; pasados 2 minutos sí sale
    assert cliente.post("/api/emergencia", json={}, headers=CABECERAS).json() == {"enviados": 1}
    assert len(enviados) == 1
    reloj.ahora += timedelta(minutes=3)
    cliente.post("/api/emergencia", json={}, headers=CABECERAS)
    assert len(enviados) == 2


def test_llamar_contacto(armar):
    cliente, *_ = armar(
        [
            ([herramienta("llamar", {"nombre": "mi sobrino"})], "tool_use"),
            ([texto("Le marco a Juan.")], "end_turn"),
        ]
    )
    cliente.post(
        "/api/contactos", json={"nombre": "Juan", "parentesco": "sobrino", "telefono": "5512345678"}, headers=CABECERAS
    )
    r = cliente.post("/api/hablar", json={"texto": "márcale a mi sobrino"}, headers=CABECERAS).json()
    assert r["acciones"] == [{"tipo": "llamar", "nombre": "Juan", "telefono": "5512345678"}]


def test_cita_crea_recordatorios(armar):
    cliente, app, reloj, _, _ = armar()
    cliente.post("/api/citas", json={"descripcion": "Doctor del corazón", "cuando": "2026-10-16T11:00"}, headers=CABECERAS)
    reloj.ahora = datetime(2026, 10, 15, 11, 0)
    app.state.revisor.revisar(reloj())
    reloj.ahora = datetime(2026, 10, 16, 9, 5)
    app.state.revisor.revisar(reloj())
    textos = [m["texto"] for m in cliente.get("/api/mensajes", headers=CABECERAS).json()]
    assert len(textos) == 2
    assert textos[0].startswith("Le recuerdo que mañana") and textos[1].startswith("En dos horas")


def test_error_de_herramienta_regresa_a_claude(armar):
    cliente, _, _, claude, _ = armar(
        [
            ([herramienta("crear_recordatorio", {"texto": "x", "cuando": "no es fecha"})], "tool_use"),
            ([texto("¿Para qué hora se lo pongo?")], "end_turn"),
        ]
    )
    r = cliente.post("/api/hablar", json={"texto": "recuérdame algo"}, headers=CABECERAS).json()
    assert r["texto"] == "¿Para qué hora se lo pongo?"
    resultado = claude.llamadas[1]["messages"][-1]["content"][0]
    assert resultado["is_error"] is True


def test_platica_se_reinicia_tras_silencio(armar):
    cliente, _, reloj, claude, _ = armar([([texto("uno")], "end_turn"), ([texto("dos")], "end_turn"), ([texto("tres")], "end_turn")])
    cliente.post("/api/hablar", json={"texto": "a"}, headers=CABECERAS)
    reloj.ahora += timedelta(minutes=2)
    cliente.post("/api/hablar", json={"texto": "b"}, headers=CABECERAS)
    assert len(claude.llamadas[1]["messages"]) == 3  # continúa la plática
    reloj.ahora += timedelta(minutes=30)
    cliente.post("/api/hablar", json={"texto": "c"}, headers=CABECERAS)
    assert len(claude.llamadas[2]["messages"]) == 1  # plática nueva


def test_aviso_celular_desconectado(armar):
    cliente, app, reloj, _, enviados = armar()
    cliente.get("/api/mensajes", headers=CABECERAS)  # latido 7:55
    reloj.ahora += timedelta(minutes=61)
    app.state.revisor.revisar(reloj())
    app.state.revisor.revisar(reloj())
    assert len(enviados) == 1 and "no se reporta" in enviados[0]
