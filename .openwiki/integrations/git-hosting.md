---
type: integration
title: Integración con git y forjas
description: How OpenWiki Service uses the git binary as an external dependency - URL validation, shallow clone with ref fallback, per-forge token authentication over HTTPS, unshallow for incremental updates, git init for uploaded sources, and the push-back commit - plus the safe.directory requirement of the container image.
tags: [git, clone, authentication, forge, push, safe-directory, ingestion, publisher]
sources:
  - id: openwiki-source-1bf21716e80d908cac0774fa
    resource: repo://app/core/util.py
  - id: openwiki-source-8832fa4e30af9055e856d9c7
    resource: repo://app/routers/wikis.py
  - id: openwiki-source-499c7d24cf6d363b118ec0e1
    resource: repo://app/services/ingestion.py
  - id: openwiki-source-88002490f9fe36143a262df6
    resource: repo://app/services/pipeline.py
  - id: openwiki-source-854a1a022ad2b98151d36890
    resource: repo://app/services/publisher.py
  - id: openwiki-source-41ad7047de607612f2c7d0e8
    resource: repo://app/worker/loop.py
  - id: openwiki-source-bb1ebe868e35e9e500714501
    resource: repo://Dockerfile
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-bde9a53225e597c9b2eaf2e1
    resource: repo://tests/test_ingestion.py
  - id: openwiki-source-aed5fc52a2bc4adb74104780
    resource: repo://tests/test_push.py
generated: { by: "openwiki/0.6.0", at: "2026-09-27T09:33:59.807Z" }
verified:
  - by: openwiki/0.6.0
    at: 2026-09-27T09:33:59.807Z
---

# Integración con git y forjas

`git` es la dependencia externa más consecuente del servicio. El `Dockerfile` lo
instala explícitamente (`git: cloning repositories`) y solo dos módulos del servicio
lo manejan:

| Módulo | Responsabilidad |
| --- | --- |
| `app/services/ingestion.py` | Es dueño de la capa de ejecución de git (`run_git`, `run_git_status`, `git_auth_prefix`, `clone_git`, `ensure_full_history`, `ensure_git_repo`) y de la validación de URLs. |
| `app/services/publisher.py` | Importa esa capa y la usa para preparar, commitear y empujar la wiki generada. |

Todo lo que está por debajo del router es agnóstico de git: `app/services/pipeline.py`
solo llama a `ingestion.clone_git`, `ingestion.ensure_full_history`,
`ingestion.ensure_git_repo` y `publisher.publish_wiki`, y la API nunca ejecuta git por
sí misma. La única frontera que decide *si* se toca el remoto para escribir es el
publisher; ver [/openwiki/workflows/push-back.md](/openwiki/workflows/push-back.md).

## Validar la URL de origen

`validate_git_url(url, *, allow_local)` es el único lugar donde viven las reglas de
URLs de git; `POST /wikis` lo llama y traduce un `IngestionError` en HTTP `422` en
lugar de reimplementar las reglas. La función recorta la entrada y devuelve la URL
**sin cambios** cuando tiene éxito:

| Forma de entrada | Aceptada cuando |
| --- | --- |
| `git@host:org/repo.git` (SSH estilo scp) | Siempre, devuelta tal cual. |
| `http://`, `https://`, `ssh://`, `git://` | `urlparse` produce un `netloc` no vacío. |
| `file://` | `allow_local` (`ALLOW_LOCAL_GIT`) es true. |
| Ruta de unidad Windows (`^[A-Za-z]:[\\/]`) | `allow_local` es true y la ruta existe. |
| Ruta local sin esquema | `allow_local` es true y la ruta existe. |
| Cualquier otro esquema (por ejemplo `ftp://`) | Rechazada: `unsupported URL scheme`. |

Los mensajes de error pasan por `redact_url`, así que una URL que lleve credenciales
`user:password@` incrustadas nunca puede devolverse con la contraseña intacta.

El router añade política de protocolo por encima de la validación, y todas estas son
respuestas `422` emitidas antes de crear el job:

- un token (`source.auth.token`) solo se acepta para URLs `http://`/`https://`;
- `push` exige una URL `http(s)` — las fuentes `git@`/`ssh://` se rechazan con
  "SSH keys are not available in the service", y `git://` se rechaza de plano;
- un `push` por `http(s)` sin token se rechaza, porque el push sería anónimo.

Una vez aceptada, la URL, el `ref` y el token se guardan en la fila del job; el token
permanece ahí para que un reintento pueda clonar de nuevo sin que el cliente lo
reenvíe, y la vista pública (`WikiView.source_url`, vía `wiki_to_view`) solo expone la
URL redactada.

## La capa de ejecución de git: `run_git_status` frente a `run_git`

Ambos helpers lanzan `git` con `asyncio.create_subprocess_exec`, con `stdin` en
`DEVNULL` y `stdout`/`stderr` en tuberías, y ambos aplican la misma política de timeout
y de redacción. La diferencia es cómo tratan una salida distinta de cero:

| | `run_git_status` | `run_git` |
| --- | --- | --- |
| Devuelve | `(exit_code, stdout, stderr)` | `stdout` |
| Salida distinta de cero | Se devuelve al llamador | Lanza `IngestionError` con las últimas 8 líneas de `stderr` (o `stdout`), con el prefijo `git <subcommand> failed (exit N)` |
| Timeout | Mata el proceso y lanza `IngestionError("git <subcommand> timed out after Ns")` | Igual, a través de `run_git_status` |
| Salida | `redact_text` aplicado a ambos flujos | Heredado de `run_git_status` |

`run_git_status` existe porque varios comandos de git usan el código de salida como
dato y no como señal de error, y un wrapper que siempre lanzara los haría inutilizables:

- `git diff --cached --quiet` en el publisher: `0` significa "nada preparado", `1`
  significa "hay un cambio preparado" y cualquier valor por encima de `1` es un fallo
  real que el publisher reporta como `PushError`.
- `git rev-parse HEAD`, `git symbolic-ref --short -q HEAD`,
  `git check-ref-format --branch` y `git ls-remote`: un código distinto de cero
  significa "detached", "rama desconocida" o "remoto no alcanzable", y los llamadores
  lo resuelven en un resultado deliberado (`_remote_commit` devuelve `None`,
  `_target_branch` lanza `PushError`).

La redacción importa aquí: `secrets=(token,)` se propaga en cada llamada, así que
`redact_text` enmascara las credenciales de URL (`scheme://user:***@host`), los valores
de cabecera `authorization`/`private-token` y cualquier aparición literal del token
antes de que el texto llegue a un fichero de log o a un mensaje de excepción.

Los timeouts son por punto de llamada:

| Constante | Valor | Usada por |
| --- | --- | --- |
| `_GIT_TIMEOUT_SECONDS` (por defecto) | 300 s | `add`, `commit`, el `fetch`/`checkout` dentro de `clone_git`, `init`, `rev-parse`, `symbolic-ref`, `check-ref-format`, `diff` |
| `_CLONE_TIMEOUT_SECONDS` | 600 s | el `clone` en sí, el `fetch` del ref y `--unshallow` |
| `_PUSH_TIMEOUT_SECONDS` | 300 s | `git push` |
| `_REMOTE_LOOKUP_TIMEOUT_SECONDS` | 60 s | `git ls-remote` antes de decidir entre `no_changes` y un push |

## Autenticación por token: una cabecera HTTP, un usuario por forja

`git_auth_prefix(url, token)` devuelve el prefijo `git -c` que autentica las peticiones
HTTPS; se antepone a cada lista de argv que habla con el remoto. Devuelve una lista
vacía cuando no hay token (repositorios públicos, rutas locales) y lanza
`IngestionError` cuando se combina un token con una URL que no es `http(s)` — la misma
regla que aplica el router en el momento del envío.

El token viaja en una cabecera HTTP, nunca en la URL:

```
-c http.extraHeader=Authorization: Basic <base64(username:token)>
```

`token_basic_header` elige el nombre de usuario que la forja destino espera junto al
token (inspecciona el `netloc` en minúsculas, así que la coincidencia es por subcadena
del host):

| Forja | Host coincidente (`netloc` contiene) | Nombre de usuario |
| --- | --- | --- |
| GitHub | `github` | `x-access-token` |
| GitLab | `gitlab` | `oauth2` |
| Bitbucket | `bitbucket` | `x-token-auth` |
| Gitea, Forgejo, genérica/autoalojada | cualquier otra cosa | `oauth2` |

Es un mapeo deliberadamente heurístico: una instancia autoalojada cuyo hostname no
contenga ninguna de esas subcadenas cae en `oauth2`, que es lo que aceptan Gitea y
Forgejo. Añadir una forja significa extender esta función, no los llamadores.

## `clone_git`: clonado superficial con fallback de ref

`clone_git(url, dest, *, ref=None, token=None)` produce el workspace sobre el que corre
todo el pipeline. En la práctica `dest` no debe existir: si existe se elimina
(`shutil.rmtree`), que es lo que hace seguro un re-clonado tras un fallo parcial.

Todo clonado es superficial y sin tags — `git clone --depth 1 --no-tags` — porque el
servicio solo necesita la punta de una revisión; los tags añadirían objetos que nadie
lee y una historia completa de un repositorio grande es puro coste.

```mermaid
flowchart TD
    A["clone_git(url, dest, ref, token)"] --> B["rmtree dest si existe, mkdir del padre"]
    B --> C["prefix = git_auth_prefix"]
    C --> D{"ref dado"}
    D -- no --> H["git clone --depth 1 --no-tags url dest"]
    D -- sí --> E["git clone --depth 1 --no-tags --branch ref --single-branch url dest"]
    E -- "exit 0" --> Z["workspace listo"]
    E -- "IngestionError" --> F["rmtree dest"]
    F --> H
    H --> G["git fetch --depth 1 origin ref"]
    G --> I["git checkout --quiet FETCH_HEAD"]
    I --> Z
```

*`clone_git`: la vía rápida por rama y el fallback fetch/checkout usado para refs que `--branch` no puede resolver.*

- Con un `ref`, el primer intento es
  `git clone --depth 1 --no-tags --branch <ref> --single-branch <url> <dest>`, que
  resuelve ramas y tags directamente en el remoto y deja `HEAD` en la revisión pedida.
- `git clone --branch` no puede hacer checkout de un SHA de commit en crudo, así que un
  `IngestionError` de ese intento se traga, el `dest` parcial se borra y se ejecuta el
  fallback: un clonado superficial normal de la rama por defecto y después
  `git fetch --depth 1 origin <ref>` seguido de
  `git checkout --quiet FETCH_HEAD`. Traerse un SHA arbitrario exige además que el
  servidor lo permita (`uploadpack.allowAnySHA1InWant`); el test de ingesta lo habilita
  en el repositorio de origen precisamente por eso.
- Un fallo en el `fetch`/`checkout` del fallback *no* se traga: se propaga como
  `IngestionError` y hace fallar la wiki, porque no hay una tercera estrategia.

`FETCH_HEAD` es el pivote del fallback: el fetch escribe ahí la revisión pedida sin
crear ninguna rama local, así que el workspace acaba en un checkout detached. Eso se
observa aguas abajo — ver la regla de rama de push más abajo.

## `ensure_full_history`: hacer posible `--update`

Los clonados superficiales y las actualizaciones incrementales están en conflicto
directo. `openwiki --update` diferencia el `HEAD` actual contra el commit registrado en
`openwiki/.last-update.json` (`gitHead`), y un clonado `--depth 1` simplemente no
contiene ese commit: la actualización calcularía un resumen de cambios vacío y no haría
trabajo útil.

`ensure_full_history(repo_dir, *, url, token)` es la reparación:

1. Si `<repo>/.git/shallow` no existe, el clonado ya está completo y la función devuelve
   `False` sin tocar la red.
2. En caso contrario ejecuta `git fetch --quiet --unshallow origin` (600 s, autenticado
   con el mismo prefijo) y devuelve `True`.

`app/services/pipeline.py` la llama solo cuando el modo resuelto es `update`, y registra
`fetched the full history so --update can diff against the last documented commit`
cuando realmente hizo el fetch. `tests/test_push.py::test_update_wikis_fetch_the_full_history`
verifica ambos lados: el marcador de clonado superficial ya no está en el workspace y la
línea de log está presente.

## `ensure_git_repo`: dejar git disponible para todo lo demás

OpenWiki versiona su evidencia de fuentes mediante git, así que un workspace sin
repositorio es inutilizable. `ensure_git_repo(repo_dir)` devuelve `False` cuando `.git`
ya existe y, si no, ejecuta:

```
git init --quiet
git -c user.email=openwiki-service@localhost -c user.name=openwiki-service add --all
git -c user.email=... -c user.name=... commit --quiet --allow-empty -m "Import source for OpenWiki"
```

La identidad se pasa por comando en lugar de configurarse globalmente, así que la imagen
nunca depende de que haya una identidad de git en el entorno. `--allow-empty` mantiene
válido un archivo vacío. El pipeline la llama de forma incondicional después del fetch,
así que es un no-op para fuentes git y la vía efectiva solo para subidas cuyo archivo no
trae `.git`.

## Interacción completa de git en una generación

El diagrama siguiente sitúa los tres momentos en los que el servicio habla con el
remoto — el clonado, el `--unshallow` de los updates y el push — y muestra qué comandos
llevan el prefijo de autenticación (solo los de red: `clone`, `fetch`, `ls-remote` y
`push`).

```mermaid
sequenceDiagram
    participant P as pipeline.run_pipeline
    participant I as ingestion
    participant B as publisher
    participant G as binario git
    participant R as remoto
    P->>I: clone_git(url, dest, ref, token)
    I->>G: clone --depth 1 --no-tags con --branch ref --single-branch
    G->>R: HTTPS con Authorization Basic
    alt --branch no puede resolver el ref
        I->>G: fetch --depth 1 origin ref
        I->>G: checkout --quiet FETCH_HEAD
    end
    P->>I: ensure_full_history solo cuando el modo es update
    I->>G: fetch --quiet --unshallow origin
    G->>R: trae la historia completa
    P->>B: publish_wiki(...)
    B->>G: add --all --force con pathspec del artifact dir
    B->>G: diff --cached --quiet
    B->>G: commit --quiet --message
    B->>G: rev-parse HEAD
    B->>G: ls-remote --quiet url refs/heads/branch
    G->>R: consulta de solo lectura
    B->>G: push --quiet url HEAD:refs/heads/branch
    G->>R: escritura de la rama destino
```

*Los tres contactos con el remoto a lo largo de una generación: clonado superficial, `--unshallow` para updates y push.*

## Push de vuelta: resolución de rama, commit y `ls-remote`

`publish_wiki` es el único escritor del remoto. Sus pasos relacionados con git son:

1. `_target_branch` — `push.branch` si se da, y si no `git symbolic-ref --short -q
   HEAD`. El checkout desnudo que deja el fallback de `FETCH_HEAD` no tiene ref
   simbólica, así que una fuente por tag/SHA lanza
   `PushError("push.branch is required when the source is checked out at a tag or commit")`.
   El nombre resuelto se valida después con `git check-ref-format --branch` (el
   validador Pydantic de la API ya prefiltra las formas malas evidentes con un `422`).
2. `_expose_artifact_dir` — renombra el `openwiki/` hardcodeado de OpenWiki a
   `WIKI_ARTIFACT_DIR`, reemplazando un árbol ya existente en esa ruta, de modo que solo
   la wiki dentro del directorio de artefactos acaba en el commit.
3. `git add --all --force -- <artifact_dir> :(exclude)<artifact_dir>/.run.json` — el
   pathspec limita el staging al directorio de artefactos y excluye el estado transitorio
   de reanudación, que no debe commitearse.
4. `git diff --cached --quiet` vía `run_git_status` — el código de salida decide si hay
   commit o no.
5. `git -c user.name=<PUSH_AUTHOR_NAME> -c user.email=<PUSH_AUTHOR_EMAIL> commit
   --quiet --message <message>`, donde el mensaje es `push.message` o el valor por
   defecto del modo: `docs: generate OpenWiki wiki` / `docs: update OpenWiki wiki`.
6. `git rev-parse HEAD` para el id de commit y después `git ls-remote --quiet <url>
   refs/heads/<branch>` (60 s). Si la punta remota ya es igual al commit local y no
   había nada preparado, el resultado es `{"status": "no_changes", ...}` y se omite el
   push; un `ls-remote` fallido o vacío devuelve `None` en lugar de un error, así que
   nunca puede bloquear un push.
7. `git push --quiet <url> HEAD:refs/heads/<branch>` (300 s), con el mismo
   `git_auth_prefix`, produciendo `{"status": "pushed", "branch", "commit", ...}`.

`git_auth_prefix` solo se aplica a los dos comandos de red — el `ls-remote` del paso 6 y
el `push` del paso 7 —, de modo que el push y la consulta al remoto se autentican igual
que el clonado; el staging, el `diff`, el `commit` y el `rev-parse` son locales y corren
sin prefijo. `PushError` es la única moneda de fallo: `IngestionError` se convierte en
ella, y `WorkerLoop._run` la mapea a `status="failed"` con `error="push failed: ..."`
(truncado a 500 caracteres) más un `push_result` de `{"status": "failed"}`. El
`wiki.zip` empaquetado sigue siendo descargable y `POST /wikis/{id}/retry` vuelve a
ejecutar el push.

Páginas relacionadas: [/openwiki/workflows/generation-from-git.md](/openwiki/workflows/generation-from-git.md)
para el lado de la generación, [/openwiki/workflows/push-back.md](/openwiki/workflows/push-back.md)
para el contrato del push de vuelta, [/openwiki/concepts/credentials-and-redaction.md](/openwiki/concepts/credentials-and-redaction.md)
para cómo se mantiene el token fuera de logs, respuestas y errores, y
[/openwiki/operations/deployment.md](/openwiki/operations/deployment.md) para ejecutar
la imagen.

## `safe.directory`: por qué la imagen configura git a nivel de sistema

Las versiones recientes de git se niegan a operar sobre un repositorio cuyo propietario
es distinto del usuario que ejecuta el comando (`fatal: detected dubious ownership in
repository at ...`). Esa es la situación normal aquí: los repositorios se montan en el
contenedor o se clonan en un volumen que puede pertenecer a otro usuario, mientras que el
proceso corre como `appuser` (uid `10001`).

`safe.directory` **no** puede pasarse por invocación con `git -c` — git solo lo honra
desde la **configuración de sistema o global** —, y por eso está horneado en la imagen en
lugar de añadirse al argv:

```
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app \
    && git config --system --add safe.directory '*'
```

El `'*'` confía en todos los directorios dentro del contenedor. Es un compromiso
deliberado: el contenedor es de propósito único y de vida corta, y la alternativa sería
fallar en cada workspace montado. Sí implica que la imagen desactiva a propósito una de
las comprobaciones de propiedad de git, así que no debería usarse como base para trabajo
multi-inquilino.

Ejecutar el servicio **fuera de Docker** (desarrollo local, tests, un `uvicorn` desnudo)
significa que esta configuración no está presente. Si git entonces rechaza un workspace
con `dubious ownership`, quien opera debe configurarlo a mano:

```bash
git config --global --add safe.directory <path>
```

La misma advertencia está registrada junto al helper de subprocesos en el código fuente y
en el README, porque un fallo de `dubious ownership` aflora como un opaco
`IngestionError: git clone failed (exit 128)`: el mensaje viene del `stderr` de git y se
redacta, pero no se interpreta de ninguna otra forma.

## Tests enfocados

| Test | Qué fija |
| --- | --- |
| `tests/test_ingestion.py::test_validate_git_url_accepts_http_and_ssh` / `_rejects_unknown_scheme` / `_local_paths_are_opt_in` | El conjunto de esquemas aceptados y el opt-in de `allow_local`. |
| `tests/test_ingestion.py::test_token_basic_header_per_forge` | El mapeo de usuario por forja produce una cabecera `Authorization: Basic`. |
| `tests/test_ingestion.py::test_ensure_git_repo_initializes_and_is_idempotent` | `True` en la primera llamada, `False` una vez que existe `.git`. |
| `tests/test_ingestion.py::test_clone_git_checks_out_branch` / `_checks_out_commit_sha` | La vía rápida con `--branch` y el fallback fetch/`FETCH_HEAD` (el test del SHA habilita `uploadpack.allowAnySHA1InWant` en el origen). |
| `tests/test_push.py::test_git_auth_prefix` | Prefijo vacío sin token, prefijo de cabecera con uno, `IngestionError` para URLs `git@`. |
| `tests/test_push.py::test_push_commits_the_wiki_to_the_cloned_branch` | Push de extremo a extremo a un origen desnudo cuya rama es `main`: el árbol de artefactos (incluido `.claims/`) se commitea, `.run.json` y `AGENTS.md` no, y el autor es `openwiki-service <openwiki-service@localhost>`. |
| `tests/test_push.py::test_push_to_a_custom_branch` | `HEAD:refs/heads/<branch>` crea la rama pedida y deja intacta la rama clonada. |
| `tests/test_push.py::test_push_reports_no_changes_when_the_wiki_is_identical` | La ruta `diff --cached --quiet` / `ls-remote` devuelve `no_changes`. |
| `tests/test_push.py::test_push_requires_a_token_for_https` / `_rejects_ssh_sources` / `_with_an_invalid_branch_is_rejected` | La política de protocolo previa al push del router y la validación de rama como `422`. |
| `tests/test_push.py::test_push_failure_keeps_the_wiki_downloadable` | Un push rechazado (origen no desnudo con la rama en checkout) produce `status=failed` con `push_result.status="failed"` mientras `/download` sigue devolviendo `200`. |
| `tests/test_push.py::test_push_of_a_detached_checkout_needs_a_branch` | El checkout de `FETCH_HEAD` a partir de un tag en `ref` exige un `push.branch` explícito. |
| `tests/test_push.py::test_disabled_push_is_skipped` | `push.enabled=false` nunca toca el remoto. |
| `tests/test_push.py::test_update_wikis_fetch_the_full_history` | `ensure_full_history` elimina `.git/shallow` en una ejecución `update`. |
