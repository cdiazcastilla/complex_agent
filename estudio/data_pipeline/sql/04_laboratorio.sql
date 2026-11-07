-- =====================================================================
-- 04_laboratorio.sql — Experimentos de optimización ANTES / DESPUÉS.
-- Correr con:  python pipeline/benchmark.py
--
-- Modos:  explain (default) → EXPLAIN (ANALYZE, BUFFERS), mediana de 3 corridas
--         fetch             → ejecuta y TRAE las filas (mide red + detoast real)
--         ddl               → ejecuta (crear índices, ANALYZE, VACUUM...)
--         show              → ejecuta e imprime el resultado
-- =====================================================================


-- name: e0_reset | ddl
-- Deja la BD como al principio para poder repetir el laboratorio.
DROP INDEX IF EXISTS public.historial_created_id;
DROP INDEX IF EXISTS public.lip_clips_tablet;
DROP INDEX IF EXISTS public.historial_created_inc_user;
DROP INDEX IF EXISTS public.historial_created_brin;
DROP STATISTICS IF EXISTS public.lip_clips_idioma_cat;
ANALYZE public.lip_clips;


-- ─────────── E1. Paginación keyset: sin índice vs con índice compuesto ───────────
-- name: e1_keyset_sin_indice
SELECT id, user_id, created_at FROM public.historial
WHERE (created_at, id) > (now() - interval '30 days', '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY created_at, id LIMIT 50000;

-- name: e1_ddl_indice | ddl
-- El ORDEN de las columnas importa: (created_at, id) sirve para el ORDER BY y el WHERE de tupla.
CREATE INDEX IF NOT EXISTS historial_created_id ON public.historial (created_at, id);

-- name: e1_keyset_con_indice
SELECT id, user_id, created_at FROM public.historial
WHERE (created_at, id) > (now() - interval '30 days', '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY created_at, id LIMIT 50000;


-- ─────────── E2. OFFSET vs keyset (página profunda) ───────────
-- name: e2_offset_profundo
-- OFFSET lee y DESCARTA 800.000 filas para devolver 100. Costo O(offset).
SELECT id, created_at FROM public.historial ORDER BY created_at, id OFFSET 800000 LIMIT 100;

-- name: e2_keyset_profundo
-- Keyset salta directo con el índice. Costo O(página).
SELECT id, created_at FROM public.historial
WHERE (created_at, id) > (now() - interval '20 days', '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY created_at, id LIMIT 100;


-- ─────────── E3. Sargabilidad: funciones sobre la columna matan el índice ───────────
-- name: e3_no_sargable
-- Envolver la columna en una función/cast obliga a calcularla en CADA fila → Seq Scan.
SELECT COUNT(*) FROM public.historial
WHERE (created_at AT TIME ZONE 'America/Bogota')::date = current_date - 3;

-- name: e3_sargable
-- Mismo resultado, pero la columna queda "desnuda" y se compara contra un RANGO → Index Scan.
SELECT COUNT(*) FROM public.historial
WHERE created_at >= ((current_date - 3)::timestamp AT TIME ZONE 'America/Bogota')
  AND created_at <  ((current_date - 2)::timestamp AT TIME ZONE 'America/Bogota');


-- ─────────── E4. SELECT * con JSONB pesado (TOAST) ───────────
-- name: e4_select_estrella | fetch
-- frames vive en TOAST (fuera de la fila). Pedirlo = descomprimir + mandar MBs por la red.
SELECT * FROM public.lip_clips WHERE created_at > now() - interval '60 days';

-- name: e4_columnas_necesarias | fetch
SELECT id, idioma, device_type, quality_score FROM public.lip_clips
WHERE created_at > now() - interval '60 days';

-- name: e4_calcular_en_bd | fetch
-- Si solo necesitas el TAMAÑO del array, calcúlalo en la BD (ELT) y no traigas el JSON (ETL).
SELECT id, jsonb_array_length(frames) AS frames_reales FROM public.lip_clips
WHERE created_at > now() - interval '60 days';


-- ─────────── E5. CARDINALIDAD: fan-out de JOINs ───────────
-- name: e5_fanout_calculo | show
-- Antes de unir 1:N con 1:N, calcula cuántas filas va a generar el JOIN intermedio:
-- por usuario, filas = historial × clips. Sumado sobre todos los usuarios:
SELECT to_char(SUM(h.n * c.n), 'FM999,999,999,999') AS filas_intermedias_del_join,
       to_char(SUM(h.n), 'FM999,999,999') AS filas_historial,
       to_char(SUM(c.n), 'FM999,999,999') AS filas_clips
FROM (SELECT user_id, COUNT(*) n FROM public.historial GROUP BY user_id) h
JOIN (SELECT user_id, COUNT(*) n FROM public.lip_clips GROUP BY user_id) c USING (user_id);

-- name: e5_unir_luego_agrupar
-- ❌ Une primero (explota a cientos de millones de filas) y "arregla" con COUNT(DISTINCT).
SELECT u.id, COUNT(DISTINCT h.id) AS registros, COUNT(DISTINCT c.id) AS clips
FROM auth.users u
LEFT JOIN public.historial h ON h.user_id = u.id
LEFT JOIN public.lip_clips c ON c.user_id = u.id
GROUP BY u.id;

-- name: e5_agrupar_luego_unir
-- ✅ Reduce cada lado a 1 fila por usuario y DESPUÉS une: el JOIN es 1:1:1.
SELECT u.id, COALESCE(h.n, 0) AS registros, COALESCE(c.n, 0) AS clips
FROM auth.users u
LEFT JOIN (SELECT user_id, COUNT(*) n FROM public.historial GROUP BY user_id) h ON h.user_id = u.id
LEFT JOIN (SELECT user_id, COUNT(*) n FROM public.lip_clips GROUP BY user_id) c ON c.user_id = u.id;


-- ─────────── E6. Baja cardinalidad: índice normal vs índice parcial ───────────
-- name: e6_perfil_device | show
SELECT device_type, COUNT(*), round(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM public.lip_clips GROUP BY 1 ORDER BY 2 DESC;

-- name: e6_tablet_sin_indice
SELECT id, quality_score FROM public.lip_clips WHERE device_type = 'tablet';

-- name: e6_ddl_indice_parcial | ddl
-- Un índice sobre device_type completo es inútil para 'mobile' (85%: el planner prefiere
-- Seq Scan). Un índice PARCIAL solo indexa el 3% raro: diminuto y útil.
CREATE INDEX IF NOT EXISTS lip_clips_tablet ON public.lip_clips (created_at)
INCLUDE (quality_score) WHERE device_type = 'tablet';

-- name: e6_tablet_con_indice
SELECT id, quality_score FROM public.lip_clips WHERE device_type = 'tablet';


-- ─────────── E7. Columnas correlacionadas: el planner asume independencia ───────────
-- name: e7_estimacion_mala
-- categoria depende de idioma ('en_saludo' ⇒ idioma='en'). El planner MULTIPLICA
-- selectividades (0.2 × 0.067) y subestima ~5x. Mira "peor_estimacion" en el reporte.
SELECT COUNT(*) FROM public.lip_clips WHERE idioma = 'en' AND categoria = 'en_saludo';

-- name: e7_ddl_stats_extendidas | ddl
CREATE STATISTICS IF NOT EXISTS lip_clips_idioma_cat (dependencies, ndistinct, mcv)
  ON idioma, categoria FROM public.lip_clips;
ANALYZE public.lip_clips;

-- name: e7_estimacion_buena
SELECT COUNT(*) FROM public.lip_clips WHERE idioma = 'en' AND categoria = 'en_saludo';


-- ─────────── E8. Anti-join: NOT IN vs NOT EXISTS ───────────
-- name: e8_not_in
-- Además de lento si no cabe en memoria, NOT IN devuelve VACÍO si la subconsulta tiene un NULL.
SELECT COUNT(*) FROM auth.users u
WHERE u.id NOT IN (SELECT user_id FROM public.historial);

-- name: e8_not_exists
-- Hash Anti Join: semántica correcta con NULLs y el planner lo optimiza mejor.
SELECT COUNT(*) FROM auth.users u
WHERE NOT EXISTS (SELECT 1 FROM public.historial h WHERE h.user_id = u.id);


-- ─────────── E9. Índice cubriente → Index Only Scan ───────────
-- name: e9_sin_cubrir
SELECT user_id, COUNT(*) FROM public.historial
WHERE created_at > now() - interval '7 days' GROUP BY user_id;

-- name: e9_ddl_cubriente | ddl
-- INCLUDE guarda user_id en la hoja del índice: no hay que visitar la tabla (heap).
-- VACUUM actualiza el visibility map, requisito para el Index Only Scan.
CREATE INDEX IF NOT EXISTS historial_created_inc_user ON public.historial (created_at) INCLUDE (user_id);
VACUUM (ANALYZE) public.historial;

-- name: e9_cubierto
SELECT user_id, COUNT(*) FROM public.historial
WHERE created_at > now() - interval '7 days' GROUP BY user_id;


-- ─────────── E10. Tamaño: B-tree vs BRIN ───────────
-- name: e10_ddl_brin | ddl
CREATE INDEX IF NOT EXISTS historial_created_brin ON public.historial USING brin (created_at);

-- name: e10_tamanos | show
SELECT indexrelname AS indice, pg_size_pretty(pg_relation_size(indexrelid)) AS tamano,
       idx_scan AS veces_usado
FROM pg_stat_user_indexes WHERE relname = 'historial' ORDER BY pg_relation_size(indexrelid) DESC;
