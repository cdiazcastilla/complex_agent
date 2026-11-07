-- =====================================================================
-- 01_arquitectura.sql — Capas (medallion), roles, constraints y guardrails
--
--   public.*  (OLTP, la app)  ──Extract──▶  raw.*   (bronze: copia fiel, append-only)
--                                              │  ELT: transformar con SQL dentro de la BD
--                                              ▼
--                                           stg.*   (silver: limpio, tipado, deduplicado)
--                                              │
--                                              ▼
--                                           marts.* (gold: hechos/dimensiones para BI)
--                                           ops.*   (logs, watermarks, data quality)
--
-- Por qué NO en public: en Supabase, public se expone por la API REST (PostgREST).
-- Un mart en public = datos agregados publicados en internet si falla una política RLS.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------------
-- ROLES: mínimo privilegio. El pipeline LEE la app y ESCRIBE solo en sus capas.
-- ---------------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_reader') THEN
    CREATE ROLE etl_reader NOLOGIN;      -- solo lectura sobre la app
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_writer') THEN
    CREATE ROLE etl_writer NOLOGIN;      -- escribe en raw/stg/marts/ops
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_reader') THEN
    CREATE ROLE bi_reader NOLOGIN;       -- Power BI: solo ve marts
  END IF;
END $$;

GRANT USAGE ON SCHEMA public, auth TO etl_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO etl_reader;
GRANT SELECT (id, created_at) ON auth.users TO etl_reader;  -- ¡sin email! (PII)

GRANT USAGE, CREATE ON SCHEMA raw, stg, marts, ops TO etl_writer;
GRANT USAGE ON SCHEMA marts TO bi_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA marts GRANT SELECT ON TABLES TO bi_reader;

-- GUARDRAILS a nivel de rol: ninguna query del pipeline puede colgar la BD de la app.
ALTER ROLE etl_reader SET statement_timeout = '60s';
ALTER ROLE etl_reader SET lock_timeout = '2s';
ALTER ROLE etl_reader SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE etl_reader SET default_transaction_read_only = on;
ALTER ROLE bi_reader  SET statement_timeout = '30s';
ALTER ROLE bi_reader  SET work_mem = '16MB';

-- ---------------------------------------------------------------------
-- OPS: observabilidad del pipeline
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
  run_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  step        text        NOT NULL,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status      text        NOT NULL DEFAULT 'running'
              CHECK (status IN ('running','ok','failed','skipped')),
  rows_in     bigint,
  rows_out    bigint,
  detail      jsonb
);

-- Watermark compuesto (created_at, id): keyset estable aunque haya empates de timestamp.
CREATE TABLE IF NOT EXISTS ops.watermarks (
  source  text PRIMARY KEY,
  last_ts timestamptz NOT NULL DEFAULT '1970-01-01',
  last_id uuid        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'
);

CREATE TABLE IF NOT EXISTS ops.dq_results (
  run_id      bigint REFERENCES ops.pipeline_runs(run_id),
  check_name  text   NOT NULL,
  severity    text   NOT NULL CHECK (severity IN ('warn','error')),
  failed_rows bigint NOT NULL,
  checked_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- RAW (bronze): copia fiel + metadatos de carga. Sin constraints de negocio:
-- raw acepta lo que venga, para no perder datos. Se valida después.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.historial (
  id         uuid,
  user_id    uuid,
  texto      text,
  duracion   text,
  created_at timestamptz,
  _batch_id  bigint      NOT NULL,
  _loaded_at timestamptz NOT NULL DEFAULT now()
);
-- BRIN: índice diminuto, ideal para tablas append-only ordenadas por tiempo de carga.
CREATE INDEX IF NOT EXISTS raw_historial_batch_brin ON raw.historial USING brin (_batch_id);

-- ---------------------------------------------------------------------
-- STG (silver): aquí SÍ hay constraints. Si un dato viola una regla, falla la carga
-- (o se manda a cuarentena) en vez de contaminar los marts.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg.historial (
  id           uuid        PRIMARY KEY,                 -- dedupe natural (idempotencia)
  user_id      uuid        NOT NULL,
  texto        text,
  texto_len    integer     NOT NULL CHECK (texto_len >= 0),
  duracion_seg numeric(8,2)          CHECK (duracion_seg >= 0 AND duracion_seg < 3600),
  created_at   timestamptz NOT NULL,
  fecha_bog    date        NOT NULL,                    -- fecha local Colombia, precalculada
  _batch_id    bigint      NOT NULL
);
CREATE INDEX IF NOT EXISTS stg_historial_user_fecha ON stg.historial (user_id, fecha_bog);

CREATE TABLE IF NOT EXISTS stg.historial_cuarentena (LIKE raw.historial, motivo text);

CREATE TABLE IF NOT EXISTS stg.lip_clips (
  id               uuid    PRIMARY KEY,
  persona_id       text    NOT NULL,                    -- COALESCE(user_id, anon_id)
  is_anon          boolean NOT NULL,
  idioma           text    NOT NULL,
  categoria        text,
  device_type      text    NOT NULL CHECK (device_type IN ('mobile','desktop','tablet','unknown')),
  frame_count      integer NOT NULL CHECK (frame_count >= 0),
  frames_reales    integer NOT NULL CHECK (frames_reales >= 0),
  frames_ok        boolean GENERATED ALWAYS AS (frame_count = frames_reales) STORED,
  apertura_media   real,
  movimiento_medio real,
  quality_score    real    CHECK (quality_score BETWEEN 0 AND 1),
  created_at       timestamptz NOT NULL,
  _batch_id        bigint  NOT NULL
);

-- ---------------------------------------------------------------------
-- MARTS (gold): GRANO declarado explícitamente. Regla senior: "una fila = ___".
-- ---------------------------------------------------------------------
-- Grano: una fila = un usuario en un día (hora Colombia)
CREATE TABLE IF NOT EXISTS marts.fct_uso_diario (
  user_id      uuid    NOT NULL,
  fecha        date    NOT NULL,
  registros    integer NOT NULL CHECK (registros > 0),
  duracion_seg numeric(12,2) NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, fecha)
);

-- Grano: una fila = un usuario
CREATE TABLE IF NOT EXISTS marts.dim_usuarios (
  user_id     uuid PRIMARY KEY,
  creado_en   timestamptz NOT NULL,
  primer_uso  date,
  ultimo_uso  date,
  dias_activos integer NOT NULL DEFAULT 0,
  registros    bigint  NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------
-- ÍNDICES QUE EL PIPELINE EXIGE EN LA APP (el cost gate de Python los pide)
-- En producción: CREATE INDEX CONCURRENTLY → no bloquea las escrituras de la app.
-- (CONCURRENTLY no puede correr dentro de una transacción: va suelto.)
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS lip_clips_created_id ON public.lip_clips (created_at, id);
-- historial_created_id (created_at, id) se crea en el laboratorio, experimento E1.
