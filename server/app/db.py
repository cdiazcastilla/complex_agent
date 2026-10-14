"""Almacenamiento en SQLite: medicinas, tomas, recordatorios, citas, notas, contactos e historial."""

import sqlite3
import threading
from datetime import datetime

ESQUEMA = """
CREATE TABLE IF NOT EXISTS medicinas (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    dosis TEXT NOT NULL DEFAULT '',
    horas TEXT NOT NULL,              -- "08:00,20:00"
    indicaciones TEXT NOT NULL DEFAULT '',
    activa INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tomas (
    id INTEGER PRIMARY KEY,
    medicina_id INTEGER NOT NULL REFERENCES medicinas(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,              -- "2026-10-14"
    hora TEXT NOT NULL,               -- "08:00"
    estado TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente | tomada | omitida
    creada TEXT NOT NULL,
    confirmada TEXT,
    escalada INTEGER NOT NULL DEFAULT 0,
    UNIQUE (medicina_id, fecha, hora)
);
CREATE TABLE IF NOT EXISTS recordatorios (
    id INTEGER PRIMARY KEY,
    texto TEXT NOT NULL,
    cuando TEXT NOT NULL,             -- ISO local "2026-10-14T17:00"
    disparado INTEGER NOT NULL DEFAULT 0,
    cita_id INTEGER REFERENCES citas(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS citas (
    id INTEGER PRIMARY KEY,
    cuando TEXT NOT NULL,
    descripcion TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notas (
    id INTEGER PRIMARY KEY,
    texto TEXT NOT NULL,
    creada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contactos (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    parentesco TEXT NOT NULL DEFAULT '',
    telefono TEXT NOT NULL
);
-- Mensajes que el celular debe decir en voz alta (recordatorios, medicinas).
CREATE TABLE IF NOT EXISTS mensajes (
    id INTEGER PRIMARY KEY,
    texto TEXT NOT NULL,
    creado TEXT NOT NULL,
    entregado INTEGER NOT NULL DEFAULT 0,
    toma_id INTEGER REFERENCES tomas(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS historial (
    id INTEGER PRIMARY KEY,
    momento TEXT NOT NULL,
    tipo TEXT NOT NULL,               -- platica | medicina | aviso | emergencia | sistema
    detalle TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estado (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


class DB:
    """Envoltura mínima y segura entre hilos sobre una conexión SQLite."""

    def __init__(self, ruta: str):
        self._con = sqlite3.connect(ruta, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self._con.executescript(ESQUEMA)

    def ejecutar(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._con.execute(sql, params)
            self._con.commit()
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def todos(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, params).fetchall()]

    def uno(self, sql: str, params: tuple = ()) -> dict | None:
        filas = self.todos(sql, params)
        return filas[0] if filas else None

    # --- utilidades comunes ---

    def registrar(self, ahora: datetime, tipo: str, detalle: str) -> None:
        self.ejecutar(
            "INSERT INTO historial (momento, tipo, detalle) VALUES (?, ?, ?)",
            (ahora.isoformat(timespec="seconds"), tipo, detalle),
        )

    def obtener_estado(self, clave: str) -> str | None:
        fila = self.uno("SELECT valor FROM estado WHERE clave = ?", (clave,))
        return fila["valor"] if fila else None

    def guardar_estado(self, clave: str, valor: str) -> None:
        self.ejecutar(
            "INSERT INTO estado (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, valor),
        )
