-- =====================================================================
-- 03_calidad.sql — Data Quality checks. Cada check devuelve UN número:
-- la cantidad de filas que fallan. 0 = OK.
-- Formato:  -- name: <check> | <warn|error>
-- "error" detiene el pipeline ANTES de que el BI vea datos malos.
-- =====================================================================

-- name: stg_historial_sin_huerfanos | error
-- Integridad referencial: todo user_id de stg debe existir en auth.users.
SELECT COUNT(*) FROM stg.historial h
WHERE NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = h.user_id);

-- name: reconciliacion_lote | error
-- Reconciliación: toda fila VÁLIDA del lote tiene que haber llegado a stg (anti-join).
SELECT COUNT(*)
FROM (SELECT DISTINCT id FROM raw.historial
      WHERE _batch_id = %(batch_id)s
        AND id IS NOT NULL AND user_id IS NOT NULL AND created_at IS NOT NULL) r
WHERE NOT EXISTS (SELECT 1 FROM stg.historial s WHERE s.id = r.id);

-- name: fct_cuadra_con_stg | error
-- El hecho agregado debe sumar EXACTAMENTE lo mismo que el detalle. Si no: fan-out o pérdida.
SELECT ABS((SELECT COALESCE(SUM(registros), 0) FROM marts.fct_uso_diario)
         - (SELECT COUNT(*) FROM stg.historial));

-- name: duracion_no_parseable | warn
-- Formatos de duración que el parser no reconoce (hay que ampliar la regex).
SELECT COUNT(*) FROM raw.historial
WHERE _batch_id = %(batch_id)s AND NULLIF(trim(duracion), '') IS NOT NULL
  AND trim(duracion) !~ '^\d+(\.\d+)?s?$' AND trim(duracion) !~ '^\d+:\d{2}$';

-- name: frescura_datos | warn
-- Freshness: si el dato más nuevo tiene más de 2 días, algo se rompió río arriba.
SELECT CASE WHEN MAX(created_at) < now() - interval '2 days' THEN 1 ELSE 0 END
FROM stg.historial;

-- name: clips_frames_inconsistentes | warn
SELECT COUNT(*) FROM stg.lip_clips WHERE NOT frames_ok;
