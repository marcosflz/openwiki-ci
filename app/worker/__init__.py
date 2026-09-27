"""Worker runtime: claims wikis from the SQLite queue and runs them."""

from .loop import WorkerLoop

__all__ = ["WorkerLoop"]
