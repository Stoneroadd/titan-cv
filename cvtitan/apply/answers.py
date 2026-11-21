"""Decide qué valor va en cada campo de un formulario.

Prioridad: respuestas guardadas del candidato > reglas sobre el perfil y preferencias > Claude.
Lo que ninguna fuente puede responder con certeza se pregunta al candidato (nunca se inventa).
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from .. import llm
from ..profile import Profile
from ..utils import dump_yaml, load_yaml, normalize

DECLINE = ("prefiero no", "prefer not", "decline", "no deseo", "no responder", "not to answer", "no especificar",
           "i don't wish", "i do not wish")
EEO = r"genero|gender|sexo|raza|race|etnia|ethnic|veteran|discapacidad|disability|orientacion sexual|pronombre|pronoun"


def label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", normalize(label)).strip()[:160]


class SavedAnswers:
    """candidate/answers.yaml: respuestas que el candidato dio una vez y se reutilizan."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = (load_yaml(path).get("answers") or {}) if path.exists() else {}

    def get(self, label: str) -> str | None:
        key = label_key(label)
        if not key:
            return None
        if key in self.data:
            return self.data[key]
        match = difflib.get_close_matches(key, list(self.data), n=1, cutoff=0.92)
        return self.data[match[0]] if match else None

    def put(self, label: str, value: str) -> None:
        key = label_key(label)
        if key and value not in (None, ""):
            self.data[key] = value
            dump_yaml({"answers": dict(sorted(self.data.items()))}, self.path)


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    if len(parts) >= 4:
        return " ".join(parts[:2]), " ".join(parts[2:])
    return parts[0] if parts else "", " ".join(parts[1:])


CURRENCY_HINTS = [("USD", r"\busd\b|dolar|dollar|us\$"), ("CLP", r"\bclp\b|pesos? chilenos?|\bpesos?\b"),
                  ("EUR", r"\beur\b|euro"), ("UF", r"\buf\b")]
# Campos cuya moneda fija la plataforma aunque no la diga la etiqueta.
FIELD_CURRENCY = {"job_application[expected_salary]": "USD"}      # Get on Board: USD brutos al mes


def _currency(field: dict, label: str, job: dict | None) -> str | None:
    if field.get("name") in FIELD_CURRENCY:
        return FIELD_CURRENCY[field["name"]]
    for code, pattern in CURRENCY_HINTS:
        if re.search(pattern, label):
            return code
    job = job or {}
    if "getonbrd.com" in (job.get("url") or ""):
        return "USD"                         # Get on Board siempre pide USD brutos al mes
    if (job.get("salary") or {}).get("salary_currency"):
        return job["salary"]["salary_currency"]
    return "CLP" if job.get("country") == "Chile" else None


def _num(text: str) -> float | None:
    t = normalize(text).replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d{3})*(?:[.,]\d+)?)(?![\d.,])(?:(mm|millones|m|k|mil)(?![a-z]))?", t)
    if not m:
        return None
    raw, unit = m.group(1), m.group(2)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        value = float(re.sub(r"[.,]", "", raw))
    else:
        value = float(raw.replace(",", "."))
    if unit in ("mm", "m", "millones"):
        value *= 1_000_000
    elif unit in ("k", "mil"):
        value *= 1_000
    return value


def option_range(option: str) -> tuple[float, float] | None:
    """'$1.500.000 - $2.000.000' -> (1.5M, 2M); 'Más de 3 millones' -> (3M, inf); 'Hasta 1M' -> (0, 1M)."""
    o = normalize(option)
    nums = [n for n in (_num(x) for x in re.findall(r"\d[\d.,]*\s*(?:mm|m\b|millones|k\b|mil\b)?", o)) if n]
    if not nums:
        return None
    if re.search(r"mas de|sobre|superior a|mayor a|over|more than|\+|o mas", o):
        return nums[0], float("inf")
    if re.search(r"hasta|menos de|inferior a|menor a|under|less than|up to", o):
        return 0.0, nums[0]
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    return nums[0], nums[0]


def pick_range(options: list[str], amount: float) -> str | None:
    """Opción cuyo rango contiene `amount` (o la más cercana)."""
    best, best_dist = None, None
    for o in options or []:
        r = option_range(o)
        if not r:
            continue
        lo, hi = r
        dist = 0 if lo <= amount <= hi else min(abs(amount - lo), abs(amount - hi))
        if best_dist is None or dist < best_dist:
            best, best_dist = o, dist
    return best


PERIOD_ANNUAL = r"anual|annual|per year|yearly|a year|al ano|por ano|/\s*a(n|ñ)o|/\s*y(ea)?r|\bpa\b|per annum"
PERIOD_HOURLY = r"por hora|hourly|per hour|an hour|/\s*h(ou)?r\b|tarifa horaria|hour rate"


def _salary_answer(field: dict, label: str, prefs: dict, job: dict | None = None) -> str | None:
    """Pretensión de renta desde preferences.salary_gross {MONEDA: monto MENSUAL}, p. ej. {USD: 3000, CLP: 3000000}.
    - Moneda: la que pide el campo/portal/vacante; si no se sabe, CLP para Chile y USD para el resto.
    - Período: mensual por defecto; anual = ×12; por hora = ÷160.
    - Bruto y líquido usan la misma pretensión (indicada así por el candidato).
    Si no hay pretensión en esa moneda, se convierte con settings.fx solo entre USD y CLP; si no, pendiente."""
    monthly = prefs.get("salary_gross") or {}
    job = job or {}
    currency = _currency(field, label, job)
    if not currency:
        currency = "CLP" if job.get("country") == "Chile" else "USD"
    amount = monthly.get(currency)
    if amount is None:
        # Compatibilidad: perfiles con solo `salary_expectation` (texto). Se usa tal cual si la moneda coincide
        # o si el campo no indica moneda; nunca se convierte entre monedas sin un monto explícito.
        text = prefs.get("salary_expectation")
        if not text or (re.search(r"[a-z]{3}", normalize(str(text))) and currency.lower() not in normalize(str(text))
                        and _currency(field, label, {}) is not None):
            return None
        if field.get("options"):
            n = _num(str(text))
            return pick_range(field["options"], n) if n else None
        if field.get("type") == "number":
            return re.sub(r"[^\d]", "", str(text).split(",")[0]) or None
        return str(text)
    amount = float(amount)
    period = "annual" if re.search(PERIOD_ANNUAL, label) else "hourly" if re.search(PERIOD_HOURLY, label) else "monthly"
    if period == "annual":
        amount *= 12
    elif period == "hourly":
        amount = round(amount / 160, 2)
    if field.get("options"):
        return pick_range(field["options"], amount)
    if field.get("type") == "number":
        return f"{amount:.2f}".rstrip("0").rstrip(".") if period == "hourly" else str(int(round(amount)))
    english = bool(re.search(r"\b(desired|expected|expectation|salary|compensation|rate|per|your)\b", label))
    num = (f"{amount:,.2f}" if period == "hourly" else f"{int(round(amount)):,}")
    if english:
        unit = {"monthly": "per month", "annual": "per year", "hourly": "per hour"}[period]
        return f"{currency} {num} {unit}"
    num = num.replace(",", "X").replace(".", ",").replace("X", ".")
    unit = {"monthly": "mensuales", "annual": "anuales", "hourly": "por hora"}[period]
    return f"{currency} {num} {unit}"


MOTIVATION = r"por que te interesa|por que quieres|razon para postular|motivacion|why (are you|do you)|interested in|reason to apply|reason_to_apply"


# «¿Por qué trabajar con nosotros?»: respuesta genérica del candidato (preferences.motivation_generic),
# sin nombrar a la empresa.
WHY_US = (r"con nosotros|trabajar aqui|unirte a nuestro|unirte a nosotros|nuestra empresa|nuestro equipo|"
          r"why us|with us|join us|join our|work here|our company|our team")


def _english(label: str) -> bool:
    return bool(re.search(r"\b(why|what|your|you|our|with|the)\b", label)) and not re.search(r"¿|\bpor que\b", label)


def _motivation(field: dict, label: str, profile: Profile, job: dict | None) -> str | None:
    """Respuesta a «¿por qué te interesa…?» armada solo con hechos del perfil (sin inventar)."""
    if field.get("type") not in ("textarea", "text"):
        return None
    prefs = profile.data.get("preferences") or {}
    generic = prefs.get("motivation_generic") or {}
    if re.search(WHY_US, label) and generic:
        text = generic.get("en" if _english(label) else "es") or generic.get("es")
        return " ".join(str(text).split()) if text else None
    if not re.search(MOTIVATION, label):
        return None
    job = job or {}
    prefs = profile.data.get("preferences") or {}
    named = re.search(r"(?:trabajar en|working at|work at|join)\s+(.+?)\s*\??$", field.get("label", ""), re.I)
    company = job.get("company") or (named.group(1).strip(" ?¿") if named else "la empresa")
    title = job.get("title") or "este cargo"
    asked = [s["key"] for s in job.get("skills") or []]
    backed = [profile.tax.name(k) for k in asked if profile.is_backed(k)][:5]
    projects = [p for p in profile.data.get("projects") or [] if p.get("status", "completed") == "completed"]
    keys = set(asked)
    best = max(projects, key=lambda p: len(keys & {profile.tax.key_for(s, register=False) for s in p.get("skills") or []}),
               default=None)
    exp = (profile.data.get("experience") or [{}])[0]
    parts = []
    if _english(label) or job.get("language") == "en":
        # Versión en inglés solo con nombres (skills, proyecto, cargo): las descripciones del perfil están en español.
        skills_txt = (", ".join(backed[:-1]) + (" and " if len(backed) > 1 else "") + backed[-1]) if backed else ""
        parts.append(f"I am interested in the {title} role at {company}"
                     + (f" because it requires {skills_txt}, which I use in real projects." if backed else "."))
        if best:
            used = [s for s in best.get("skills") or [] if profile.is_backed(profile.tax.key_for(s, register=False))]
            parts.append(f"I built {best['name'].split(':')[0]}" + (f" using {', '.join(used[:5])}." if used else "."))
        if exp.get("role"):
            parts.append(f"I currently work as {exp['role']} at {exp.get('company', '')}, combining operations and data.")
        parts.append("I want to contribute by building software that solves concrete business problems.")
        if prefs.get("salary_statement_en"):
            parts.append(prefs["salary_statement_en"])
        text = " ".join(parts)
        return text[:990].rsplit(" ", 1)[0] + "…" if len(text) > 1000 else (text if len(text) >= 50 else None)
    if backed:
        parts.append(f"Me interesa el cargo de {title} en {company} porque pide {', '.join(backed[:-1])}"
                     f"{' y ' if len(backed) > 1 else ''}{backed[-1]}, tecnologías que uso en proyectos reales.")
    else:
        parts.append(f"Me interesa el cargo de {title} en {company} y el desafío de aportar con mi experiencia.")
    if best:
        desc = re.split(r"(?<=\.)\s+", " ".join(str(best.get("description", "")).split()))
        parts.append(f"Desarrollé {best['name']}. " + " ".join(desc[:2]))
    if exp.get("role"):
        parts.append(f"Hoy me desempeño como {exp['role']} en {exp.get('company', '')}, donde combino operación y datos.")
    parts.append("Quiero aportar construyendo software que resuelva problemas concretos del negocio.")
    if prefs.get("salary_statement_es"):
        parts.append(prefs["salary_statement_es"])
    text = " ".join(parts)
    if len(text) > 1000:
        text = text[:990].rsplit(" ", 1)[0] + "…"
    return text if len(text) >= 50 else None


def _heard_from(field: dict, job: dict | None) -> str:
    """Fuente real donde se encontró la vacante (para «How did you hear about us?»)."""
    src = normalize(((job or {}).get("url") or "") + " " + ((job or {}).get("source") or ""))
    name = ("LinkedIn" if "linkedin" in src else "Get on Board" if "getonbrd" in src else
            "Remote OK" if "remoteok" in src else "We Work Remotely" if "weworkremotely" in src else
            "Remotive" if "remotive" in src else "Job board")
    opts = field.get("options") or []
    for want in (name, "Job board", "Job Board", "Online job board", "Other", "Otro"):
        for o in opts:
            if normalize(want) in normalize(o):
                return o
    return name


def _pick_or_text(field: dict, patterns: list[str], text: str) -> str | None:
    """Con opciones: la primera que calce con los patrones (o None = pendiente). Sin opciones: el texto."""
    opts = field.get("options") or []
    if not opts:
        return text
    for pat in patterns:
        for o in opts:
            if re.search(pat, normalize(o)):
                return o
    return None


def profile_url(value: str | None) -> str | None:
    """'linkedin.com/in/josé-x' -> 'https://www.linkedin.com/in/jos%C3%A9-x' (formato que aceptan los formularios)."""
    if not value:
        return None
    from urllib.parse import quote, urlsplit, urlunsplit
    v = str(value).strip()
    if not re.match(r"https?://", v):
        v = "https://" + (("www." + v) if v.startswith("linkedin.com") else v)
    parts = urlsplit(v)
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%-_.~"), parts.query, parts.fragment))


def rule_value(field: dict, profile: Profile, files: dict, job: dict | None = None) -> str | None:
    """Campos estándar que se responden directo desde el perfil, sin IA."""
    label = normalize(field.get("label", "")) + " " + normalize(field.get("name", ""))
    p = profile.data.get("personal") or {}
    prefs = profile.data.get("preferences") or {}
    t = field["type"]

    if t == "file":
        if re.search(r"carta|cover|motivaci|presentaci", label):
            return files.get("cover_letter")
        if re.search(r"\bcv\b|curriculum|resume|resumé|hoja de vida|cv/", label) or field.get("required"):
            return files.get("cv")
        for doc in profile.data.get("documents") or []:
            keys = [normalize(k) for k in [doc.get("type", ""), doc.get("name", ""), *(doc.get("keywords") or [])]]
            if any(k and k in label for k in keys):
                return files.get(f"doc:{doc.get('name')}")
        return None

    from .legal import legal_answer
    handled, legal_value, why = legal_answer(field, label, profile.declarations(), job or {})
    if handled and not (t in ("text", "textarea") and re.search(r"donde|where|which city|ciudad|direccion|address|"
                                                               r"ubicacion|location", label)):
        field["_answered_by"] = f"declaraciones: {why}" if legal_value else f"pendiente: {why}"
        return legal_value

    motivation = _motivation(field, label, profile, job)
    if motivation:
        field["_answered_by"] = "plantilla con tu evidencia (revísala)"
        return motivation

    first, last = _split_name(p.get("name", ""))
    first, last = p.get("first_name") or first, p.get("last_name") or last
    rules = [
        (r"full name|nombre completo|nombre y apellido|^name\b", p.get("name")),
        (r"first name|^nombres?\b(?!.*(empresa|company|referencia))|given name", first),
        (r"last name|apellidos?|surname|family name", last),
        (r"e-?mail|correo", p.get("email")),
        (r"phone|telefono|celular|movil|whatsapp|mobile", p.get("phone")),
        (r"how did you (hear|find|learn)|where did you (hear|find)|como (te enteraste|supiste|llegaste)|"
         r"donde (viste|encontraste)|fuente de (la )?postulacion|source of (application|referral)",
         _heard_from(field, job)),
        (r"linkedin|perfil profesional en linea|perfil de linkedin|linked in", profile_url(p.get("linkedin"))),
        (r"github", p.get("github")),
        (r"portfolio|portafolio|sitio web|website|personal site|pagina web|url de tu perfil|profile url",
         profile_url(p.get("website") or p.get("github") or p.get("linkedin"))),
        (r"ciudad|city|comuna|localidad|ubicacion|location|located|direccion|address|where do you live|donde vives",
         p.get("location")),
        (r"pais|country", prefs.get("country")),
        (r"pretension|expectativa|salari|sueldo|renta|remuneracion|compensation|salary|desired pay|"
         r"hourly rate|rate expectation|tarifa",
         prefs.get("salary_expectation")),
        (r"disponibilidad|availability|fecha de inicio|start date|notice period|preaviso|cuando podrias",
         prefs.get("availability")),
        (r"residencia legal|residente legal|legal residen|legally reside|resido en|reside en",
         None),  # depende del país: se resuelve abajo
        (r"autorizad|authorized|permiso de trabajo|work permit|visa|sponsorship|patrocinio",
         None),  # depende del país: se resuelve abajo
        (r"reubica|relocat|traslad", prefs.get("willing_to_relocate")),
        (r"rut\b|dni|documento de identidad|cedula|national id", prefs.get("national_id")),
        (r"fecha de nacimiento|date of birth|birth", prefs.get("birth_date")),
    ]
    for pattern, value in rules:
        if re.search(pattern, label):
            if pattern.startswith("residencia legal"):
                res = prefs.get("legal_residence") or {}
                for country, answer in res.items():
                    if normalize(country) in label:
                        return answer
                return None
            if pattern.startswith("autorizad"):
                auth = prefs.get("work_authorization") or {}
                for country, answer in auth.items():
                    if normalize(country) in label:
                        return answer
                return auth.get("default")
            if pattern.startswith("pretension"):
                return _salary_answer(field, label, prefs, job)
            if value not in (None, ""):
                return str(value)
            return None

    # Sección «Education» de Greenhouse/Lever/Ashby: School · Degree · Discipline, desde el título completado.
    edu = next((e for e in profile.data.get("education") or [] if str(e.get("year", "")).isdigit()), None)
    if edu and re.search(r"^(school|university|institution|college|universidad|institucion|casa de estudios)\b",
                         label):
        return _pick_or_text(field, [normalize(edu["institution"])], edu["institution"])
    if edu and re.search(r"^(degree|titulo obtenido)\b", label):
        # Equivalencia confirmada por el candidato (2026-11-18): título universitario de pregrado (Bachelor's).
        # Nunca se marca un posgrado (Master/PhD).
        return _pick_or_text(field, [r"engineer|ingenier", r"bachelor|licenciad|universitari|undergraduate|pregrado",
                                     r"^other$|^otro$"],
                             prefs.get("degree_equivalence_en") or "Ingeniería en Informática (Computer Engineering)")
    if edu and re.search(r"^(discipline|field of study|major|area of study|carrera)\b", label):
        return _pick_or_text(field, [r"computer (science|engineering)", r"informat", r"information tech", r"software"],
                             "Computer Engineering / Informatics")
    if re.search(r"nivel de (educacion|estudios)|nivel educacional|education level|level of education|"
                 r"grado academico|highest degree", label) and field.get("options"):
        prefer = [r"profesional", r"universitari\w* complet", r"superior complet", r"titulad",
                  r"bachelor|licenciatura|university|universidad"]
        for pat in prefer:
            for o in field["options"]:
                if re.search(pat, normalize(o)) and not re.search(r"incomplet|en curso|cursando", normalize(o)):
                    return o
        return None
    if re.search(r"comente su experiencia|describe (your|su) experiencia|experiencia (relacionada|en relacion)|"
                 r"relevant experience|describe your experience|cuentanos (sobre )?tu experiencia", label) \
            and t in ("textarea", "text"):
        exp = (profile.data.get("experience") or [{}])[0]
        achievements = [" ".join(a["text"].split()) for a in exp.get("achievements") or []][:3]
        text = " ".join([" ".join(str(profile.data.get("summary", "")).split()), *achievements])
        return text[:990].rsplit(" ", 1)[0] if len(text) > 1000 else text
    if re.search(EEO, label) and field.get("options"):
        for o in field["options"]:
            if any(d in normalize(o) for d in DECLINE):
                return o
        # «¿Se siente cómodo comentando su género?» / «¿Desea informar…?»: la opción que no revela datos.
        if re.search(r"comod|desea|quiere|prefier|dispuest|willing|comfortable|would you like|share|disclose|informar",
                     label):
            for o in field["options"]:
                if normalize(o) in ("no", "no deseo", "no, gracias", "no thanks"):
                    return o
        return None
    return None


def resolve(fields: list[dict], profile: Profile, saved: SavedAnswers, files: dict, job: dict,
            user_answers: dict | None = None) -> tuple[dict, list[dict]]:
    """Devuelve ({field_id: valor}, [preguntas para el candidato])."""
    user_answers = user_answers or {}
    values: dict[str, str] = {}
    pending: list[dict] = []
    from .. import knowledge as kb
    from .question_bank import BANK_FILE, QuestionBank
    candidate_dir = Path(getattr(profile, "path", Path("."))).parent
    bank = QuestionBank(profile, candidate_dir / BANK_FILE)
    know = kb.Knowledge(candidate_dir.parent, candidate_dir)
    placeholder = re.compile(r"^(select|selecciona|seleccione|choose|elige)\b[ .…]*(an? option|una opcion)?[ .…]*$")
    for i, f in enumerate(fields):
        # Campo interno de una lista con buscador (react-select de Greenhouse): su «etiqueta» es el texto de ayuda
        # «Select...» y se completa al elegir la opción de la lista anterior. No es una pregunta.
        if placeholder.match(normalize(f.get("label", ""))) and i and fields[i - 1].get("role") == "combobox":
            continue
        if f["type"] == "file" and f.get("value"):
            continue
        if f["type"] not in ("file", "checkbox", "radio") and f.get("value") and f["type"] != "select":
            continue  # ya completado (p. ej. autocompletado de la plataforma)
        if f["type"] == "select" and f.get("value") and normalize(f["value"]) not in ("", "select", "seleccione",
                                                                                         "select an option",
                                                                                         "selecciona una opcion"):
            continue
        v = user_answers.get(label_key(f["label"])) or saved.get(f["label"]) or rule_value(f, profile, files, job)
        if v in (None, "") and f["type"] not in ("file", "checkbox") \
                and not str(f.get("_answered_by", "")).startswith("pendiente"):
            v = bank.answer(f)
            if v not in (None, ""):
                f["_answered_by"] = "diccionario"
        if (v in (None, "") and f["type"] in ("text", "textarea")
                and not str(f.get("_answered_by", "")).startswith("pendiente")):
            v = know.answer(f)             # tus propias palabras (cvtitan entrevista)
            if v not in (None, ""):
                f["_answered_by"] = "conocimiento"
        if v not in (None, ""):
            values[f["id"]] = v
        else:
            pending.append(f)

    # La IA nunca ve: archivos, casillas (consentimientos/declaraciones) ni preguntas legales que el guardián
    # dejó pendientes a propósito. Esas solo se responden con candidate/declarations.yaml.
    unresolved = [f for f in pending if f["type"] not in ("file", "checkbox")
                  and not str(f.get("_answered_by", "")).startswith("pendiente")]
    if unresolved and llm.available():
        context = {
            "perfil": {k: profile.data.get(k) for k in ("personal", "summary", "experience", "projects",
                                                       "education", "certifications", "languages")},
            "skills_respaldadas": [profile.skill_map[k]["name"] for k in profile.backed_keys()],
            "preferencias": profile.data.get("preferences") or {},
            "anos_experiencia_total": profile.years_experience(),
            "conocimiento_del_candidato": know.context(),
            "vacante": {"cargo": job.get("title"), "empresa": job.get("company"), "pais": job.get("country"),
                        "requisitos": [s["name"] for s in job.get("skills", [])[:15]]},
        }
        compact = [{"field_id": f["id"], "pregunta": f["label"], "tipo": f["type"],
                    "opciones": f.get("options"), "obligatorio": f.get("required")} for f in unresolved]
        try:
            result = llm.answer_fields(compact, context)
            by_id = {a.field_id: a for a in result.answers}
        except llm.LLMUnavailable:
            by_id = {}
        still = []
        for f in pending:
            a = by_id.get(f["id"])
            if a and not a.needs_user and a.value not in (None, ""):
                if a.source == "redactado" and kb.SENSITIVE_Q.search(normalize(f["label"])):
                    f["proposal"] = a.value          # tema sensible redactado por la IA: lo revisa el candidato
                    f["question"] = "Review: the AI drafted this answer on a sensitive topic."
                    still.append(f)
                    continue
                values[f["id"]] = a.value
                f["_answered_by"] = a.source
            else:
                if a and a.question_for_user:
                    f["question"] = a.question_for_user
                still.append(f)
        pending = still

    questions = [f for f in pending if f.get("required")]
    if questions and getattr(profile, "path", None):
        try:                                # aprender de lo pendiente: `cvtitan entrevista --pendientes`
            kb.record_pending(candidate_dir.parent, questions, job)
        except OSError:
            pass
    return values, questions
