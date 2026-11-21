"""Base de conocimiento, entrevista guiada, preguntas pendientes y revisión de lo sensible (datos ficticios)."""
import re
import shutil

import pytest

from cvtitan import knowledge as kb
from cvtitan import llm
from cvtitan.apply.answers import SavedAnswers, resolve
from cvtitan.cli import main
from cvtitan.profile import Profile
from cvtitan.taxonomy import Taxonomy
from cvtitan.utils import PACKAGE_ROOT


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "candidate").mkdir()
    shutil.copy(PACKAGE_ROOT / "candidate" / "profile.example.yaml", tmp_path / "candidate" / "profile.yaml")
    return tmp_path


@pytest.fixture
def profile(ws):
    tax = Taxonomy.load(PACKAGE_ROOT / "config" / "skills_taxonomy.yaml")
    return Profile.load(ws / "candidate" / "profile.yaml", tax)


def field(label, ftype="textarea", **kw):
    return {"id": "f1", "type": ftype, "label": label, "required": True, "value": "", **kw}


def test_catalog_is_valid():
    catalog = kb.load_catalog(PACKAGE_ROOT)
    ids = [q["id"] for q in catalog]
    assert len(catalog) >= 35 and len(ids) == len(set(ids))
    for q in catalog:
        assert q["ask"] and q["topic"] and q["patterns"]
        for p in q["patterns"]:
            assert "\x08" not in p                   # «\b» dentro de comillas YAML se vuelve retroceso
            re.compile(p)


def test_answer_matches_topic_in_the_right_language(ws):
    k = kb.Knowledge(ws)
    k.put("fortalezas", "Soy metódica y cierro lo que empiezo.", "I am methodical and I finish what I start.")
    es = k.answer(field("¿Cuáles son tus fortalezas?"))
    en = k.answer(field("What are your strengths and how do you use them?"))
    assert es.startswith("Soy metódica") and en.startswith("I am methodical")
    assert k.answer(field("¿Cuáles son tus fortalezas?", "select")) is None      # solo texto libre
    assert k.answer(field("Ciudad")) is None                                      # campo corto, no pregunta
    assert k.answer(field("¿Algo totalmente distinto sobre la vida?")) is None


def test_no_answer_when_the_language_version_is_missing(ws):
    k = kb.Knowledge(ws)
    k.put("fortalezas", "Solo en español.")
    assert k.answer(field("What are your strengths and how do you use them?")) is None   # lo decide la IA o tú


def test_progress_context_and_remove(ws):
    k = kb.Knowledge(ws)
    assert not k.answered("fortalezas")
    k.put("fortalezas", "Hecho real.", question="¿Fortalezas?")
    k2 = kb.Knowledge(ws)                                     # persiste en candidate/knowledge.yaml
    assert k2.answered("fortalezas") and k2.context()[0]["respuesta_del_candidato"] == "Hecho real."
    done, total = k2.progress()["perfil"]
    assert done == 1 and total > 1
    assert k2.remove("fortalezas") and not kb.Knowledge(ws).answered("fortalezas")


def test_pending_questions_are_recorded_once_and_counted(ws):
    q = [{"label": "¿Tienes licencia clase B?", "type": "text", "options": None}]
    assert kb.record_pending(ws, q, {"company": "Acme"}) == 1
    assert kb.record_pending(ws, q, {"company": "Otra"}) == 0
    rows = kb.read_pending(ws)
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["first_company"] == "Acme"
    kb.drop_pending(ws, {rows[0]["key"]})
    assert kb.read_pending(ws) == [] and not (ws / kb.PENDING_FILE).exists()


def test_resolve_uses_the_knowledge_before_the_ai_and_records_the_rest(ws, profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    kb.Knowledge(ws).put("fortalezas", "Soy metódica.")
    fields = [field("¿Cuáles son tus fortalezas principales?"),
              {**field("Cuéntanos una historia sobre tu abuela en el campo"), "id": "f2"}]
    values, questions = resolve(fields, profile, SavedAnswers(ws / "candidate" / "answers.yaml"), {}, {"company": "Acme"})
    assert values["f1"] == "Soy metódica." and fields[0]["_answered_by"] == "conocimiento"
    assert [q["id"] for q in questions] == ["f2"]
    assert kb.read_pending(ws)[0]["label"].startswith("Cuéntanos una historia")      # aprende de lo pendiente


def test_ai_drafts_on_sensitive_topics_go_to_review_not_to_the_form(ws, profile, monkeypatch):
    answers = [llm.FieldAnswer(field_id="f1", value="Sin conflictos con competidores", source="redactado", needs_user=False),
               llm.FieldAnswer(field_id="f2", value="Me motiva el impacto.", source="redactado", needs_user=False)]
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "answer_fields", lambda fields, context: llm.FormAnswers(answers=answers))
    fields = [field("Cuéntanos sobre tus antecedentes laborales con empresas competidoras de esta compañía"),
              {**field("Cuéntanos qué te inspira de forma muy personal"), "id": "f2"}]
    values, questions = resolve(fields, profile, SavedAnswers(ws / "candidate" / "answers.yaml"), {}, {})
    assert "f1" not in values and values["f2"] == "Me motiva el impacto."          # lo sensible lo revisas tú
    assert questions[0]["proposal"] == "Sin conflictos con competidores"


def test_ai_receives_the_candidates_own_words(ws, profile, monkeypatch):
    kb.Knowledge(ws).put("logro_principal", "Reduje el tiempo de cierre de 5 a 2 días.", question="Tu mayor logro")
    seen = {}

    def fake(fields, context):
        seen.update(context)
        return llm.FormAnswers(answers=[])

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "answer_fields", fake)
    resolve([field("Describe una situación inesperada en la que tuviste que improvisar")], profile,
            SavedAnswers(ws / "candidate" / "answers.yaml"), {}, {})
    assert "5 a 2 días" in str(seen["conocimiento_del_candidato"])


def test_interview_command_saves_and_resumes(ws, monkeypatch, capsys):
    typed = iter(["Soy analista con foco en datos.", "", "I am a data analyst.", "", "q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(typed))
    assert main(["--root", str(ws), "entrevista", "--tema", "presentacion"]) == 0
    k = kb.Knowledge(ws)
    assert k.entries["sobre_ti"]["es"].startswith("Soy analista") and k.entries["sobre_ti"]["en"].startswith("I am")
    assert main(["--root", str(ws), "entrevista", "--listar"]) == 0
    assert "presentacion" in capsys.readouterr().out
    assert main(["--root", str(ws), "entrevista", "--borrar", "sobre_ti"]) == 0
    assert not kb.Knowledge(ws).answered("sobre_ti")


def test_pending_command_learns_text_and_options(ws, monkeypatch):
    kb.record_pending(ws, [{"label": "¿Cuál es tu color favorito para trabajar?", "type": "text"},
                           {"label": "Tipo de jornada", "type": "select", "options": ["Completa", "Parcial"]}])
    typed = iter(["Azul, me ayuda a concentrarme.", "", "2"])
    monkeypatch.setattr("builtins.input", lambda *_: next(typed))
    assert main(["--root", str(ws), "entrevista", "--pendientes"]) == 0
    assert kb.read_pending(ws) == []
    saved = SavedAnswers(ws / "candidate" / "answers.yaml")
    assert saved.get("Tipo de jornada") == "Parcial"
    assert kb.Knowledge(ws).answer(field("¿Cuál es tu color favorito para trabajar?")).startswith("Azul")


def test_knowledge_file_is_never_published():
    ignore = (PACKAGE_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "candidate/knowledge.yaml" in ignore
