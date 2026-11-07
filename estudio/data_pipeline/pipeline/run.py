"""Orquestador del pipeline: ELT (historial) + ETL (lip_clips) + calidad + publicación.

Uso:
    python pipeline/run.py                     # corrida incremental normal
    python pipeline/run.py --dry-run           # ejecuta todo y hace ROLLBACK al final
    python pipeline/run.py --solo elt          # solo una parte (elt | etl)
    python pipeline/run.py --chunk 20000       # tamaño de lote del extract

Patrón Write-Audit-Publish: transformar → auditar (DQ) → publicar (COMMIT).
Si un check 'error' falla, se hace ROLLBACK y los marts NUNCA ven datos malos.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from comun import GuardrailError, cargar_sql, conexion, cost_gate, params_para

COLS_HIST = ("id", "user_id", "texto", "duracion", "created_at")
# Lista blanca de columnas: JAMÁS "SELECT *" sobre lip_clips (frames es JSONB pesado/TOAST).
COLS_CLIPS = ("id", "user_id", "anon_id", "idioma", "categoria", "device_type",
              "frame_count", "frames", "quality_score", "created_at")
LOOKBACK = "5 minutes"   # re-leer un margen por datos que llegan tarde (el upsert lo absorbe)


# ---------------------------------------------------------------------------
# Logging en ops.pipeline_runs con conexión AUTOCOMMIT: el log sobrevive al ROLLBACK
# ---------------------------------------------------------------------------
class Bitacora:
    def __init__(self):
        self._cm = conexion(autocommit=True, nombre="pipeline-log")
        self.conn = self._cm.__enter__()

    def inicio(self, paso: str) -> int:
        return self.conn.execute("INSERT INTO ops.pipeline_runs (step) VALUES (%s) RETURNING run_id",
                                 (paso,)).fetchone()[0]

    def fin(self, run_id: int, status: str, rows_in=None, rows_out=None, detalle=None):
        self.conn.execute(
            "UPDATE ops.pipeline_runs SET finished_at = now(), status = %s, rows_in = %s,"
            " rows_out = %s, detail = %s WHERE run_id = %s",
            (status, rows_in, rows_out, Jsonb(detalle or {}), run_id))

    def dq(self, run_id: int, nombre: str, severidad: str, fallas: int):
        self.conn.execute("INSERT INTO ops.dq_results VALUES (%s, %s, %s, %s)",
                          (run_id, nombre, severidad, fallas))

    def cerrar(self):
        self._cm.__exit__(None, None, None)


def leer_watermark(cur, fuente: str):
    cur.execute("INSERT INTO ops.watermarks (source) VALUES (%s) ON CONFLICT DO NOTHING", (fuente,))
    # FOR UPDATE: si alguien corre el pipeline 2 veces a la vez, la segunda espera (no duplica).
    cur.execute("SELECT last_ts, last_id FROM ops.watermarks WHERE source = %s FOR UPDATE", (fuente,))
    return cur.fetchone()


def extraer_keyset(cur, tabla: str, columnas, ts, last_id, chunk: int):
    """Paginación KEYSET (no OFFSET): cada página cuesta O(chunk), no O(offset + chunk)."""
    sql = (f"SELECT {', '.join(columnas)} FROM public.{tabla} "
           "WHERE (created_at, id) > (%(ts)s, %(id)s) ORDER BY created_at, id LIMIT %(n)s")
    primera = True
    while True:
        p = {"ts": ts, "id": last_id, "n": chunk}
        if primera:  # el gate se revisa una vez: el plan es el mismo en cada página
            print(f"   cost gate {tabla}: {cost_gate(cur, sql, p)}")
            primera = False
        cur.execute(sql, p)
        filas = cur.fetchall()
        if not filas:
            return
        yield filas
        ts, last_id = filas[-1][columnas.index("created_at")], filas[-1][0]


# ---------------------------------------------------------------------------
# ELT — historial: Extract + Load crudo a raw.*, la T la hace SQL dentro de la BD
# ---------------------------------------------------------------------------
def elt_historial(conn, bit: Bitacora, chunk: int) -> int:
    run_id = bit.inicio("elt_extract_load_historial")
    with conn.cursor() as cur:
        ts, last_id = leer_watermark(cur, "historial")
        cur.execute("SELECT %s::timestamptz - %s::interval", (ts, LOOKBACK))
        desde = cur.fetchone()[0]
        n, t0 = 0, time.perf_counter()
        nuevo_ts, nuevo_id = ts, last_id
        with conn.cursor() as cur_extract:
            for filas in extraer_keyset(cur_extract, "historial", COLS_HIST, desde, last_id, chunk):
                # COPY: carga binaria por streaming, ~10x más rápido que INSERT fila por fila.
                with cur.copy("COPY raw.historial (id, user_id, texto, duracion, created_at, _batch_id)"
                              " FROM STDIN") as cp:
                    for f in filas:
                        cp.write_row((*f, run_id))
                n += len(filas)
                nuevo_ts, nuevo_id = filas[-1][4], filas[-1][0]
        # El watermark avanza en la MISMA transacción que la carga: o pasan las dos o ninguna.
        if n:
            cur.execute("UPDATE ops.watermarks SET last_ts = %s, last_id = %s WHERE source = 'historial'",
                        (max(nuevo_ts, ts), nuevo_id))
    seg = time.perf_counter() - t0
    bit.fin(run_id, "ok", rows_in=n, rows_out=n, detalle={"segundos": round(seg, 2)})
    print(f"   raw.historial ← {n:,} filas en {seg:.1f}s (batch {run_id})")
    return run_id


def transformar(conn, bit: Bitacora, batch_id: int):
    for nombre, _, sql in cargar_sql("02_transformaciones.sql"):
        run_id = bit.inicio(f"T:{nombre}")
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(sql, params_para(sql, {"batch_id": batch_id}))
            filas = cur.rowcount if cur.rowcount >= 0 else None
        seg = time.perf_counter() - t0
        bit.fin(run_id, "ok", rows_out=filas, detalle={"segundos": round(seg, 3)})
        print(f"   {nombre:<24} {filas if filas is not None else '-':>10} filas  {seg:6.2f}s")


# ---------------------------------------------------------------------------
# ETL — lip_clips: la T se hace en Python porque procesa el JSON de frames
# ---------------------------------------------------------------------------
def transformar_clips(df: pd.DataFrame) -> pd.DataFrame:
    def stats(frames):
        arr = np.asarray(frames or [], dtype=float)
        if arr.ndim != 2 or len(arr) == 0:
            return 0, np.nan, np.nan
        movimiento = np.abs(np.diff(arr[:, :2], axis=0)).sum(axis=1).mean() if len(arr) > 1 else 0.0
        return len(arr), arr[:, 1].mean(), movimiento

    out = pd.DataFrame({
        "id": df["id"],
        "persona_id": df["user_id"].astype("string").fillna(df["anon_id"]),
        "is_anon": df["user_id"].isna(),
        "idioma": df["idioma"].fillna("desconocido").str.lower().str.strip(),
        "categoria": df["categoria"],
        "device_type": df["device_type"].where(df["device_type"].isin(["mobile", "desktop", "tablet"]),
                                               "unknown"),
        "frame_count": df["frame_count"].fillna(0).astype(int),
    })
    out[["frames_reales", "apertura_media", "movimiento_medio"]] = pd.DataFrame(
        df["frames"].map(stats).tolist(), index=df.index)
    # Guardrail de rango: lo que esté fuera de [0,1] se vuelve NULL en vez de romper el CHECK.
    out["quality_score"] = df["quality_score"].where(df["quality_score"].between(0, 1))
    out["created_at"] = df["created_at"]
    return out[out["persona_id"].notna()]   # sin ningún id → no se puede atribuir


def etl_clips(conn, bit: Bitacora, chunk: int):
    run_id = bit.inicio("etl_lip_clips")
    n_in = n_out = 0
    with conn.cursor() as cur:
        ts, last_id = leer_watermark(cur, "lip_clips")
        cur.execute("CREATE TEMP TABLE tmp_clips (LIKE stg.lip_clips INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("ALTER TABLE tmp_clips DROP COLUMN frames_ok")
        cols = ["id", "persona_id", "is_anon", "idioma", "categoria", "device_type", "frame_count",
                "frames_reales", "apertura_media", "movimiento_medio", "quality_score", "created_at",
                "_batch_id"]
        with conn.cursor() as cur_extract:
            # chunk pequeño: cada fila trae JSON pesado → controlar memoria del proceso
            for filas in extraer_keyset(cur_extract, "lip_clips", COLS_CLIPS, ts, last_id, min(chunk, 5000)):
                df = transformar_clips(pd.DataFrame(filas, columns=COLS_CLIPS))
                df["_batch_id"] = run_id
                with cur.copy(f"COPY tmp_clips ({', '.join(cols)}) FROM STDIN") as cp:
                    for fila in df[cols].itertuples(index=False):
                        cp.write_row([None if (isinstance(v, float) and np.isnan(v)) else v for v in fila])
                n_in += len(filas)
                n_out += len(df)
                ts, last_id = filas[-1][-1], filas[-1][0]
        cur.execute(f"""
            INSERT INTO stg.lip_clips ({', '.join(cols)})
            SELECT {', '.join(cols)} FROM tmp_clips
            ON CONFLICT (id) DO UPDATE SET quality_score = EXCLUDED.quality_score,
                                           frames_reales = EXCLUDED.frames_reales,
                                           _batch_id = EXCLUDED._batch_id
            WHERE stg.lip_clips.quality_score IS DISTINCT FROM EXCLUDED.quality_score""")
        if n_in:
            cur.execute("UPDATE ops.watermarks SET last_ts = %s, last_id = %s WHERE source = 'lip_clips'",
                        (ts, last_id))
    bit.fin(run_id, "ok", rows_in=n_in, rows_out=n_out)
    print(f"   stg.lip_clips ← {n_out:,} de {n_in:,} filas (descartadas: {n_in - n_out})")


# ---------------------------------------------------------------------------
# AUDIT: data quality antes de publicar
# ---------------------------------------------------------------------------
def auditar(conn, bit: Bitacora, batch_id: int) -> bool:
    run_id = bit.inicio("data_quality")
    ok = True
    with conn.cursor() as cur:
        for nombre, sev, sql in cargar_sql("03_calidad.sql"):
            cur.execute(sql, params_para(sql, {"batch_id": batch_id}))
            fallas = cur.fetchone()[0] or 0
            bit.dq(run_id, nombre, sev, fallas)
            icono = "✅" if fallas == 0 else ("❌" if sev == "error" else "⚠️ ")
            print(f"   {icono} {nombre:<30} {fallas:>8} [{sev}]")
            ok &= not (fallas and sev == "error")
    bit.fin(run_id, "ok" if ok else "failed")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--solo", choices=["elt", "etl"])
    ap.add_argument("--chunk", type=int, default=50_000)
    args = ap.parse_args()

    bit = Bitacora()
    try:
        with conexion(nombre="pipeline") as conn:
            batch_id = None
            if args.solo in (None, "elt"):
                print("▶ ELT historial: extract + load")
                batch_id = elt_historial(conn, bit, args.chunk)
                print("▶ ELT historial: transform (SQL en la BD)")
                transformar(conn, bit, batch_id)
            if args.solo in (None, "etl"):
                print("▶ ETL lip_clips (transform en Python)")
                etl_clips(conn, bit, args.chunk)
            print("▶ Audit (data quality)")
            if not auditar(conn, bit, batch_id or -1):
                conn.rollback()
                print("✖ Falló un check 'error' → ROLLBACK. Los marts no cambiaron.")
                return 1
            if args.dry_run:
                conn.rollback()
                print("◼ dry-run → ROLLBACK")
            else:
                conn.commit()
                print("✔ Publish → COMMIT")
    except GuardrailError as e:
        print(f"⛔ Guardrail: {e}")
        return 2
    finally:
        bit.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
