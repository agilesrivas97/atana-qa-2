# ATANA Agents

Sistema de descarga automática de liquidaciones para múltiples portales financieros.
Corre como servicio Windows en la máquina del cliente, descarga los archivos de liquidación
de cada proveedor según el horario configurado y los deja disponibles para que el sistema
contable (ATANA) los importe.

---

## Índice

1. [Arquitectura](#arquitectura)
2. [Desarrollo](#desarrollo)
3. [Producción — Instalación en cliente](#producción--instalación-en-cliente)
4. [Base de datos — Tablas](#base-de-datos--tablas)
5. [Build — Generar el instalador](#build--generar-el-instalador)
6. [Operación y mantenimiento](#operación-y-mantenimiento)
7. [Estado de agentes](#estado-de-agentes)

---

## Arquitectura

```
atana-agents/
├── dispatcher/
│   ├── main.py          ← Entry point: scheduler, modos CLI, startup
│   ├── db.py            ← Toda la capa de acceso a SQL Server
│   ├── api.py           ← API HTTP interna en localhost:8765
│   ├── agent_loader.py  ← Registro estático de agentes disponibles
│   ├── autoupdater.py   ← Auto-update del exe desde GitHub Releases
│   ├── notifier.py      ← Notificaciones SMTP consolidadas
│   └── version.py       ← Versión embebida en el exe (generada por build.py)
├── agents/
│   ├── base.py          ← Clase base: retry, deduplicación, logging
│   ├── fiserv.py        ← TOTP automático vía REST API
│   ├── mercadopago.py   ← REST API + polling de reportes
├── shared/
│   ├── session_store.py ← Sesiones cifradas (Fernet) en SQL Server
│   ├── file_manager.py  ← Renombrado de archivos con patrones
│   └── paths.py         ← BASE_DIR y CONFIG_FILE resueltos en exe o dev
├── ui/
│   ├── tray.py          ← Ícono de bandeja (pystray + Pillow)
│   └── setup_db.py      ← Wizard de configuración inicial (tkinter)
├── build/
│   └── build.py         ← PyInstaller → atana_dispatcher.exe
├── installer/
│   ├── atana_setup.iss  ← Inno Setup — instalador del cliente
│   └── nssm.exe         ← NSSM service wrapper (x64)
├── schema.sql           ← DDL: tablas, índices, usuarios, vistas
├── config.example.json  ← Template de config.json para el cliente
└── requirements.txt
```

### Flujo de ejecución

```
Windows Service (NSSM)
  └─ atana_dispatcher.exe [service mode]
       ├─ APScheduler — cron por agente (08:00, 08:05, ...)
       │    └─ db.create_job(provider)         → INSERT agent_jobs
       ├─ Ciclo cada 5 min — db.get_pending_jobs()
       │    └─ run_provider(provider, job_id)
       │         ├─ agent.login()
       │         ├─ agent.list_files()
       │         ├─ agent.download(item)       → archivo en destination_folder
       │         └─ db.insert_file(...)        → INSERT downloaded_files
       └─ API HTTP localhost:8765
            ├─ GET  /health
            ├─ GET  /status           (requiere X-API-Key)
            ├─ POST /jobs/{provider}  (requiere X-API-Key)
            ├─ POST /jobs/{provider}  (requiere X-API-Key)
            └─ POST /jobs/all (requiere X-API-Key)

atana_dispatcher.exe [tray mode]  ← proceso separado, sesión del usuario
  └─ Lee /status cada 30s y muestra el estado en la bandeja del sistema
```

### Criptografía y Seguridad (Fernet)

El sistema utiliza el estándar **Fernet** (provisto por la librería `cryptography`) para proteger cualquier dato sensible en reposo dentro de la base de datos (contraseñas, tokens TOTP, y sesiones de navegador).

- **Estándar**: Fernet es una implementación de criptografía simétrica autenticada. Utiliza **AES en modo CBC con una clave de 128 bits** para el cifrado y **HMAC con SHA256** para la autenticación del mensaje.
- **Claves Maestras**: Se manejan dos claves independientes guardadas en la tabla `system_config`:
  - `fernet_key`: Cifra las credenciales fijas de los agentes (ej: `password_enc`, `totp_secret_enc`).
  - `session_key`: Cifra los payloads dinámicos de las sesiones de navegador en `session_store` (cookies, JWTs), lo cual mitiga el riesgo de Session Hijacking.
- **Transparencia**: Los sufijos `_enc` en los campos de la base de datos le indican a la capa de abstracción de datos (`db.py`) que debe descifrar el valor al vuelo antes de entregarlo al agente en memoria. El texto plano nunca se loguea ni persiste en disco.

### Arquitectura de la API Interna

El dispatcher levanta un servidor HTTP liviano (`http.server.HTTPServer`) en un hilo en segundo plano, independiente del ciclo del orquestador de tareas.

- **Seguridad y Binding Local**: Por defecto y por diseño de seguridad, la API **sólo escucha en `localhost`** (127.0.0.1). Esto aísla la API de la red externa y previene ataques laterales en la red del cliente.
- **Autorización**: Todos los endpoints (excepto `/health`) requieren el header `X-API-Key` cuyo valor debe coincidir con el `api_key` de la tabla `system_config`.

#### Exponer la API en un Servidor (Red Externa)

Si la aplicación principal de ATANA necesita orquestar los agentes desde un servidor externo, **no debes modificar el código del dispatcher para escuchar en `0.0.0.0`**, ya que el protocolo nativo HTTP expondría el `X-API-Key` en texto plano por la red.

La manera estándar y profesional de exponerlo es utilizando un **Reverse Proxy (Nginx, IIS, o Caddy)** en la misma máquina Windows donde corre el servicio:

1. El dispatcher sigue escuchando privadamente en `http://127.0.0.1:8765`.
2. Instalas y configuras el proxy inverso para escuchar en el puerto `443` (HTTPS) de la interfaz de red pública de la máquina, atado a un certificado SSL válido.
3. Configuras el proxy para redirigir (*proxy_pass*) todo el tráfico entrante hacia `127.0.0.1:8765`.
4. De esta manera, tu servidor ATANA externo hace llamadas cifradas (HTTPS) al proxy de la máquina del cliente, y el proxy entrega la petición al dispatcher localmente de forma segura.

---

## Desarrollo

### Requisitos

- Python 3.11 o superior
- SQL Server (local, Express, Docker o Azure SQL)
- ODBC Driver 18 for SQL Server
- Git

### 1. Clonar e instalar dependencias

```bash
git clone <url-del-repo>
cd atana-agents

python -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows

pip install -r requirements.txt
playwright install chromium
```

### 2. Crear la base de datos

En SQL Server Management Studio o `sqlcmd`, ejecutar `schema.sql`:

```bash
sqlcmd -S localhost -U sa -P <password> -i schema.sql
```

### 3. Configurar `config.json`

Copiar el template y completar con las credenciales de BD:

```bash
cp config.example.json config.json
```

```json
{
  "database": {
    "server": "localhost,1433",
    "database": "atana",
    "trusted_connection": false,
    "trust_server_certificate": true,
    "driver": "ODBC Driver 18 for SQL Server",
    "username": "atana_svc",
    "password": "TU_PASSWORD_AQUI"
  }
}
```

> `trust_server_certificate: true` es necesario en desarrollo cuando SQL Server usa
> un certificado autofirmado. En producción con certificado válido, poner `false`.

### 4. Generar claves Fernet

El sistema usa dos claves Fernet independientes para cifrar credenciales y sesiones:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Ejecutar dos veces: una para fernet_key, otra para session_key
```

### 5. Cifrar credenciales de agentes

Las contraseñas de los portales se guardan cifradas. Para cifrar una contraseña:

```python
from cryptography.fernet import Fernet

fernet_key = b"TU_FERNET_KEY_AQUI"  # la misma que pusiste en system_config
password   = b"contraseña_del_portal"

token = Fernet(fernet_key).encrypt(password)
print(token.decode())  # este valor va en password_enc o extra_config._enc
```

Luego actualizar `agent_config` en la BD:

```sql
-- password_enc es VARBINARY — convertir desde el token cifrado
UPDATE agent_config
SET    password_enc = CONVERT(VARBINARY(MAX), '<token-fernet>'),
       enabled      = 1
WHERE  provider = 'fiserv';
```

### 6. Ejecutar en desarrollo

```bash
# Modo normal — scheduler + API (sin servicio Windows)
python dispatcher/main.py

# Ejecutar un agente específico ahora mismo
python dispatcher/main.py --run --provider fiserv
Las instrucciones detalladas para preparar el entorno de programación, levantar la base de datos SQL Server y correr simulaciones individuales de los agentes, se encuentran ahora en la guía exclusiva del desarrollador.

👉 **[Ver el Manual de Desarrollo (README_DEV.md)](./README_DEV.md)**

---

## Producción — Instalación en cliente

Para entender a fondo las topologías de autenticación que soporta ATANA de forma nativa frente a Microsoft SQL Server (Autenticación SQL vs Trusted Connection/Windows OS) junto con el aprovisionamiento como servicio Windows usando NSSM.

👉 **[Ver el Manual de Producción (README_PROD.md)](./README_PROD.md)**

---

### Actualización de versión

Ejecutar el nuevo instalador sobre la instalación existente **como Administrador**.
El instalador detecta el servicio ya instalado y hace automáticamente:
1. Detiene el servicio
2. Cierra el tray viejo
3. Reemplaza `atana_dispatcher.exe`
4. Re-aplica la configuración de NSSM
5. Inicia el servicio y lanza el nuevo tray

`config.json` nunca se toca en un upgrade.

Si el auto-update está habilitado (`github_token_enc` configurado en la BD),
el servicio descarga e instala nuevas versiones automáticamente sin intervención.

### Desinstalación

Usar Agregar/Quitar Programas, o manualmente:

```powershell
& "C:\Program Files\ATANA\nssm.exe" stop   AtanaDispatcher
& "C:\Program Files\ATANA\nssm.exe" remove AtanaDispatcher confirm
Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" "AtanaTray" -EA SilentlyContinue
Remove-Item "C:\Program Files\ATANA" -Recurse -Force
```

---

## Base de datos — Tablas

### `agent_jobs` — Cola de trabajos

Cada vez que el scheduler decide que un agente debe correr, se inserta una fila aquí.
El dispatcher lee esta tabla periódicamente y ejecuta los jobs pendientes.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Identificador del job |
| `provider` | VARCHAR(50) | Nombre del agente: `fiserv`, `mercadopago`, etc. |
| `status` | VARCHAR(30) | Estado actual del job (ver abajo) |
| `intervention_reason` | VARCHAR(200) | Por qué el agente necesita intervención manual |
| `error_msg` | VARCHAR(500) | Último mensaje de error si `status = 'error'` |
| `attempts` | INT | Cantidad de intentos realizados |
| `max_retries` | INT | Máximo de intentos antes de marcar como error |
| `requested_at` | DATETIME | Cuándo fue encolado el job |
| `started_at` | DATETIME | Cuándo comenzó a ejecutarse |
| `finished_at` | DATETIME | Cuándo terminó (éxito o error) |
| `authorized_at` | DATETIME | Cuándo el usuario hizo click en Play |
| `started_by` | VARCHAR(50) | Origen: `scheduler`, `api`, `run`, `batch:N` |

**Estados posibles de `status`:**

| Estado | Descripción |
|---|---|
| `pending` | En cola, esperando ser procesado |
| `running` | En ejecución actualmente |
| `authorized` | El usuario hizo Play — listo para ejecutar |
| `ok` | Completado exitosamente |
| `error` | Falló después de agotar los reintentos |
| `requires_intervention` | Necesita que el usuario haga login manualmente |
| `ignored` | El usuario eligió ignorar la intervención |

---

### `downloaded_files` — Archivos descargados

Registro permanente de cada archivo descargado. Sirve como mecanismo de deduplicación:
antes de descargar un archivo, el agente verifica que `original_name` no esté ya en esta tabla.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Identificador del archivo |
| `job_id` | INT FK | Job que generó la descarga |
| `provider` | VARCHAR(50) | Agente que descargó el archivo |
| `original_name` | VARCHAR(300) | Nombre original del archivo en el portal (usado para deduplicar) |
| `final_name` | VARCHAR(300) | Nombre final en disco (puede estar renombrado por `rename_pattern`) |
| `local_path` | VARCHAR(500) | Path completo en disco |
| `file_date` | DATE | Fecha extraída del nombre del archivo (si aplica) |
| `bytes` | BIGINT | Tamaño en bytes |
| `sha256` | VARCHAR(64) | Hash SHA-256 del contenido |
| `downloaded_at` | DATETIME | Cuándo fue descargado |
| `processed_by_atana` | BIT | `0` = pendiente de importar por el sistema contable, `1` = ya importado |

La vista `vw_files_to_import` filtra solo los archivos con `processed_by_atana = 0`,
que es lo que el sistema ATANA consulta para saber qué importar.

---

### `agent_status` — Estado actual de cada agente

Una fila por agente. Se actualiza al finalizar cada job. El tray y el panel de admin
leen esta tabla para mostrar el estado en tiempo real.

| Columna | Tipo | Descripción |
|---|---|---|
| `provider` | VARCHAR(50) PK | Nombre del agente |
| `current_version` | VARCHAR(20) | Versión del agente (para reportes) |
| `last_run` | DATETIME | Cuándo corrió por última vez |
| `last_result` | VARCHAR(20) | `ok`, `error` o `requires_intervention` |
| `last_error` | VARCHAR(500) | Mensaje del último error |
| `files_today` | INT | Archivos descargados hoy |
| `session_active` | BIT | Si hay una sesión de browser activa y válida |
| `next_run` | DATETIME | Próxima ejecución programada |
| `active` | BIT | Si el agente está habilitado en el sistema |
| `updated_at` | DATETIME | Última actualización de esta fila |

---

### `agent_config` — Configuración de cada agente

Una fila por agente. Contiene credenciales, carpeta de destino, horario y configuración
específica de cada portal. Las contraseñas se guardan cifradas con Fernet.

| Columna | Tipo | Descripción |
|---|---|---|
| `provider` | VARCHAR(50) PK | Nombre del agente |
| `enabled` | BIT | `1` = el agente corre en el horario programado |
| `username` | VARCHAR(200) | Usuario del portal (en texto plano — no es una contraseña) |
| `password_enc` | VARBINARY(MAX) | Contraseña cifrada con la `fernet_key` de `system_config` |
| `destination_folder` | VARCHAR(500) | Carpeta local donde se guardan los archivos descargados (path absoluto) |
| `rename_pattern` | VARCHAR(200) | Patrón de renombrado: `{original}`, `{date}`, `{merchant}`, etc. |
| `max_retries` | INT | Intentos máximos antes de marcar el job como error |
| `retry_interval_min` | INT | Minutos de espera entre reintentos |
| `portal_url` | VARCHAR(500) | URL del portal (referencia, algunos agentes la usan) |
| `schedule_hour` | INT | Hora de ejecución diaria (0–23). `NULL` = sin schedule |
| `schedule_minute` | INT | Minuto de ejecución (0–59, default 0) |
| `extra_config` | NVARCHAR(MAX) | JSON con campos específicos del agente (ver abajo) |
| `updated_at` | DATETIME | Última modificación |

**`extra_config` por agente:**

| Agente | Campos en `extra_config` |
|---|---|
| `fiserv` | `auth_mode`, `totp_secret_enc`, `days_back` |
| `naranjax` | `imap_host`, `imap_username`, `imap_password_enc`, `otp_sender` |
| `mercadopago` | `accounts` (array con `alias`, `access_token_enc`), `days_back`, `timezone`, `separator` |
| `getnet` | `captcha_api_key`, `mode` |

Los campos con sufijo `_enc` dentro de `extra_config` son automáticamente
descifrados por `db.py` antes de pasarlos al agente.

---

### `system_config` — Configuración global de la aplicación

Tabla clave-valor para configuración que aplica a todo el sistema.
Los valores con sufijo `_enc` en el nombre de clave se cifran con Fernet.

| Clave | Descripción |
|---|---|
| `fernet_key` | Clave maestra Fernet para cifrar credenciales de portales |
| `session_key` | Clave Fernet para cifrar sesiones de browser en `session_store` |
| `api_key` | Clave para autenticar llamadas a la API interna (localhost:8765) |
| `log_dir` | Directorio donde se escriben los logs (ej: `C:\ATANA\logs`) |
| `debug` | `true` / `false` — activa logs DEBUG en el dispatcher |
| `api_port` | Puerto de la API interna (default: `8765`) |
| `check_jobs_interval_min` | Cada cuántos minutos el dispatcher chequea jobs pendientes (default: `5`) |
| `check_update_interval_hours` | Cada cuántas horas se chequea GitHub por actualizaciones (default: `6`, `0` = desactivado) |
| `github_token_enc` | Personal Access Token de GitHub cifrado — para descargar releases privados |
| `github_owner` | Usuario u organización de GitHub donde están los releases |
| `github_repo` | Nombre del repositorio de releases |
| `smtp_host` | Servidor SMTP para notificaciones (ej: `smtp.gmail.com`) |
| `smtp_port` | Puerto SMTP (ej: `587`) |
| `smtp_username` | Email remitente |
| `smtp_password_enc` | Contraseña del email cifrada con Fernet |
| `smtp_recipient` | Email destino para notificaciones de error e intervención |
| `smtp_enabled` | `true` / `false` — activa el envío de emails |

---

### `session_store` — Sesiones de browser cifradas

Tokens de sesión (JWTs, cookies de Playwright) persistidos entre ejecuciones.
Cada proveedor tiene a lo sumo una sesión activa.
El contenido está cifrado con la `session_key` de `system_config`.

| Columna | Tipo | Descripción |
|---|---|---|
| `provider` | VARCHAR(50) PK | Nombre del agente |
| `encrypted_data` | VARBINARY(MAX) | JSON cifrado con Fernet (contiene JWT, cookies, etc.) |
| `created_at` | DATETIME | Cuándo se guardó la sesión |
| `expires_at` | DATETIME | Cuándo vence — el dispatcher no reutiliza sesiones vencidas |
| `valid` | BIT | `0` = sesión invalidada manualmente (ej: 401 del portal) |

---

### `orchestrator_agent_jobs` — Jobs de orquestador

Permite que el sistema ATANA (C#) dispare múltiples agentes con un solo INSERT,
sin necesidad de hacer N llamadas a la API.

```sql
-- Disparar fiserv + mercadopago + cabal de una vez
INSERT INTO orchestrator_agent_jobs (agents, started_by)
VALUES ('fiserv,mercadopago,cabal', 'atana');
```

El dispatcher lee esta tabla en cada ciclo, crea los jobs individuales en
`agent_jobs` y marca el batch como procesado.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | INT PK | Identificador del batch |
| `agents` | VARCHAR(500) | Lista CSV de proveedores a ejecutar |
| `status` | VARCHAR(20) | `pending`, `processed`, `error` |
| `requested_at` | DATETIME | Cuándo fue creado |
| `started_at` | DATETIME | Cuándo el dispatcher lo procesó |
| `started_by` | VARCHAR(50) | Origen del batch (ej: `atana`, `manual`) |
| `error_msg` | VARCHAR(500) | Error si algún provider del batch era desconocido |

---

### Vistas

**`vw_files_to_import`** — Archivos descargados pendientes de importar.
El sistema ATANA la consulta para saber qué archivos procesar.
Filtra `processed_by_atana = 0`.

**`vw_agent_status`** — Estado completo de cada agente cruzado con el último job.
Útil para dashboards y monitoreo desde C#.

---



### Orden de ejecución recomendado

```
1. schema.sql          → crea tablas, índices, usuarios y filas vacías
```

**SMTP — notificaciones de error:**
El sistema envía un email cuando un agente falla o necesita intervención.
La contraseña se guarda cifrada:
```sql
UPDATE system_config SET value_config = '<token-fernet>' WHERE key_config = 'smtp_password_enc';
```

**Auto-update desde GitHub:**
Si se completa `github_token_enc`, `github_owner` y `github_repo`, el servicio
se actualiza automáticamente al detectar una nueva release.
El token debe tener permisos `repo` (para repos privados) o `public_repo`.
```sql
UPDATE system_config SET value_config = '<ghp_... cifrado>' WHERE key_config = 'github_token_enc';
```
Si se deja en blanco, el auto-update está desactivado.

**Configuración de cada agente:**
Cada agente tiene un bloque UPDATE en `agent_config`. Los campos principales:
- `enabled = 0` — no activar hasta que las credenciales estén verificadas
- `username` — usuario del portal (texto plano)
- `password_enc` — contraseña cifrada con Fernet (cargar con un UPDATE separado)
- `destination_folder` — path absoluto donde se guardan los archivos descargados
- `schedule_hour` / `schedule_minute` — hora de ejecución diaria

Habilitar un agente recién cuando sus credenciales estén verificadas:
```sql
UPDATE agent_config SET enabled = 1 WHERE provider = 'fiserv';
```

### Cifrar una contraseña paso a paso

```python
# 1. Obtener la fernet_key de la BD
fernet_key = b"TU_FERNET_KEY_AQUI"

# 2. Cifrar la contraseña
from cryptography.fernet import Fernet
token = Fernet(fernet_key).encrypt(b"contraseña-del-portal")
print(token.decode())
# Ejemplo de salida: gAAAAABl...== (cadena base64)

# 3. Cargar en la BD
# UPDATE agent_config
# SET password_enc = CONVERT(VARBINARY(MAX), 'gAAAAABl...==')
# WHERE provider = 'fiserv';
```

---

## Build — Generar el instalador

El proceso de build es exclusivo del equipo ATANA. Los clientes reciben el `.exe`
del instalador compilado.

### Integración Continua (GitHub Actions)

El proyecto utiliza GitHub Actions para automatizar las compilaciones.

#### 1. Build Tools (`build-tools.yml`)
Se activa automáticamente al hacer **push a la rama `main`** si hay cambios en los archivos de `tools/`.
- Configura Python 3.12 en un runner de Windows.
- Instala dependencias (`pyinstaller`, `pyodbc`, `cryptography`, etc.).
- Compila las herramientas satélites: `atana_setup` (Wizard DB CLI) y el extractor de TOTP.
- Hace un **commit automático** con los nuevos binarios (`.exe`) generados dentro de `tools/dist/` y los pushea al repositorio para que estén siempre actualizados y listos para usar en producción.

#### 2. Build & Release del Dispatcher (`build.yml`)
Se activa exclusivamente cuando se hace **push de un tag** con versión (ej: `v3.1.0`).
- Configura Python 3.12 e instala Playwright con Chromium.
- Compila `atana_dispatcher.exe` inyectando la versión mediante `build/build.py`.
- Descarga y valida el envoltorio de servicios `nssm.exe`.
- Compila el instalador gráfico final usando **Inno Setup** (`atana_setup.iss`).
- Publica un **Release Oficial en GitHub** subiendo automáticamente los assets: el instalador cliente (`AtanaAgents_Setup_v*.exe`), el ejecutable solo (`atana_dispatcher.exe`) y el metadata de actualización (`build_info.json`).

---

### Build Local (Desarrollo)

Si necesitas compilar manualmente en tu entorno local:

#### Requisitos de build

- Windows (para generar el `.exe` de Windows)
- Python 3.11+ con el entorno virtual activo
- Inno Setup 6 instalado en `C:\Program Files (x86)\Inno Setup 6\`
- `nssm.exe` (x64) en `installer/`

#### Pasos

```bash
# 1. Activar entorno virtual
venv\Scripts\activate

# 2. Construir el exe (incluye todos los agentes dentro)
python build/build.py --version 3.1.0

# Output:
#   dist/exe/atana_dispatcher.exe   ← ejecutable
#   dist/exe/build_info.json        ← versión y SHA256 del exe

# 3. Compilar el instalador Inno Setup
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\atana_setup.iss /DMyAppVersion=3.1.0

# Output:
#   installer\Output\AtanaAgents_Setup_v3.1.0.exe
```

### Publicar una nueva release manual

```bash
# Crear tag en git
git tag v3.1.0
git push origin v3.1.0

# (Si la Action de Release falla, puedes subir manualmente los archivos)
```

> El auto-updater verifica el SHA256 del exe contra `build_info.json` antes de
> aplicar la actualización. Sin `build_info.json` en el release, el update es rechazado.

---

## Operación y mantenimiento

### Logs disponibles

| Archivo | Contenido |
|---|---|
| `logs/dispatcher_YYYY-MM-DD.log` | Log principal del servicio (rotación diaria, retención 30 días) |
| `logs/service.log` | Stdout del servicio capturado por NSSM (igual que el anterior) |
| `logs/service_err.log` | Stderr del servicio — errores de Python no capturados por loguru |
| `logs/tray_YYYY-MM-DD.log` | Log del proceso de bandeja |
| `logs/setup_db.log` | Log del wizard de configuración inicial |
| `%TEMP%\atana_update_*.ps1` (transitorio) | Script PowerShell de auto-update (se auto-elimina) |
| `%TEMP%\atana_update.log` | Log del script de auto-update |

### Intervención manual (ícono amarillo)

Cuando un agente no puede hacer login automáticamente (sesión expirada, 2FA, etc.):

1. El ícono de bandeja se pone **amarillo**
2. Click derecho → **▶ Play PROVEEDOR**
3. Se abre Chromium con el portal para hacer login manual
4. Al completar el login, la sesión queda guardada en `session_store`
5. El dispatcher retoma el job en el siguiente ciclo (máx. 5 min)

Para ignorar la intervención (ej: portal caído ese día):
Click derecho → **✗ Ignorar PROVEEDOR**

### Disparar un agente manualmente desde la API

```powershell
$headers = @{ "X-API-Key" = "TU_API_KEY" }

# Disparar fiserv ahora
Invoke-WebRequest http://localhost:8765/jobs/fiserv -Method POST -Headers $headers

# Ver estado de todos los agentes
Invoke-WebRequest http://localhost:8765/status -Headers $headers
```

### Disparar agentes desde SQL (integración C#/ATANA)

```sql
-- Disparar un agente específico
INSERT INTO orchestrator_agent_jobs (agents, started_by)
VALUES ('fiserv', 'atana');

-- Disparar varios agentes a la vez
INSERT INTO orchestrator_agent_jobs (agents, started_by)
VALUES ('fiserv,mercadopago,cabal', 'atana');
```

### Jobs stuck en estado 'running'

Si el servicio crashea mientras ejecuta un job, el job queda en `running`.
El dispatcher los resetea a `pending` automáticamente al arrancar:

```sql
-- Si se necesita hacer a mano:
UPDATE agent_jobs SET status = 'pending' WHERE status = 'running';
```

---

## Estado de agentes

| Agente | Estado | Tecnología de login |
|---|---|---|
| Fiserv | Completo | TOTP automático (REST API, sin browser) |
| MercadoPago | Completo | Access token por cuenta (REST API) |
