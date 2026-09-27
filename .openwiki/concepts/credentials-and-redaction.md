---
type: concept
title: Credentials, git authentication and redaction
description: How the personal access token (PAT) of a private source travels in git HTTP headers instead of the URL, how it is persisted on the job to survive retries, how redact_url/redact_text keep it out of logs, errors, /health and API responses, and the declared security limits (token in /data/openwiki.db, PAT push, ALLOW_LOCAL_GIT, unauthenticated API).
tags: [security, credentials, token, redaction, git, authentication, logging, configuration]
sources:
  - id: openwiki-source-1040945f5d802682e6971c01
    resource: repo://app/core/config.py
  - id: openwiki-source-adaf794dc610fe5ba99131eb
    resource: repo://app/core/storage.py
  - id: openwiki-source-1bf21716e80d908cac0774fa
    resource: repo://app/core/util.py
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
  - id: openwiki-source-854a1a022ad2b98151d36890
    resource: repo://app/services/publisher.py
  - id: openwiki-source-095266a4eaf6119f4f5af1f6
    resource: repo://app/services/wiki_runner.py
  - id: openwiki-source-41ad7047de607612f2c7d0e8
    resource: repo://app/worker/loop.py
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-6f28b34f5f5d9c611f895743
    resource: repo://tests/test_api_wikis.py
  - id: openwiki-source-bde9a53225e597c9b2eaf2e1
    resource: repo://tests/test_ingestion.py
  - id: openwiki-source-c867ed2692ecc41f0a6d22ce
    resource: repo://tests/test_model_status.py
  - id: openwiki-source-aed5fc52a2bc4adb74104780
    resource: repo://tests/test_push.py
generated: { by: "openwiki/0.6.0", at: "2026-09-27T09:33:59.807Z" }
verified:
  - by: openwiki/0.6.0
    at: 2026-09-27T09:33:59.807Z
---

# Credenciales, autenticación git y redacción

OpenWiki Service gestiona exactamente una *credencial de usuario*: el personal
access token (PAT) que un cliente puede enviar para un repositorio Git privado en
`source.auth.token` (`app/schemas.py::GitAuth`). Las claves de API de los
proveedores de modelos (`OPENAI_API_KEY`, ...) son otra historia: **no se declaran
a propósito** en `Settings` ni en los modelos de petición — viven en el entorno del
proceso / `<OPENWIKI_CONFIG_DIR>/.env` y se reenvían **sin modificar** al
subproceso de OpenWiki. Ver
[/openwiki/integrations/model-providers.md](/openwiki/integrations/model-providers.md)
para la tabla de proveedores y ese camino; esta página solo fija el invariante y no
lo repite.

El PAT plantea dos problemas distintos, y el código los resuelve por separado:

| Preocupación | Dónde vive | Página |
| --- | --- | --- |
| **Transporte y almacenamiento** — llevar el token a `git` y conservarlo para los reintentos | `ingestion.git_auth_prefix`, `token_basic_header`, la columna JSON `source` en `openwiki.db` | esta página, [/openwiki/integrations/git-hosting.md](/openwiki/integrations/git-hosting.md) |
| **Ocultación** — mantenerlo fuera de logs, errores y respuestas | `app/core/util.py` (`redact_url`, `redact_text`) más cada frontera de log/error/respuesta | esta página |

## Transporte: una cabecera HTTP, un usuario por forja, nunca la URL

El token solo se coloca en una cabecera HTTP `Authorization: Basic` para un remoto
`http(s)`. `git_auth_prefix(url, token)` devuelve el prefijo `git -c` que lo
transporta; devuelve `[]` cuando no hay token (repositorios públicos, rutas
locales) y lanza `IngestionError` cuando se combina un token con una URL que no es
`http(s)`, de modo que un token nunca puede colarse en un remoto SSH o `file://`:

```
-c http.extraHeader=Authorization: Basic <base64(username:token)>
```

`token_basic_header(url, token)` construye ese valor y elige el nombre de usuario
que la forja destino espera junto al token como contraseña. La coincidencia es una
**subcadena del host** sobre el `netloc` en minúsculas, lo que la convierte en una
heurística y no en una consulta a un registro:

| Forja | `netloc` contiene | Nombre de usuario |
| --- | --- | --- |
| GitHub | `github` | `x-access-token` |
| GitLab | `gitlab` | `oauth2` |
| Bitbucket | `bitbucket` | `x-token-auth` |
| Gitea, Forgejo, genérica o autoalojada | cualquier otra cosa | `oauth2` |

Añadir una forja significa editar `token_basic_header`; todos los llamadores quedan
intactos.

¿Por qué no incrustar las credenciales en la URL (`https://user:token@host/...`)?
Porque esa forma filtra por construcción: la URL es la única pieza de la petición
que acaba en los registros de trabajo, en líneas de log, en mensajes de error y en
`WikiView.source_url`. El servicio, por tanto, almacena la URL y el token como
campos separados y nunca compone una URL autenticada. Un matiz operativo
relacionado: la cabecera se pasa como argumento `git -c`, así que la credencial
base64 aparece en el argv del proceso `git` lanzado (visible con `ps` dentro del
contenedor) en lugar de en la URL remota.

```mermaid
sequenceDiagram
    participant C as Client
    participant API as POST /wikis
    participant DB as openwiki.db
    participant W as Worker pipeline
    participant G as git
    C->>API: source.url + source.auth.token
    API->>API: validate_git_url, reject token on non-http(s)
    API->>DB: INSERT source {url, ref, token}
    DB-->>W: claim_next returns source
    W->>G: clone --depth 1 with -c http.extraHeader=Authorization Basic
    W->>G: fetch --unshallow with the same prefix
    W->>G: push HEAD:refs/heads/branch with the same prefix
```

*Dónde entra el PAT en el sistema y cada llamada a git que autentica.*

El mismo prefijo lo reutilizan `clone_git`, `ensure_full_history` y el publisher
(`_remote_commit`, `git push`), así que clone, unshallow, `ls-remote` y push
autentican de forma idéntica. El prefijo siempre procede del dict `source` en bruto
del trabajo; nunca se reconstruye a partir de una cadena ya redactada.

### Validación en el momento del envío

`POST /wikis` superpone política de protocolo a `validate_git_url` y rechaza la
petición con `422` **antes** de crear ningún trabajo:

- `source.auth.token` solo se acepta para URL `http://`/`https://`;
- `push` exige una URL `http(s)` — las fuentes `git@`/`ssh://` se rechazan
  ("SSH keys are not available in the service") y `git://` se rechaza directamente;
- un `push` por `http(s)` sin token se rechaza, ya que el push sería anónimo.

La misma regla se aplica, por tanto, dos veces: en el envío (`422` amigable) y de
nuevo dentro de `git_auth_prefix` (defensa en profundidad para cualquier llamador
que construya el argv por su cuenta).

## Persistencia: el token vive en la fila del trabajo

Tras la validación, el router almacena el token en bruto en el JSON `source` del
trabajo (`{"type": "git", "url": ..., "ref": ..., "token": ...}`) mediante
`JobStore.create`, que lo serializa en la columna `source TEXT NOT NULL` de
`<data_dir>/openwiki.db` (`/data/openwiki.db` en el contenedor). Esto es
deliberado: `POST /wikis/{id}/retry` vuelve a encolar una wiki fallida o cancelada
y el siguiente worker debe poder re-clonar el repositorio privado sin que el
cliente reenvíe el token.

Consecuencias de ese diseño:

- El token está en reposo en el volumen SQLite y sobrevive a la petición.
- `DELETE /wikis/{id}` borra la fila (y el directorio del trabajo), que es la única
  forma de eliminarlo; `purge_expired` hace lo mismo con wikis terminales antiguas.
- El token **no** forma parte de ningún modelo de respuesta: `source` nunca aparece
  en `WikiView`, y `PushOptions` solo lleva `enabled`, `branch` y `message`.
  `wiki_to_view` mapea el registro interno `job` a la vista pública y renderiza
  `source_url = redact_url(source["url"])`, descrito en el esquema como
  "Redacted URL for git sources".

El pipeline lee el token una vez por generación (`source.get("token")`) y construye
`secrets = (token,) if token else ()`, que después atraviesa todas las llamadas de
logging y de git. El token **no** se inyecta en el entorno del subproceso de
OpenWiki — `build_env` copia `os.environ` y solo sobrescribe variables propias del
servicio (`OPENWIKI_CONFIG_DIR`, `OPENWIKI_TELEMETRY_DISABLED`, la concurrencia de
páginas y, para el proxy compatible con OpenAI, la sobrescritura de la URL base).
Las credenciales de proveedor que ya están en el entorno se reenvían sin
modificar, que es el mecanismo descrito en
[/openwiki/integrations/model-providers.md](/openwiki/integrations/model-providers.md).

## Redacción: dos funciones, aplicadas en cada frontera

`app/core/util.py` es toda la caja de herramientas de redacción:

- `redact_url(url)` enmascara credenciales incrustadas en URL: la regex
  `scheme://user:password@` se sustituye por `scheme://user:***@`. Es idempotente
  para URL sin userinfo.
- `redact_text(text, secrets=())` aplica tres pasadas en orden:
  1. la misma sustitución de credenciales en URL;
  2. una pasada de cabeceras de autenticación — sin distinguir mayúsculas, el valor
     que sigue a `authorization` o `private-token` y a `:` o `=` se sustituye por
     `***` (así, el `Authorization: Basic <base64>` que imprime git pasa a
     `Authorization: ***`);
  3. una pasada literal que reemplaza cada cadena secreta conocida por `***`.

La tercera pasada es la que hace que un token sea seguro incluso cuando git o un
proveedor lo repiten en un mensaje de texto libre. `redact_url` es el helper más
estrecho para los casos en que la única fuga plausible es una URL.

Merece la pena conocer dos límites antes de confiar en cualquiera de las dos
funciones como saneador general. Primero, la sustitución de URL solo se dispara con
la forma literal `scheme://user:password@` — un segmento de userinfo sin dos puntos
(`https://TOKEN@host/...`) o un remoto estilo scp sin esquema
(`git@host:org/repo.git`) se dejan intactos, que es precisamente la razón por la
que el servicio nunca compone una URL autenticada. Segundo, la pasada de cabeceras
enmascara un único token delimitado por espacios tras la palabra clave, así que
neutraliza `Authorization: Basic <base64>` pero no es un analizador de cabeceras
general; los valores con varias palabras solo quedarían cubiertos del todo por la
pasada de secretos literales.

### Puntos de aplicación

| Frontera | Llamada | Qué protege |
| --- | --- | --- |
| Fichero de log del pipeline (`jobs/<id>/logs.txt`) | `pipeline.make_logger(log_path, secrets)` envuelve cada `log(message)` en `redact_text` | líneas de clone/push/plan que escribe el pipeline |
| stdout/stderr del CLI OpenWiki, volcado al log | `run_openwiki` escribe cada línea a través de `redact_text(line, secrets)` | salida del CLI, incluidos errores de SDK de proveedores |
| stdout/stderr de `git` | `run_git_status` aplica `redact_text(..., secrets)` a ambos flujos antes de devolverlos | el detalle de `IngestionError` de `run_git` ya llega redactado |
| Líneas de log del publisher | `log(f"pushing {artifact_dir}/ to {redact_url(url)} …")` | la URL remota en el log de push |
| Errores de validación de URL | `IngestionError(f"invalid git URL: {redact_url(url)}")` | devolver una URL malformada que incrustaba una contraseña |
| `WikiView.source_url` | `redact_url(source.get("url", ""))` en `wiki_to_view` | `GET /wikis` y `GET /wikis/{id}` |
| Bloque `model` de `GET /health` | `describe_model` redacta `base_url` con `redact_url` y solo reporta *nombres* de variables | el valor de `base_url`, el valor de la credencial |
| `GET /health/model` | los errores de la prueba pasan por `redact_text(str(exc), (credential,))` y el `endpoint` reportado por `redact_url` | claves de API de proveedor repetidas en un mensaje 401, URL de API con userinfo |
| Proxy de proveedor en loopback | el proxy ni registra peticiones (`access_log=False`) ni reescribe credenciales; reenvía las cabeceras del llamador menos las hop-by-hop | todo lo que el CLI envía aguas arriba queda fuera de los logs de uvicorn del worker |
| Columnas `error` / `push_result.detail` del trabajo | derivadas de texto de excepción ya redactado, acotado antes de guardarse | el mensaje de fallo duradero que muestra la API |

El texto de fallo duradero está **acotado, no reformateado**: `run_git` lanza
`IngestionError` solo con las últimas ocho líneas de `stderr` (o `stdout`), y el
bucle del worker guarda el mensaje truncado a 500 caracteres
(`error=str(exc)[:500]`, `push_result["detail"] = str(exc)[:500]`). Como los flujos
subyacentes ya fueron redactados por `run_git_status`, lo que sobrevive a ese
truncado sigue siendo texto redactado; el truncado nunca vuelve a exponer nada.

`GET /health` es el caso deliberado de "nombres, nunca valores". `describe_model`
devuelve `credential_env`, `credential_present`, `credential_source`, `base_url`
(redactada), `probe_supported` y `extra_headers`; las cabeceras extra se reducen a
`sorted(parse_extra_headers(...))`, es decir, una lista solo de **nombres** de
cabecera, y un error de parseo se reporta como texto en `extra_headers_error`. La
prueba activa (`/health/model`) es el único lugar del código del servicio que usa
realmente una credencial de proveedor, y lo hace contra el proveedor, sin exponerla
nunca en la respuesta; la prueba habla directamente con la URL base configurada,
saltándose el proxy compatible con OpenAI. Ver
[/openwiki/operations/observability-and-recovery.md](/openwiki/operations/observability-and-recovery.md)
para el contrato del endpoint y
[/openwiki/integrations/model-providers.md](/openwiki/integrations/model-providers.md)
para la tabla de proveedores.

## Riesgos declarados y flags de seguridad

El README declara estos límites de forma explícita en vez de insinuarlos:

- **Sin autenticación en la API.** Está pensada para una red interna o para
  colocarse detrás de un reverse proxy autenticado; un bearer token sería el
  siguiente paso natural. No hay ninguna dependencia de autenticación en
  `create_app` ni en los routers.
- **El PAT se persiste en `/data/openwiki.db`** (fuera de logs y respuestas) para
  que un reintento pueda re-clonar sin reenviarlo. Poseer el volumen de datos es
  poseer el token.
- **`push` escribe en tu repositorio con ese PAT.** Revisa `push.branch`; un push a
  la rama por defecto confirma directamente ahí. Si el push falla, la generación
  queda `failed` pero el `wiki.zip` sigue siendo descargable, y
  `POST /wikis/{id}/retry` reanuda y reintenta el push. Ver
  [/openwiki/workflows/push-back.md](/openwiki/workflows/push-back.md).
- **`ALLOW_LOCAL_GIT=true`** (por defecto `false`) habilita clonar desde rutas
  locales y URL `file://`. Es una comodidad de desarrollo — un contenedor con este
  flag activo puede leer cualquier repositorio git alcanzable a través de su
  sistema de archivos — y el README dice que no se active en producción.
  `validate_git_url` es el único punto de aplicación: las rutas locales y `file://`
  se rechazan salvo que `allow_local` sea true, y el mensaje de error nombra el flag
  (`set ALLOW_LOCAL_GIT=true to enable`).

## Tests focalizados

- `tests/test_ingestion.py::test_token_basic_header_per_forge` comprueba la forma
  `Authorization: Basic` para hosts GitHub y GitLab.
- `tests/test_push.py::test_git_auth_prefix` fija las tres reglas de transporte: sin
  token devuelve `[]`, con token devuelve `-c http.extraHeader=Authorization: Basic …`,
  y una URL SSH con token lanza `IngestionError`.
- `tests/test_push.py::test_push_requires_a_token_for_https` y
  `test_push_rejects_ssh_sources` cubren la política de envío `422`;
  `tests/test_api_wikis.py::test_token_requires_https_url` cubre la misma regla
  para `source.auth.token` sin `push`.
- `tests/test_model_status.py::test_model_check_provider_error_is_redacted`
  comprueba que una clave de proveedor repetida en un mensaje de error nunca llega
  a `/health/model` y que la respuesta contiene `***` en su lugar.
- `tests/test_worker_queue.py` encola trabajos cuyo `source` lleva
  `TOKEN = "ghp_test123"`, ejercitando la ruta de persistencia sin filtrarlo en las
  aserciones de la API.

Páginas relacionadas: [/openwiki/concepts/wiki-lifecycle.md](/openwiki/concepts/wiki-lifecycle.md)
para los estados del trabajo implicados en el reintento,
[/openwiki/operations/configuration.md](/openwiki/operations/configuration.md)
para las variables de entorno anteriores, y
[/openwiki/integrations/git-hosting.md](/openwiki/integrations/git-hosting.md)
para la capa completa de ejecución de git.
