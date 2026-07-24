"""
agents/fiserv.py
================
Download agent for Fiserv — TOTP/API mode only, 100% autónomo.

Authentication flow (confirmed from network capture 2026-04-16):
  1. POST /api/Users/requestOtp   → returns data.totpToken
  2. Generate OTP via pyotp.TOTP(shared_secret).now()
  3. POST /api/Users/authenticate → returns data.token (JWT)
  4. POST /settlement/Settlement/SettlementFileList  → file list
  5. POST /settlement/Settlement/downloadUploadedFile → file content

Diseño clave: el browser Playwright NUNCA se cierra entre jobs.
Radware ve continuidad de sesión real → no bloquea en ejecuciones sucesivas.
Un browser abierto = un usuario real que no cerró la pestaña.

Resolución de reCAPTCHA (gratuita, sin API key):
  Primario:  OpenAI Whisper — offline, sin límites de API, más preciso.
  Fallback:  SpeechRecognition + Google Speech — online, gratuito.
  Último:    CapSolver API — pago, solo si captcha_api_key está configurado.

Config keys (extra_config en la DB):
  auth_mode                "totp" (único modo soportado)
  totp_secret              TOTP shared secret (base32)
  api_base_url             override opcional (default: API_BASE_URL)
  chrome_profile_path      ruta al perfil persistente de Chrome
  headless                 bool (default: true)
  captcha_api_key          CapSolver API key (opcional, fallback de pago)
  fiserv_keepalive_interval_min  int (default: 20, usado por dispatcher)
"""
__version__ = "5.0.0"

import base64 as _b64
import json as _json
import os
import random
import re as _re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta as _td
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import pyotp
from loguru import logger

from agents.base import AgentBase, InterventionRequired

# Imports opcionales — verificados en runtime; None si no están instalados
try:
    import speech_recognition as _sr
except ImportError:
    _sr = None

try:
    import whisper as _whisper
except ImportError:
    _whisper = None

try:
    from pydub import AudioSegment as _AudioSegment
except ImportError:
    _AudioSegment = None

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
except ImportError:
    _sync_playwright = None

try:
    from playwright_stealth import stealth_sync as _stealth_sync
except ImportError:
    _stealth_sync = None

try:
    from playwright._impl._driver import compute_driver_executable as _compute_driver_executable
except ImportError:
    _compute_driver_executable = None


API_BASE_URL = "https://merchantcenter.fiservapp.com"
RECAPTCHA_SITE_KEY = "6LdQlQ0pAAAAADjbxloDIchjMWSdfO5H3l6Lnvzi"

# Fiserv trunca silenciosamente (sin error) los resultados de SettlementFileList
# cuando el rango From/To pedido es muy largo (~30 días observado). list_files()
# trocea el rango en ventanas de este tamaño para evitar perder archivos sin darse cuenta.
MAX_RANGE_DAYS = 25


class FiservAgent(AgentBase):

    name = "fiserv"

    def __init__(self, config: dict):
        super().__init__(config)
        self._base     = self.config.get("api_base_url", API_BASE_URL).rstrip("/")
        self._pw       = None
        self._pw_ctx   = None
        self._api_page = None
        self.jwt: Optional[str] = None

        # True hasta el primer login exitoso de esta sesión del browser.
        # Controla cuántos segundos se simulan en Zona 1 (_simular_lectura pre-login).
        self._primer_login_de_sesion = True

        self._profile_path = Path(
            self.config.get("chrome_profile_path", "./fiserv_chrome_profile")
        )
        self._setup_ffmpeg()

    # ── ffmpeg empaquetado ────────────────────────────────────────────────────

    def _setup_ffmpeg(self):
        """
        Configura pydub para usar el ffmpeg empaquetado dentro del .exe.
        En PyInstaller los binarios se extraen a sys._MEIPASS.
        En desarrollo usa el ffmpeg del PATH del sistema.
        """
        if hasattr(sys, "_MEIPASS"):
            ffmpeg_dir  = Path(sys._MEIPASS) / "ffmpeg"
            ffmpeg_exe  = ffmpeg_dir / "ffmpeg.exe"
            ffprobe_exe = ffmpeg_dir / "ffprobe.exe"
            if ffmpeg_exe.exists() and _AudioSegment is not None:
                _AudioSegment.converter = str(ffmpeg_exe)
                _AudioSegment.ffmpeg    = str(ffmpeg_exe)
                _AudioSegment.ffprobe   = str(ffprobe_exe)
                logger.debug(f"[{self.name}] ffmpeg empaquetado: {ffmpeg_exe}")
                return
            logger.warning(f"[{self.name}] ffmpeg no encontrado en {ffmpeg_dir}")
        else:
            logger.debug(f"[{self.name}] ffmpeg desde PATH del sistema")

    # ── Intervención — nunca necesaria ───────────────────────────────────────

    def requires_intervention(self) -> bool:
        return False

    # ── Login / Logout / Shutdown ─────────────────────────────────────────────

    def login(self) -> bool:
        self._ensure_pw_ctx()
        return self._login_totp()

    def logout(self):
        """
        Solo invalida el JWT local. El browser context queda vivo intencionalmente.
        Radware ve continuidad de sesión → no bloquea en la próxima ejecución.
        fireLogoutTasks solo se llama en shutdown() antes del cierre real.
        """
        self.jwt = None
        logger.debug(f"[{self.name}] JWT invalidado — browser context sigue vivo")

    def shutdown(self):
        """
        Cierre real del browser. Llamar SOLO cuando el dispatcher se apaga.
        No llamar entre ejecuciones de jobs.
        """
        logger.info(f"[{self.name}] Cerrando browser definitivamente")

        # Zona 6 — tiempo real antes de logout (2-10s medido en sesiones humanas)
        try:
            if self.jwt and self._api_page:
                time.sleep(random.uniform(2, 8))
                self._pw_post(
                    f"{self._base}/apiTrxSender/FileExporter/fireLogoutTasks",
                    {},
                    extra_headers={"Authorization": f"Bearer {self.jwt}"},
                )
        except Exception:
            pass

        if self._api_page:
            try:
                self._api_page.close()
            except Exception:
                pass
        if self._pw_ctx:
            try:
                self._pw_ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._api_page = self._pw_ctx = self._pw = None
        self.jwt = None

    # ── Playwright context ────────────────────────────────────────────────────

    def _ensure_pw_ctx(self):
        if self._pw_ctx is not None:
            return

        perfil_nuevo = not (self._profile_path / "Default" / "Cookies").exists()

        self._pw = _sync_playwright().start()
        if not Path(self._pw.chromium.executable_path).exists():
            self._ensure_chromium()

        # launch_persistent_context: el contexto ES el browser (no hay _browser separado)
        # headless: siempre True en producción (servicio Windows, Session 0).
        # En dev respeta la config de la DB; si no está configurado, default True.
        headless_cfg = self.config.get("headless", True)
        headless = True if os.environ.get("ATANA_SERVICE") == "1" else bool(headless_cfg)

        self._pw_ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir       = str(self._profile_path),
            headless            = headless,
            user_agent          = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport            = {"width": 1280, "height": 800},
            locale              = "es-AR",
            timezone_id         = "America/Argentina/Buenos_Aires",
            args                = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
            ignore_default_args = ["--enable-automation"],
        )

        self._api_page = self._pw_ctx.new_page()
        self._api_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.__nativeFetch = fetch.bind(window);"
        )

        if _stealth_sync:
            try:
                _stealth_sync(self._api_page)
            except Exception as e:
                logger.warning(f"[{self.name}] stealth unavailable: {e}")

        if perfil_nuevo:
            logger.info(f"[{self.name}] Perfil nuevo — calentando (~2 min, solo la primera vez)...")
            self._calentar_perfil()
        else:
            logger.info(f"[{self.name}] Perfil existente — warm-up rápido")
            self._warmup_rapido()

        logger.info(f"[{self.name}] Playwright context ready — {self._api_page.url!r}")

    def _ensure_chromium(self):
        logger.info(f"[{self.name}] Chromium not found — installing (first run, ~15s)...")
        if _compute_driver_executable is None:
            raise RuntimeError("[fiserv] playwright._impl._driver not available")
        try:
            driver, env = _compute_driver_executable()
            result = subprocess.run(
                [driver, "install", "chromium"],
                env=env, capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[:500])
        except Exception as e:
            raise RuntimeError(f"[fiserv] Could not install Chromium: {e}")
        logger.info(f"[{self.name}] Chromium installed successfully")

    # ── Comportamiento humano ─────────────────────────────────────────────────

    def _calentar_perfil(self):
        """
        Solo corre cuando el perfil no existe (primera vez absoluta).
        Navega ~2 minutos autónomamente para que Radware acumule comportamiento real.
        """
        logger.info(f"[{self.name}] Calentando perfil (~2 min, solo la primera vez)")
        urls = [self._base, self._base + "/login"]
        for url in urls:
            self._navegar_con_comportamiento(url)
            self._simular_lectura(duracion_seg=random.uniform(20, 35))
        self._navegar_con_comportamiento(self._base + "/login")
        self._simular_lectura(duracion_seg=random.uniform(10, 20))
        logger.info(f"[{self.name}] Calentamiento completado")

    def _warmup_rapido(self):
        """Warm-up para ejecuciones normales con browser ya vivo."""
        try:
            self._api_page.goto(
                self._base + "/login",
                wait_until="networkidle",
                timeout=30_000,
            )
        except Exception:
            try:
                self._api_page.goto(
                    self._base + "/login",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as e:
                logger.warning(f"[{self.name}] Warm-up falló: {e}")
        time.sleep(random.uniform(2, 4))
        self._simular_comportamiento_minimo()

    def _navegar_con_comportamiento(self, url: str):
        try:
            self._api_page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception:
            try:
                self._api_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                logger.warning(f"[{self.name}] Nav a {url} falló: {e}")
        time.sleep(random.uniform(1, 2))

    def _simular_lectura(self, duracion_seg: float):
        """
        Simula que un usuario está leyendo la página.
        Scroll gradual, movimientos de mouse en arco no lineales, pauses variables.
        Basado en comportamiento humano real observado en capturas de red.
        Acciones con pesos: scroll_down 40%, mouse_move 30%, pause 15%, scroll_up 10%, hover 5%.
        """
        page     = self._api_page
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        inicio   = time.time()
        mouse_x  = random.randint(300, 600)
        mouse_y  = random.randint(100, 300)
        try:
            page.mouse.move(mouse_x, mouse_y)
        except Exception:
            pass
        scroll_y = 0

        while time.time() - inicio < duracion_seg:
            accion = random.choices(
                ["scroll_down", "mouse_move", "pause", "scroll_up", "hover"],
                weights=[40, 30, 15, 10, 5],
            )[0]

            try:
                if accion == "scroll_down":
                    delta = random.randint(80, 250)
                    scroll_y += delta
                    page.evaluate(f"window.scrollBy({{top: {delta}, behavior: 'smooth'}})")
                    time.sleep(random.uniform(0.3, 0.8))

                elif accion == "scroll_up" and scroll_y > 0:
                    delta = random.randint(50, 150)
                    scroll_y = max(0, scroll_y - delta)
                    page.evaluate(f"window.scrollBy({{top: -{delta}, behavior: 'smooth'}})")
                    time.sleep(random.uniform(0.2, 0.5))

                elif accion == "mouse_move":
                    target_x = random.randint(100, viewport["width"] - 100)
                    target_y = random.randint(100, viewport["height"] - 100)
                    pasos    = random.randint(5, 12)
                    for i in range(pasos):
                        ix = mouse_x + (target_x - mouse_x) * (i + 1) / pasos
                        iy = mouse_y + (target_y - mouse_y) * (i + 1) / pasos
                        ix += random.uniform(-3, 3)
                        iy += random.uniform(-3, 3)
                        page.mouse.move(ix, iy)
                        time.sleep(random.uniform(0.02, 0.06))
                    mouse_x, mouse_y = target_x, target_y

                elif accion == "pause":
                    time.sleep(random.uniform(0.5, 1.5))

                elif accion == "hover":
                    try:
                        page.hover("body", position={"x": mouse_x, "y": mouse_y})
                    except Exception:
                        pass
                    time.sleep(random.uniform(0.1, 0.3))
            except Exception as e:
                logger.debug(f"[{self.name}] _simular_lectura interrupted: {e}")
                break

    def _simular_comportamiento_minimo(self):
        page     = self._api_page
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        for _ in range(random.randint(2, 4)):
            page.mouse.move(
                random.randint(200, viewport["width"] - 200),
                random.randint(100, viewport["height"] - 100),
            )
            time.sleep(random.uniform(0.2, 0.5))

    # ── Helpers de red ────────────────────────────────────────────────────────

    def _ensure_page_origin(self):
        """Re-navega si la página quedó en un origen distinto al de la API."""
        current_url     = self._api_page.url
        current_netloc  = urlparse(current_url).netloc if current_url.startswith("http") else ""
        expected_netloc = urlparse(self._base).netloc
        if current_netloc != expected_netloc:
            logger.warning(
                f"[{self.name}] Página en origen incorrecto: {current_url!r} "
                f"(esperado: {expected_netloc}) — re-navegando"
            )
            try:
                self._api_page.goto(self._base, wait_until="domcontentloaded", timeout=20_000)
                logger.info(f"[{self.name}] Re-navegación ok → {self._api_page.url!r}")
            except Exception as e:
                logger.error(f"[{self.name}] Re-navegación falló: {e}")

    def _browser_headers(self, extra: dict = None) -> dict:
        headers = {
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding":    "gzip, deflate, br",
            "Origin":             self._base,
            "Referer":            self._base + "/",
            "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest":     "empty",
            "Sec-Fetch-Mode":     "cors",
            "Sec-Fetch-Site":     "same-origin",
            "Content-Type":       "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _pw_post(self, url: str, payload: dict, extra_headers: dict = None) -> dict:
        """POST via page.evaluate(fetch) — usa el stack TLS real de Chrome."""
        self._ensure_pw_ctx()
        self._ensure_page_origin()
        logger.debug(f"[{self.name}] fetch() POST {url} desde página: {self._api_page.url!r}")
        headers = self._browser_headers(extra_headers)
        result  = self._api_page.evaluate(
            """async ({url, body, headers}) => {
                const fetchFn = window.__nativeFetch || fetch;
                try {
                    const r = await fetchFn(url, {method: 'POST', headers, body});
                    const text = await r.text();
                    return {status: r.status, text, fetchError: null};
                } catch(e) {
                    return {status: 0, text: '', fetchError: e.message + ' | ' + e.constructor.name};
                }
            }""",
            {"url": url, "body": _json.dumps(payload), "headers": headers},
        )
        if result.get("fetchError"):
            raise RuntimeError(f"[fiserv][pw] fetch falló: {result['fetchError']}")
        status = result["status"]
        text   = result["text"]
        logger.debug(f"[{self.name}] POST {url} → status={status} body={text[:300]}")
        if status not in (200, 201):
            error_preview = text[:200].replace('\n', ' ').replace('\r', '')
            raise RuntimeError(f"[fiserv][pw] HTTP {status}: {error_preview}")
        if "Radware Bot Manager" in text or ("captcha" in text.lower() and "<html" in text.lower()):
            raise RuntimeError("[fiserv][pw] Radware bloqueó la solicitud")
        try:
            return _json.loads(text) if text.strip() else {}
        except ValueError:
            raise RuntimeError(f"[fiserv][pw] Non-JSON response: {text[:300]}")

    def _pw_bytes(self, url: str, payload: dict, extra_headers: dict = None) -> bytes:
        """POST via page.evaluate(fetch) — retorna bytes (bridge base64 desde JS ArrayBuffer)."""
        self._ensure_pw_ctx()
        self._ensure_page_origin()
        logger.debug(f"[{self.name}] fetch() POST {url} (bytes) desde página: {self._api_page.url!r}")
        headers = self._browser_headers(extra_headers)
        result  = self._api_page.evaluate(
            """async ({url, body, headers}) => {
                const fetchFn = window.__nativeFetch || fetch;
                try {
                    const r = await fetchFn(url, {method: 'POST', headers, body});
                    if (!r.ok) return {status: r.status, fetchError: null, error: await r.text(), data: null};
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let bin = '';
                    for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
                    return {status: r.status, fetchError: null, error: null, data: btoa(bin)};
                } catch(e) {
                    return {status: 0, fetchError: e.message + ' | ' + e.constructor.name, error: null, data: null};
                }
            }""",
            {"url": url, "body": _json.dumps(payload), "headers": headers},
        )
        status = result["status"]
        if result.get("fetchError"):
            raise RuntimeError(f"[fiserv][pw] fetch falló: {result['fetchError']}")
        if result.get("error") is not None:
            if status == 401:
                raise RuntimeError("[fiserv][pw] JWT expirado (401)")
            error_preview = result['error'][:200].replace('\n', ' ').replace('\r', '')
            raise RuntimeError(f"[fiserv][pw] Download HTTP {status}: {error_preview}")
        content = _b64.b64decode(result["data"])
        logger.debug(f"[{self.name}] POST {url} → status={status} size={len(content):,} bytes")
        return content

    # ── JWT ───────────────────────────────────────────────────────────────────

    def _jwt_expirado(self) -> bool:
        if not self.jwt:
            return True
        try:
            payload = self.jwt.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            data     = _json.loads(_b64.b64decode(payload))
            restante = data["exp"] - time.time()
            logger.debug(f"[{self.name}] JWT expira en {restante:.0f}s")
            return restante < 300
        except Exception:
            return True

    def _get_jwt_valido(self) -> str:
        """Renueva el JWT si expiró (o está por expirar). El browser sigue vivo."""
        if not self._jwt_expirado():
            return self.jwt
        logger.info(f"[{self.name}] JWT expirado — renovando (browser sigue vivo)")
        self._login_totp()
        return self.jwt

    # ── Autenticación TOTP ────────────────────────────────────────────────────

    def _login_totp(self) -> bool:
        """
        Autenticación vía API REST Fiserv usando código TOTP.

        Delays basados en timestamps de sesiones humanas reales:
          Zona 1  GET /login → requestOtp:    35-55s (primer login) / 8-20s (re-login)
          Zona 2  requestOtp → authenticate:  18-32s (simular apertura app TOTP)
          Zona 3  post-authenticate:          3-12s  (simular navegación al menú)
        """
        secret   = self.config.get("totp_secret", "")
        username = self.config.get("username", "")
        password = self.config.get("password", "")

        if not secret:
            raise RuntimeError("[fiserv] TOTP secret no configurado")

        MAX_INTENTOS = 3
        for intento in range(1, MAX_INTENTOS + 1):
            logger.info(f"[{self.name}] Login intento {intento}/{MAX_INTENTOS}")
            try:
                # ZONA 1 — simular comportamiento pre-login
                if self._primer_login_de_sesion:
                    duracion = random.uniform(35, 55)
                    logger.debug(
                        f"[{self.name}] Primer login de sesión — "
                        f"simulando {duracion:.0f}s de lectura"
                    )
                else:
                    duracion = random.uniform(8, 20)
                    logger.debug(
                        f"[{self.name}] Re-login — "
                        f"simulando {duracion:.0f}s de lectura"
                    )
                self._simular_lectura(duracion_seg=duracion)

                # Step 1 — requestOtp
                body1      = self._pw_post(
                    f"{self._base}/api/Users/requestOtp",
                    {
                        "username":           username,
                        "password":           password,
                        "deviceName":         "",
                        "phoneNumber":        "",
                        "typeAuthentication": "TOTP",
                    },
                )
                # requestOtp responde HTTP 200 incluso con credenciales incorrectas —
                # el error viene en el body ("success": false, "message": "wrongUserOrPass"),
                # no como HTTP 401. Cortar acá evita gastar las Zonas 2/3 (hasta ~44s) y
                # reintentos inútiles contra credenciales que no van a cambiar solas.
                if body1.get("success") is False and body1.get("message") == "wrongUserOrPass":
                    raise InterventionRequired(
                        "[fiserv] Credenciales inválidas — usuario o contraseña incorrectos (requestOtp)"
                    )

                totp_token = body1.get("data", body1).get("totpToken", "")
                logger.debug(f"[{self.name}] totpToken recibido: {bool(totp_token)}")

                # ZONA 2 — simular tiempo de buscar el teléfono y abrir la app TOTP
                delay_z2 = random.uniform(18, 32)
                logger.debug(
                    f"[{self.name}] Zona 2 — esperando {delay_z2:.0f}s "
                    f"(simular apertura app TOTP)"
                )
                time.sleep(delay_z2)

                # Step 2 — generar OTP
                secret_b32 = _re.sub(r'[\s\-=]', '', secret).upper()
                totp_obj   = pyotp.TOTP(secret_b32)
                otp_code   = totp_obj.now()
                ciclo_rest = 30 - int(time.time()) % 30
                logger.debug(
                    f"[{self.name}] OTP: {otp_code} "
                    f"({ciclo_rest}s restantes en ciclo TOTP)"
                )

                # Esperar siguiente ciclo si quedan menos de 3s — evita expiración durante el request
                if ciclo_rest < 3:
                    logger.debug(
                        f"[{self.name}] Ciclo TOTP casi terminado — "
                        f"esperando el siguiente ciclo"
                    )
                    time.sleep(ciclo_rest + 1)
                    otp_code = totp_obj.now()
                    logger.debug(f"[{self.name}] Nuevo OTP: {otp_code}")

                # Step 3 — authenticate
                body2 = self._pw_post(
                    f"{self._base}/api/Users/authenticate",
                    {
                        "username":   username,
                        "password":   password,
                        "otp":        otp_code,
                        "deviceName": username,
                        "totpToken":  totp_token,
                    },
                )
                data2 = body2.get("data", body2)
                jwt   = (
                    data2.get("token")
                    or data2.get("jwt")
                    or data2.get("access_token")
                    or data2.get("accessToken")
                )
                if not jwt:
                    raise RuntimeError(f"[fiserv] No JWT en authenticate: {body2}")

                self.jwt = jwt
                self._primer_login_de_sesion = False

                # ZONA 3 — simular navegación post-login hacia el menú de reportes
                delay_z3 = random.uniform(3, 12)
                logger.debug(f"[{self.name}] Zona 3 — post-login {delay_z3:.0f}s")
                time.sleep(delay_z3)

                logger.info(f"[{self.name}] Login exitoso en intento {intento}")
                return True

            except RuntimeError as e:
                msg = str(e)
                if "Radware" in msg:
                    logger.warning(f"[{self.name}] Intento {intento}: Radware bloqueó")
                    if intento < MAX_INTENTOS:
                        recuperado = self._handle_radware_block()
                        if not recuperado:
                            break
                        try:
                            self._resolver_recaptcha_audio()
                        except Exception as cap_e:
                            logger.warning(
                                f"[{self.name}] reCAPTCHA resolver falló (no crítico): {cap_e}"
                            )
                    continue
                if "401" in msg:
                    # Usuario/contraseña rechazados por Fiserv — reintentar no sirve de nada.
                    # InterventionRequired detiene el job de inmediato (sin retries) y lo marca
                    # requires_intervention para que un humano corrija las credenciales.
                    raise InterventionRequired(
                        "[fiserv] Credenciales inválidas — usuario o contraseña incorrectos"
                    )
                logger.warning(f"[{self.name}] Intento {intento}: {e}")
                if intento < MAX_INTENTOS:
                    time.sleep(random.uniform(5, 15))

        raise RuntimeError(f"[fiserv] Login falló después de {MAX_INTENTOS} intentos")

    # ── List files ────────────────────────────────────────────────────────────

    def list_files(self) -> list[dict]:
        """
        POST /settlement/Settlement/SettlementFileList

        Fiserv trunca silenciosamente los resultados cuando el rango From/To
        pedido es muy largo (sin devolver error) — por eso el rango total
        (self.period_from → self.period_to) se trocea en ventanas de
        MAX_RANGE_DAYS antes de pedir cada una.

        Delays de Zona 3/4 incluidos:
          Inicio:         2-5s  (navegación al menú de reportes)
          Entre páginas:  1.5-4s
          Entre ventanas: 1.5-4s
        """
        logger.info(
            f"[{self.name}] Fetching file list "
            f"{self.period_from.date()} → {self.period_to.date()}..."
        )

        # Zona 3 / inicio de actividad de negocio
        time.sleep(random.uniform(2, 5))

        seen  = set()
        items = []

        windows = self._date_windows(self.period_from, self.period_to)
        for i, (window_from, window_to) in enumerate(windows):
            window_items = self._fetch_window(window_from, window_to)
            for item in window_items:
                name = item["name"]
                if name not in seen:
                    seen.add(name)
                    items.append(item)

            logger.info(
                f"[{self.name}] Ventana {window_from.date()} → {window_to.date()}: "
                f"{len(window_items)} file(s)"
            )

            if i < len(windows) - 1:
                # Zona 4 — entre ventanas de fecha
                time.sleep(random.uniform(1.5, 4))

        logger.info(f"[{self.name}] {len(items)} file(s) found (total)")
        return items

    @staticmethod
    def _date_windows(period_from, period_to) -> list[tuple]:
        """Divide [period_from, period_to] en ventanas de máximo MAX_RANGE_DAYS días."""
        windows      = []
        window_start = period_from
        while window_start <= period_to:
            window_end = min(
                window_start + _td(days=MAX_RANGE_DAYS - 1),
                period_to,
            )
            windows.append((window_start, window_end))
            window_start = window_end + _td(days=1)
        return windows

    def _fetch_window(self, window_from, window_to) -> list[dict]:
        """Pagina (Skip/Take) los resultados de SettlementFileList para una sola ventana de fecha."""
        jwt        = self._get_jwt_valido()
        url        = f"{self._base}/settlement/Settlement/SettlementFileList"
        end_date   = window_to.date() + _td(days=1)
        from_str   = f"{window_from.date().isoformat()}T03:00:00.000Z"
        to_str     = f"{end_date.isoformat()}T03:00:00.000Z"
        auth       = {"Authorization": f"Bearer {jwt}"}

        PAGE_SIZE = 40
        items     = []
        skip      = 0

        while True:
            body = self._pw_post(
                url,
                {
                    "From":              from_str,
                    "To":                to_str,
                    "MerchantDocuments": [""],
                    "MerchantNumbers":   [],
                    "DateRangeType":     "SETT_DATE",
                    "Currency":          "",
                    "Skip":              skip,
                    "Take":              PAGE_SIZE,
                },
                extra_headers=auth,
            )
            page = (
                body if isinstance(body, list)
                else body.get("data", body.get("files", []))
            )

            for entry in page:
                logger.debug(
                    f"[{self.name}] entry fields: "
                    f"{list(entry.keys()) if isinstance(entry, dict) else entry}"
                )
                name = (
                    entry.get("aggregatorHeaderId")
                    or entry.get("fileName")
                    or entry.get("name")
                    or ""
                )
                if name:
                    items.append({"name": name})

            logger.debug(f"[{self.name}] Page skip={skip}: {len(page)} record(s)")

            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
            # Zona 4 — entre páginas paginadas
            time.sleep(random.uniform(1.5, 4))

        return items

    # ── Download ──────────────────────────────────────────────────────────────

    def download(self, item: dict) -> Optional[Path]:
        """
        POST /settlement/Settlement/downloadUploadedFile
        Body: {"fileName": aggregatorHeaderId}

        Delay de Zona 4 al inicio de cada descarga (1.5-4s).
        """
        jwt  = self._get_jwt_valido()
        name = item.get("name", "")
        if not name:
            logger.error(f"[{self.name}] download() called with empty name")
            return None

        # Zona 4 — entre archivos
        time.sleep(random.uniform(1.5, 4))

        url  = f"{self._base}/settlement/Settlement/downloadUploadedFile"
        dest = self.destination / self.rename(name)
        auth = {"Authorization": f"Bearer {jwt}"}

        logger.info(f"[{self.name}] Downloading: {name} → {dest.name}")
        try:
            content = self._pw_bytes(url, {"fileName": name}, extra_headers=auth)

            if not content:
                logger.error(f"[{self.name}] Empty response body for {name}")
                return None

            if not self._verify_content(content, name):
                return None

            dest.write_bytes(content)
            logger.info(f"[{self.name}] Saved: {dest} ({len(content):,} bytes)")
            return dest

        except Exception as e:
            logger.error(
                f"[{self.name}] Download exception for {name}: "
                f"{type(e).__name__}: {e}"
            )
            return None

    # ── Fallbacks anti-Radware ────────────────────────────────────────────────

    def _handle_radware_block(self) -> bool:
        """
        Fallbacks en cascada cuando Radware bloquea:
          Nivel 1: calentamiento extendido sin cerrar el browser.
          Nivel 2: reconstruir perfil desde cero.
        """
        # Nivel 1 — calentamiento extendido SIN cerrar el browser
        logger.warning(f"[{self.name}] Radware bloqueó — nivel 1: calentamiento extendido")
        try:
            self._calentar_perfil()
            return True
        except Exception as e:
            logger.warning(f"[{self.name}] Calentamiento extendido falló: {e}")

        # Nivel 2 — reconstruir perfil desde cero
        logger.warning(f"[{self.name}] Nivel 2 — reconstruyendo perfil desde cero")
        try:
            if self._api_page:
                try:
                    self._api_page.close()
                except Exception:
                    pass
            if self._pw_ctx:
                try:
                    self._pw_ctx.close()
                except Exception:
                    pass
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._api_page = self._pw_ctx = self._pw = None
            self._primer_login_de_sesion = True

            if self._profile_path.exists():
                shutil.rmtree(self._profile_path)
                logger.info(f"[{self.name}] Perfil eliminado — reconstruyendo")

            self._ensure_pw_ctx()
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Reconstrucción de perfil falló: {e}")
            return False

    # ── Resolución de reCAPTCHA ───────────────────────────────────────────────

    def _transcribir_audio(self, mp3_path: Path) -> str:
        """
        Transcribe el audio challenge de reCAPTCHA.

        Estrategia por prioridad:
          1. OpenAI Whisper   — offline, sin límites de API, más preciso.
             Instalar: pip install openai-whisper
          2. SpeechRecognition + Google Speech — online, gratuito.
             Instalar: pip install SpeechRecognition pydub
             Requiere: ffmpeg en PATH (dev) o empaquetado en .exe

        Whisper es preferido porque no depende de APIs externas ni
        tiene rate limits. Lee mp3 directamente (maneja su propia conversión).
        """
        # Primario: Whisper offline
        if _whisper is not None:
            try:
                logger.debug(f"[{self.name}] Transcribiendo con Whisper (offline)")
                model  = _whisper.load_model("tiny")
                result = model.transcribe(str(mp3_path), language="en")
                texto  = result["text"].strip()
                if texto:
                    logger.debug(f"[{self.name}] Whisper: {texto!r}")
                    return texto
            except Exception as e:
                logger.warning(f"[{self.name}] Whisper falló: {e} — usando SpeechRecognition")

        # Fallback: SpeechRecognition + Google Speech (necesita conversión a WAV)
        if _sr is not None and _AudioSegment is not None:
            wav_path = mp3_path.with_suffix(".wav")
            try:
                logger.debug(f"[{self.name}] Transcribiendo con SpeechRecognition (Google)")
                _AudioSegment.from_mp3(str(mp3_path)).export(str(wav_path), format="wav")
                recognizer = _sr.Recognizer()
                with _sr.AudioFile(str(wav_path)) as source:
                    audio_data = recognizer.record(source)
                texto = recognizer.recognize_google(audio_data, language="en-US")
                logger.debug(f"[{self.name}] SpeechRecognition: {texto!r}")
                return texto
            except _sr.UnknownValueError:
                raise RuntimeError("Audio no reconocido por Google Speech")
            except _sr.RequestError as e:
                raise RuntimeError(f"Google Speech API error: {e}")
            except Exception as e:
                raise RuntimeError(f"SpeechRecognition falló: {e}")
            finally:
                wav_path.unlink(missing_ok=True)

        raise RuntimeError(
            "No hay motor de transcripción disponible. "
            "Instalar openai-whisper (recomendado) o SpeechRecognition+pydub."
        )

    def _resolver_recaptcha_audio(self) -> bool:
        """
        Resuelve reCAPTCHA v2 mediante el challenge de audio.
        Completamente gratuito — usa Whisper (offline) o Google Speech.
        Fallback a CapSolver solo si audio falla 3 veces y captcha_api_key está configurado.

        site_key confirmado de capturas de red: 6LdQlQ0pAAAAADjbxloDIchjMWSdfO5H3l6Lnvzi
        """
        page = self._api_page

        # Detectar si hay reCAPTCHA en la página
        try:
            recaptcha_frame = page.frame_locator(
                "iframe[src*='recaptcha'][src*='anchor']"
            ).first
            recaptcha_frame.locator("#recaptcha-anchor").wait_for(timeout=3_000)
        except Exception:
            return True  # no hay captcha presente

        MAX_INTENTOS_AUDIO = 3
        for intento in range(1, MAX_INTENTOS_AUDIO + 1):
            try:
                logger.info(
                    f"[{self.name}] reCAPTCHA detectado — "
                    f"resolviendo via audio (intento {intento})"
                )

                # Click en checkbox "I'm not a robot"
                recaptcha_frame.locator("#recaptcha-anchor").click()
                time.sleep(2)

                # Frame del challenge de audio
                challenge_frame = page.frame_locator(
                    "iframe[src*='recaptcha'][src*='bframe']"
                ).first

                # Click en botón de accesibilidad (audio)
                challenge_frame.locator("#recaptcha-audio-button").click()
                time.sleep(2)

                # Obtener URL del audio mp3
                audio_url = challenge_frame.locator(
                    ".rc-audiochallenge-tdownload-link"
                ).get_attribute("href")

                if not audio_url:
                    raise RuntimeError("No se encontró URL del audio challenge")

                # Descargar el mp3
                response = httpx.get(audio_url, timeout=30)
                response.raise_for_status()

                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f_mp3:
                    f_mp3.write(response.content)
                    mp3_path = Path(f_mp3.name)

                try:
                    texto = self._transcribir_audio(mp3_path)
                finally:
                    mp3_path.unlink(missing_ok=True)

                logger.debug(f"[{self.name}] Audio transcripto: {texto!r}")

                # Ingresar texto en el campo del challenge
                challenge_frame.locator("#audio-response").fill(texto.lower())
                time.sleep(0.5)

                # Click en verificar
                challenge_frame.locator("#recaptcha-verify-button").click()
                time.sleep(2)

                # Extraer token generado
                token = page.evaluate(
                    "document.getElementById('g-recaptcha-response')?.value || ''"
                )

                if token:
                    logger.info(f"[{self.name}] reCAPTCHA resuelto via audio OK")
                    return True

                raise RuntimeError("Token vacío después de verificar")

            except Exception as e:
                logger.warning(f"[{self.name}] Audio intento {intento} falló: {e}")
                if intento < MAX_INTENTOS_AUDIO:
                    time.sleep(random.uniform(3, 6))

        # Fallback pago — CapSolver (solo si está configurado)
        api_key = self.config.get("captcha_api_key", "")
        if api_key:
            logger.warning(f"[{self.name}] Audio agotado — usando CapSolver (fallback de pago)")
            return self._resolver_captcha_capsolver(api_key)

        raise RuntimeError(
            "[fiserv] No se pudo resolver el reCAPTCHA. "
            "Configurar captcha_api_key para usar CapSolver como fallback de pago."
        )

    def _resolver_captcha_capsolver(self, api_key: str) -> bool:
        """Fallback de pago usando CapSolver API."""
        resp = httpx.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type":       "ReCaptchaV2TaskProxyLess",
                    "websiteKey": RECAPTCHA_SITE_KEY,
                    "websiteURL": self._base,
                },
            },
            timeout=30,
        )
        task_id = resp.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"CapSolver error: {resp.text[:200]}")

        for _ in range(20):
            time.sleep(5)
            res  = httpx.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            )
            data = res.json()
            if data.get("status") == "ready":
                token = data["solution"]["gRecaptchaResponse"]
                self._api_page.evaluate(
                    f"document.getElementById('g-recaptcha-response').value = '{token}'"
                )
                logger.info(f"[{self.name}] reCAPTCHA resuelto via CapSolver")
                return True

        raise RuntimeError("[fiserv] CapSolver timeout — sin respuesta en 100s")
