"""Borrado selectivo de los datos que TITAN genera en `data/` (derecho a borrar lo que guarda sobre ti).

Nunca toca `candidate/` (tu perfil, declaraciones y CV) ni `.env`: eso lo gestionas tú. Solo borra rutas
dentro de `<workspace>/data/`.
"""
from __future__ import annotations

import shutil
from pathlib import Path

# Grupo -> rutas relativas a `data/`.
GROUPS: dict[str, tuple[str, ...]] = {
    "outputs": ("outputs", "jobs"),
    "sessions": ("browser-profile", "linkedin_guard.json", "robot.lock"),
    "history": ("cvtitan.db", "cvtitan.db-wal", "cvtitan.db-shm", "cvtitan.db-journal", "auto", "llm_audit.jsonl",
                "pending_questions.jsonl"),
    "cache": ("cache", "market_cache.jsonl", "busqueda.csv", "geo"),
}


def plan(ws, groups: list[str]) -> list[Path]:
    """Rutas existentes que se borrarían para los `groups` indicados."""
    data = (ws.root / "data").resolve()
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise ValueError(f"Grupo desconocido: {', '.join(unknown)}. Válidos: {', '.join(GROUPS)}.")
    found: list[Path] = []
    for group in groups:
        for rel in GROUPS[group]:
            path = (data / rel).resolve()
            if data not in path.parents:          # defensa: nunca salir de data/
                continue
            if path.exists() and path not in found:
                found.append(path)
    return found


def execute(paths: list[Path]) -> tuple[int, list[Path]]:
    """Borra las rutas. Devuelve (borradas, fallidas)."""
    done, failed = 0, []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            done += 1
        except OSError:
            failed.append(path)
    return done, failed
