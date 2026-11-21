"""Integración con la API de Claude.

Tres usos, todos con salida estructurada y validada:
1. extract_job: entiende la vacante (país, modalidad, salario, requisitos).
2. rewrite_cv: mejora la redacción de cada bloque del CV conservando el hecho.
   Cada reescritura pasa luego por la auditoría; si falla, se vuelve al texto original.
3. answer_fields: completa formularios usando solo el perfil, las preferencias y la vacante.
   Si un dato no está en esas fuentes, marca needs_user=True en vez de inventarlo.

Sin credenciales, todo el sistema sigue funcionando en modo determinista.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional, TypeVar

from pydantic import BaseModel, Field

from . import llm_privacy

MODEL = os.environ.get("CVTITAN_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(Exception):
    pass


def load_env(root: Path) -> None:
    """Carga variables de <root>/.env sin pisar las que ya existen."""
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def available() -> bool:
    """IA solo si hay clave Y el candidato la activó (config/settings.yaml -> llm.enabled: true)."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return has_key and os.environ.get("CVTITAN_LLM_ENABLED") == "1"


_M = TypeVar("_M", bound=BaseModel)


def _parse(system: str, user: str, schema: type[_M], effort: str = "medium",
           max_tokens: int = 16000, purpose: str = "unknown", sent: list[str] | None = None) -> _M:
    if not available():
        raise LLMUnavailable("No hay ANTHROPIC_API_KEY configurada (archivo .env).")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable('Falta el paquete "anthropic". Instálalo con: pip install -e ".[ai]"') from exc

    try:
        response = anthropic.Anthropic().beta.messages.parse(
            model=MODEL,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable(f"API key inválida: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("Límite de uso de la API alcanzado; reintenta en unos minutos.") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"Error de la API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("Sin conexión con la API de Claude.") from e

    if response.stop_reason == "refusal":
        raise LLMUnavailable("El modelo declinó la solicitud.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise LLMUnavailable("Respuesta incompleta del modelo.")
    llm_privacy.record(purpose, MODEL, sent or [], len(system) + len(user), getattr(response, "usage", None))
    return response.parsed_output


# ---------------------------------------------------------------------- 1. vacante
class JobFacts(BaseModel):
    title: str
    company: Optional[str] = None
    country: Optional[str] = Field(None, description="País del puesto en español, p. ej. 'Chile'")
    city: Optional[str] = None
    modality: Optional[Literal["remoto", "híbrido", "presencial"]] = None
    seniority: Optional[Literal["trainee", "junior", "semi-senior", "senior", "lead"]] = None
    years_required: Optional[int] = None
    salary_text: Optional[str] = Field(None, description="Salario tal como aparece, o null si no se publica")
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = Field(None, description="ISO 4217: CLP, USD, EUR...")
    salary_period: Optional[Literal["hora", "mes", "año"]] = None
    required_skills: list[str] = Field(default_factory=list, description="Habilidades técnicas o de dominio obligatorias")
    preferred_skills: list[str] = Field(default_factory=list, description="Habilidades deseables")
    responsibilities: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list, description="Idioma y nivel, p. ej. 'Inglés intermedio'")


JOB_SYSTEM = (
    "Eres un analista de reclutamiento. Extraes datos de una oferta de empleo con exactitud. "
    "Usa null cuando un dato no esté en el texto: nunca lo deduzcas ni lo inventes. "
    "Las habilidades deben ser nombres cortos y canónicos (p. ej. 'Python', 'Power BI', 'Gestión de stakeholders'), "
    "no frases. No incluyas títulos académicos ni beneficios como habilidades."
)


def extract_job(text: str, url: str | None = None) -> JobFacts:
    user = f"URL: {url or 'no disponible'}\n\n<oferta>\n{text}\n</oferta>"
    return _parse(JOB_SYSTEM, user, JobFacts, effort="low", purpose="extract_job", sent=["url", "oferta"])


# ---------------------------------------------------------------------- 2. CV
class Rewrite(BaseModel):
    source: str
    text: str


class CVRewrite(BaseModel):
    headline: str
    summary: str
    bullets: list[Rewrite]


CV_SYSTEM = (
    "Eres un experto en CV para ATS y reclutadores. Reescribes un CV para una vacante concreta.\n"
    "Reglas no negociables:\n"
    "- Cada bullet conserva exactamente el mismo hecho de su fuente (campo `source_text`). Puedes cambiar el orden "
    "de las palabras, usar la terminología de la vacante y empezar con un verbo de acción.\n"
    "- No agregues cifras, porcentajes, plazos, tamaños de equipo, herramientas ni tecnologías que no estén "
    "en `source_text`. Si una tecnología de la vacante no aparece en la fuente, no la menciones.\n"
    "- Bullets de menos de 35 palabras, en el mismo idioma del CV.\n"
    "- El resumen (máx. 70 palabras) solo usa hechos del resumen original y de las habilidades respaldadas.\n"
    "- El titular (headline) usa el nombre del cargo de la vacante solo si el perfil lo respalda; si no, "
    "conserva el original.\n"
    "Devuelve un elemento en `bullets` por cada bloque recibido, con el mismo `source`."
)


def rewrite_cv(draft: dict, job: dict, backed_skills: list[str]) -> CVRewrite:
    blocks = [{"source": b["source"], "source_text": b["text"]}
              for e in draft["experience"] for b in e["bullets"]]
    payload = {
        "vacante": {"cargo": job.get("title"), "empresa": job.get("company"),
                    "requisitos": [s["name"] for s in job["skills"] if s["importance"] == "required"],
                    "responsabilidades": job.get("responsibilities", [])[:10]},
        "headline_original": draft.get("headline"),
        "resumen_original": draft["summary"]["text"],
        "habilidades_respaldadas": backed_skills,
        "bloques": blocks,
    }
    return _parse(CV_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1), CVRewrite, effort="medium",
                  purpose="rewrite_cv", sent=list(payload))


# ---------------------------------------------------------------------- 3. formularios
class FieldAnswer(BaseModel):
    field_id: str
    value: Optional[str] = Field(None, description="Valor a escribir o la opción exacta a elegir; null si falta el dato")
    source: Literal["perfil", "preferencias", "respuestas_guardadas", "conocimiento", "vacante", "redactado", "ninguna"]
    needs_user: bool = Field(description="True si el dato no está en las fuentes y debe preguntarse al candidato")
    question_for_user: Optional[str] = None


class FormAnswers(BaseModel):
    answers: list[FieldAnswer]


FORM_SYSTEM = (
    "Completas formularios de postulación laboral en nombre de un candidato, usando solo las fuentes entregadas: "
    "perfil verificado, preferencias, respuestas guardadas y la vacante.\n"
    "- Nunca inventes datos personales, cifras, años de experiencia, certificaciones, salario, visa, "
    "disponibilidad ni respuestas legales o demográficas. Si el dato no está en las fuentes: value=null, "
    "needs_user=true y una pregunta breve en español en question_for_user.\n"
    "- Para select o radio, value debe ser exactamente una de las opciones listadas.\n"
    "- Para preguntas abiertas de motivación o presentación ('¿por qué te interesa?', 'cuéntanos de ti'), "
    "redacta 2-4 frases con hechos reales del perfil relacionados con la vacante (source='redactado').\n"
    "- Para años de experiencia con una tecnología, responde solo si el perfil permite calcularlo; si no, pregunta.\n"
    "- `conocimiento_del_candidato` son respuestas del propio candidato, con sus palabras. Para preguntas abiertas de ese estilo, reutiliza sus ideas, hechos y tono (adaptando la redacción a la pregunta y a la vacante, source='conocimiento'); no agregues hechos, cifras ni opiniones que no estén ahí o en el perfil.\n"
    "- Responde en el idioma de la pregunta."
)


def answer_fields(fields: list[dict], context: dict) -> FormAnswers:
    context = llm_privacy.scrub_contact(context)
    user = json.dumps({"fuentes": context, "campos": fields}, ensure_ascii=False, indent=1)
    return _parse(FORM_SYSTEM, user, FormAnswers, effort="medium", purpose="answer_fields",
                  sent=["fuentes." + k for k in context] + ["campos"])


class CoverLetter(BaseModel):
    text: str


def cover_letter(job: dict, context: dict) -> str:
    context = llm_privacy.scrub_contact(context)
    system = ("Redactas cartas de presentación breves (máx. 220 palabras) y concretas, en el idioma de la vacante. "
              "Solo usas hechos del perfil entregado; no inventes cifras, herramientas ni logros. "
              "Sin fórmulas vacías ni adulación.")
    user = json.dumps({"vacante": {"cargo": job.get("title"), "empresa": job.get("company"),
                                   "requisitos": [s["name"] for s in job["skills"][:12]]},
                       "candidato": context}, ensure_ascii=False, indent=1)
    return _parse(system, user, CoverLetter, effort="medium", purpose="cover_letter",
                  sent=["vacante"] + ["candidato." + k for k in context]).text
