-- =====================================================================
-- 00_mock_seed.sql  —  Base de datos FALSA que imita a deeprvoice, pero
-- 50-1000x más grande, para que EXPLAIN ANALYZE muestre diferencias reales.
--
-- ⚠️  SOLO para un Postgres local o un proyecto Supabase de pruebas.
--     El guardrail de abajo aborta si detecta un Supabase real.
-- =====================================================================

-- GUARDRAIL: si ya existe auth.users con datos, estamos en Supabase real → abortar.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'auth' AND table_name = 'users')
     AND current_setting('mock.allow', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'auth.users ya existe: parece un Supabase real. Seed abortado.';
  END IF;
END $$;

SELECT setseed(0.42);  -- datos reproducibles

CREATE SCHEMA IF NOT EXISTS auth;

-- ---------- auth.users : 20.000 usuarios ----------
CREATE TABLE auth.users (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email      text NOT NULL,
  created_at timestamptz NOT NULL
);
INSERT INTO auth.users (email, created_at)
SELECT 'user' || g || '@mail.test',
       now() - (random() * interval '400 days')
FROM generate_series(1, 20000) g;

-- tabla auxiliar para escoger usuarios con sesgo (pocos usuarios = mucha actividad)
CREATE TEMP TABLE u AS
SELECT row_number() OVER (ORDER BY id) AS n, id FROM auth.users;
CREATE INDEX ON u(n);

-- ---------- public.historial : 1.000.000 filas, distribución tipo ley de potencia ----------
CREATE TABLE public.historial (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid,
  texto      text,
  duracion   text,           -- TEXTO, igual que en producción (formato sucio a propósito)
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.historial (user_id, texto, duracion, created_at)
SELECT u.id,
       CASE WHEN random() < 0.02 THEN NULL
            WHEN random() < 0.02 THEN '   '
            ELSE 'frase de prueba numero ' || g END,
       CASE (g % 6)
            WHEN 0 THEN (1 + (random()*30)::int) || 's'          -- '12s'
            WHEN 1 THEN '0:' || lpad((random()*59)::int::text, 2, '0')  -- '0:12'
            WHEN 2 THEN round((random()*30)::numeric, 1)::text   -- '12.4'
            WHEN 3 THEN ''                                       -- vacío
            WHEN 4 THEN NULL
            ELSE (1 + (random()*30)::int)::text                  -- '12'
       END,
       now() - (random() ^ 2) * interval '365 days'               -- más actividad reciente
FROM (SELECT g, 1 + floor(20000 * random() ^ 4)::int AS n    -- sesgo fuerte
        FROM generate_series(1, 1000000) g) s
JOIN u USING (n);

-- ---------- public.lip_clips : 50.000 clips con JSONB pesado (frames) ----------
CREATE TABLE public.lip_clips (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid,
  anon_id       text,
  frase         text,
  idioma        text,
  categoria     text,
  frames        jsonb,
  frame_count   integer,
  duration_ms   integer,
  quality_score real,
  device_type   text,
  browser       text,
  origen        text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.lip_clips (user_id, anon_id, frase, idioma, categoria, frames,
                              frame_count, duration_ms, quality_score, device_type,
                              browser, origen, created_at)
SELECT CASE WHEN r.a < 0.3 THEN NULL ELSE u.id END,
       CASE WHEN r.a < 0.3 THEN 'anon_' || (g % 3000) END,
       'frase ' || (g % 500),
       r.idioma,
       -- categoria CORRELACIONADA con idioma (para la lección de estadísticas extendidas)
       r.idioma || '_' || (ARRAY['saludo','numero','emocion'])[1 + (g % 3)],
       (SELECT jsonb_agg(jsonb_build_array(round(random()::numeric,3), round(random()::numeric,3),
                                           round(random()::numeric,3), round(random()::numeric,3)))
          FROM generate_series(1, 60 + (g % 40))),
       CASE WHEN g % 50 = 0 THEN 10 ELSE 60 + (g % 40) END,   -- 2% inconsistentes a propósito
       2000 + (g % 3000),
       CASE WHEN g % 100 = 0 THEN NULL ELSE round(random()::numeric, 3) END,
       CASE WHEN r.b < 0.85 THEN 'mobile' WHEN r.b < 0.97 THEN 'desktop' ELSE 'tablet' END,
       (ARRAY['chrome','safari','firefox','edge'])[1 + (g % 4)],
       'web',
       now() - random() * interval '200 days'
FROM (SELECT g, 1 + floor(20000 * random() ^ 3)::int AS n
        FROM generate_series(1, 50000) g) s
JOIN u USING (n)
CROSS JOIN LATERAL (SELECT random() AS a, random() AS b,
                           (ARRAY['es','es','es','en','pt'])[1 + (g % 5)] AS idioma) r;

-- ---------- public.lip_visemas : ~4 por clip, 20% de clips sin visemas ----------
CREATE TABLE public.lip_visemas (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid,
  user_id         uuid,
  visema          integer,
  orden           integer,
  duracion_frames integer,
  created_at      timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.lip_visemas (clip_id, user_id, visema, orden, duracion_frames, created_at)
SELECT c.id, c.user_id, (random()*14)::int, o, 2 + (random()*10)::int, c.created_at
FROM (SELECT id, user_id, created_at FROM public.lip_clips TABLESAMPLE BERNOULLI (80)) c
CROSS JOIN generate_series(1, 4) o;

-- ---------- tablas pequeñas ----------
CREATE TABLE public.daily_usage (uid uuid, usage_date date, tts_count integer);
INSERT INTO public.daily_usage
SELECT u.id, current_date - (random()*60)::int, (random()*40)::int
FROM u WHERE random() < 0.15;

CREATE TABLE public.user_voices (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                                 user_id uuid, voice_id text, created_at timestamptz DEFAULT now());
INSERT INTO public.user_voices (user_id, voice_id)
SELECT id, 'v_' || n FROM u WHERE random() < 0.05;

CREATE TABLE public.hub_subscriptions (user_id uuid, product_id text, plan text,
                                       status text, updated_at timestamptz DEFAULT now());
INSERT INTO public.hub_subscriptions (user_id, product_id, plan, status)
SELECT id, 'deeprvoice', 'pro',
       (ARRAY['active','active','trialing','canceled'])[1 + (n % 4)::int]
FROM u WHERE random() < 0.04;

ANALYZE;  -- estadísticas frescas para el planificador
