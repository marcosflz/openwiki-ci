"""Unit tests for OpenWiki run-state parsing and progress formatting."""

from __future__ import annotations

import json
import zipfile

import pytest

from app.core.config import Settings
from app.services.packer import pack_wiki
from app.services.wiki_runner import adopt_hidden_wiki, decide_mode, format_progress, read_run_state


def test_wiki_artifact_dir_validation(tmp_path):
    assert Settings(data_dir=tmp_path, wiki_artifact_dir=".openwiki").wiki_artifact_dir == ".openwiki"
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, wiki_artifact_dir="bad/name")


def test_adopt_hidden_wiki(tmp_path):
    hidden = tmp_path / ".openwiki"
    hidden.mkdir()
    (hidden / "index.md").write_text("# wiki\n", encoding="utf-8")

    assert adopt_hidden_wiki(tmp_path, ".openwiki") is True
    assert (tmp_path / "openwiki" / "index.md").exists()
    # Already adopted / native directory: no-op.
    assert adopt_hidden_wiki(tmp_path, ".openwiki") is False
    assert adopt_hidden_wiki(tmp_path, "openwiki") is False


def test_pack_wiki_renames_root(tmp_path):
    repo = tmp_path / "repo"
    wiki = repo / "openwiki"
    wiki.mkdir(parents=True)
    (wiki / "index.md").write_text("# wiki\n", encoding="utf-8")
    (wiki / "quickstart.md").write_text("# start\n", encoding="utf-8")

    pages, size = pack_wiki(repo, tmp_path / "wiki.zip", root_name=".openwiki")
    assert pages == 2
    assert size > 0
    with zipfile.ZipFile(tmp_path / "wiki.zip") as zf:
        assert set(zf.namelist()) == {".openwiki/index.md", ".openwiki/quickstart.md"}


def test_decide_mode(tmp_path):
    assert decide_mode(tmp_path, None) == "init"
    assert decide_mode(tmp_path, "auto") == "init"
    assert decide_mode(tmp_path, "update") == "init"  # falls back without a wiki
    assert decide_mode(tmp_path, "init") == "init"

    wiki = tmp_path / "openwiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# wiki\n", encoding="utf-8")
    assert decide_mode(tmp_path, "auto") == "update"
    assert decide_mode(tmp_path, "update") == "update"
    assert decide_mode(tmp_path, "init") == "init"


def test_read_run_state_missing_file(tmp_path):
    assert read_run_state(tmp_path) is None


def test_read_run_state_summarizes_plan(tmp_path):
    wiki = tmp_path / "openwiki"
    wiki.mkdir()
    (wiki / ".run.json").write_text(
        json.dumps(
            {
                "phase": "generating",
                "plan": {
                    "pages": [
                        {"path": "/openwiki/a.md", "status": "complete"},
                        {"path": "/openwiki/b.md", "status": "pending"},
                        {"path": "/openwiki/c.md", "status": "pending"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert read_run_state(tmp_path) == {
        "phase": "generating",
        "total": 3,
        "completed": 1,
        "pending": 2,
        "current": "/openwiki/b.md",
    }


def test_read_run_state_tolerates_garbage(tmp_path):
    wiki = tmp_path / "openwiki"
    wiki.mkdir()
    (wiki / ".run.json").write_text("{not json", encoding="utf-8")
    assert read_run_state(tmp_path) is None

    (wiki / ".run.json").write_text(json.dumps({"plan": {"pages": "nope"}}), encoding="utf-8")
    assert read_run_state(tmp_path) == {
        "phase": None,
        "total": 0,
        "completed": 0,
        "pending": 0,
        "current": None,
    }


def test_format_progress():
    assert format_progress({"completed": 2, "total": 7, "phase": "generating"}) == "2/7 pages · generating"
    assert format_progress({"completed": 2, "total": 7, "phase": None}) == "2/7 pages"
    assert format_progress(None, 3) == "3 pages written"
    assert format_progress(None, 0) is None
    assert format_progress({"total": 0}, 3) == "3 pages written"
