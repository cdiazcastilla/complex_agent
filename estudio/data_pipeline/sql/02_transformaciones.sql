-- =====================================================================
-- 02_transformaciones.sql — La "T" del ELT: todo corre DENTRO de la BD.
-- El orquestador Python ejecuta cada bloque "-- name:" por separado y
-- reemplaza %(batch_id)s. Todo es IDEMPOTENTE: correrlo 2 veces = mismo resultado.
-- =====================================================================

-- name: cuarentena_historial
-- Filas que violan reglas no se botan: se aíslan con el motivo, para auditar.
INSERT INTO stg.historial_cuarentena
SELECT r.*, CASE WHEN r.user_id IS NULL    THEN 'user_id nulo'
                 WHEN r.created_at IS NULL THEN 'created_at nulo'
                 WHEN r.id IS NULL         THEN 'id nulo' END
FROM raw.historial r
WHERE r._batch_id = %(batch_id)s
  AND (r.user_id IS NULL OR r.created_at IS NULL OR r.id IS NULL);

-- name: raw_a_stg_historial
WITH lote AS (
  -- Dedupe dentro del lote: si llegó 2 veces el mismo id, gana la última carga.
  SELECT DISTINCT ON (id) *
  FROM raw.historial
  WHERE _batch_id = %(batch_id)s                 -- solo el lote nuevo (incremental)
    AND user_id IS NOT NULL AND created_at IS NOT NULL AND id IS NOT NULL
  ORDER BY id, _loaded_at DESC
), limpio AS (
  SELECT id, user_id,
         NULLIF(trim(texto), '')                   AS texto,
         COALESCE(length(NULLIF(trim(texto), '')), 0) AS texto_len,
         CASE
           WHEN trim(duracion) ~ '^\d+(\.\d+)?s?$'          -- '12', '12.4', '12s'
             THEN rtrim(trim(duracion), 's')::numeric
           WHEN trim(duracion) ~ '^\d+:\d{2}$'              -- '0:12'
             THEN split_part(trim(duracion), ':', 1)::int * 60
                + split_part(trim(duracion), ':', 2)::int
         END                                      AS duracion_seg,
         created_at,
         (created_at AT TIME ZONE 'America/Bogota')::date AS fecha_bog,
         _batch_id
  FROM lote
)
INSERT INTO stg.historial AS t
SELECT * FROM limpio
ON CONFLICT (id) DO UPDATE
   SET texto = EXCLUDED.texto, texto_len = EXCLUDED.texto_len,
       duracion_seg = EXCLUDED.duracion_seg, _batch_id = EXCLUDED._batch_id
 -- Guardrail de costo: no reescribir filas idénticas (evita WAL, bloat y triggers inútiles)
 WHERE (t.texto, t.duracion_seg) IS DISTINCT FROM (EXCLUDED.texto, EXCLUDED.duracion_seg);

-- name: stg_a_fct_uso_diario
-- Incremental por CLAVES AFECTADAS: solo se recalculan los (usuario, día) que tocó el lote.
-- Recalcular todo = O(tabla). Recalcular afectados = O(lote). Esa es la diferencia senior.
WITH afectadas AS (
  SELECT DISTINCT user_id, fecha_bog
  FROM stg.historial
  WHERE _batch_id = %(batch_id)s
)
INSERT INTO marts.fct_uso_diario AS f (user_id, fecha, registros, duracion_seg)
SELECT h.user_id, h.fecha_bog, COUNT(*), COALESCE(SUM(h.duracion_seg), 0)
FROM stg.historial h
JOIN afectadas a USING (user_id, fecha_bog)   -- usa el índice (user_id, fecha_bog)
GROUP BY h.user_id, h.fecha_bog
ON CONFLICT (user_id, fecha) DO UPDATE
   SET registros = EXCLUDED.registros, duracion_seg = EXCLUDED.duracion_seg
 WHERE (f.registros, f.duracion_seg) IS DISTINCT FROM (EXCLUDED.registros, EXCLUDED.duracion_seg);

-- name: fct_a_dim_usuarios
-- La dimensión se alimenta del HECHO ya agregado (miles de filas), no de stg (millones).
WITH afectados AS (
  SELECT DISTINCT user_id FROM stg.historial WHERE _batch_id = %(batch_id)s
)
INSERT INTO marts.dim_usuarios AS d
       (user_id, creado_en, primer_uso, ultimo_uso, dias_activos, registros)
SELECT u.id, u.created_at, MIN(f.fecha), MAX(f.fecha), COUNT(*), SUM(f.registros)
FROM afectados a
JOIN auth.users u            ON u.id = a.user_id
JOIN marts.fct_uso_diario f  ON f.user_id = a.user_id
GROUP BY u.id, u.created_at
ON CONFLICT (user_id) DO UPDATE
   SET primer_uso = EXCLUDED.primer_uso, ultimo_uso = EXCLUDED.ultimo_uso,
       dias_activos = EXCLUDED.dias_activos, registros = EXCLUDED.registros;

-- name: mart_lead_score
-- Vista materializada: el BI lee un resultado precalculado en vez de re-ejecutar 5 JOINs.
-- REFRESH CONCURRENTLY exige un índice UNIQUE y no bloquea las lecturas de Power BI.
DO $$
BEGIN
  IF to_regclass('marts.lead_score') IS NULL THEN
    EXECUTE $mv$
      CREATE MATERIALIZED VIEW marts.lead_score AS
      WITH tts AS (SELECT uid AS user_id, SUM(tts_count) AS tts FROM public.daily_usage GROUP BY uid),
           voces AS (SELECT user_id, COUNT(*) AS voces FROM public.user_voices GROUP BY user_id),
           pagando AS (SELECT DISTINCT user_id FROM public.hub_subscriptions
                       WHERE status IN ('active','trialing')),
           base AS (
             SELECT d.user_id, d.registros, d.dias_activos, d.ultimo_uso,
                    COALESCE(t.tts, 0) AS tts, COALESCE(v.voces, 0) AS voces,
                      d.registros * 0.2 + d.dias_activos * 5 + COALESCE(t.tts, 0)
                    + COALESCE(v.voces, 0) * 20
                    + CASE WHEN d.ultimo_uso > current_date - 7  THEN 15
                           WHEN d.ultimo_uso > current_date - 30 THEN 5 ELSE 0 END AS score
             FROM marts.dim_usuarios d
             LEFT JOIN tts t   USING (user_id)
             LEFT JOIN voces v USING (user_id)
             WHERE NOT EXISTS (SELECT 1 FROM pagando p WHERE p.user_id = d.user_id)
           )
      SELECT *, NTILE(4) OVER (ORDER BY score DESC) AS cuartil   -- 1 = más caliente
      FROM base
    $mv$;
    CREATE UNIQUE INDEX lead_score_pk ON marts.lead_score (user_id);
  ELSE
    REFRESH MATERIALIZED VIEW CONCURRENTLY marts.lead_score;
  END IF;
END $$;
