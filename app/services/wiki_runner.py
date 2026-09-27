"""The only module that knows how to invoke the OpenWiki CLI.

Keeping every OpenWiki-specific detail here means:
* upgrading OpenWiki is a container rebuild (``OPENWIKI_VERSION`` build arg),
* new OpenWiki modes/features only require changes in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.util import redact_text

#: OpenWiki hardcodes this directory name in the repository (no option to change it).
NATIVE_WIKI_DIR = "openwiki"


class OpenWikiRunError(Exception):
    """Raised when the OpenWiki CLI cannot complete a run."""


class OpenWikiRunCancelled(Exception):
    """Raised when a run is stopped because the wiki was cancelled."""


def split_command(command: str) -> list[str]:
    """Split ``OPENWIKI_BIN`` into argv, keeping Windows backslashes intact."""
    if os.name == "nt":
        return [token.strip('"') for token in shlex.split(command, posix=False) if token.strip('"')]
    return shlex.split(command)


def build_env(
    settings: Settings,
    *,
    page_concurrency: int | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the OpenWiki subprocess.

    Provider credentials already live in the process environment and are
    forwarded untouched; only service-owned variables are overridden.
    """
    env = os.environ.copy()
    env["OPENWIKI_CONFIG_DIR"] = str(settings.resolved_config_dir)
    env["OPENWIKI_TELEMETRY_DISABLED"] = env.get("OPENWIKI_TELEMETRY_DISABLED", "1")
    if page_concurrency:
        env["OPENWIKI_PAGE_CONCURRENCY"] = str(page_concurrency)
    if extra:
        env.update(extra)
    return env


def decide_mode(repo_dir: Path, requested: str | None) -> str:
    """Choose between ``init`` and ``update`` for a repository workspace.

    ``auto`` (the service default) updates when the source already ships
    OpenWiki docs, and initializes otherwise. An explicit ``update`` on a
    source without docs falls back to ``init``.
    """
    has_wiki = (Path(repo_dir) / "openwiki" / "index.md").exists()
    mode = (requested or "auto").strip().lower()
    if mode == "auto":
        return "update" if has_wiki else "init"
    if mode == "update" and not has_wiki:
        return "init"
    return mode


def adopt_hidden_wiki(repo_dir: Path, artifact_dir: str) -> bool:
    """Move a hidden artifact directory (e.g. ``.openwiki/``) to ``openwiki/``.

    OpenWiki only reads and updates its hardcoded ``openwiki/`` directory, so a
    repository that ships the wiki as ``.openwiki/`` gets it adopted before the
    run. Returns True when a directory was moved.
    """
    if artifact_dir == NATIVE_WIKI_DIR:
        return False
    native = Path(repo_dir) / NATIVE_WIKI_DIR
    hidden = Path(repo_dir) / artifact_dir
    if hidden.is_dir() and not native.exists():
        native.parent.mkdir(parents=True, exist_ok=True)
        hidden.rename(native)
        return True
    return False


def read_run_state(repo_dir: Path) -> dict[str, Any] | None:
    """Summarize OpenWiki's durable run state (``openwiki/.run.json``).

    Returns ``None`` when the file does not exist yet or cannot be parsed.
    """
    path = Path(repo_dir) / "openwiki" / ".run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plan = data.get("plan") if isinstance(data, dict) else None
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list):
        pages = []
    completed = 0
    pending: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        if page.get("status") == "complete":
            completed += 1
        else:
            pending.append(page)
    return {
        "phase": data.get("phase"),
        "total": len(pages),
        "completed": completed,
        "pending": len(pending),
        "current": (pending[0].get("path") if pending else None),
    }


def format_progress(state: dict[str, Any] | None, pages_written: int = 0) -> str | None:
    """Human-readable progress string for job.json."""
    if state and state.get("total"):
        text = f"{state['completed']}/{state['total']} pages"
        if state.get("phase"):
            text += f" · {state['phase']}"
        return text
    if pages_written:
        return f"{pages_written} pages written"
    return None


def prepare_instructions(repo_dir: Path, language: str | None) -> Path | None:
    """Write ``openwiki/INSTRUCTIONS.md`` when a language is requested.

    A user-authored brief shipped with the source always wins.
    """
    if not language:
        return None
    instructions = Path(repo_dir) / "openwiki" / "INSTRUCTIONS.md"
    if instructions.exists():
        return None
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(
        "# OpenWiki instructions\n\n"
        f"- Write every wiki page in `{language}`.\n"
        "- Keep code identifiers, file paths, commands and API names untouched.\n"
        "- Prefer architecture, data flow and public interfaces over line-by-line commentary.\n",
        encoding="utf-8",
    )
    return instructions


async def run_openwiki(
    repo_dir: Path,
    *,
    settings: Settings,
    log_path: Path,
    mode: str = "init",
    page_concurrency: int | None = None,
    secrets: tuple[str, ...] = (),
    env_overrides: dict[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Run OpenWiki in one-shot mode (``openwiki --init -p``) inside ``repo_dir``.

    Output is streamed to ``log_path``. Raises :class:`OpenWikiRunError` on a
    non-zero exit code or when the job timeout is exceeded, and
    :class:`OpenWikiRunCancelled` when ``cancel_event`` gets set.
    """
    repo_dir = Path(repo_dir)
    log_path = Path(log_path)
    command = split_command(settings.openwiki_bin) + [f"--{mode}", "-p"]
    if not command[0]:
        raise OpenWikiRunError("OPENWIKI_BIN is not configured")

    timeout_seconds = settings.job_timeout_minutes * 60
    env = build_env(settings, page_concurrency=page_concurrency, extra=env_overrides)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo_dir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise OpenWikiRunError(f"OpenWiki binary not found: {command[0]!r}") from exc

    assert process.stdout is not None
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8", errors="replace") as log_file:

        def write_line(text: str) -> None:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            log_file.write(f"[{stamp}] {text if text.endswith(chr(10)) else text + chr(10)}")
            log_file.flush()

        write_line(f"$ {' '.join(command)} (cwd={repo_dir})")

        async def pump() -> None:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                write_line(redact_text(line, secrets))

        async def wait_for_process() -> int:
            """Wait for the CLI, honouring the cancel event and the timeout."""
            pump_task = asyncio.create_task(pump())
            cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            try:
                waiters = {pump_task} if cancel_task is None else {pump_task, cancel_task}
                done, _ = await asyncio.wait(
                    waiters, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_task is not None and cancel_task in done and pump_task not in done:
                    process.kill()
                    await process.wait()
                    write_line("! openwiki killed: generation cancelled")
                    raise OpenWikiRunCancelled("cancelled by user")
                if pump_task in done:
                    pump_task.result()
                else:
                    raise asyncio.TimeoutError
                return await process.wait()
            finally:
                if cancel_task is not None:
                    cancel_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_task
                if not pump_task.done():
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump_task

        try:
            returncode = await wait_for_process()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            write_line(f"! openwiki exceeded the {settings.job_timeout_minutes} minute timeout and was killed")
            raise OpenWikiRunError(f"openwiki timed out after {settings.job_timeout_minutes} minutes")
        except asyncio.CancelledError:
            # The worker is shutting down: never leave an orphan process behind.
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise

    if returncode != 0:
        raise OpenWikiRunError(f"openwiki exited with code {returncode} (see the job logs)")
