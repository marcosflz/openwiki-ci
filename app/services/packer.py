"""Package the generated wiki into a downloadable zip."""

from __future__ import annotations

import contextlib
import zipfile
from pathlib import Path

from .wiki_runner import NATIVE_WIKI_DIR


class PackError(Exception):
    """Raised when a repository did not produce a complete wiki."""


def count_pages(repo_dir: Path) -> int:
    """Number of Markdown pages written so far (progress reporting)."""
    wiki = Path(repo_dir) / NATIVE_WIKI_DIR
    if not wiki.is_dir():
        return 0
    return sum(1 for _ in wiki.rglob("*.md"))


def pack_wiki(
    repo_dir: Path,
    dest_zip: Path,
    *,
    root_name: str = NATIVE_WIKI_DIR,
) -> tuple[int, int]:
    """Zip ``<repo>/openwiki`` into ``dest_zip`` under ``root_name``.

    ``root_name`` renames the top-level folder inside the archive (for example
    ``.openwiki``) without touching the workspace layout OpenWiki requires.
    Returns ``(pages, size_bytes)`` and raises :class:`PackError` when the wiki
    is missing or incomplete.
    """
    repo_dir = Path(repo_dir)
    dest_zip = Path(dest_zip)
    wiki = repo_dir / NATIVE_WIKI_DIR
    if not wiki.is_dir():
        raise PackError("openwiki/ directory was not produced")
    if not (wiki / "index.md").exists():
        raise PackError("openwiki/index.md is missing; the run did not complete")

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_name(dest_zip.name + ".tmp")
    pages = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(wiki.rglob("*")):
            if path.is_dir():
                continue
            arcname = f"{root_name}/{path.relative_to(wiki).as_posix()}"
            zf.write(path, arcname=arcname)
            if path.suffix == ".md":
                pages += 1
    with contextlib.suppress(OSError):
        dest_zip.unlink()
    tmp.replace(dest_zip)
    return pages, dest_zip.stat().st_size
