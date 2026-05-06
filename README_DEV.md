# ATANA Agents — Manual de Desarrollo

Este documento es una guía destinada a programadores que desean mantener o expandir la infraestructura de `atana-agents`.

---

## 1. Stack Tecnológico
- **Lenguaje:** Python 3.11+
- **BD Motor:** SQL Server (On-Premises, Azure, o local vía SSMS)
- **ODBC:** ODBC Driver 18 for SQL Server (requerido para las interfaces de conexión).
- **Frameworks de Agentes:**
  - `httpx`: Llamadas REST API directas.
  - `playwright` (Chromium): Automatización e interactividad en portales sin API nativa.
- **Orquestador y Schedulers:** Generado con `APScheduler`.

## 2. Inicializar entorno de desarrollo

Clona el repositorio desde git.

```bash
git clone <url-del-repo>
cd atana-agents
```

Crea tu ambiente virtual en Python:
```bash
python -m venv venv
# en windows:
venv\Scripts\activate

# instala librerias e instala los navegadores de Playwright:
pip install -r requirements.txt
playwright install chromium
```

## 3. Base de Datos en Modo Desarrollo

Debes levantar tu SQL Server (vía Docker `mcr.microsoft.com/mssql/server` o directamente instalado).
Una vez que el motor de SQL Server esté corriendo y cuentes con acceso (ej. como usuario `sa`), no necesitas ejecutar scripts SQL a mano. 

La forma más rápida de inicializar tu entorno local es ejecutando el Wizard Gráfico que automatiza la creación del archivo `config.json`, la inyección del DDL (tablas y vistas) y la generación de las claves criptográficas Fernet:

```bash
python ui/setup_db.py
```

Completando esa pantalla con la conexión a tu base local, se generará tu `config.json` automáticamente y tu base de datos quedará estructuralmente lista. 

*Tip*: Si pruebas en localhost y tu base de datos auto-genera firmas locales, el Wizard se encargará de inyectar por detrás en el String de Conexión `trust_server_certificate: true` para sortear que la firma de tu cert no sea válida.

Si prefieres hacerlo manualmente a bajo nivel, debes ejecutar `schema.sql` en tu SSMS, copiar `config.example.json` a `config.json` e inyectar manualmente las claves de Fernet en la tabla `system_config` generándolas con un print de python.

## 4. Cargando datos de Agentes a la Base (Seeding)

Con la base de datos estructuralmente lista, el ecosistema de orquestación existirá pero de forma inerte (sin perfiles de agentes creados). 

Para poblar tu motor y probar descargas, debes usar la **CLI interactiva**:
```bash
python tools/setup_db_cli.py
```
> Selecciona uno a uno los agentes y responde en consola. Esta herramienta encriptará internamente los datos sensitivos utilizando el `fernet_key` autogenerado por el Wizard interactivo y los guardará en la base de datos de manera segura.

## 5. Probando Agentes Individuales
Puedes correr el `dispatcher.main` enfocando a un agente prefigurado para debugear flujos directos.
```bash
# esto levantará interactividad sobre el agente target (ej: mercado_pago o fiserv), asumiendo que tienes todo mockeado.
python -m dispatcher.main
```

## 6. Diseño Arquitectónico (Core Components)
Revisa `dispatcher/api.py`, ya que el corazón HTTP lo controla allí e incorpora lógica de routing.
Y `dispatcher/database_factory.py` es tu único punto de inyección para el manejo de credenciales contra SQL Server. No quemes cadenas estáticas de ConnectionStrings en ninguna parte del source.


