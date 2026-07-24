-- ============================================================
-- reset_fiserv.sql
-- ============================================================
-- Borra todas las descargas registradas de fiserv (downloaded_files)
-- y dispara un reprocesamiento del MISMO rango de fechas que ya se
-- había descargado antes (no solo "desde ayer").
--
-- QUÉ HACE:
--   1. Detecta el rango original: la fecha period_from más antigua
--      entre los jobs 'ok' de fiserv (el primer día que se descargó).
--   2. Borra todas las filas de downloaded_files de fiserv.
--      (No toca los archivos físicos en disco — solo los registros
--       en la base de datos, así vuelven a considerarse "no descargados").
--   3. Borra todo el historial de agent_jobs de fiserv.
--   4. Inserta un job "ancla" con status='ok' cuyo period_to apunta al
--      día anterior al rango original. El dispatcher calcula
--      period_from del próximo job como (period_to del último job 'ok') + 1 día
--      (ver dispatcher/db.py::get_provider_last_period_end y
--       agents/base.py::AgentBase.run) — así el próximo run vuelve a
--      pedir el rango completo original en vez de solo "ayer".
--   5. Encola un batch en orchestrator_agent_jobs para que el dispatcher
--      cree un agent_job 'pending' de fiserv en su próximo ciclo
--      (check_jobs_interval_min, default 5 min).
--
-- QUÉ NO HACE:
--   - No borra archivos del disco (destination_folder de fiserv).
--   - No requiere que el dispatcher esté corriendo para ejecutarse,
--     pero el reprocesamiento real solo arranca cuando el dispatcher
--     levante el batch de orchestrator_agent_jobs.
--
-- CÓMO EJECUTARLO:
--   sqlcmd -S <server> -d atana -i tools/reset_fiserv.sql
--   (o pegarlo en SSMS / Azure Data Studio conectado a la base 'atana')
--
-- Revisar el bloque "PREVIEW" de abajo ANTES de correr el resto —
-- muestra qué se va a borrar sin modificar nada.
-- ============================================================

USE atana
GO

-- ============================================================
-- PREVIEW (solo lectura) — correr esto primero para confirmar el impacto
-- ============================================================
-- SELECT COUNT(*) AS archivos_a_borrar FROM downloaded_files WHERE provider = 'fiserv';
-- SELECT COUNT(*) AS jobs_a_borrar     FROM agent_jobs       WHERE provider = 'fiserv';
-- SELECT MIN(CAST(period_from AS DATE)) AS rango_original_desde
--   FROM agent_jobs WHERE provider = 'fiserv' AND status = 'ok' AND period_from IS NOT NULL;
-- GO

-- ============================================================
-- RESET
-- ============================================================
BEGIN TRANSACTION

DECLARE @provider         VARCHAR(50) = 'fiserv';
DECLARE @original_from    DATE;
DECLARE @anchor_period_to DATETIME;
DECLARE @files_count      INT;
DECLARE @jobs_count       INT;

-- 1. Rango original a preservar: el period_from más antiguo de un job 'ok'
SELECT @original_from = MIN(CAST(period_from AS DATE))
FROM agent_jobs
WHERE provider = @provider
  AND status = 'ok'
  AND period_from IS NOT NULL;

IF @original_from IS NULL
BEGIN
    PRINT 'No se encontraron jobs "ok" previos de fiserv con period_from — nada que preservar. Abortando sin cambios.';
    ROLLBACK TRANSACTION;
    RETURN;
END

SELECT @files_count = COUNT(*) FROM downloaded_files WHERE provider = @provider;
SELECT @jobs_count  = COUNT(*) FROM agent_jobs        WHERE provider = @provider;

PRINT CONCAT('Rango original detectado (period_from más antiguo): ', CONVERT(VARCHAR, @original_from, 23));
PRINT CONCAT('Archivos a borrar de downloaded_files: ', @files_count);
PRINT CONCAT('Jobs a borrar de agent_jobs: ', @jobs_count);

-- 2. Borrar registros de archivos descargados (no toca el disco)
DELETE FROM downloaded_files WHERE provider = @provider;

-- 3. Borrar todo el historial de jobs
DELETE FROM agent_jobs WHERE provider = @provider;

-- 4. Job ancla: period_to = día anterior al rango original, para que
--    get_provider_last_period_end() haga que el próximo job arranque
--    exactamente en @original_from.
SET @anchor_period_to = DATEADD(DAY, -1, CAST(@original_from AS DATETIME));

INSERT INTO agent_jobs
    (provider, status, period_from, period_to,
     requested_at, started_at, finished_at, started_by, files_downloaded)
VALUES
    (@provider, 'ok', @anchor_period_to, @anchor_period_to,
     @anchor_period_to, @anchor_period_to, @anchor_period_to, 'manual-reset', 0);

-- 5. Resetear contador cosmético del día
UPDATE agent_status SET files_today = 0, updated_at = GETDATE() WHERE provider = @provider;

-- 6. Encolar el reprocesamiento — el dispatcher lo recoge en su próximo ciclo
INSERT INTO orchestrator_agent_jobs (agents, started_by)
VALUES (@provider, 'manual-reset-sql');

COMMIT TRANSACTION;

PRINT CONCAT('Listo. Downloads de fiserv borrados y reprocesamiento encolado desde ', CONVERT(VARCHAR, @original_from, 23), '.');
GO
