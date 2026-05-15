# ATANA Agents — Manual de Producción e Instalación

Este documento detalla el procedimiento de despliegue en ambientes de producción (máquinas de clientes). Describe cómo instalar el servicio automatizado y cómo configurar la base de datos de manera correcta utilizando las herramientas provistas.

---

## 1. Opciones de Autenticación a SQL Server

Atana Agents soporta dos métodos de conexión a la base de datos `atana` en producción. Debes decidir cuál usar antes de comenzar la instalación:

### Opción A: Autenticación de Windows (Recomendada / Trusted Connection)
Consiste en utilizar cuentas propias de Windows (Service Account) con permisos sobre el SQL Server.
- **Ventaja**: Las contraseñas nunca se guardan en el archivo de texto `config.json`.
- **Requisito mandatorio**: El servicio de Windows (`AtanaDispatcher`) instalado por defecto utiliza la cuenta de sistema (`LocalSystem`), por lo que no tendrá permisos sobre el motor SQL de forma automática. Finalizada la instalación, el técnico deberá usar la consola de Windows Services o NSSM (`nssm set AtanaDispatcher ObjectName "DOMINIO\Usuario" "Pass"`) para asignarle un usuario de Windows específico que tenga permisos explícitos en la base de datos.
- **Configuración NSSM (Post-Instalación)**:
  ```powershell
  # Detener el servicio
  nssm stop AtanaDispatcher
  # Configurar cuenta del dominio / cuenta local con acceso a SQL
  nssm set AtanaDispatcher ObjectName "DOMINIO\UsuarioDeServicio" "SuContraseña"
  # Reiniciar el servicio
  nssm start AtanaDispatcher
  ```

### Opción B: Autenticación SQL
Consiste en generar credenciales propias en SQL Server (`atana_svc` y `atana_ro`) que se empleen independientemente del usuario de Windows.
- **Ventaja**: El servicio `AtanaDispatcher` puede correr tranquilamente como `LocalSystem` u otro log-on predeterminado sin impactar la base de datos.
- **Requisito mandatorio**: Previamente a correr el instalador, deberás ir al **SQL Server Management Studio** (SSMS) del cliente y crear manualmente el **Log in** de base de datos (`CREATE LOGIN atana_svc WITH PASSWORD = '...';`).
- **Consideración**: La contraseña se guardará en texto plano dentro de la carpeta protegida de la aplicación en `config.json`.

---

## 2. Ejecutar el Instalador Automático

Copia el ejecutable **`AtanaAgents_Setup_vX.X.X.exe`** al entorno del cliente y ejecútalo como **Administrador**.

**Acciones que automatiza el instalador de fondo (`atana_setup.iss`):**
1. Copia y extrae los binarios a la ruta de instalación (por lo general `C:\Program Files\ATANA\`).
2. Registra e inicializa el servicio `AtanaDispatcher` en el registro de Windows usando NSSM.
3. Abre al usuario un **Wizard Interactivo de Configuración de Base de Datos** (`setup_db.py`).

### En el Wizard de Configuración (Interacción del Técnico)
Cuando el flujo detenga su marcha y el pop-up te consulte sobre la conexión:
- Ingresa instacia y Base o URL (`localhost\SQLEXPRESS`).
- **Autenticación Windows**: Si elegiste este flujo, marca el checkbox correspondite. El wizard comprobará dinámicamente si tu usuario interactivo conectado posee rol para forjar las DBs de ATANA.
- **Autenticación SQL**: Introduce los credenciales del login creado manualmente.

Al pulsar **Aplicar**, el propio instalador forjará el archivo `config.json`, contactará a SQL Server e **instalará toda la metadata automáticamente**. Específicamente ejecutará el set de instrucciones DDL (las mismas que en `schema.sql`), creando las tablas necesarias y forjando **todas las Claves Criptográficas (Fernets)** internas por ti de manera transparente.

---

## 3. Sembrado y Configuración de Clientes (Seeding)

Una vez terminada la instalación gráfica, las tablas existirán, las claves estarán provisionadas y el servicio estará corriendo en estado durmiente en Windows. Todo estará funcionando nativamente, pero las instrucciones de tus Agentes Financieros se presentarán **vacías**.

**El técnico de ATANA debe inyectar credenciales al sistema de las siguientes formas:**

**A través de Consola Rápida (`tools/setup_db_cli.py`)** -> *(Recomendado)*
Abre Powershell/CMD sobre `C:\Program Files\ATANA\` y abre el gestor de lineas de comandos de ATANA para ingresar proveedores de a uno:
```bash
python tools/setup_db_cli.py
```
> El script te consultará de manera interactiva credenciales a inyectar en la base de datos (Ej: secretos TOTP, tokens), leyendo a la vez las claves de Fernet autogeneradas por el Installer.

---

## 4. Validar el Despliegue

La instancia debería quedar en estado funcional. Para comprobarlo abre Powershell:

```powershell
### Verifica que el servicio Windows de ATANA corra adecuadamente
Get-Service AtanaDispatcher

### Hacer ping al servicio local
Invoke-WebRequest http://localhost:8765/health

### Revisar Logs de control central
Get-Content "C:\ATANA\logs\dispatcher_$(Get-Date -Format 'yyyy-MM-dd').log" -Tail 20
```

Si todo funcionó, verás en la bandeja de sistema un ícono color GRIS (inicializando) o VERDE (activo, listo y esperando a la hora fijada).

---

## 5. Ícono de Bandeja (System Tray)

El ícono de bandeja se inicia automáticamente con cada inicio de sesión del usuario (entrada en el registro `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`). El servicio mantiene esta entrada actualizada en cada arranque.

**Estados del ícono:**
| Color | Significado |
|-------|-------------|
| Gris | Dispatcher no disponible o inicializando |
| Verde | Todo OK |
| Azul | Agente corriendo |
| Amarillo | Requiere intervención manual |
| Rojo | Error en algún agente |

**El ícono no tiene opción de cierre** — se mantiene siempre activo para garantizar visibilidad del estado del servicio.

Si por alguna razón operativa es necesario cerrarlo, hacerlo desde PowerShell:

```powershell
# Cerrar solo el proceso del tray (deja el servicio corriendo)
Get-WmiObject Win32_Process | Where-Object {
    $_.Name -eq "atana_dispatcher.exe" -and $_.CommandLine -like "*--tray*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

O bien desde el **Administrador de Tareas** → pestaña **Detalles** → buscar `atana_dispatcher.exe` con `--tray` en la línea de comandos → clic derecho → **Finalizar tarea**.

> El tray volverá a iniciarse automáticamente en el próximo inicio de sesión del usuario.

---

## 6. Patrón de renombre de archivos

Cada agente puede renombrar los archivos descargados antes de guardarlos.
El patrón se configura en la tabla `agent_config` (campo `rename_pattern`).

**Si el patrón está vacío**, el archivo se guarda con su nombre original tal como lo devuelve el portal.

### Variables disponibles

| Variable | Descripción | Ejemplo |
|---|---|---|
| `{original}` | Nombre completo original (con extensión) | `settle_20260515.csv` |
| `{originfilename}` | Nombre original **sin extensión** | `settle_20260515` |
| `{ext}` | Extensión sin punto | `csv` |
| `{date}` | Fecha de referencia del job (`YYYY-MM-DD`) | `2026-05-15` |
| `{merchant}` | Usuario configurado para el agente | `acme_corp` |

### Ejemplos de patrones

| Patrón | Archivo resultante |
|---|---|
| *(vacío)* | `settle_20260515.csv` (nombre original) |
| `FISERV_{date}.{ext}` | `FISERV_2026-05-15.csv` |
| `FISERV_{originfilename}.{ext}` | `FISERV_settle_20260515.csv` |
| `{merchant}_{date}.{ext}` | `acme_corp_2026-05-15.csv` |
| `liquidacion_{date}_{originfilename}.{ext}` | `liquidacion_2026-05-15_settle_20260515.csv` |

### Configurar el patrón

Actualizar directamente en SQL Server:

```sql
UPDATE agent_config
SET rename_pattern = 'FISERV_{originfilename}.{ext}'
WHERE provider = 'fiserv';
```

O dejarlo vacío para usar el nombre original:

```sql
UPDATE agent_config
SET rename_pattern = ''
WHERE provider = 'fiserv';
```
