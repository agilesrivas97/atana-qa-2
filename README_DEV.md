# ATANA Agents — Manual de Desarrollo

Este documento es una guía destinada a programadores que desean mantener o expandir la infraestructura de `atana-agents`.

---

## 1. Stack Tecnológico
- **Lenguaje:** Python 3.12
- **BD Motor:** SQL Server (On-Premises, Azure, o local vía Docker)
- **ODBC:** ODBC Driver 18 for SQL Server (requerido para las interfaces de conexión)
- **Frameworks de Agentes:**
  - `httpx` / `curl_cffi`: Llamadas REST API directas
  - `playwright` (Chromium): Automatización e interactividad en portales sin API nativa
- **Orquestador y Schedulers:** `APScheduler`
- **Audio reCAPTCHA:** `openai-whisper` (offline, primario) + `SpeechRecognition` (Google Speech, fallback) — usan `ffmpeg` para convertir el audio del captcha de MP3 a WAV antes de transcribirlo. `ffmpeg` va empaquetado dentro del `.exe` y no requiere instalación en el cliente.

## 2. Inicializar entorno de desarrollo en Mac (recomendado)

El script `dev_setup.sh` automatiza todo el setup desde cero: verifica prerequisitos, levanta SQL Server en Docker, carga el schema, crea el venv con Python 3.12, instala dependencias y corre el wizard de configuración de Fiserv.

```bash
chmod +x dev_setup.sh && ./dev_setup.sh
```

Al finalizar, el script muestra el comando exacto para correr el agente.

### Setup manual (alternativa)

Clona el repositorio y crea el ambiente virtual con **Python 3.12** (no 3.13/3.14 — las dependencias nativas aún no tienen wheels para esas versiones):

```bash
git clone <url-del-repo>
cd atana-agents

# Mac
python3.12 -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
.venv\Scripts\activate
```

Instala dependencias y navegadores:

```bash
pip install -r requirements.txt
pip install openai-whisper          # recomendado: reCAPTCHA offline sin API key
playwright install chromium
```

## 3. Base de Datos en Modo Desarrollo

Levanta SQL Server vía Docker:

```bash
docker run -e "ACCEPT_EULA=Y" -e "SA_PASSWORD=AtanaDev1234!" \
           -p 1433:1433 --name atana-sql -d \
           mcr.microsoft.com/mssql/server:2022-latest
```

Una vez corriendo, puedes usar el Wizard Gráfico para inicializar la DB, generar claves Fernet y crear `config.json`:

```bash
python ui/setup_db.py
```

O manualmente: ejecutar `schema.sql` en SSMS/sqlcmd, copiar `config.example.json` a `config.json` y generar claves Fernet en `system_config`.

Para configurar el agente Fiserv específicamente (credenciales, perfil de Chrome, carpeta destino):

```bash
python setup_dev_fiserv.py
```

## 4. Cargando datos de Agentes a la Base (Seeding)

Con la base de datos estructuralmente lista, usa la **CLI interactiva**:

```bash
python tools/setup_db_cli.py
```

> Selecciona uno a uno los agentes y responde en consola. Esta herramienta encriptará internamente los datos sensitivos utilizando el `fernet_key` autogenerado por el Wizard y los guardará en la base de datos de manera segura.

## 5. Corriendo el dispatcher

```bash
PYTHONPATH=. python dispatcher/main.py --run --provider fiserv
```

`PYTHONPATH=.` es obligatorio para que los imports `from dispatcher import ...` y `from shared import ...` resuelvan correctamente desde la raíz del proyecto.

Otros modos disponibles:

```bash
PYTHONPATH=. python dispatcher/main.py --tui              # dashboard TUI
PYTHONPATH=. python dispatcher/main.py --tray             # ícono de bandeja
PYTHONPATH=. python dispatcher/main.py --setup-db         # wizard de configuración
```

## 6. Diseño Arquitectónico (Core Components)

- `dispatcher/api.py` — Servidor HTTP interno (puerto 8765). Controla routing de jobs y status.
- `dispatcher/database_factory.py` — Único punto de inyección para connection strings a SQL Server. No hardcodear cadenas de conexión en ninguna otra parte.
- `dispatcher/main.py` — Entry point del dispatcher. Gestiona scheduler, keepalive del browser Fiserv y lifecycle del proceso.
- `agents/fiserv.py` — Agente Fiserv. El browser Playwright **nunca se cierra entre jobs** (diseño intencional para evadir Radware Bot Manager).
- `shared/paths.py` — Centraliza todos los paths. Detecta si corre como `.exe` (PyInstaller) o en dev.
