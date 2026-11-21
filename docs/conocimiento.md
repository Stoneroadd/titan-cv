# Tu base de conocimiento: que el agente responda con tus palabras

Los formularios de postulación hacen preguntas abiertas («¿cuál es tu mayor logro?», «¿por qué quieres cambiar de trabajo?»).
TITAN **no inventa** tus opiniones ni tus hechos. Lo que hace es dejar que **tú** los digas una vez y reutilizarlos siempre.

## Cómo se responde una pregunta (en este orden)

1. **Respuestas guardadas** (`candidate/answers.yaml`): lo que ya contestaste a esa misma pregunta.
2. **Reglas sobre tu perfil** (nombre, contacto, sueldo, años de experiencia...).
3. **Diccionario** (`candidate/question_bank.yaml`).
4. **Tu base de conocimiento** (`candidate/knowledge.yaml`): tus respuestas de la entrevista, con tus palabras. Sin IA.
5. **IA (opcional)**: redacta variantes a partir de tu perfil **y de tus respuestas**, nunca de la nada.
6. Si nada responde: queda **pendiente**, se registra y la próxima vez puedes contestarla.

Las respuestas legales y demográficas (permiso de trabajo, antecedentes, discapacidad, género...) salen **solo** de
`candidate/declarations.yaml`. La IA no las ve.

## 1. Entrevista guiada

```
cvtitan entrevista                    # recorre las ~40 preguntas que aún no respondiste
cvtitan entrevista --tema perfil      # solo un tema (ver --listar)
cvtitan entrevista --listar           # avance por tema
cvtitan entrevista --rehacer          # vuelve a preguntar las ya respondidas
cvtitan entrevista --borrar fortalezas
```

Responde con tus palabras (varias líneas; una línea vacía termina). Una línea vacía sola salta la pregunta; `q` guarda y sale.
Puedes dar una versión en inglés: si la pregunta del formulario está en inglés y no hay versión en inglés,
TITAN no usa la de español: lo decide la IA (si está activa) o queda pendiente.

El catálogo está en `config/interview_questions.yaml`; puedes copiarlo a tu carpeta `config/` y agregar tus propias preguntas.

## 2. Aprender de lo pendiente

Cada vez que una postulación se frena por una pregunta que no se pudo responder, TITAN la registra en
`data/pending_questions.jsonl` (sin duplicar; cuenta cuántas veces apareció).

```
cvtitan entrevista --pendientes
```

Recorre las pendientes de la más repetida a la menos. Para listas eliges un número; para texto escribes tu respuesta (o `p`
para aceptar la propuesta de la IA). Tu respuesta se guarda y esa pregunta no vuelve a frenar a TITAN.

## 3. Activar la IA para preguntas abiertas (opcional)

La IA está **apagada por defecto**. Para activarla (lo haces tú):

1. Pon tu `ANTHROPIC_API_KEY` en el archivo `.env` (TITAN nunca la pide ni la guarda por ti).
2. En `config/settings.yaml`: `llm.enabled: true`.

Qué se envía: hechos de tu perfil (experiencia, proyectos, estudios, skills respaldadas, preferencias), tus respuestas de
`knowledge.yaml` y el **texto** de las preguntas pendientes. **No se envía**: contraseñas, RUT, declaraciones legales,
casillas, archivos ni formularios completos; tu nombre, correo, teléfono y ubicación tampoco (`llm.send_contact: false`).
Cada petición queda en `data/llm_audit.jsonl` (solo metadatos) y la ves con `cvtitan llmlog`.

## 4. Lo sensible lo revisas tú

Aunque la IA esté activa, si redacta una respuesta sobre un tema sensible (sueldo, visa o permiso, mudanza o reubicación,
salud, antecedentes, referencias, disponibilidad o fecha de inicio), **no se escribe sola en el formulario**: la postulación
queda pendiente con la propuesta a la vista, y la revisas con `cvtitan entrevista --pendientes` (`p` acepta la propuesta).
Tus propias palabras guardadas en la entrevista sí se usan directamente: son tuyas.

## Privacidad

`candidate/knowledge.yaml` y `data/pending_questions.jsonl` son locales: están en `.gitignore`, no se publican, y
`cvtitan vault lock` cifra `candidate/*.yaml`. `cvtitan purge` borra los registros de `data/`.
