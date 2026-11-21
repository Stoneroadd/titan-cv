"""Robot de postulación: un hilo dueño de Playwright que procesa comandos en cola.

Flujo por vacante:
  start   -> abre la URL, lee la vacante, analiza, genera CV, abre el formulario y lo completa
  resume  -> continúa tras tus respuestas, un login o un CAPTCHA resuelto por ti
  submit  -> solo cuando tú lo confirmas: pulsa "Enviar" y verifica la confirmación
Nunca envía por su cuenta: se detiene en "Listo para revisar".
"""
from __future__ import annotations

import queue
import re
import random
import threading
import time
import traceback

from ..engine import Engine, EngineError
from ..job_intel import parse_html
from ..utils import load_yaml, normalize
from . import forms
from .answers import SavedAnswers, label_key, resolve
from .platforms import detect


class Runner(threading.Thread):
    def __init__(self, engine: Engine, headless: bool = False):
        super().__init__(daemon=True, name="cvtitan-runner")
        self.engine = engine
        self.db = engine.db
        self.headless = headless
        self.q: queue.Queue = queue.Queue()
        self.pages: dict = {}
        self.ctx = None
        self.pw = None
        self.ready = threading.Event()
        self.browser_error: str | None = None
        cfg = engine.settings.get("browser") or {}
        self.channel = cfg.get("channel", "chrome")
        self.slow_mo = int(cfg.get("slow_mo_ms", 120))
        self.max_steps = int(cfg.get("max_form_steps", 12))
        self.on_before_submit = None      # callback(job_id): el registro marca el clic (idempotencia)

    # ------------------------------------------------------------------ cola
    def send(self, cmd: str, arg=None, **kw) -> None:
        self.q.put((cmd, arg, kw))

    def run(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            self.pw = pw
            self.ready.set()
            while True:
                cmd, arg, kw = self.q.get()
                if cmd == "stop":
                    break
                try:
                    getattr(self, f"_{cmd}")(arg, **kw)
                except EngineError as e:
                    if arg and cmd != "open":
                        self.db.set_status(arg, "error", str(e))
                except Exception as e:  # el robot nunca debe morir por una página rara
                    if arg and cmd != "open":
                        self.db.event(arg, "error", traceback.format_exc()[-1500:])
                        self.db.set_status(arg, "error", f"{type(e).__name__}: {e}"[:300])
            if self.ctx:
                try:
                    self.ctx.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ navegador
    def context(self):
        if self.ctx is not None:
            try:
                _ = self.ctx.pages
                return self.ctx
            except Exception:
                self.ctx = None
        kwargs = dict(user_data_dir=str(self.engine.ws.browser_profile), headless=self.headless,
                      slow_mo=self.slow_mo, accept_downloads=True, locale="es-CL",
                      viewport=None if not self.headless else {"width": 1280, "height": 900},
                      args=[] if self.headless else ["--start-maximized"])
        self.engine.ws.browser_profile.mkdir(parents=True, exist_ok=True)
        last = None
        for attempt in range(4):           # el perfil puede tardar unos segundos en liberarse tras cerrar Chrome
            try:
                self.ctx = self.pw.chromium.launch_persistent_context(channel=self.channel, **kwargs)
                break
            except Exception as e:
                last = e
                msg = str(e)
                if "Executable doesn't exist" in msg or "is not found" in msg.lower():
                    # Sin Google Chrome instalado: navegador de Playwright (requiere `playwright install chromium`).
                    self.ctx = self.pw.chromium.launch_persistent_context(**kwargs)
                    break
                if attempt == 0:                 # restos de ejecuciones anteriores (sin ventana, sin padre)
                    from .human_browser import release_orphans
                    if release_orphans(self.engine.ws.browser_profile):
                        time.sleep(2)
                        continue
                time.sleep(5)
        else:
            raise RuntimeError(
                "Could not open the robot's Chrome: its profile (data/browser-profile) is in use. Close any Chrome "
                "window opened by `cvtitan sesiones` or `--ver` and try again. "
                f"Detail: {str(last).splitlines()[0][:200]}") from last
        self.ctx.on("close", lambda _: setattr(self, "ctx", None))
        if self.headless:
            self.ctx.route("**/*", lambda route: route.abort()
                           if route.request.resource_type in ("image", "media", "font") else route.continue_())
        # Ninguna operación del navegador puede esperar para siempre (evita cuelgues en portales lentos).
        self.ctx.set_default_timeout(30_000)
        self.ctx.set_default_navigation_timeout(60_000)
        return self.ctx

    @staticmethod
    def settle(page, max_ms: int = 3000) -> None:
        """Espera a que la página termine de CARGAR (evento load) y luego, como máximo 400 ms, a que la red quede
        quieta. Antes se esperaba «networkidle» hasta `max_ms`, que en sitios con analítica casi nunca llega:
        cada paso pagaba el tope completo. `max_ms` sigue siendo el límite para páginas lentas."""
        try:
            page.wait_for_load_state("load", timeout=max_ms)
        except Exception:
            return
        try:
            page.wait_for_load_state("networkidle", timeout=400)
        except Exception:
            pass

    def page_for(self, job_id: str, url: str | None = None):
        page = self.pages.get(job_id)
        if page is not None and not page.is_closed():
            return page
        ctx = self.context()
        blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/") and p not in self.pages.values()]
        page = blank[0] if blank else ctx.new_page()
        self.pages[job_id] = page
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.settle(page, 2500)
        return page

    def _human_pause(self, platform_key: str) -> None:
        if platform_key in ("linkedin", "indeed"):
            time.sleep(random.uniform(1.5, 3.5))

    def _shot(self, job_id: str, page, name: str) -> str | None:
        path = self.engine.ws.out_dir(job_id) / f"{name}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    def _diagnose(self, job_id: str) -> str | None:
        """Al pausar: captura, campos detectados y HTML de la página, para depurar formularios nuevos."""
        page = self.pages.get(job_id)
        if page is None or page.is_closed():
            return None
        import json
        out = self.engine.ws.out_dir(job_id)
        try:
            (out / "debug_fields.json").write_text(json.dumps(
                {"url": page.url, "fields": forms.redact_fields(forms.extract_fields(page)),
                 "buttons": forms.find_buttons(page)},
                ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            (out / "debug_page.html").write_text(forms.redact_html(page.content()), encoding="utf-8")
        except Exception as e:
            self.db.event(job_id, "warning", f"Diagnóstico incompleto: {e}"[:300])
        return self._shot(job_id, page, "pausa")

    def _pause(self, job_id: str, reason: str, step: str, kind: str, questions: list | None = None) -> None:
        shot = self._diagnose(job_id)
        if shot:
            self.db.update_data(job_id, screenshot=shot)
        self.db.update_data(job_id, step=step, wait_kind=kind, questions=questions or [])
        self.db.set_status(job_id, "needs_input", reason)

    # ------------------------------------------------------------------ comandos
    def _open(self, url, **_):
        page = self.context().new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()

    def _close(self, job_id, **_):
        page = self.pages.pop(job_id, None)
        if page is not None and not page.is_closed():
            page.close()

    def _start(self, job_id, force: bool = False, **_):
        row = self.db.get(job_id)
        url = row["url"]
        self.db.update_data(job_id, force=force, form_opened=False, questions=[], filled=[])
        self.db.set_status(job_id, "analyzing")
        page = self.page_for(job_id)
        if forms.DEMO:
            forms.DEMO["say"](f"  [OPEN] {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        from .. import telemetry
        telemetry.visit(url)
        self.settle(page, 3000)
        platform = detect(page.url)
        self.db.upsert(job_id, platform=detect(url).key)

        raw = parse_html(page.content())
        if len(raw["text"]) < 400:
            visible = ""
            for selector in ("main", "body"):
                try:
                    visible = page.inner_text(selector, timeout=3000)
                    break
                except Exception:
                    continue
            if len(visible) > len(raw["text"]):
                raw["text"] = visible
        if len(raw["text"].strip()) < 120 or (platform.requires_login and forms.needs_login(page)):
            page.bring_to_front()
            where = f"to {platform.name} " if platform.key != "generic" else ""
            return self._pause(job_id, f"Could not read the job posting. Sign in {where}or open the posting in the "
                                       "Chrome window and press Continue.", step="analyze", kind="login")
        raw["title"] = raw.get("title") or page.title()

        job, m = self.engine.analyze(job_id, url, raw)
        min_score = float(self.engine.settings.get("min_score", 30))
        if m["score"] < min_score and not force:
            return self.db.set_status(job_id, "skipped",
                                      f"Compatibilidad {m['score']} < {min_score:g}. Puedes forzarla si te interesa.")
        self.db.set_status(job_id, "generating")
        self.engine.generate(job_id, pw=self.pw)
        self.db.set_status(job_id, "filling")
        self._fill(job_id)

    def _resume(self, job_id, answers: dict | None = None, remember: bool = True, **_):
        row = self.db.get(job_id)
        data = row.get("data") or {}
        if answers:
            stored = data.get("user_answers") or {}
            saved = SavedAnswers(self.engine.ws.answers_path)
            for label, value in answers.items():
                if value in (None, ""):
                    continue
                stored[label_key(label)] = value
                if remember:
                    saved.put(label, value)
            self.db.update_data(job_id, user_answers=stored)
        if data.get("step") == "analyze":
            return self._start(job_id, force=data.get("force", False))
        self.db.set_status(job_id, "filling")
        self._fill(job_id)

    def _fill(self, job_id):
        row = self.db.get(job_id)
        data = row.get("data") or {}
        page = self.page_for(job_id, row["url"])
        platform = detect(page.url)
        _, profile = self.engine.load()
        job = load_yaml(self.engine.ws.job_dir(job_id) / "job.yaml")
        from urllib.parse import unquote
        if re.search(r"en ingl[eé]s|in english", normalize(unquote(page.url or ""))):
            job["language"] = "en"          # la plataforma exige respuestas en inglés (error de validación)
            job["_rewrite_motivation"] = True
        saved = SavedAnswers(self.engine.ws.answers_path)
        files = dict(data.get("files") or {})
        for doc in profile.data.get("documents") or []:
            path = self.engine.ws.root / doc.get("path", "")
            if doc.get("path") and path.exists():
                files[f"doc:{doc.get('name')}"] = str(path)

        from .platforms import BOARD_APPLY_TEXTS, is_job_board
        if not data.get("form_opened"):
            board = is_job_board(page.url)
            fields = [] if board else [f for f in forms.extract_fields(page) if f["type"] not in ("checkbox", "radio")]
            if len(fields) < 2:
                btn = forms.find_apply_button(page, BOARD_APPLY_TEXTS if board else platform.apply_texts)
                applied_hint = None if btn else forms.page_text_has(page, forms.ALREADY_APPLIED_TEXTS)
                if applied_hint:
                    self._shot(job_id, page, "ya_postulada")
                    return self.db.set_status(job_id, "applied",
                                              f"{platform.name} reports you already applied («{applied_hint}»).")
                if not btn and forms.blocked_reason(page):
                    return self.db.set_status(job_id, "skipped",
                                              f"{platform.name} no permite postular: «{forms.blocked_reason(page)}».")
                if not btn:
                    page.bring_to_front()
                    return self._pause(job_id, "Apply button not found. Open the form in the Chrome window "
                                               "and press Continue.", step="fill", kind="manual")
                before = len(self.context().pages)
                self._human_pause(platform.key)
                forms.click_button(page, btn)
                self.settle(page, 3500)
                pages = self.context().pages
                if len(pages) > before:              # la postulación se abrió en otra pestaña
                    page = pages[-1]
                    page.wait_for_load_state("domcontentloaded")
                    self.settle(page, 2000)
                    self.pages[job_id] = page
                    self.db.event(job_id, "info", f"Formulario externo: {page.url}")
                if board and is_job_board(page.url):
                    # El portal no llevó al empleador (p. ej. exige cuenta del portal para ver el enlace).
                    page.bring_to_front()
                    return self._pause(job_id, "The job board does not show the employer's application link "
                                               "(it probably requires a board account).",
                                       step="fill", kind="manual")
            self.db.update_data(job_id, form_opened=True)

        filled_log = data.get("filled") or []
        nudges = 0
        for _step in range(self.max_steps):
            platform = detect(page.url)
            if is_job_board(page.url):           # nunca llenar formularios del propio agregador
                page.bring_to_front()
                return self._pause(job_id, "Still on the job board page, not the employer's.",
                                   step="fill", kind="manual")
            if forms.has_captcha(page):
                page.bring_to_front()
                return self._pause(job_id, "CAPTCHA detected: solve it in the Chrome window and press Continue.",
                                   step="fill", kind="captcha")
            if forms.needs_login(page):
                page.bring_to_front()
                return self._pause(job_id, f"{platform.name} requires signing in or creating an account. Do it in the window "
                                           "and press Continue.", step="fill", kind="login")

            fields = forms.extract_fields(page)
            if job.get("_rewrite_motivation"):    # reescribir en inglés la motivación que quedó en español
                from .answers import MOTIVATION
                for f in fields:
                    if f["type"] in ("textarea", "text") and re.search(MOTIVATION, normalize(f["label"] + " " + f.get("name", ""))):
                        f["value"] = ""
            if any(f["type"] == "file" and "cover_letter" not in files and
                   any(k in f["label"].lower() for k in ("carta", "cover", "motivac")) for f in fields):
                letter = self.engine.cover_letter(job_id, pw=self.pw)
                if letter:
                    files["cover_letter"] = letter

            values, questions = resolve(fields, profile, saved, files, job,
                                        (self.db.get(job_id).get("data") or {}).get("user_answers"))
            by_id = {f["id"]: f for f in fields}
            blockers = [f["_ineligible"] for f in fields if f.get("_ineligible")]
            if blockers:                     # el formulario revela que no es para alguien que vive en Chile
                self._shot(job_id, page, "no_elegible")
                return self.db.set_status(job_id, "skipped", f"Not eligible from Chile: {blockers[0]} "
                                                             "(mandatory form question).")
            codes = [f for f in fields if re.search(r"(?i)verification code|security code|codigo de verificacion|"
                                                    r"código de verificación|one[- ]time code", f.get("label", ""))]
            if codes:
                self._shot(job_id, page, "codigo_correo")
                page.bring_to_front()
                return self._pause(job_id, "The portal emailed you a verification code to complete the "
                                           "submission. Enter it yourself (cvtitan postular --pendientes --ver).",
                                   step="fill", kind="code")
            for fid, value in values.items():
                f = by_id[fid]
                ok = forms.fill_field(page, f, value)
                shown = value if f["type"] != "file" else value.split("\\")[-1].split("/")[-1]
                filled_log.append({"label": f["label"][:120], "value": str(shown)[:300], "ok": ok,
                                   "by": f.get("_answered_by", "perfil")})
                if not ok and f.get("required"):
                    questions.append(f)
            self.db.update_data(job_id, filled=filled_log[-80:])

            if questions:
                qs = [{"label": f["label"], "type": f["type"], "options": f.get("options"),
                       "question": f.get("question"), "proposal": f.get("proposal")} for f in questions]
                page.bring_to_front()
                return self._pause(job_id, f"{len(qs)} question(s) I cannot answer from your profile.",
                                   step="fill", kind="questions", questions=qs)

            buttons = forms.find_buttons(page)
            if buttons["next"]:
                signature = (page.url, tuple(f["label"] for f in fields))
                self._human_pause(platform.key)
                forms.click_button(page, buttons["next"][0])
                self.settle(page, 2500)
                after = forms.extract_fields(page)
                if (page.url, tuple(f["label"] for f in after)) == signature:
                    errors = forms.validation_errors(page)
                    empty = [f for f in after if f.get("required") and not f.get("value") and f["type"] != "checkbox"]
                    if errors or empty:
                        qs = [{"label": f["label"], "type": f["type"], "options": f.get("options"),
                               "question": None} for f in empty]
                        page.bring_to_front()
                        return self._pause(job_id, "The form did not advance: " + ("; ".join(errors)[:300] or
                                           "required fields are still empty."), step="fill",
                                           kind="questions" if qs else "manual", questions=qs)
                continue

            if buttons["submit"]:
                shot = self._shot(job_id, page, "revision")
                self.db.update_data(job_id, screenshot=shot, step="review")
                page.bring_to_front()
                return self.db.set_status(job_id, "awaiting_review",
                                          "Review the form in Chrome and confirm the submission from CV TITAN.")
            if forms.submitted(page):
                return self.db.set_status(job_id, "applied", "The platform confirmed the application.")
            if nudges < 2 and forms.disabled_progress(page):
                # «Siguiente/Enviar» deshabilitado con todo ya completo (p. ej. editores prellenados de Get on
                # Board): el sitio espera eventos de teclado para validar. Se simulan y se reintenta el paso.
                nudges += 1
                touched = forms.nudge_inputs(page)
                self.db.event(job_id, "info", f"Form validation nudged ({touched} field(s)); retrying step.")
                self.settle(page, 1500)
                continue
            page.bring_to_front()
            return self._pause(job_id, "Unsure how to continue on this page. Advance manually in Chrome and press "
                                       "Continue, or mark the application as sent.", step="fill", kind="manual")
        return self._pause(job_id, "The form has too many steps; finish in Chrome and mark as sent.",
                           step="fill", kind="manual")

    def _submit(self, job_id, **_):
        row = self.db.get(job_id)
        page = self.pages.get(job_id)
        if page is None or page.is_closed():
            return self.db.set_status(job_id, "needs_input", "The form window was closed. Press Retry.")
        platform = detect(row["url"])
        limits = self.engine.settings.get("daily_limits") or {}
        limit = int(limits.get(platform.key, limits.get("default", 40)))
        if self.db.count_applied_today(platform.key) >= limit:
            return self.db.set_status(job_id, "awaiting_review",
                                      f"{platform.name} daily limit reached ({limit}). Submit it tomorrow.")
        buttons = forms.find_buttons(page)
        if not buttons["submit"]:
            if buttons["next"]:          # p. ej. "Revisar" antes de "Enviar"
                self.db.set_status(job_id, "filling")
                return self._fill(job_id)
            return self.db.set_status(job_id, "awaiting_review",
                                      "Submit button not found; submit in Chrome and mark as sent.")
        target = sorted(buttons["submit"], key=lambda b: -len(b["text"]))[0]
        self.db.set_status(job_id, "submitting")
        self._human_pause(platform.key)
        if self.on_before_submit:
            self.on_before_submit(job_id)
        forms.click_button(page, target)
        for _ in range(12):
            page.wait_for_timeout(500)
            if forms.submitted(page):
                break
        if forms.submitted(page):
            shot = self._shot(job_id, page, "confirmacion")
            self.db.update_data(job_id, confirmation=shot, step="done")
            return self.db.set_status(job_id, "applied", "Application submitted and confirmed by the platform.")
        errors = forms.validation_errors(page)
        if errors:
            self.db.event(job_id, "warning", "; ".join(errors)[:500])
            self.db.set_status(job_id, "filling")
            return self._fill(job_id)
        buttons = forms.find_buttons(page)
        if buttons["submit"] or buttons["next"]:
            self.db.set_status(job_id, "filling")
            return self._fill(job_id)
        shot = self._shot(job_id, page, "despues_envio")
        self.db.update_data(job_id, confirmation=shot)
        return self.db.set_status(job_id, "awaiting_review",
                                  "Clicked Submit but saw no clear confirmation. Verify in Chrome and mark as sent.")
