# OpenWiki Service

Servicio HTTP (Docker) que recibe un repositorio — URL git pública, privada con
personal access token, o un `zip`/`tar` subido — y devuelve la documentación
generada por [OpenWiki](https://github.com/langchain-ai/openwiki) como un
`wiki.zip` (Markdown + `openwiki/.claims/`). Opcionalmente, con `push`, la
commitea de vuelta al repositorio de origen.

- **API**: Python 3.12 + FastAPI; encola, sirve estado/logs/zip y reencola
  claims caducados (no genera).
- **Workers**: contenedores de la misma imagen (`python -m app.worker`) que
  ejecutan el CLI `openwiki` (Node 22) en modo one-shot (`openwiki --init -p`);
  escala horizontal con `docker compose up -d --scale worker=N`.
- **Cola**: SQLite (`openwiki.db`, WAL) en el volumen compartido; claims
  atómicos, progreso por heartbeat y reencolado de claims muertos.
- **Desacople**: todo el contacto con OpenWiki vive en
  `app/services/wiki_runner.py`; actualizar OpenWiki es reconstruir la imagen
  con otro `OPENWIKI_VERSION`, sin tocar el código del servicio.

## Quickstart

```bash
cp .env.example .env
# edita .env y pon tu proveedor de modelos + API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...)

docker compose up --build -d --scale worker=2
curl http://localhost:8000/health
```

Documentación interactiva de la API: http://localhost:8000/docs

## Arquitectura y escalado

```
                        ┌──────────────────────────────┐
        POST /wikis ───▶│ api (FastAPI) · no genera    │
                        └───────────────┬──────────────┘
                                        │  openwiki.db (SQLite, WAL)
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
                 ┌────────────┐                  ┌────────────┐
                 │  worker 1  │   ...            │  worker N  │
                 │  OpenWiki  │                  │  OpenWiki  │
                 └────────────┘                  └────────────┘
```

- **api**: crea las wikis (`queued`), sirve estado/logs/zip y reencola claims
  caducados. Es ligera y se puede reiniciar sin matar generaciones.
- **worker**: reclama la siguiente wiki encolada y ejecuta el pipeline
  (clone → OpenWiki → zip → push opcional). Misma imagen que la API, comando
  distinto.
- **Reanudación**: si un worker muere a mitad, la API reencola su claim tras
  `WORKER_LEASE_SECONDS` y otro worker continúa desde `openwiki/.run.json`.

```bash
docker compose up -d --scale worker=4
docker compose ps
curl http://localhost:8000/health   # pool.workers = workers vistos recientemente
```

- Cada worker hace **1 generación a la vez** por defecto
  (`MAX_CONCURRENT_JOBS` por contenedor); la concurrencia la da el número de
  réplicas.
- API y workers se comunican solo por el volumen (`openwiki.db` + `jobs/`): si
  la API se cae, los workers terminan sus generaciones igualmente.
- Pensado para **un host** (volumen local). Para varios hosts, sustituye
  `app/core/storage.py` por un backend Postgres con los mismos métodos.
- Modo de un solo contenedor (dev): `RUN_LOCAL_WORKER=true` arranca un worker
  dentro de la API (`docker compose up -d` sin réplicas de worker).

## API

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/wikis` | Crea una wiki a partir de una URL git (JSON). |
| `POST` | `/wikis/upload` | Crea una wiki a partir de un `zip`/`tar` (multipart). |
| `GET` | `/wikis` | Lista las wikis (más recientes primero). |
| `GET` | `/wikis/{id}` | Estado, progreso, páginas, errores. |
| `GET` | `/wikis/{id}/logs?tail=50` | Cola del log de ejecución de OpenWiki. |
| `GET` | `/wikis/{id}/download` | Descarga la wiki (raíz `.openwiki/` dentro del zip). |
| `POST` | `/wikis/{id}/cancel` | Cancela una wiki en cola o en ejecución. |
| `POST` | `/wikis/{id}/retry` | Reencola una wiki fallida/cancelada; OpenWiki reanuda desde `.run.json`. |
| `DELETE` | `/wikis/{id}` | Borra una wiki terminada. |
| `GET` | `/health` | Salud del servicio, pool de workers y configuración del modelo (sin llamar al proveedor). |
| `GET` | `/health/model` | Comprobación activa de que el modelo configurado responde (cacheada; `?force=true`). |

Estados de una wiki: `queued → fetching → generating → finalizing → done`
(o `failed`, `cancelled`). Un worker la reclama de la cola (`claimed_by`,
`attempts`) y su progreso actualiza el heartbeat; son resumibles: si el timeout
la mata o la cancelas, `POST /wikis/{id}/retry` la reencola y OpenWiki continúa
desde su cola de páginas (`openwiki/.run.json`). Si un worker muere, la API
reencola el claim caducado y otro worker la reanuda en el mismo workspace.

Durante la generación, `progress` se lee del propio plan de OpenWiki
(`3/7 pages · generating`) y los logs llevan marca de tiempo
(`[HH:MM:SS]`).

### Repositorio público

```bash
curl -X POST http://localhost:8000/wikis \
  -H "Content-Type: application/json" \
  -d '{"source": {"url": "https://github.com/org/repo.git"}, "language": "es"}'
```

```json
{ "wiki_id": "e6c1...", "status": "queued", "links": { "status": "/wikis/e6c1...", "logs": "...", "wiki": "..." } }
```

### Repositorio privado (PAT)

```bash
curl -X POST http://localhost:8000/wikis \
  -H "Content-Type: application/json" \
  -d '{
        "source": {
          "url": "https://github.com/org/private-repo.git",
          "ref": "main",
          "auth": { "token": "ghp_xxx" }
        }
      }'
```

El token se envía en la cabecera HTTP del `git clone` (nunca en la URL) y se
redacta de logs y errores. Usuarios por forja: GitHub `x-access-token`,
GitLab `oauth2`, Bitbucket `x-token-auth`; otras forjas (Gitea, ...) `oauth2`.

### Push de la wiki al repositorio

Con `push`, al terminar la generación la wiki se commitea y se empuja al
repositorio de origen (solo fuentes git; requiere un PAT con permiso de
escritura):

```bash
curl -X POST http://localhost:8000/wikis \
  -H "Content-Type: application/json" \
  -d '{
        "source": {"url": "https://github.com/org/repo.git", "auth": {"token": "ghp_xxx"}},
        "push": {"branch": "openwiki/update", "message": "docs: update OpenWiki"}
      }'
```

- `push.branch` (opcional): rama destino. Por defecto la rama clonada; con
  `openwiki/update` se crea una rama aparte (ideal para abrir un PR) sin tocar
  la principal.
- `push.message` (opcional): mensaje de commit; por defecto
  `docs: generate OpenWiki wiki` (init) o `docs: update OpenWiki wiki` (update).
- `push.enabled: false`: guarda la configuración pero no empuja.
- Se commitea **solo** la carpeta `WIKI_ARTIFACT_DIR/` (`.openwiki/`: páginas,
  `.claims/`, `.last-update.json`, `.page-manifest.json`), excluyendo el estado
  transitorio `.run.json`; el andamiaje (`AGENTS.md`, `CLAUDE.md`, workflow) no
  se toca. Autor por defecto `openwiki-service` (`PUSH_AUTHOR_NAME`,
  `PUSH_AUTHOR_EMAIL`).
- El token viaja en la cabecera HTTP del `git push` (nunca en la URL) y se
  redacta de logs y errores.
- Si el push falla (permisos, rama protegida, non-fast-forward), la wiki queda
  `failed` con el detalle en `push_result`, pero el `wiki.zip` sigue
  descargable; `POST /wikis/{id}/retry` reanuda y reintenta el push.
- El resultado se expone en `push_result` (`pushed` / `no_changes` / `failed` /
  `skipped`) con `branch` y `commit`.
- Las wikis en modo `update` sobre un clon superficial hacen
  `git fetch --unshallow`: OpenWiki necesita el commit documentado
  (`gitHead` de `.last-update.json`) para calcular el diff incremental.

### Subida de un zip/tar

```bash
curl -X POST http://localhost:8000/wikis/upload \
  -F "file=@code.zip" \
  -F "language=es"
```

Acepta `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, `.txz`.
La extracción rechaza path traversal, rutas absolutas, symlinks/hardlinks y
archivos que expandan más de `MAX_EXTRACTED_MB`. Si el contenido no trae `.git`,
el servicio crea un repositorio con un commit inicial (OpenWiki versiona la
evidencia de sus Claims con git).

### Consultar y descargar

```bash
curl http://localhost:8000/wikis/<wiki_id>
curl "http://localhost:8000/wikis/<wiki_id>/logs?tail=100"
curl -OJ http://localhost:8000/wikis/<wiki_id>/download
```

Opciones por wiki: `language` (idioma de la wiki; se escribe
`openwiki/INSTRUCTIONS.md` si el repo no trae uno), `concurrency` (1–8,
páginas en paralelo de OpenWiki), `mode` y `push` (commit al repo, ver
arriba):

- `auto` (por defecto): `--update` si el source ya trae `openwiki/` con wiki,
  `--init` si no.
- `init`: regenera la wiki completa desde cero (reemplaza wiki y Claims).
- `update`: incremental; OpenWiki solo revisa cambios del repo y claims
  obsoletos, y **un update limpio no hace trabajo de modelo**.

OpenWiki solo escribe en `openwiki/` (nombre fijo del CLI), así que el workspace
interno siempre usa `openwiki/`; el zip descargable se empaqueta con la raíz
`WIKI_ARTIFACT_DIR` (`.openwiki` por defecto). Si un repo trae la wiki
commiteada como `.openwiki/`, el servicio la adopta como `openwiki/` antes de
correr para poder hacer `--update` incremental.

### Estado del modelo

| Endpoint | Comprueba | Coste |
| --- | --- | --- |
| `GET /health` | Configuración pasiva: proveedor, modelo, si la credencial está presente y de dónde sale (nunca muestra su valor). No llama al modelo. | Ninguno |
| `GET /health/model` | Prueba activa: envía la petición mínima al proveedor para confirmar que credenciales y conectividad funcionan. | Una llamada mínima al proveedor |

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/model              # usa la caché (30 s por defecto)
curl "http://localhost:8000/health/model?force=true" # fuerza una prueba nueva
```

`GET /health` incluye el bloque `model`:

```json
"model": {
  "provider": "openai",
  "provider_source": "environment",
  "model": "gpt-5.6-terra",
  "model_source": "default",
  "credential_env": "OPENAI_API_KEY",
  "credential_present": true,
  "credential_source": "environment",
  "base_url": null,
  "probe_supported": true,
  "active_check_endpoint": "/health/model"
}
```

`GET /health/model` devuelve `200` cuando el estado es `ok` o `unsupported`, y
`503` cuando es `error` o `not_configured`:

```json
{
  "status": "ok",
  "provider": "openai",
  "model": "gpt-5.6-terra",
  "credential_env": "OPENAI_API_KEY",
  "checked_at": "2026-09-26T20:00:00Z",
  "latency_ms": 834,
  "cached": false,
  "detail": "provider answered successfully"
}
```

Estados posibles:

- `ok` — el proveedor respondió con contenido.
- `error` — falló la llamada (HTTP/texto del proveedor, redactado) o llegó una
  respuesta vacía (típico de gateways solo-streaming).
- `not_configured` — falta `OPENWIKI_PROVIDER`, la credencial, el modelo o la
  base URL según el proveedor; el detalle dice exactamente qué variable falta.
- `unsupported` — proveedores cuya autenticación no es una API key (Bedrock con
  IAM, Copilot, ChatGPT login, Gemini Enterprise con ADC) o proveedor
  desconocido.

El chequeo detecta además errores comunes de configuración y los explica en
`detail`: base URL con el path del endpoint incluido (p. ej.
`https://host/v1/chat/completions`; debe ser `https://host/v1`), base URL sin
`/v1` (prueba la variante y dice cuál funciona) o una página HTML en vez de
JSON.

### Gateways OpenAI-compatible con cabeceras extra

Algunos gateways (p. ej. [OpenCode Go](https://opencode.ai/docs/go/)) exigen que
el cliente se identifique con cabeceras propias (`x-opencode-session` y un
User-Agent de cliente). OpenWiki no puede enviarlas por sí mismo, así que
cuando configuras `OPENAI_COMPATIBLE_EXTRA_HEADERS` el servicio arranca un
**proxy loopback** y apunta OpenWiki a él; el proxy reenvía todo al gateway
añadiendo esas cabeceras. `/health/model` las envía también, contra el
upstream real.

```dotenv
OPENWIKI_PROVIDER=openai-compatible
OPENAI_COMPATIBLE_API_KEY=tu-api-key
OPENAI_COMPATIBLE_BASE_URL=https://opencode.ai/zen/go/v1
OPENAI_COMPATIBLE_EXTRA_HEADERS={"x-opencode-session":"openwiki-service"}
OPENWIKI_MODEL_ID=deepseek-v4-flash
```

- El valor es un objeto JSON de cabeceras; si no indicas `User-Agent` se añade
  `openwiki-service/<versión>` (los gateways suelen rechazar el User-Agent
  genérico del SDK). Las cabeceras extra reemplazan a las que envíe OpenWiki.
- `/health` muestra solo los **nombres** de las cabeceras, nunca sus valores.
- `COMPAT_PROXY_PORT` (9100 por defecto) elige el puerto; el proxy solo escucha
  en `127.0.0.1` dentro del contenedor y se apaga con el servicio.

La configuración se resuelve igual que en OpenWiki: entorno del contenedor
sobre `<OPENWIKI_CONFIG_DIR>/.env` (el entorno gana). La prueba activa está
implementada para OpenAI (Responses API), Anthropic, Gemini, OpenRouter,
OpenAI-compatible, NVIDIA, Fireworks y Baseten (`/chat/completions`).

## Configuración

Variables propias del servicio:

| Variable | Default | Descripción |
| --- | --- | --- |
| `DATA_DIR` | `/data` (Linux) | Estado persistente: wikis, repos, logs. |
| `JOB_TIMEOUT_MINUTES` | `45` | Timeout por generación; al superarlo se mata el proceso. |
| `MAX_UPLOAD_MB` | `200` | Tamaño máximo de subida. |
| `MAX_EXTRACTED_MB` | `1024` | Tamaño máximo descomprimido. |
| `MAX_CONCURRENT_JOBS` | `1` | Generaciones por contenedor worker (escala con `--scale worker=N`). |
| `RUN_LOCAL_WORKER` | `false` | Arranca un worker dentro de la API (un solo contenedor / dev). |
| `WORKER_ID` | hostname | Identidad del worker en la cola. |
| `WORKER_POLL_SECONDS` | `2` | Sondeo de la cola cuando no hay trabajo. |
| `WORKER_PROGRESS_SECONDS` | `2` | Frecuencia de heartbeats/progreso durante la generación. |
| `WORKER_LEASE_SECONDS` | `180` | Sin heartbeat durante este tiempo, el claim se reencola. |
| `WORKER_REAP_SECONDS` | `30` | Barrido de claims caducados en la API. |
| `DEFAULT_PAGE_CONCURRENCY` | `2` | `OPENWIKI_PAGE_CONCURRENCY` por defecto (1–8). |
| `JOB_RETENTION_HOURS` | `0` | Borra wikis terminadas tras N horas (`0` = conservar). |
| `MODEL_CHECK_TTL_SECONDS` | `30` | Caché de `/health/model` (`0` = probar en cada llamada). |
| `MODEL_CHECK_TIMEOUT_SECONDS` | `20` | Timeout de la prueba activa del modelo. |
| `OPENAI_COMPATIBLE_EXTRA_HEADERS` | — | JSON con cabeceras extra para gateways OpenAI-compatible (ver abajo). |
| `COMPAT_PROXY_PORT` | `9100` | Puerto loopback del proxy que inyecta esas cabeceras. |
| `WIKI_ARTIFACT_DIR` | `.openwiki` | Carpeta raíz dentro del `wiki.zip` (el workspace interno sigue siendo `openwiki/`). |
| `PUSH_AUTHOR_NAME` | `openwiki-service` | Autor de los commits creados por `push`. |
| `PUSH_AUTHOR_EMAIL` | `openwiki-service@localhost` | Email del autor de esos commits. |
| `ALLOW_LOCAL_GIT` | `false` | Permite clonar rutas locales / `file://` (solo desarrollo). |
| `OPENWIKI_BIN` | `openwiki` | Comando del CLI (admite `"python" "script.py"` para tests). |

El proveedor de modelos se configura con las variables de OpenWiki
(`OPENWIKI_PROVIDER`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...), ver
[`.env.example`](.env.example) y la
[lista de proveedores](https://github.com/langchain-ai/openwiki#model-providers).
El servicio fuerza `OPENWIKI_CONFIG_DIR=/data/.openwiki-config` y
`OPENWIKI_TELEMETRY_DISABLED=1`.

## Actualizar OpenWiki

```bash
# fija la nueva versión en el build (recomendado)
docker compose build --build-arg OPENWIKI_VERSION=0.7.0
docker compose up -d

# o directamente con la última publicada
docker compose build --build-arg OPENWIKI_VERSION=latest
```

No hay que cambiar código del servicio mientras el CLI mantenga
`openwiki --init -p`. Si OpenWiki cambia su interfaz, el único archivo a tocar
es `app/services/wiki_runner.py`.

## Desarrollo y tests

```bash
# con uv (https://docs.astral.sh/uv/)
uv venv --python 3.12
uv pip install -e ".[test]"
uv run pytest

# ejecución local: API + un worker in-process (dev)
RUN_LOCAL_WORKER=true uv run uvicorn app.main:create_app --factory --reload

# o en dos procesos, como en Docker (el worker reclama de la cola SQLite)
uv run uvicorn app.main:create_app --factory
uv run python -m app.worker
```

Los tests corren todo el pipeline (clone → openwiki fake → zip → descarga) con
un CLI simulado (`tests/fixtures/fake_openwiki.py`) y un worker in-process, así
que no necesitan proveedor de modelos ni red.

> Nota: la imagen configura `safe.directory` a nivel de sistema para poder
> clonar repositorios montados o con otro propietario. Si ejecutas el servicio
> fuera de Docker y git rechaza un repo con `dubious ownership`, añade
> `git config --global --add safe.directory <ruta>`.

## Estructura

```
app/
├── main.py                  # factory de FastAPI + lifespan (reaper, worker local)
├── core/config.py           # Settings (env vars)
├── core/storage.py          # cola y estado en SQLite (openwiki.db, WAL)
├── core/util.py             # redacción de credenciales
├── routers/wikis.py         # endpoints de wikis
├── routers/health.py        # /health
├── services/pipeline.py     # clone → openwiki → zip → push (una generación)
├── services/ingestion.py    # clone git, subidas, extracción segura, git init
├── services/wiki_runner.py  # ÚNICO punto de contacto con el CLI OpenWiki
├── services/packer.py       # empaquetado de openwiki/ en wiki.zip
├── services/publisher.py    # commit + push de la wiki al repositorio
└── worker/loop.py           # worker: reclama de la cola y ejecuta el pipeline
```

### Añadir endpoints / features

1. La API está organizada por routers: crear `app/routers/<recurso>.py` e
   incluirlo en `create_app()`.
2. La lógica de negocio vive en `app/services/`; los routers no hablan con git
   ni con el CLI directamente.
3. La coordinación API↔workers vive en `app/core/storage.py` (SQLite en el
   volumen). Para multi-host, sustituye ese módulo por Postgres manteniendo los
   métodos (`claim_next`, `heartbeat`, `complete`, `recover_stale`, ...).

Roadmap natural ya previsto: `GET /wikis/{id}/pages` (páginas parseadas desde el
front matter), webhooks de finalización y servido del visualizador
estático (`openwiki visualize --export`).

## Seguridad y límites conocidos (MVP)

- **Sin autenticación** en la API: pensada para red interna o detrás de un
  reverse proxy con auth. Añadir un bearer token es el siguiente paso natural.
- La cola es un SQLite en el volumen (`/data/openwiki.db`, WAL): sirve para un
  host con varios contenedores; para multi-host usa un backend Postgres.
- El token de un repo privado se guarda en la base del volumen
  (`/data/openwiki.db`, fuera de logs y respuestas) para poder reintentar la
  wiki sin reenviarlo; si el workspace se pierde, la wiki fallará y hay que
  reenviarla.
- `push` escribe en tu repositorio con ese PAT: revisa la rama destino. Si el
  push falla, la generación queda `failed` pero la wiki sigue descargable.
- `ALLOW_LOCAL_GIT=true` permite clonar rutas locales/`file://` (útil en
  desarrollo); no lo actives en producción.
