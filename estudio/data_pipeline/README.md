# Mock pipeline de datos: deeprvoice (nivel senior)

Pipeline completo **ELT + ETL** sobre una copia falsa de la base de deeprvoice, con
guardrails de costo, data quality, idempotencia y un laboratorio de optimización con
`EXPLAIN ANALYZE`. Todo está probado en Postgres 16, y los números de abajo son mediciones
reales de ese entorno.

> **Por qué una base falsa:** con 203 filas cualquier query tarda menos de 1 ms y no se
> aprende nada. El seed genera la misma estructura con **1M de filas en `historial`, 50k
> clips con JSON pesado y 320k visemas**, con datos sesgados como los de la vida real.

---

## 1. Arquitectura

```
 APP (OLTP)                         PIPELINE                            CONSUMO
┌──────────────┐  Extract keyset ┌───────────┐  SQL   ┌──────────┐ SQL ┌─────────────┐   ┌──────────┐
│ public.*     │ ──────────────▶ │ raw.*     │ ─────▶ │ stg.*    │ ──▶ │ marts.*     │ ─▶│ Power BI │
│ auth.users   │   COPY (ELT)    │ bronze    │   T    │ silver   │     │ gold        │   │ bi_reader│
└──────────────┘                 │ append-   │        │ limpio,  │     │ fct_ / dim_ │   └──────────┘
       │                         │ only      │        │ tipado,  │     │ lead_score  │
       │   Extract + T en Python └───────────┘        │ PK/CHECK │     └─────────────┘
       └─────────────── (ETL) ───────────────────────▶└──────────┘
                                                            │
                               ops.pipeline_runs · ops.watermarks · ops.dq_results
```

| Capa | Para qué | Reglas |
|---|---|---|
| `raw` | Copia fiel de la fuente, con `_batch_id` y `_loaded_at` | Sin constraints: **nunca se pierde un dato** |
| `stg` | Limpio, tipado, deduplicado | PK, NOT NULL, CHECK. Lo que no cumple va a **cuarentena** |
| `marts` | Lo que consume el BI | **Grano declarado** ("una fila = un usuario en un día") |
| `ops` | Observabilidad | Logs, watermarks y resultados de DQ |

**¿Por qué no en `public`?** En Supabase, `public` se expone por la API REST. Un mart en
`public` con una política RLS mal puesta equivale a datos de negocio publicados en internet.

### ETL vs ELT: cuándo usar cada uno

| | ELT (`historial`) | ETL (`lip_clips`) |
|---|---|---|
| Dónde se transforma | Dentro de la BD, con SQL | En Python, antes de cargar |
| Cuándo conviene | Transformaciones relacionales: limpiar, agrupar, unir | Lógica difícil en SQL (procesar arrays de frames con numpy, llamar APIs, ML) |
| Costo | El motor procesa los datos donde viven, sin pasar por la red | Los datos viajan a Python (el JSON pesado sale por la red) |
| Reprocesar | Fácil: raw guarda todo y se re-corre el SQL | Hay que volver a extraer |

Regla senior: **ELT por defecto; ETL solo cuando la transformación lo exige.** El
experimento E4 lo demuestra: calcular `jsonb_array_length` en la BD transfiere 0.8 MB, mientras
que traer el JSON a Python transfiere 39 MB.

---

## 2. Guardrails (qué impide que el pipeline haga daño)

| Guardrail | Dónde | Qué evita |
|---|---|---|
| **Cost gate** con `EXPLAIN` (sin ANALYZE, así que no ejecuta) | `comun.py` | Correr una query cuyo costo estimado pase el límite o que haga Seq Scan sobre tablas grandes |
| `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` | Rol y sesión | Que una query cuelgue o bloquee la BD de la app |
| `default_transaction_read_only` en `etl_reader` | Rol | Que el pipeline escriba por error en las tablas de la app |
| Email excluido del GRANT (`GRANT SELECT (id, created_at)`) | Rol | Que la PII llegue a los marts |
| Lista blanca de columnas (nunca `SELECT *`) | `run.py` | Traer el JSONB/TOAST pesado sin necesidad |
| **Write-Audit-Publish**: DQ antes del COMMIT | `run.py` | Que el BI vea datos malos (si falla un check, se hace ROLLBACK) |
| Watermark actualizado en la **misma transacción** que la carga | `run.py` | Huecos o duplicados si el proceso muere a la mitad |
| `SELECT ... FOR UPDATE` sobre el watermark | `run.py` | Dos ejecuciones simultáneas que dupliquen datos |
| `ON CONFLICT ... WHERE ... IS DISTINCT FROM` | SQL | Reescribir filas idénticas (WAL, bloat y triggers inútiles) |
| Bitácora con conexión **autocommit** aparte | `run.py` | Perder el log de una corrida fallida por el ROLLBACK |
| Seed que aborta si detecta un Supabase real | `00_mock_seed.sql` | Ensuciar producción con datos falsos |

**Verificado en pruebas:**
- El cost gate bloqueó la extracción de `historial` (costo 81.828 > 20.000, Seq Scan). Con el índice `(created_at, id)` el costo bajó a 4.418 y pasó.
- Inserté un registro huérfano (user_id inexistente): el check `error` lo detectó, se hizo ROLLBACK, `stg` quedó sin el registro y el log de la falla se conservó.
- La segunda corrida releyó 3.082 filas (ventana de lookback de 5 min) y **escribió 0**: es idempotente.
- Una caída del servidor a mitad de la carga dejó `raw` vacío y el watermark intacto: es atómico.

> Discusión de diseño: con un huérfano, el pipeline queda **bloqueado** hasta que alguien
> lo arregle (*fail-closed*). La alternativa es mandarlo a cuarentena y seguir (*fail-open*).
> ¿Qué elegirías para datos financieros? ¿Y para métricas de uso?

---

## 3. Laboratorio: resultados medidos

Mediana de 3 corridas de `EXPLAIN (ANALYZE, BUFFERS)`. "Buffers" son páginas de 8 KB tocadas:
es el **gasto real de I/O**, más estable que los milisegundos.

| # | Experimento | Antes | Después | Lección |
|---|---|---|---|---|
| E1 | Keyset sin índice vs índice `(created_at, id)` | 85 ms · costo 81.828 · Seq Scan | 34 ms · costo 4.418 · Index Scan | El orden de las columnas del índice debe calzar con el `ORDER BY` |
| E2 | `OFFSET 800000` vs keyset | 94 ms · 4.767 buffers | **0.05 ms · 4 buffers** | OFFSET lee y descarta; su costo es O(offset) |
| E3 | `col::date = X` vs rango `col >= a AND col < b` | 78 ms · Seq Scan | **1.8 ms** · Index Only | Una función sobre la columna mata el índice (sargabilidad) |
| E4 | `SELECT *` vs columnas, con JSONB pesado | 1.568 ms · 39 MB | **42 ms · 1 MB** | TOAST: lo pesado vive fuera de la fila; no lo pidas si no lo usas |
| E5 | JOIN→agrupar vs agrupar→JOIN | **TIMEOUT >20 s** (131.700.929 filas intermedias) | **104 ms** | Fan-out: 1:N × 1:N multiplica filas |
| E6 | `device_type='tablet'` (3%) sin índice vs índice parcial | 13 ms · Seq Scan | 1.7 ms | Para baja cardinalidad con sesgo, el índice parcial indexa solo lo raro |
| E7 | Columnas correlacionadas sin vs con `CREATE STATISTICS` | Estimación 4.9x errada | Estimación exacta | El planner asume independencia; arreglar la estimación importa en los JOINs |
| E8 | `NOT IN` vs `NOT EXISTS` | **TIMEOUT >20 s** | 58 ms | NOT IN no cabe en work_mem y además falla con NULLs |
| E9 | Índice normal vs cubriente `INCLUDE (user_id)` | 49 ms · 13.451 buffers | 30 ms · **687 buffers** | Index Only Scan: no visita la tabla (requiere VACUUM) |
| E10 | Tamaño B-tree vs BRIN sobre `created_at` | 39 MB | **24 kB** | BRIN es ~1600x más chico; sirve si el orden físico coincide con el lógico |

Detalle fino de E1: con índice tarda menos, pero toca **más** buffers (50.245 vs 12.795),
porque el Index Scan salta por la tabla página por página. Los milisegundos y los buffers no
siempre dicen lo mismo, y saber explicar por qué es nivel senior.

---

## 4. Cardinalidad: lo que tienes que saber

- **Cardinalidad de una columna** = cuántos valores distintos tiene. Postgres la guarda en `pg_stats.n_distinct` (si es negativa, es una fracción de las filas).
- **Selectividad** = qué fracción de filas pasa un filtro. Alta selectividad (pocas filas) → índice. Baja (85% de `mobile`) → Seq Scan, y el planner tiene razón.
- **Cardinalidad estimada vs real**: si en `EXPLAIN ANALYZE` difieren más de 10x, el plan está en riesgo (elegirá un Nested Loop donde iba un Hash Join, etc.). Se arregla con `ANALYZE`, subiendo `default_statistics_target` o con `CREATE STATISTICS`.
- **Fan-out de un JOIN**: filas resultantes ≈ Σ por clave (filas_izq × filas_der). **Calcúlalo antes** (E5: 856k × 35k ⇒ 131M).
- **Grano**: antes de unir dos tablas, reduce cada una al grano del resultado.
- **Conteo aproximado**: para tablas enormes, `reltuples` en `pg_class` da un conteo gratis; `COUNT(*)` escanea todo.

`pipeline/perfilador.py` lee `pg_stats` (costo ~0, sin escanear la tabla) y recomienda qué
tipo de índice usar por columna.

---

## 5. Cómo correrlo

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://postgres@localhost:5432/deeprvoice_mock"
createdb deeprvoice_mock
psql -d deeprvoice_mock -f sql/00_mock_seed.sql      # ~20 s, ~250 MB
psql -d deeprvoice_mock -f sql/01_arquitectura.sql
python pipeline/run.py                # 1ª vez: el cost gate lo BLOQUEA (a propósito)
python pipeline/benchmark.py          # laboratorio: crea los índices que faltan
python pipeline/run.py                # ahora sí: carga completa (~25 s)
python pipeline/run.py                # incremental: ~1 s, 0 escrituras
python pipeline/run.py --dry-run      # corre todo y hace ROLLBACK
python pipeline/perfilador.py public.lip_clips public.historial
```

⚠️ **No corras el seed ni la arquitectura sobre tu Supabase de producción.** Usa Postgres
local (Docker: `docker run -e POSTGRES_HOST_AUTH_METHOD=trust -p 5432:5432 postgres:16`) o un
proyecto de Supabase aparte. Con tus datos reales solo corre queries `SELECT`.

Antes de aplicar el parser de `duracion` a los datos reales, mira qué formatos existen de verdad:
`SELECT duracion, COUNT(*) FROM historial GROUP BY 1 ORDER BY 2 DESC;`

---

## 6. Ejercicios

1. **Lee un plan:** corre `EXPLAIN (ANALYZE, BUFFERS)` de `e5_agrupar_luego_unir` y explica cada nodo de abajo hacia arriba.
2. **Rompe el gate:** baja `MAX_PLAN_COST=3000` y corre `run.py`. ¿Qué paso falla y por qué?
3. **ETL → ELT:** reescribe `etl_clips` para que `frames_reales` se calcule con `jsonb_array_length` en SQL. Mide el tiempo y los MB antes y después.
4. **Late data:** inserta un registro en `public.historial` con `created_at = now() - interval '1 hour'`. ¿El pipeline lo recoge? ¿Qué tamaño de `LOOKBACK` haría falta? ¿Cuánto cuesta agrandarlo?
5. **Nuevo check de DQ:** agrega a `03_calidad.sql` un check `warn` para usuarios con más de 1.000 registros en un día (posible bot).
6. **Cardinalidad:** sin ejecutarla, estima cuántas filas devuelve `historial JOIN lip_visemas USING (user_id)`. Después verifícalo con el patrón de E5.
7. **Particionamiento:** ¿convendría particionar `historial` por mes? ¿A partir de cuántas filas? ¿Qué pasa con el keyset y con el índice `(created_at, id)`?

---

## 7. Preguntas de entrevista (con respuesta corta)

- **¿Cómo optimizas una query lenta?** Primero mido con `EXPLAIN (ANALYZE, BUFFERS)`. Después busco: estimado vs real (estadísticas), Seq Scan sobre tablas grandes con filtros selectivos (índice), funciones sobre columnas (sargabilidad), fan-out en JOINs (agregar antes), `SELECT *` sobre columnas pesadas. Cambio una cosa a la vez y vuelvo a medir.
- **¿Por qué no indexar todo?** Cada índice hace más lento cada INSERT/UPDATE, ocupa disco y RAM, y un índice sobre una columna de baja cardinalidad casi nunca se usa. Reviso `pg_stat_user_indexes.idx_scan = 0` para borrar los que no se usan.
- **¿Qué es un pipeline idempotente?** Correrlo N veces deja el mismo resultado que correrlo una vez. Se logra con upsert por clave natural, watermark atómico y transformaciones por claves afectadas.
- **¿Carga full o incremental?** Incremental con watermark compuesto `(created_at, id)` y una ventana de lookback para datos que llegan tarde. La carga full se hace solo para backfills o cuando cambia la lógica.
- **¿Cómo garantizas calidad?** Constraints en `stg`, cuarentena, checks de reconciliación (lo que entró = lo que llegó), de integridad referencial y de frescura, y **Write-Audit-Publish**: si falla un check crítico, no se publica.
- **¿ETL o ELT?** ELT por defecto, porque el motor es más eficiente transformando donde viven los datos y raw permite reprocesar. ETL cuando la transformación necesita Python (ML, arrays, APIs).
