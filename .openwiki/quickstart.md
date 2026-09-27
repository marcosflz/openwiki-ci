---
type: guide
title: Quickstart y mapa de la wiki
description: "Punto de entrada del wiki de OpenWiki Service: qué hace el servicio (URL git pública o privada con PAT, o un zip/tar subido → wiki.zip del CLI OpenWiki, con push opcional), el arranque mínimo con docker compose y el desarrollo local con uv y pytest, y la tabla de enrutado tarea→página que reparte cualquier cambio entre architecture, concepts, workflows, integrations, operations y testing."
tags: [quickstart, overview, routing, entrypoint, deployment]
verified:
  - by: openwiki/0.6.0
    at: 2026-09-27T09:33:59.807Z
sources:
  - id: openwiki-source-5f5b95b3d6a215fa02ceb945
    resource: repo://.env.example
  - id: openwiki-source-6d4b4e707b8d60b6ccfa3425
    resource: repo://.github/workflows/openwiki-update.yml
  - id: openwiki-source-1040945f5d802682e6971c01
    resource: repo://app/core/config.py
  - id: openwiki-source-adaf794dc610fe5ba99131eb
    resource: repo://app/core/storage.py
  - id: openwiki-source-21c0a295e6c6f5529dc70d5f
    resource: repo://app/main.py
  - id: openwiki-source-747d0b3536ac169fca4d6f42
    resource: repo://app/routers/health.py
  - id: openwiki-source-8832fa4e30af9055e856d9c7
    resource: repo://app/routers/wikis.py
  - id: openwiki-source-095266a4eaf6119f4f5af1f6
    resource: repo://app/services/wiki_runner.py
  - id: openwiki-source-32d826d89052242a48e0af9e
    resource: repo://app/worker/__main__.py
  - id: openwiki-source-41ad7047de607612f2c7d0e8
    resource: repo://app/worker/loop.py
  - id: openwiki-source-b79fbbd921df689b4bbdc82f
    resource: repo://docker-compose.yml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
generated: { by: "openwiki/0.6.0", at: "2026-09-27T09:33:59.807Z" }
---

# Quickstart y mapa de la wiki

OpenWiki Service convierte un repositorio en documentación: le envías una URL git
pública, una privada con personal access token, o un `zip`/`tar` subido, y devuelve lo
que generó el CLI OpenWiki como un `wiki.zip` descargable (Markdown más
`openwiki/.claims/`). Con `push` también puede commitear esa wiki de vuelta al
repositorio de origen.

Dos preocupaciones están separadas a propósito:

- **API** (`uvicorn app.main:create_app --factory`, Python 3.12 + FastAPI): crea wikis
  (`queued`), sirve estado/logs/zip y reencola claims caducados. Nunca genera.
- **Workers** (`python -m app.worker`): la misma imagen con otro comando. Cada uno
  reclama la siguiente wiki encolada, ejecuta el pipeline (clone → OpenWiki → zip →
  push opcional) y reporta progreso por heartbeat.

Se comunican solo a través del volumen compartido: `openwiki.db` (SQLite, WAL) para la
coordinación y `jobs/<wiki_id>/` para el workspace, los logs y los artefactos. Todo el
contacto con el CLI OpenWiki está confinado en `app/services/wiki_runner.py`, así que
actualizar OpenWiki es reconstruir la imagen, no cambiar el código.

Superficie HTTP: `POST /wikis`, `POST /wikis/upload`, `GET /wikis`, `GET /wikis/{id}`,
`GET /wikis/{id}/logs`, `GET /wikis/{id}/download`, `POST /wikis/{id}/cancel`,
`POST /wikis/{id}/retry`, `DELETE /wikis/{id}`, `GET /health` y `GET /health/model`.

## Arranque mínimo

```bash
cp .env.example .env
# edita .env: elige tu proveedor de modelos y su clave (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...)

docker compose up --build -d --scale worker=2
curl http://localhost:8000/health
```

Documentación interactiva de la API: http://localhost:8000/docs

- `--scale worker=N` es la forma de obtener concurrencia: cada contenedor worker ejecuta
  `MAX_CONCURRENT_JOBS` generaciones (por defecto 1).
- Modo de un solo contenedor / dev: define `RUN_LOCAL_WORKER=true` en `.env` y ejecuta
  `docker compose up -d` sin réplicas de worker; la API arranca un bucle de worker
  dentro de su propio proceso.
- `docker compose ps` más el bloque `pool` de `GET /health` muestran los workers vistos
  recientemente. El servicio `worker` desactiva su healthcheck; solo la API expone uno.

Primera wiki en dos llamadas (los flujos completos están en
[/openwiki/workflows/generation-from-git.md](/openwiki/workflows/generation-from-git.md)):

```bash
curl -X POST http://localhost:8000/wikis \
  -H "Content-Type: application/json" \
  -d '{"source": {"url": "https://github.com/org/repo.git"}, "language": "es"}'

curl http://localhost:8000/wikis/<wiki_id>          # estado, progreso, paginas, errores
curl -OJ http://localhost:8000/wikis/<wiki_id>/download
```

Desarrollo local sin Docker (ambos procesos reclaman de la misma cola SQLite):

```bash
uv venv --python 3.12
uv pip install -e ".[test]"

# API + un worker in-process
RUN_LOCAL_WORKER=true uv run uvicorn app.main:create_app --factory --reload

# o dos procesos, como en Docker
uv run uvicorn app.main:create_app --factory
uv run python -m app.worker
```

Los tests ejecutan el pipeline completo (clone → CLI simulado → zip → descarga) sin
proveedor de modelos ni red:

```bash
uv run pytest
```

## Qué leer según la tarea

| Si trabajas en… | Lee |
| --- | --- |
| Mapa de subsistemas, fronteras contenedor/volumen, las dos reglas de desacople | [/openwiki/architecture/overview.md](/openwiki/architecture/overview.md) |
| Factory de FastAPI, routers, modelos de petición/respuesta, mapeo de errores HTTP | [/openwiki/architecture/api-service.md](/openwiki/architecture/api-service.md) |
| Esquema de `openwiki.db`, claims atómicos, heartbeats, `recover_stale`, retry/cancel | [/openwiki/architecture/job-store.md](/openwiki/architecture/job-store.md) |
| Las etapas de una generación reclamada (`run_pipeline`), progreso, cancelación | [/openwiki/architecture/pipeline.md](/openwiki/architecture/pipeline.md) |
| Bucle de claim del worker, concurrencia por contenedor, cierre como done/failed/cancelled | [/openwiki/architecture/worker-pool.md](/openwiki/architecture/worker-pool.md) |
| Estados de la wiki (`queued → fetching → generating → finalizing → done/failed/cancelled`), claims, leases, reanudación | [/openwiki/concepts/wiki-lifecycle.md](/openwiki/concepts/wiki-lifecycle.md) |
| El reparto `openwiki/` (CLI) vs `.openwiki` (`WIKI_ARTIFACT_DIR`), contenido del zip y del commit, adopción | [/openwiki/concepts/wiki-artifact-and-paths.md](/openwiki/concepts/wiki-artifact-and-paths.md) |
| Transporte y almacenamiento del token, usuarios git por forja, redacción, `ALLOW_LOCAL_GIT`, API sin autenticación | [/openwiki/concepts/credentials-and-redaction.md](/openwiki/concepts/credentials-and-redaction.md) |
| Fin a fin: `POST /wikis` con una URL git → `wiki.zip` descargable | [/openwiki/workflows/generation-from-git.md](/openwiki/workflows/generation-from-git.md) |
| Fin a fin: subida multipart, extracción endurecida, fallback `git init` | [/openwiki/workflows/upload-and-extraction.md](/openwiki/workflows/upload-and-extraction.md) |
| Commit y push de la wiki generada al repositorio, estados de `push_result` | [/openwiki/workflows/push-back.md](/openwiki/workflows/push-back.md) |
| Cancelar (inmediato vs cooperativo), reintentar, reaper/expiración de lease, reanudar desde `.run.json` | [/openwiki/workflows/cancel-retry-and-resume.md](/openwiki/workflows/cancel-retry-and-resume.md) |
| `/health` vs `/health/model`: configuración pasiva, sonda activa, caché TTL, estados | [/openwiki/workflows/model-health-check.md](/openwiki/workflows/model-health-check.md) |
| Contrato de `wiki_runner.py`: comando efectivo, env forzado, `INSTRUCTIONS.md`, timeout | [/openwiki/integrations/openwiki-cli.md](/openwiki/integrations/openwiki-cli.md) |
| git como dependencia externa: validación de URLs, clonado superficial, unshallow, push | [/openwiki/integrations/git-hosting.md](/openwiki/integrations/git-hosting.md) |
| Credenciales del proveedor hasta el CLI, proveedores soportados, proxy loopback de cabeceras | [/openwiki/integrations/model-providers.md](/openwiki/integrations/model-providers.md) |
| Referencia de `Settings`: defaults, validadores, rutas derivadas, variables reenviadas | [/openwiki/operations/configuration.md](/openwiki/operations/configuration.md) |
| Imagen Docker, build arg `OPENWIKI_VERSION`, servicios de compose, escalado, healthcheck | [/openwiki/operations/deployment.md](/openwiki/operations/deployment.md) |
| Campos de pool de `/health`, logs por wiki, retención, pruning de workers, límites MVP | [/openwiki/operations/observability-and-recovery.md](/openwiki/operations/observability-and-recovery.md) |
| El workflow programado de GitHub Actions que refresca esta wiki | [/openwiki/operations/wiki-refresh-workflow.md](/openwiki/operations/wiki-refresh-workflow.md) |
| Arnés de tests: CLI simulado, worker in-process, fixtures git, servidor OpenAI simulado | [/openwiki/testing/strategy.md](/openwiki/testing/strategy.md) |

## Reglas generales al cambiar este servicio

- La lógica de negocio vive en `app/services/`; los routers no hablan con git ni con el
  CLI directamente. Una superficie HTTP nueva implica un módulo nuevo en
  `app/routers/` incluido en `create_app()`.
- La coordinación API↔workers vive en `app/core/storage.py`. Sus métodos
  (`claim_next`, `heartbeat`, `complete`, `recover_stale`, …) son la costura a
  sustituir por un backend Postgres si algún día abandonas el diseño de un solo host.
- La configuración se lee una sola vez a través de `Settings`; las credenciales del
  proveedor de modelos deliberadamente *no* se declaran ahí y se reenvían al subproceso
  sin tocar.
- Esta wiki se escribe en `es` según `openwiki/INSTRUCTIONS.md` (identificadores, rutas
  y comandos intactos), y el workflow programado de refresco es su dueño: prefiere
  editar el código fuente y dejar que OpenWiki regenere las páginas en vez de editar a
  mano las generadas.
