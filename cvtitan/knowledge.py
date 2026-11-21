"""Base de conocimiento del candidato: sus respuestas, con sus palabras, a las preguntas abiertas de los formularios.

Archivo local `candidate/knowledge.yaml` (ignorado por git, nunca se publica). Se llena con `cvtitan entrevista`
(entrevista guiada) y con las preguntas que quedaron pendientes en una postulación. Sirve para:

1. Responder de forma determinista, con sus palabras, preguntas que coinciden con un tema (sin IA).
2. Darle a la IA (si el candidato la activó) su voz y sus hechos, para redactar variantes sin inventar.

Reglas: nunca se inventa. Lo que no está aquí ni en el perfil queda pendiente o va a revisión del candidato.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import PACKAGE_ROOT, dump_yaml, load_yaml, normalize

KNOWLEDGE_FILE = "knowledge.yaml"
PENDING_FILE = Path("data") / "pending_questions.jsonl"
CATALOG_FILE = "interview_questions.yaml"
MIN_LABEL_CHARS = 14            # «País», «Ciudad», «Nombre»: campos, no preguntas abiertas
# Preguntas cuyo error cuesta (dinero, mudanza, salud, visa, antecedentes): si las redacta la IA, el candidato las revisa.
SENSITIVE_Q = re.compile(
    r"salari|salary|sueldo|renta|remuner|compensation|pretension|expectativa|visa|permiso de trabajo|work permit|"
    r"authori[sz]ed to work|relocat|reubica|mudar|trasladar|salud|health|enfermedad|medical|discapacidad|disability|"
    r"antecedentes|criminal|convict|background check|embarg|demanda|lawsuit|referencias|references|notice period|"
    r"preaviso|disponibilidad|availability|start date|fecha de inicio")


def catalog_path(root: Path) -> Path:
    local = root / "config" / CATALOG_FILE
    return local if local.exists() else PACKAGE_ROOT / "config" / CATALOG_FILE


def load_catalog(root: Path) -> list[dict[str, Any]]:
    return list((load_yaml(catalog_path(root)) or {}).get("questions") or [])


def _is_english(label: str) -> bool:
    return len(re.findall(r"\b(you|your|do|have|are|what|how|why|the|of|with|tell|describe|about)\b", label)) >= 2


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(r"(?<![a-z0-9])(?:" + p + ")"))
        except re.error:
            continue                # un patrón roto nunca debe impedir llenar el formulario
    return out


class Knowledge:
    """`candidate/knowledge.yaml`: {entries: {id: {es, en, source, updated, patterns?, question?}}}."""

    def __init__(self, root: Path, candidate_dir: Path | None = None):
        self.root = root
        self.path = (candidate_dir or root / "candidate") / KNOWLEDGE_FILE
        self.entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self.entries = dict((load_yaml(self.path) or {}).get("entries") or {})
            except Exception:
                self.entries = {}
        self.catalog = {q["id"]: q for q in load_catalog(root)}

    # ------------------------------------------------------------------ escritura
    def put(self, key: str, es: str = "", en: str = "", *, source: str = "entrevista",
            question: str = "", patterns: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"es": es.strip(), "en": en.strip(), "source": source,
                                 "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        if question:
            entry["question"] = question
        if patterns:
            entry["patterns"] = patterns
        self.entries[key] = entry
        dump_yaml({"entries": dict(sorted(self.entries.items()))}, self.path)

    def remove(self, key: str) -> bool:
        if key in self.entries:
            del self.entries[key]
            dump_yaml({"entries": dict(sorted(self.entries.items()))}, self.path)
            return True
        return False

    # ------------------------------------------------------------------ lectura
    def answered(self, key: str) -> bool:
        e = self.entries.get(key)
        return bool(e and (e.get("es") or e.get("en")))

    def progress(self) -> dict[str, tuple[int, int]]:
        """topic -> (respondidas, total) sobre el catálogo."""
        out: dict[str, list[int]] = {}
        for qid, q in self.catalog.items():
            t = out.setdefault(q.get("topic", "otros"), [0, 0])
            t[1] += 1
            t[0] += 1 if self.answered(qid) else 0
        return {k: (v[0], v[1]) for k, v in out.items()}

    def _patterns_for(self, key: str, entry: dict[str, Any]) -> list[re.Pattern[str]]:
        pats = list(entry.get("patterns") or []) or list((self.catalog.get(key) or {}).get("patterns") or [])
        return _compile([str(p) for p in pats])

    def answer(self, field: dict[str, Any]) -> str | None:
        """Respuesta del candidato para una pregunta abierta de texto; None si no hay tema que calce.

        Solo campos de texto libre: las listas y casillas se responden con reglas, diccionario y declaraciones.
        """
        if field.get("type") not in ("text", "textarea"):
            return None
        label = normalize(field.get("label", ""))
        if len(label) < MIN_LABEL_CHARS:
            return None
        plain = pending_key(label)          # sin signos: calza las preguntas aprendidas de postulaciones
        english = _is_english(label)
        for key, entry in self.entries.items():
            text = (entry.get("en") if english else entry.get("es")) or ""
            if not text:
                continue                  # sin versión en el idioma de la pregunta: que decida la IA o el candidato
            if any(rx.search(text_) for rx in self._patterns_for(key, entry) for text_ in (label, plain)):
                return str(text)
        return None

    def context(self, limit: int = 60) -> list[dict[str, str]]:
        """Para la IA: las palabras del propio candidato (nunca se le pide inventar más)."""
        rows = []
        for key, entry in list(self.entries.items())[:limit]:
            text = entry.get("es") or entry.get("en") or ""
            if text:
                q = entry.get("question") or (self.catalog.get(key) or {}).get("ask") or key
                rows.append({"tema": key, "pregunta": q, "respuesta_del_candidato": text})
        return rows


# ---------------------------------------------------------------------- preguntas pendientes (aprender de ellas)
def pending_key(label: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", normalize(label)).strip()[:160]


def record_pending(root: Path, questions: list[dict[str, Any]], job: dict[str, Any] | None = None) -> int:
    """Guarda (sin duplicar) las preguntas que el agente no pudo responder; `cvtitan entrevista --pendientes` las recorre."""
    path = root / PENDING_FILE
    known: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                known[row["key"]] = row
            except (ValueError, KeyError):
                continue
    new = 0
    for q in questions:
        label = str(q.get("label") or "").strip()
        key = pending_key(label)
        if not key:
            continue
        row = known.get(key)
        if row is None:
            known[key] = {"key": key, "label": label[:300], "type": q.get("type"), "options": q.get("options"),
                          "proposal": q.get("proposal"), "count": 1,
                          "first_company": (job or {}).get("company") or "",
                          "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            new += 1
        else:
            row["count"] = int(row.get("count") or 1) + 1
            if q.get("proposal") and not row.get("proposal"):
                row["proposal"] = q["proposal"]
    if known:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in known.values()) + "\n", encoding="utf-8")
    return new


def read_pending(root: Path) -> list[dict[str, Any]]:
    path = root / PENDING_FILE
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return sorted(rows, key=lambda r: -int(r.get("count") or 1))


def drop_pending(root: Path, keys: set[str]) -> None:
    rows = [r for r in read_pending(root) if r.get("key") not in keys]
    path = root / PENDING_FILE
    if rows:
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def learned_id(label: str) -> str:
    return "p-" + hashlib.sha1(pending_key(label).encode("utf-8")).hexdigest()[:8]
