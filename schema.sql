-- ============================================================
-- ATANA Agents — SQL Server Schema
-- Ejecutar UNA VEZ sobre la base de datos ATANA
-- ============================================================

USE atana
GO

-- ============================================================
-- TABLAS
-- ============================================================

-- Jobs encolados por C# o el scheduler
CREATE TABLE agent_jobs (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    provider            VARCHAR(50)  NOT NULL,
    status              VARCHAR(30)  NOT NULL DEFAULT 'pending',
    -- status: pending | running | authorized | ok | error | requires_intervention | ignored
    intervention_reason VARCHAR(200) NULL,
    error_msg           VARCHAR(500) NULL,
    attempts            INT          NOT NULL DEFAULT 0,
    max_retries         INT          NOT NULL DEFAULT 3,
    files_downloaded    INT          NOT NULL DEFAULT 0,
    period_from        DATETIME     NULL,
    period_to        DATETIME     NULL,
    requested_at        DATETIME     NOT NULL DEFAULT GETDATE(),
    started_at          DATETIME     NULL,
    finished_at         DATETIME     NULL,
    authorized_at       DATETIME     NULL,
    started_by          VARCHAR(50)  NOT NULL DEFAULT 'scheduler',
    -- started_by: scheduler | manual | atana | ui
    CONSTRAINT chk_status CHECK (status IN (
        'pending','running','authorized','ok',
        'error','requires_intervention','ignored'
    ))
)
GO

-- Archivos descargados por los agentes
CREATE TABLE downloaded_files (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    job_id              INT          NOT NULL REFERENCES agent_jobs(id),
    provider            VARCHAR(50)  NOT NULL,
    original_name       VARCHAR(300) NOT NULL,
    final_name          VARCHAR(300) NOT NULL,
    local_path          VARCHAR(500) NOT NULL,
    file_date           DATE         NULL,
    bytes               BIGINT       NOT NULL DEFAULT 0,
    sha256              VARCHAR(64)  NULL,
    downloaded_at       DATETIME     NOT NULL DEFAULT GETDATE(),
    processed_by_atana  BIT          NOT NULL DEFAULT 0
    -- processed_by_atana: C# lo pone en 1 tras importar
)
GO

-- Estado actual de cada agente (una fila por proveedor)
CREATE TABLE agent_status (
    provider            VARCHAR(50)  NOT NULL PRIMARY KEY,
    current_version     VARCHAR(20)  NULL,
    last_run            DATETIME     NULL,
    last_result         VARCHAR(20)  NULL,  -- ok | error | requires_intervention
    last_error          VARCHAR(500) NULL,
    files_today         INT          NOT NULL DEFAULT 0,
    session_active      BIT          NOT NULL DEFAULT 0,
    next_run            DATETIME     NULL,
    active              BIT          NOT NULL DEFAULT 1,
    updated_at          DATETIME     NOT NULL DEFAULT GETDATE()
)
GO

-- Configuración de agentes — una fila por agente, editable desde la plataforma Atana.
-- Las contraseñas se guardan cifradas con Fernet; la clave vive en system_config('fernet_key').
-- extra_config almacena campos específicos de cada agente como JSON:
--   mercadopago: {"accounts":[{"alias":"...","access_token_enc":"..."}], "timezone":"GMT-03", ...}
--   fiserv:      {"auth_mode":"totp", "totp_secret_enc":"...", "capture_timeout_sec":300}
CREATE TABLE agent_config (
    provider            VARCHAR(50)    NOT NULL PRIMARY KEY,
    enabled             BIT            NOT NULL DEFAULT 0,
    username            VARCHAR(200)   NULL,
    password_enc        VARBINARY(MAX) NULL,  -- Fernet-encrypted
    destination_folder  VARCHAR(500)   NULL,
    rename_pattern      VARCHAR(200)   NULL,
    max_retries         INT            NOT NULL DEFAULT 3,
    retry_interval_min  INT            NOT NULL DEFAULT 15,
    portal_url          VARCHAR(500)   NULL,
    schedule_hour       INT            NULL,  -- NULL = sin schedule automático
    schedule_minute     INT            NOT NULL DEFAULT 0,
    extra_config        NVARCHAR(MAX)  NULL,  -- JSON con campos específicos del agente
    updated_at          DATETIME       NOT NULL DEFAULT GETDATE()
)
GO

-- Configuración de la aplicación — clave/valor para SMTP, API keys, claves de cifrado.
-- Los valores con sufijo _enc están cifrados con Fernet usando 'fernet_key'.
CREATE TABLE system_config (
    key_config   VARCHAR(100)  NOT NULL PRIMARY KEY,
    value_config NVARCHAR(MAX) NOT NULL,
    updated_at   DATETIME      NOT NULL DEFAULT GETDATE()
)
GO

-- Sesiones persistidas (tokens cifrados)
CREATE TABLE session_store (
    provider            VARCHAR(50)    NOT NULL PRIMARY KEY,
    encrypted_data      VARBINARY(MAX) NOT NULL,  -- JSON cifrado con Fernet
    created_at          DATETIME       NOT NULL DEFAULT GETDATE(),
    expires_at          DATETIME       NOT NULL,
    valid               BIT            NOT NULL DEFAULT 1
)
GO


-- Orquestador: un INSERT para disparar N agentes a la vez
-- Uso: INSERT INTO orchestrator_agent_jobs (agents, started_by)
--      VALUES ('cabal,fiserv,mercadopago', 'atana')
CREATE TABLE orchestrator_agent_jobs (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    agents       VARCHAR(500)  NOT NULL,   -- CSV: 'cabal,fiserv,mercadopago'
    status       VARCHAR(20)   NOT NULL DEFAULT 'pending',
    -- status: pending | processed | error
    requested_at DATETIME      NOT NULL DEFAULT GETDATE(),
    started_at   DATETIME      NULL,
    started_by   VARCHAR(50)   NOT NULL DEFAULT 'manual',
    error_msg    VARCHAR(500)  NULL,
    CONSTRAINT chk_orch_status CHECK (status IN ('pending','processed','error'))
)
GO

-- ============================================================
-- ÍNDICES
-- ============================================================

-- agent_jobs: búsquedas por proveedor + estado (create_job, get_pending_jobs)
CREATE INDEX ix_jobs_provider_status
    ON agent_jobs (provider, status)
    INCLUDE (requested_at, started_by)
GO

-- agent_jobs: jobs de intervención activos por proveedor
CREATE INDEX ix_jobs_intervention
    ON agent_jobs (provider, status, requested_at)
    WHERE status = 'requires_intervention'
GO

-- downloaded_files: deduplicación (already_downloaded)
CREATE INDEX ix_files_provider_name
    ON downloaded_files (provider, original_name)
GO

-- downloaded_files: archivos pendientes de importar (vw_files_to_import, C#)
CREATE INDEX ix_files_pending_import
    ON downloaded_files (processed_by_atana, downloaded_at)
    INCLUDE (provider, final_name, local_path, file_date, bytes)
    WHERE processed_by_atana = 0
GO

-- downloaded_files: conteo de archivos del día (get_all_status)
CREATE INDEX ix_files_provider_date
    ON downloaded_files (provider, downloaded_at)
GO



-- ============================================================
-- DATOS INICIALES
-- ============================================================

-- Estado inicial de todos los agentes
INSERT INTO agent_status (provider, active) VALUES
    ('fiserv',      1),
    ('mercadopago', 1)
GO

-- Filas de agent_config (credenciales y configuración a completar en seed_config.sql)
INSERT INTO agent_config
    (provider, enabled, destination_folder, rename_pattern, schedule_hour, schedule_minute)
VALUES
    ('fiserv',      0, 'C:\ATANA\settlements\fiserv',      '', 8, 15),
    ('mercadopago', 0, 'C:\ATANA\settlements\mercadopago', '', 8, 30)
GO

-- Valores iniciales de system_config
-- IMPORTANTE: reemplazar las claves Fernet con valores reales antes de usar.
-- Generar con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
INSERT INTO system_config (key_config, value_config) VALUES
    ('fernet_key',                   'REPLACE_WITH_GENERATED_FERNET_KEY'),
    ('session_key',                  'REPLACE_WITH_GENERATED_FERNET_KEY'),
    ('log_dir',                      'C:\ATANA\logs'),
    ('debug',                        'false'),
    ('api_port',                     '8765'),
    ('api_key',                      'REPLACE_WITH_RANDOM_API_KEY'),
    ('check_jobs_interval_min',      '5'),
    ('check_update_interval_hours',  '6'),
    ('github_token_enc',             ''),
    ('github_owner',                 ''),
    ('github_repo',                  ''),
    ('smtp_host',                    'smtp.gmail.com'),
    ('smtp_port',                    '587'),
    ('smtp_username',                ''),
    ('smtp_password_enc',            ''),
    ('smtp_recipient',               ''),
    ('smtp_enabled',                 'false')
GO

-- ============================================================
-- VISTAS
-- ============================================================

-- Vista para C# — archivos listos para importar
CREATE VIEW vw_files_to_import AS
SELECT
    f.id,
    f.provider,
    f.final_name,
    f.local_path,
    f.file_date,
    f.bytes,
    f.downloaded_at
FROM downloaded_files f
WHERE f.processed_by_atana = 0
GO

-- Vista de estado general para el UI y la plataforma
CREATE VIEW vw_agent_status AS
SELECT
    s.provider,
    s.current_version,
    s.last_run,
    s.last_result,
    s.last_error,
    s.files_today,
    s.session_active,
    s.next_run,
    j.status               AS active_job,
    j.intervention_reason,
    j.requested_at         AS job_requested_at
FROM agent_status s
LEFT JOIN agent_jobs j ON j.provider = s.provider
    AND j.id = (
        SELECT TOP 1 id FROM agent_jobs
        WHERE provider = s.provider
        ORDER BY requested_at DESC
    )
GO

-- ============================================================
-- USUARIOS Y PERMISOS SQL SERVER
-- ============================================================
-- Ejecutar con permisos de sysadmin (sa o equivalente).
--
-- Se crean dos logins:
--   atana_svc  — cuenta del servicio Windows (lectura/escritura sobre todas las tablas)
--   atana_ro   — cuenta de solo lectura para la plataforma C# / reporting
--
-- ANTES de ejecutar: reemplazar las contraseñas por contraseñas seguras.
-- ============================================================

-- Login del servicio (usado por el dispatcher Python)
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'atana_svc')
    CREATE LOGIN atana_svc
        WITH PASSWORD    = 'REPLACE_WITH_STRONG_PASSWORD',
             CHECK_POLICY = ON,
             CHECK_EXPIRATION = OFF;
GO

CREATE USER atana_svc FOR LOGIN atana_svc;
GO

-- Permisos del servicio: escritura sobre tablas operativas
-- agent_config: INSERT/UPDATE habilitado (v3.x) — el servicio ahora escribe su
-- propia configuración, pero SOLO a través de la API interna autenticada
-- (PUT /config/agents/*, ver dispatcher/api.py — nunca acceso SQL directo sin
-- pasar por ahí). Antes era de solo lectura.
GRANT SELECT, INSERT, UPDATE ON agent_jobs              TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON downloaded_files        TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON agent_status            TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON agent_config            TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON system_config           TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON session_store           TO atana_svc;
GRANT SELECT, INSERT, UPDATE ON orchestrator_agent_jobs TO atana_svc;
GRANT SELECT ON vw_files_to_import                      TO atana_svc;
GRANT SELECT ON vw_agent_status                         TO atana_svc;
GO

-- Login de solo lectura (plataforma C# / reporting)
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'atana_ro')
    CREATE LOGIN atana_ro
        WITH PASSWORD    = 'REPLACE_WITH_STRONG_PASSWORD',
             CHECK_POLICY = ON,
             CHECK_EXPIRATION = OFF;
GO

CREATE USER atana_ro FOR LOGIN atana_ro;
GO

-- Permisos de lectura: solo las tablas que C# necesita leer y actualizar
GRANT SELECT           ON downloaded_files        TO atana_ro;
GRANT UPDATE           ON downloaded_files        TO atana_ro;  -- para marcar processed_by_atana=1
GRANT SELECT           ON agent_jobs              TO atana_ro;
GRANT SELECT           ON agent_status            TO atana_ro;
GRANT INSERT           ON orchestrator_agent_jobs TO atana_ro;  -- para disparar jobs
GRANT SELECT           ON vw_files_to_import      TO atana_ro;
GRANT SELECT           ON vw_agent_status         TO atana_ro;
GO

-- ============================================================
-- MIGRACIÓN (solo para instalaciones existentes)
-- ============================================================
-- 1. Agregar nuevas columnas a agent_jobs (v2.3)
IF NOT EXISTS (
    SELECT 1 FROM sys.columns 
    WHERE Name = N'period_from' AND Object_ID = Object_ID(N'agent_jobs')
)
BEGIN
    ALTER TABLE agent_jobs ADD
        period_from        DATETIME     NULL,
        period_to        DATETIME     NULL,
        files_downloaded    INT          NOT NULL DEFAULT 0;
END
GO

-- 2. Ampliar permisos de atana_svc en agent_config: SELECT -> SELECT/INSERT/UPDATE (v3.x)
-- Necesario para que el Panel pueda guardar configuración de agentes a través
-- de la API interna. No-op si el login no existe (instalaciones con
-- Autenticación Windows en vez de atana_svc) o si ya se aplicó.
IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'atana_svc')
BEGIN
    GRANT INSERT, UPDATE ON agent_config TO atana_svc;
END
GO
-- ============================================================
