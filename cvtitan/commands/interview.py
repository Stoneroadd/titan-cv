"""`cvtitan entrevista`: tus respuestas, con tus palabras, para que el agente no se frene en los formularios.

  cvtitan entrevista               recorre las preguntas del catálogo que aún no respondiste
  cvtitan entrevista --pendientes  responde las preguntas que el agente no pudo contestar en postulaciones reales
  cvtitan entrevista --listar      avance por tema
Todo se guarda en candidate/knowledge.yaml y candidate/answers.yaml (locales, ignorados por git).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..knowledge import Knowledge, drop_pending, learned_id, pending_key, read_pending
from ..utils import Workspace

QUIT = {"q", "salir", "quit", "exit"}
SKIP = {"s", "saltar", "skip"}


def register(sub) -> None:
    s = sub.add_parser("entrevista",
                       help="Answer common application questions once, in your own words (stored locally).")
    s.add_argument("--pendientes", action="store_true",
                   help="Answer the questions the agent could not answer in real applications.")
    s.add_argument("--listar", action="store_true", help="Show progress by topic and exit.")
    s.add_argument("--tema", help="Only this topic (see --listar).")
    s.add_argument("--rehacer", action="store_true", help="Also ask the questions you already answered.")
    s.add_argument("--borrar", metavar="ID", help="Delete one saved answer by its id.")
    s.set_defaults(func=run)


def _read_multiline(prompt: str) -> str | None:
    """Varias líneas; una línea vacía termina. Devuelve None si el candidato quiere salir."""
    lines: list[str] = []
    print(prompt)
    while True:
        try:
            line = input("> " if not lines else "  ")
        except (EOFError, KeyboardInterrupt):
            return None
        if not lines and line.strip().lower() in QUIT:
            return None
        if not line.strip():
            break
        lines.append(line.rstrip())
    return " ".join(x.strip() for x in lines).strip()


def _list(know: Knowledge) -> int:
    done = total = 0
    for topic, (a, b) in sorted(know.progress().items()):
        done, total = done + a, total + b
        print(f"  {topic:<14} {a}/{b}")
    extra = [k for k in know.entries if k not in know.catalog]
    print(f"\nRespondidas: {done}/{total} del catálogo" + (f" + {len(extra)} aprendidas de postulaciones" if extra else ""))
    pend = read_pending(know.root)
    if pend:
        print(f"Preguntas pendientes de postulaciones: {len(pend)}  ->  cvtitan entrevista --pendientes")
    return 0


def _interview(know: Knowledge, topic: str | None, redo: bool) -> int:
    todo = [q for q in know.catalog.values()
            if (not topic or q.get("topic") == topic) and (redo or not know.answered(q["id"]))]
    if not todo:
        print("No hay preguntas por responder" + (f" en el tema «{topic}»." if topic else ". ¡Todo respondido!"))
        return 0
    print(f"{len(todo)} pregunta(s). Responde con tus palabras (varias líneas; una línea vacía termina).\n"
          "Línea vacía sola = saltar · q = salir y guardar lo hecho. Nada se inventa ni se envía: queda en tu equipo.\n")
    saved = 0
    for n, q in enumerate(todo, 1):
        es = _read_multiline(f"[{n}/{len(todo)}] ({q.get('topic')}) {q['ask']}")
        if es is None:
            break
        if not es or es.lower() in SKIP:
            continue
        en = _read_multiline("  Versión en inglés (opcional; línea vacía = omitir):") or ""
        know.put(q["id"], es, en if en.lower() not in QUIT else "", source="entrevista", question=q["ask"])
        saved += 1
    print(f"\nGuardadas {saved} respuesta(s) en {know.path}")
    return 0


def _pending(know: Knowledge) -> int:
    from ..apply.answers import SavedAnswers
    rows = read_pending(know.root)
    if not rows:
        print("No hay preguntas pendientes registradas. (Se registran solas cuando una postulación se frena.)")
        return 0
    saved = SavedAnswers(know.root / "candidate" / "answers.yaml")
    resolved: set[str] = set()
    print(f"{len(rows)} pregunta(s) pendiente(s), de la más repetida a la menos. Tu respuesta se reutiliza siempre.\n")
    for n, r in enumerate(rows, 1):
        label, options = r["label"], r.get("options") or []
        print(f"[{n}/{len(rows)}] ({r.get('count', 1)}x, {r.get('first_company') or '?'}) {label}")
        if options:
            for i, o in enumerate(options, 1):
                print(f"   {i}. {o}")
            raw = input("Número de la opción (Enter = saltar, q = salir): ").strip().lower()
            if raw in QUIT:
                break
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                saved.put(label, options[int(raw) - 1])
                resolved.add(r["key"])
            continue
        if r.get("proposal"):
            print(f"   Propuesta de la IA (revísala): {r['proposal']}")
        hint = " (p = aceptar la propuesta)" if r.get("proposal") else ""
        es = _read_multiline(f"Tu respuesta{hint}:")
        if es is None:
            break
        if es.lower() == "p" and r.get("proposal"):
            es = r["proposal"]
        if not es:
            continue
        know.put(learned_id(label), es, source="pendiente", question=label[:200],
                 patterns=[re.escape(pending_key(label))])
        saved.put(label, es)
        resolved.add(r["key"])
    drop_pending(know.root, resolved)
    print(f"\n{len(resolved)} respondida(s). Las próximas postulaciones con esa pregunta ya no se frenan.")
    return 0


def run(args) -> int:
    ws = Workspace.resolve(args.root)
    know = Knowledge(ws.root, Path(ws.profile_path).parent)
    if args.borrar:
        print("Borrada." if know.remove(args.borrar) else f"No existe «{args.borrar}» (ver {know.path.name}).")
        return 0
    if args.listar:
        return _list(know)
    if args.pendientes:
        return _pending(know)
    if args.tema and args.tema not in {q.get("topic") for q in know.catalog.values()}:
        print("Tema desconocido. Temas: " + ", ".join(sorted({str(q.get('topic')) for q in know.catalog.values()})))
        return 2
    return _interview(know, args.tema, args.rehacer)
