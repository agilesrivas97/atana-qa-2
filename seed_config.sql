-- ============================================================
-- ATANA Agentes — Seed de configuración inicial  v2.3
-- Ejecutar DESPUÉS de schema.sql
--
-- PASOS PREVIOS:
--
--   1. Generar dos claves Fernet independientes (una vez por instalación):
--      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
--      Ejecutar DOS VECES: una para fernet_key, otra para session_key.
--
--   2. Cifrar credenciales con la fernet_key generada:
--      python -c "
--      from cryptography.fernet import Fernet
--      f = Fernet(b'TU_FERNET_KEY_AQUI')
--      print(f.encrypt(b'TU_SECRETO').decode())
--      "
--
--   3. Reemplazar todos los <COMPLETAR: ...> con los valores reales.
--
--   4. password_enc es VARBINARY — usar CONVERT(VARBINARY(MAX), 'token_cifrado').
--
--   5. Habilitar agentes (enabled = 1) solo cuando las credenciales
--      estén cargadas y verificadas.
--
-- RUTAS: destination_folder usa rutas relativas al directorio de instalación.
--   Ejemplo: 'settlements\fiserv' → C:\Program Files\ATANA\settlements\fiserv
--   También se aceptan rutas absolutas si se necesita otra ubicación.
-- ============================================================

USE atana
GO

-- ============================================================
-- SYSTEM CONFIG
-- ============================================================

-- Claves de encriptación (generar con Fernet.generate_key())
UPDATE system_config SET value_config = '<COMPLETAR: clave Fernet base64>'
WHERE key_config = 'fernet_key';

UPDATE system_config SET value_config = '<COMPLETAR: clave Fernet base64 distinta a fernet_key>'
WHERE key_config = 'session_key';

-- Operación
-- log_dir: ruta relativa al directorio de instalación → C:\Program Files\ATANA\logs
--          También acepta ruta absoluta si se prefiere otro disco/carpeta.
UPDATE system_config SET value_config = 'logs'   WHERE key_config = 'log_dir';
UPDATE system_config SET value_config = 'false'  WHERE key_config = 'debug';
UPDATE system_config SET value_config = '8765'   WHERE key_config = 'api_port';
UPDATE system_config SET value_config = '5'      WHERE key_config = 'check_jobs_interval_min';

-- Auto-update desde GitHub Releases
-- github_token_enc: PAT cifrado con fernet_key (sufijo _enc → se descifra automáticamente)
UPDATE system_config SET value_config = '<COMPLETAR: PAT de GitHub cifrado con Fernet>'
WHERE key_config = 'github_token_enc';
UPDATE system_config SET value_config = 'agilesrivas97' WHERE key_config = 'github_owner';
UPDATE system_config SET value_config = 'atana-agents'  WHERE key_config = 'github_repo';
UPDATE system_config SET value_config = '6'             WHERE key_config = 'check_update_interval_hours';

-- SMTP para notificaciones de error (opcional)
UPDATE system_config SET value_config = 'smtp.gmail.com'                              WHERE key_config = 'smtp_host';
UPDATE system_config SET value_config = '587'                                         WHERE key_config = 'smtp_port';
UPDATE system_config SET value_config = '<COMPLETAR: email remitente>'                WHERE key_config = 'smtp_username';
UPDATE system_config SET value_config = '<COMPLETAR: contraseña app cifrada con Fernet>' WHERE key_config = 'smtp_password_enc';
UPDATE system_config SET value_config = '<COMPLETAR: email destinatario>'             WHERE key_config = 'smtp_recipient';
UPDATE system_config SET value_config = 'false'                                       WHERE key_config = 'smtp_enabled';

GO

-- ============================================================
-- AGENT CONFIG
-- ============================================================
-- destination_folder: rutas relativas al directorio de instalación
--   → C:\Program Files\ATANA\settlements\<proveedor>
-- password_enc: CONVERT(VARBINARY(MAX), 'token_Fernet_cifrado')
-- extra_config: JSON con configuración específica de cada agente
-- ============================================================

-- ── Prisma ────────────────────────────────────────────────────────────────────
-- Autenticación: usuario + contraseña (portal web via Playwright)
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario Prisma>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\prisma',
    rename_pattern     = '',
    max_retries        = 3,
    retry_interval_min = 15,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 0,
    extra_config       = NULL
WHERE provider = 'prisma';

-- ── Cabal ─────────────────────────────────────────────────────────────────────
-- Autenticación: usuario + contraseña (portal web via Playwright)
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario Cabal>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\cabal',
    rename_pattern     = '',
    max_retries        = 3,
    retry_interval_min = 15,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 5,
    extra_config       = NULL
WHERE provider = 'cabal';

-- ── NaranjaX ──────────────────────────────────────────────────────────────────
-- Autenticación: usuario + contraseña + OTP por email (IMAP)
-- imap_password_enc: app password de Gmail cifrada con Fernet
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario NaranjaX>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\naranjax',
    rename_pattern     = '',
    max_retries        = 2,
    retry_interval_min = 30,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 10,
    extra_config       = N'{
        "imap_host":         "imap.gmail.com",
        "imap_username":     "<COMPLETAR: email para recibir OTP>",
        "imap_password_enc": "<COMPLETAR: app password Gmail cifrada con Fernet>",
        "otp_sender":        "noreply@naranjax.com"
    }'
WHERE provider = 'naranjax';

-- ── Fiserv ────────────────────────────────────────────────────────────────────
-- Autenticación: TOTP automático (sin intervención humana)
--
-- Endpoints (confirmados 2026-04-16):
--   POST /api/Users/requestOtp           → obtiene totpToken
--   POST /api/Users/authenticate         → obtiene JWT (válido ~8h)
--   POST /settlement/Settlement/SettlementFileList
--   POST /settlement/Settlement/downloadUploadedFile
--
-- totp_secret_enc: shared secret TOTP (el que se escanea con un autenticador)
--   cifrado con Fernet. Sufijo _enc → se descifra automáticamente al cargar.
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario Fiserv>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\fiserv',
    rename_pattern     = '',
    max_retries        = 2,
    retry_interval_min = 30,
    portal_url         = 'https://merchantcenter.fiservapp.com',
    schedule_hour      = 8,
    schedule_minute    = 15,
    extra_config       = N'{
        "auth_mode":           "totp",
        "totp_secret_enc":     "<COMPLETAR: shared secret TOTP cifrado con Fernet>"
    }'
WHERE provider = 'fiserv';

-- ── Amex ──────────────────────────────────────────────────────────────────────
-- Autenticación: usuario + contraseña (portal web via Playwright)
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario Amex>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\amex',
    rename_pattern     = '',
    max_retries        = 3,
    retry_interval_min = 10,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 20,
    extra_config       = NULL
WHERE provider = 'amex';

-- ── Getnet ────────────────────────────────────────────────────────────────────
-- Autenticación: sesión Playwright persistida + resolución CAPTCHA (2captcha)
-- captcha_api_key: API key de 2captcha.com (sin cifrar — no es una credencial de acceso)
UPDATE agent_config SET
    enabled            = 0,
    username           = '<COMPLETAR: usuario Getnet>',
    password_enc       = CONVERT(VARBINARY(MAX), '<COMPLETAR: contraseña cifrada con Fernet>'),
    destination_folder = 'settlements\getnet',
    rename_pattern     = '',
    max_retries        = 1,
    retry_interval_min = 0,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 25,
    extra_config       = N'{
        "captcha_api_key": "<COMPLETAR: API key de 2captcha>",
        "mode":            "persisted_session"
    }'
WHERE provider = 'getnet';

-- ── MercadoPago ───────────────────────────────────────────────────────────────
-- Autenticación: access token por cuenta (API REST — sin browser)
-- Agregar un objeto por cada cuenta adicional en el array "accounts".
-- access_token_enc: access token cifrado con Fernet
UPDATE agent_config SET
    enabled            = 0,
    username           = NULL,
    password_enc       = NULL,
    destination_folder = 'settlements\mercadopago',
    rename_pattern     = '',
    max_retries        = 5,
    retry_interval_min = 2,
    portal_url         = NULL,
    schedule_hour      = 8,
    schedule_minute    = 30,
    extra_config       = N'{
        "accounts": [
            {
                "alias":            "<COMPLETAR: nombre descriptivo de la cuenta>",
                "access_token_enc": "<COMPLETAR: access token cifrado con Fernet>"
            }
        ],
        "timezone":          "GMT-03",
        "separator":         ";",
        "poll_interval_seg": 2,
        "poll_timeout_seg":  300
    }'
WHERE provider = 'mercadopago';

GO

-- ============================================================
-- HABILITAR AGENTES
-- ============================================================
-- Ejecutar solo cuando las credenciales del agente estén cargadas y verificadas.
--
-- Habilitar todos de una vez:
-- UPDATE agent_config SET enabled = 1
-- WHERE provider IN ('prisma','cabal','naranjax','fiserv','amex','getnet','mercadopago');
--
-- O uno por uno:
-- UPDATE agent_config SET enabled = 1 WHERE provider = 'fiserv';

-- ============================================================
-- VERIFICACIÓN
-- ============================================================
-- SELECT
--     provider, enabled, username,
--     CASE WHEN password_enc IS NOT NULL THEN 'OK' ELSE 'FALTA' END AS pwd,
--     destination_folder, schedule_hour, schedule_minute,
--     extra_config
-- FROM agent_config
-- ORDER BY provider;
--
-- SELECT key_config, value_config FROM system_config ORDER BY key_config;
