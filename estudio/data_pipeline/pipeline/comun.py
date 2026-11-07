"""Utilidades compartidas: conexión con guardrails, cost gate y lector de SQL nombrado."""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# ---------------------------------------------------------------------------
# Configuración (todo por variables de entorno; nada de credenciales en el código)
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5433/deeprvoice_mock")
STATEMENT_TIMEOUT = os.environ.get("STATEMENT_TIMEOUT", "60s")
MAX_PLAN_COST = float(os.environ.get("MAX_PLAN_COST", "20000"))   # unidades del planificador
TABLAS_GRANDES = {"historial", "lip_clips", "lip_visemas"}          # Seq Scan prohibido en incremental


class GuardrailError(RuntimeError):
    """Se lanza cuando una consulta viola un límite de costo o de seguridad."""


@contextmanager
def conexion(autocommit: bool = False, nombre: str = "pipeline"):
    """Conexión con límites: ninguna query puede colgar ni bloquear la BD de la app."""
    with psycopg.connect(DATABASE_URL, autocommit=autocommit, application_name=nombre) as conn:
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET idle_in_transaction_session_timeout = '60s'")
        yield conn


# ---------------------------------------------------------------------------
# Cost gate: EXPLAIN (sin ANALYZE = NO ejecuta) antes de correr la consulta
# ---------------------------------------------------------------------------
def _recorrer(nodo: dict):
    yield nodo
    for hijo in nodo.get("Plans", []):
        yield from _recorrer(hijo)


def plan(cur: psycopg.Cursor, sql: str, params: dict | None = None) -> dict:
    cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
    raw = cur.fetchone()[0]
    return (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]


def cost_gate(cur: psycopg.Cursor, sql: str, params: dict | None = None,
              max_cost: float = MAX_PLAN_COST, prohibir_seq_scan: bool = True) -> dict:
    """Revisa el plan estimado y aborta si es demasiado caro. Devuelve un resumen."""
    p = plan(cur, sql, params)
    seq = [n["Relation Name"] for n in _recorrer(p)
           if n["Node Type"] == "Seq Scan" and n.get("Relation Name") in TABLAS_GRANDES]
    resumen = {"costo": p["Total Cost"], "filas_estimadas": p["Plan Rows"], "seq_scans": seq}
    if p["Total Cost"] > max_cost:
        raise GuardrailError(f"Costo estimado {p['Total Cost']:.0f} > límite {max_cost:.0f}: {resumen}")
    if prohibir_seq_scan and seq:
        raise GuardrailError(f"Seq Scan sobre tabla grande {seq}. ¿Falta un índice? {resumen}")
    return resumen


# ---------------------------------------------------------------------------
# SQL nombrado: bloques "-- name: x" o "-- name: x | severidad" dentro de un .sql
# ---------------------------------------------------------------------------
_PATRON = re.compile(r"^-- name:\s*(\S+)(?:\s*\|\s*(\w+))?\s*$", re.M)


def cargar_sql(archivo: str) -> list[tuple[str, str | None, str]]:
    texto = (SQL_DIR / archivo).read_text(encoding="utf-8")
    marcas = list(_PATRON.finditer(texto))
    bloques = []
    for i, m in enumerate(marcas):
        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        bloques.append((m.group(1), m.group(2), texto[m.end():fin].strip()))
    return bloques


def params_para(sql: str, params: dict) -> dict | None:
    """psycopg interpreta '%' solo si se pasan parámetros: pásalos solo si hacen falta."""
    return params if "%(" in sql else None
