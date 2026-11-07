"""Corre los experimentos de sql/04_laboratorio.sql y arma un reporte comparativo.

Métricas por consulta (modo explain):
  ms          mediana de 3 EXPLAIN ANALYZE (la 1ª corrida calienta la caché)
  buffers     páginas de 8 KB tocadas (hit = en RAM, read = de disco) → el "gasto" real
  nodo        tipo de acceso principal (Seq Scan, Index Scan, Index Only Scan...)
  est/real    filas estimadas vs reales del nodo superior
  peor_x      el nodo con peor error de cardinalidad (factor). >10x = plan en riesgo

Uso:  python pipeline/benchmark.py [--solo e5]
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

import psycopg

from comun import cargar_sql, conexion

TIMEOUT_BENCH = "20s"


def recorrer(n, bajo_limit=False):
    """Bajo un LIMIT la estimación del hijo es para TODO el scan (el LIMIT lo corta antes):
    esos nodos no cuentan como error de cardinalidad."""
    yield n, bajo_limit
    for h in n.get("Plans", []):
        yield from recorrer(h, bajo_limit or n["Node Type"] == "Limit")


def resumir(plan_json) -> dict:
    root = plan_json[0]
    p = root["Plan"]
    con_flag = list(recorrer(p))
    nodos = [n for n, _ in con_flag]
    acceso = [n["Node Type"] for n in nodos if "Scan" in n["Node Type"]]
    peor, peor_nodo = 1.0, ""
    for n, bajo_limit in con_flag:
        if bajo_limit:
            continue
        est, real = max(n["Plan Rows"], 1), max(n.get("Actual Rows", 0) * n.get("Actual Loops", 1), 1)
        f = max(est / real, real / est)
        if f > peor:
            peor, peor_nodo = f, n["Node Type"]
    return {
        "ms": root["Execution Time"],
        "hit": p.get("Shared Hit Blocks", 0),
        "read": p.get("Shared Read Blocks", 0),
        "acceso": ", ".join(dict.fromkeys(acceso)) or p["Node Type"],
        "est": p["Plan Rows"],
        "real": p.get("Actual Rows", 0),
        "peor": f"{peor:.1f}x {peor_nodo}" if peor >= 2 else "ok",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", help="prefijo del experimento, p. ej. e5")
    args = ap.parse_args()

    with conexion(autocommit=True, nombre="benchmark") as conn:
        conn.execute(f"SET statement_timeout = '{TIMEOUT_BENCH}'")
        for nombre, modo, sql in cargar_sql("04_laboratorio.sql"):
            if args.solo and not nombre.startswith(args.solo):
                continue
            modo = modo or "explain"
            try:
                if modo == "ddl":
                    t0 = time.perf_counter()
                    for stmt in filter(None, (s.strip() for s in sql.split(";"))):
                        if not all(l.strip().startswith("--") for l in stmt.splitlines()):
                            conn.execute(stmt)
                    print(f"\n🔧 {nombre}  ({time.perf_counter() - t0:.1f}s)")
                elif modo == "show":
                    cur = conn.execute(sql)
                    cols = [d.name for d in cur.description]
                    print(f"\n📋 {nombre}")
                    print("   " + " | ".join(cols))
                    for fila in cur.fetchall():
                        print("   " + " | ".join(str(v) for v in fila))
                elif modo == "fetch":
                    tiempos, mb = [], 0.0
                    for _ in range(3):
                        t0 = time.perf_counter()
                        filas = conn.execute(sql).fetchall()
                        tiempos.append(time.perf_counter() - t0)
                        mb = sum(len(str(f)) for f in filas) / 1e6
                    print(f"\n📦 {nombre:<26} {statistics.median(tiempos) * 1000:9.1f} ms   "
                          f"{len(filas):>7,} filas   ~{mb:,.1f} MB transferidos")
                else:
                    corridas = []
                    for _ in range(3):
                        cur = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
                        raw = cur.fetchone()[0]
                        corridas.append(resumir(json.loads(raw) if isinstance(raw, str) else raw))
                    r = corridas[-1]
                    r["ms"] = statistics.median(c["ms"] for c in corridas)
                    print(f"\n⏱  {nombre:<26} {r['ms']:9.1f} ms   buffers hit={r['hit']:,} read={r['read']:,}"
                          f"\n   acceso: {r['acceso']}   est/real: {r['est']:,}/{r['real']:,}"
                          f"   peor estimación: {r['peor']}")
            except psycopg.errors.QueryCanceled:
                print(f"\n💥 {nombre:<26} TIMEOUT > {TIMEOUT_BENCH}  (el guardrail la cortó)")


if __name__ == "__main__":
    main()
