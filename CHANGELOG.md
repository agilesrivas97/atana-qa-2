# Changelog

All notable changes to the **ATANA Agents** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Fiserv — truncamiento silencioso en rangos largos:** `SettlementFileList` recorta
  los resultados sin devolver error cuando el rango `From`/`To` pedido es muy largo
  (~30 días observado), lo que causaba jobs marcados `ok` con menos archivos de los
  realmente disponibles. `list_files()` ahora trocea el rango en ventanas de máximo
  `MAX_RANGE_DAYS` (25) días y agrega los resultados de todas las ventanas.

## [Unreleased / v0.0.9] - 2026-05-16

### Added
- **Fiserv Bot Evasion:** Implementación de interacciones humanas ocultas (scrolls aleatorios, pausas biológicas) durante la autenticación para evitar el bloqueo del firewall de Fiserv.
- **Autoupdater Logs:** Trazabilidad extendida y logs mejorados para el ciclo de auto-actualización (NSSM restart y descargas).

### Changed
- **Fiserv Agent Refactor:** Migración total a flujo de navegador real subyacente (Playwright/Chrome) en modo headless. Utiliza el mismo Stack TLS y Fingerprint HTTP/2 de un usuario real.
- Modificación en la gestión de sesiones: Eliminación de `session store` en favor de `session` pura para mantener el contexto vivo de forma eficiente.
- Se eliminaron las notificaciones redundantes del Autoupdater.

### Fixed
- **System Tray:** Solución al problema crítico de recursividad que provocaba el crasheo del System Tray (Performance update).
- **GitHub Actions:** Corrección y eliminación de mirror on tag en los workflows de CI/CD.

## [v0.0.8] - 2026-05-15

### Added
- Auto-sanación y bypass robusto implementado.

### Fixed
- Ajustes en el flujo de descarga de Playwright y timeouts asociados a redes lentas (Fiserv 502/504 errors).

## [v0.0.3]

### Changed
- Refactorización de scripts de inicialización.
- Documentación de expresiones regulares soportadas añadida al README.
- Supresión de re-nombres por defecto en el setup inicial.
