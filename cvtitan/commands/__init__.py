"""Comandos de la CLI, uno por módulo.

Contrato de un módulo de comando:
  register(sub)  -> agrega su subparser a `sub` y fija `set_defaults(func=run)`
  run(args)      -> ejecuta el comando; devuelve un código de salida (o None = 0)

`cli.build_parser()` llama a `register_all(sub)`. Los comandos que aún viven en `cli.py` se irán moviendo aquí
de a uno (ver el plan de refactor); `main` y `purge_diagnostics` siguen exportados desde `cvtitan.cli`.
"""
from __future__ import annotations

from . import auto, init, interview, llmlog, pipeline, preview, purge, reports, robot, schema, vault

MODULES = (auto, init, interview, llmlog, pipeline, preview, purge, reports, robot, schema, vault)


def register_all(sub) -> None:
    for module in MODULES:
        module.register(sub)
