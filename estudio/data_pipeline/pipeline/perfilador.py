"""Perfilador de cardinalidad: lee las estadísticas del planner (pg_stats) y recomienda índices.

No escanea la tabla (costo ~0): usa lo que ANALYZE ya calculó. Ideal para automatizar
la revisión de tablas nuevas o que crecieron.

Uso:  python pipeline/perfilador.py public.lip_clips public.historial
"""
from __future__ import annotations

import sys

from comun import conexion

SQL = """
SELECT s.attname, s.null_frac, s.n_distinct, s.correlation,
       s.most_common_vals::text::text[] AS mcv, s.most_common_freqs AS mcf,
       c.reltuples::bigint AS filas, pg_size_pretty(pg_relation_size(c.oid)) AS tamano,
       s.avg_width, format_type(a.atttypid, a.atttypmod) AS tipo
FROM pg_stats s
JOIN pg_class c ON c.relname = s.tablename
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = s.attname
WHERE s.schemaname = %s AND s.tablename = %s
ORDER BY s.attname
"""


RANGO = ("timestamp", "date", "integer", "bigint", "numeric", "real", "double")


def recomendar(filas, distintos, null_frac, corr, mcf, ancho, tipo) -> str:
    top = mcf[0] if mcf else 0
    if distintos <= 1:
        return "constante: no indexar"
    if tipo.startswith("json"):
        return "GIN solo si filtras DENTRO del JSON"
    if tipo == "text" and ancho > 40:
        return "texto libre: GIN (tsvector/pg_trgm) si buscas palabras"
    if tipo.startswith(RANGO) and corr is not None and abs(corr) > 0.95 and distintos > 1000:
        return "BRIN (orden físico ≈ orden lógico)"
    if null_frac > 0.5:
        return "parcial WHERE col IS NOT NULL"
    if distintos / max(filas, 1) > 0.1:
        return "B-tree (alta cardinalidad)"
    if top > 0.5:
        return f"parcial para valores raros (top={top:.0%})"
    if distintos <= 20:
        return "no indexar sola; usar en índice compuesto"
    return "B-tree si se filtra seguido"


def main(tablas: list[str]):
    with conexion(nombre="perfilador") as conn:
        for t in tablas:
            esquema, tabla = t.split(".") if "." in t else ("public", t)
            filas_stats = conn.execute(SQL, (esquema, tabla)).fetchall()
            if not filas_stats:
                print(f"\n{t}: sin estadísticas. Corre ANALYZE {t};")
                continue
            reltuples, tamano = filas_stats[0][6], filas_stats[0][7]
            print(f"\n■ {t}   ~{reltuples:,} filas   {tamano}")
            print(f"  {'columna':<16}{'distintos':>11}{'nulos':>8}{'corr':>7}  {'valor más común':<28}recomendación")
            for att, nf, nd, corr, mcv, mcf, filas, _, ancho, tipo in filas_stats:
                # n_distinct < 0 significa "fracción de las filas" (escala con la tabla)
                distintos = int(-nd * filas) if nd < 0 else int(nd)
                top = f"{mcv[0][:16]} ({mcf[0]:.0%})" if mcv else "-"
                c = f"{corr:+.2f}" if corr is not None else "   -"
                print(f"  {att:<16}{distintos:>11,}{nf:>8.0%}{c:>7}  {top:<28}"
                      f"{recomendar(filas, distintos, nf, corr, mcf, ancho, tipo)}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["public.lip_clips", "public.historial"])
