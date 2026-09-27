---
type: architecture
title: System overview
description: Mapa de subsistemas de OpenWiki Service - la API FastAPI que solo encola y sirve estado, los contenedores worker que ejecutan el pipeline de generación, el volumen compartido /data con SQLite y jobs/, el módulo único que concentra todo contacto con el CLI OpenWiki, y quién posee la configuración de proveedores de modelos.
tags: [architecture, overview, decoupling, api, worker, sqlite, volume, openwiki-cli]
verified:
  - by: openwiki/0.6.0
    at: 2026-09-27T09:33:59.807Z
sources:
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
  - id: openwiki-source-33302cb3cd1502cb37f30370
    resource: repo://app/schemas.py
  - id: openwiki-source-499c7d24cf6d363b118ec0e1
    resource: repo://app/services/ingestion.py
  - id: openwiki-source-dd9a681b27818e987233735f
    resource: repo://app/services/model_status.py
  - id: openwiki-source-88002490f9fe36143a262df6
    resource: repo://app/services/pipeline.py
  - id: openwiki-source-24d7d9ea96ef4d58f473119f
    resource: repo://app/services/provider_proxy.py
  - id: openwiki-source-095266a4eaf6119f4f5af1f6
    resource: repo://app/services/wiki_runner.py
  - id: openwiki-source-32d826d89052242a48e0af9e
    resource: repo://app/worker/__main__.py
  - id: openwiki-source-41ad7047de607612f2c7d0e8
    resource: repo://app/worker/loop.py
  - id: openwiki-source-b79fbbd921df689b4bbdc82f
    resource: repo://docker-compose.yml
  - id: openwiki-source-bb1ebe868e35e9e500714501
    resource: repo://Dockerfile
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-6f28b34f5f5d9c611f895743
    resource: repo://tests/test_api_wikis.py
  - id: openwiki-source-3ae9d3f679017b2e612e3ea4
    resource: repo://tests/test_worker_queue.py
generated: { by: "openwiki/0.6.0", at: "2026-09-27T09:33:59.807Z" }
---

# Vista general del sistema

OpenWiki Service convierte un repositorio en documentación: acepta una URL git
pública, una privada con personal access token o un `zip`/`tar` subido, y devuelve
lo que generó el CLI OpenWiki como un `wiki.zip` (Markdown más
`openwiki/.claims/`). Con `push` también puede commitear esa wiki de vuelta al
repositorio de origen.

Todo el diseño es una única regla de desacople aplicada dos veces:

1. **El trabajo HTTP y el trabajo de generación nunca comparten proceso.** La API
   encola y sirve estado; los workers generan. Se encuentran en un volumen.
2. **Todo lo específico de OpenWiki vive en un solo módulo.** El servicio conoce el
   CLI únicamente a través de `app/services/wiki_runner.py`, así que actualizar el
   CLI es reconstruir la imagen.

| Pieza | Página |
| --- | --- |
| Endpoints HTTP, validación, estados | [/openwiki/architecture/api-service.md](/openwiki/architecture/api-service.md) |
| Esquema de `openwiki.db`, claims, leases | [/openwiki/architecture/job-store.md](/openwiki/architecture/job-store.md) |
| Etapas de `run_pipeline` | [/openwiki/architecture/pipeline.md](/openwiki/architecture/pipeline.md) |
| Bucle del worker, reintentos, reanudación | [/openwiki/architecture/worker-pool.md](/openwiki/architecture/worker-pool.md) |
| Arranque y escalado del stack | [/openwiki/operations/deployment.md](/openwiki/operations/deployment.md) |

## Contenedores y el volumen compartido

```mermaid
flowchart TB
    client["HTTP client (curl, CI, UI)"]

    subgraph host["One host - docker compose"]
        subgraph apic["api container: uvicorn app.main:create_app"]
            routers["routers for wikis and health"]
            reaper["stale-claim reaper"]
            localm["in-process WorkerLoop, only when RUN_LOCAL_WORKER=true"]
        end
        subgraph workc["worker containers: python -m app.worker"]
            loop["WorkerLoop: claim, heartbeat, complete"]
            pipe["pipeline: fetch, CLI, zip, optional push"]
        end
        vol["volume /data: openwiki.db + jobs/wiki_id/ + .openwiki-config"]
        cli["openwiki CLI on Node 22: openwiki --init -p"]
    end

    client -->|"POST /wikis reads status and download"| routers
    routers -->|"INSERT queued, reads state, logs and wiki.zip"| vol
    reaper -->|"recover_stale and worker prune"| vol
    loop -->|"claim_next, heartbeat, complete"| vol
    pipe -->|"writes repo, logs.txt and wiki.zip"| vol
    pipe -->|"subprocess with cwd set to the workspace"| cli
    localm -.->|"same WorkerLoop and same pipeline"| vol
```

*Vista de contenedores: la API y los contenedores worker nunca se llaman entre sí —
ambos abren la misma base SQLite y el mismo árbol de artefactos en el volumen
`/data`, y solo los workers lanzan el CLI OpenWiki.*

`docker-compose.yml` construye una sola imagen (`openwiki-ci:latest`) y la ejecuta
dos veces:

| Servicio | Comando | Rol |
| --- | --- | --- |
| `api` | `uvicorn app.main:create_app --factory` (`CMD` de la imagen) | Crea wikis (`queued`), sirve estado/logs/zip, recupera claims caducados y opcionalmente aloja un worker |
| `worker` | `python -m app.worker` | Reclama la wiki encolada más antigua y ejecuta el pipeline: clone → OpenWiki → zip → push opcional |
| volume | `openwiki-ci-data:/data` en ambos | `openwiki.db`, `jobs/<wiki_id>/` (workspace `repo/`, `logs.txt`, `wiki.zip` y, en subidas, el archivo cargado), `.openwiki-config/` |

`docker compose up -d --scale worker=N` añade workers que reclaman de la misma cola;
cada worker ejecuta `MAX_CONCURRENT_JOBS` generaciones (por defecto `1`), así que la
concurrencia viene de las réplicas y no de hilos dentro de un contenedor.

## Dirección de las dependencias

Las dependencias apuntan **hacia el store y hacia el CLI**, nunca de vuelta a la API:

- `api` (routers + reaper) → escrituras/lecturas de filas en `JobStore`, escritura
  del archivo subido en `jobs/<wiki_id>/upload<ext>` y lecturas del sistema de
  archivos (`jobs/<wiki_id>/logs.txt`, `jobs/<wiki_id>/wiki.zip`); además borra el
  directorio de la wiki al eliminarla o al caducar por `JOB_RETENTION_HOURS`.
- `worker` → `JobStore` (`claim_next`, `heartbeat`, `complete`) y el workspace del
  trabajo bajo `jobs/<wiki_id>/repo`.
- `worker` → subproceso `openwiki`, lanzado por generación con `cwd` puesto en el
  workspace del repositorio.
- Nada dentro del sistema llama a la superficie HTTP de la API; la API es una puerta
  de entrada para clientes, no un coordinador.

La única ruta de recuperación entre actores es el store: una wiki en ejecución cuyo
worker deja de mandar heartbeat durante más de `WORKER_LEASE_SECONDS` es reencolada
por la API y la recoge otro worker, que reanuda en el mismo workspace (el pipeline
reutiliza un `repo/.git` existente y OpenWiki continúa desde `openwiki/.run.json`).

`RUN_LOCAL_WORKER=true` rompe a propósito la separación "dos procesos", no la
arquitectura: arranca un `WorkerLoop` dentro del proceso de la API para desarrollo
en un solo contenedor y para la suite de tests, usando el mismo bucle y el mismo
pipeline que los contenedores worker.

## Qué no sabe cada componente

Esta es la parte que mantiene cambiable el sistema; cada hueco lo cubre otro módulo.

| Componente | **No** sabe | Ese conocimiento vive en |
| --- | --- | --- |
| Proceso y routers de la API (`app/main.py`, `app/routers/`) | Cómo se genera una wiki: nada de clone, ni CLI, ni empaquetado, ni push. Tampoco invoca el binario `git` ni el CLI OpenWiki: los routers validan payloads, transmiten subidas y hablan con el store. | `app/services/ingestion.py`, `app/worker/loop.py` |
| Manejadores de petición de la API | Las etapas del pipeline y sus estados intermedios; exponen `status`, `progress`, `pages` y logs exactamente como los escribieron los workers. | `app/services/pipeline.py`, `app/core/storage.py` |
| Contenedores worker | Preocupaciones HTTP: sin rutas, sin modelos de respuesta, sin renderizado. Solo leen la fila reclamada y escriben resultados. | `app/routers/` |
| Capa de cola (`app/core/storage.py`) | Semántica de git, de OpenWiki y de empaquetado; almacena JSON opaco de `source`/`push` y los campos de ciclo de vida. | `app/services/*` |
| Pipeline (`app/services/pipeline.py`) | Flags del CLI, el nombre del directorio `openwiki/`, el formato de `INSTRUCTIONS.md` y el parseo de `.run.json`: solo secuencia servicios y reporta progreso. | `app/services/wiki_runner.py` |
| Configuración de modelos | Nada: `Settings` no declara credenciales de proveedor a propósito, las deja en el entorno del proceso y las reenvía al subproceso OpenWiki sin tocarlas; el servicio solo fuerza `OPENWIKI_CONFIG_DIR` y `OPENWIKI_TELEMETRY_DISABLED`. | `app/services/model_status.py` (conocimiento del proveedor y sonda activa), `app/services/provider_proxy.py` (proxy loopback que inyecta cabeceras de gateway) |
| **Todo lo de OpenWiki** | — | `app/services/wiki_runner.py` es el único punto de contacto: construcción del comando (`openwiki --init\|-p`), el directorio de salida `openwiki/` fijo, adopción de artefactos ocultos (`.openwiki/` → `openwiki/`), la decisión `auto`/`init`/`update`, `openwiki/INSTRUCTIONS.md`, el parseo de progreso de `.run.json`, el entorno de telemetría/configuración y los timeouts. |

Dos consecuencias que conviene recordar:

- Actualizar OpenWiki es `docker compose build --build-arg OPENWIKI_VERSION=x.y.z`
  más `docker compose up -d`; no hay cambios de código del servicio mientras el CLI
  mantenga `openwiki --init -p`. Si cambia la interfaz del CLI, el único archivo a
  tocar es `app/services/wiki_runner.py`.
- Añadir un endpoint significa un router nuevo en `app/routers/` incluido desde
  `create_app()`, con la lógica de negocio en `app/services/`. Los routers no deben
  crecer con llamadas a git o al CLI.

## Ciclo de vida de una petición

```mermaid
sequenceDiagram
    participant C as HTTP client
    participant A as FastAPI api
    participant Q as SQLite openwiki.db
    participant W as Worker container
    participant O as openwiki CLI

    C->>A: POST /wikis
    A->>Q: INSERT queued row
    A-->>C: 202 with wiki_id and links
    W->>Q: claim_next with BEGIN IMMEDIATE
    Q-->>W: row claimed, moved to fetching
    W->>O: openwiki --init -p inside the workspace
    O-->>W: pages written under openwiki/
    W->>Q: heartbeat with progress, then complete with done
    C->>A: GET /wikis/id/download
    A->>Q: reads the row
    A-->>C: serves jobs/wiki_id/wiki.zip
```

*Flujo desacoplado: la petición HTTP que crea una wiki y el proceso que la genera
solo están conectados por filas y archivos en el volumen.*

Los estados son `queued → fetching → generating → finalizing → done`, o `failed` /
`cancelled`; `POST /wikis/{id}/cancel` cancela de inmediato mientras está encolada y
de forma cooperativa mientras corre (el worker nota la cancelación en su siguiente
heartbeat), y `POST /wikis/{id}/retry` reencola una wiki fallida o cancelada para que
OpenWiki reanude desde su propia cola de páginas. Detalles en
[/openwiki/architecture/job-store.md](/openwiki/architecture/job-store.md) y
[/openwiki/architecture/worker-pool.md](/openwiki/architecture/worker-pool.md).

Las sondas operativas siguen la misma separación: `GET /health` es una llamada
rápida que nunca toca al proveedor y reporta version, `data_dir`, estadísticas del
pool desde el store y la configuración pasiva del modelo, mientras que
`GET /health/model` hace la sonda activa contra el proveedor (cacheada durante
`MODEL_CHECK_TTL_SECONDS`) y devuelve `503` cuando el modelo no está configurado o no
responde.

## Límites conocidos y fronteras de diseño

- **Un solo host con un volumen local.** La cola es SQLite (`/data/openwiki.db`, WAL)
  y los artefactos son un árbol de directorios local (`jobs/`), así que sirve para un
  host con varios contenedores. Para escalar a varios hosts, sustituye
  `app/core/storage.py` por una implementación sobre Postgres que exponga los mismos
  métodos (`claim_next`, `heartbeat`, `complete`, `recover_stale`, ...) y ofrece un
  volumen de artefactos compartido.
- **Sin autenticación en la API.** Está pensada para una red interna o detrás de un
  reverse proxy con auth; un bearer token es el siguiente paso natural. Cualquiera que
  alcance la API puede enviar wikis y leer sus logs.
- **Secretos en el volumen.** El token de un repositorio privado se persiste en la
  base del volumen (fuera de logs y respuestas) para que un reintento pueda
  re-clonar sin reenviarlo; si se pierde el workspace, la wiki falla y hay que
  enviarla de nuevo.
- **`push` escribe en tu repositorio** con ese PAT y exige una URL `http(s)` (las
  claves SSH no están disponibles en el contenedor). Un push fallido deja la wiki en
  `failed` con `push_result.detail`, mientras el `wiki.zip` sigue siendo descargable.
- **`ALLOW_LOCAL_GIT=true`** permite clonar rutas locales y URLs `file://`; mantenlo
  desactivado en producción.
- El servicio depende del contrato fijo `openwiki --init -p`; un cambio incompatible
  del CLI se manifiesta como generaciones `failed` y se arregla en
  `app/services/wiki_runner.py`.

## Cómo se prueba el cableado

`tests/conftest.py` configura `Settings` con `openwiki_bin` apuntando a
`tests/fixtures/fake_openwiki.py` y `run_local_worker=True`, de modo que los tests de
API (`tests/test_api_wikis.py`) recorren el pipeline real —validación, cola, worker,
empaquetado, descarga— a través del worker in-process, sin proveedor de modelos y sin
red. `tests/test_worker_queue.py` cubre el contrato de la cola en sí: claims
exclusivos atómicos, heartbeat/cancel/complete con claim ids caducados, reencolado de
claims muertos, estadísticas y migración de registros legacy `jobs/*/job.json`. El
comportamiento del push se ejercita contra repositorios bare reales en
`tests/test_push.py`.
