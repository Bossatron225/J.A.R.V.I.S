from pathlib import Path

import pytest

from memory import document_ingestion
from memory.document_ingestion import (
    index_codebase,
    ingest_document,
    recall_document_details,
    search_document_index,
)


@pytest.fixture(autouse=True)
def _isolate_index(tmp_path, monkeypatch):
    monkeypatch.setattr(document_ingestion, "INDEX_PATH", tmp_path / "document_index.json")


def test_ingest_markdown_and_recall(tmp_path):
    note = tmp_path / "project_note.md"
    note.write_text(
        "# Project goals\n\nJarvis should launch on startup and remember the user's favorite coffee.\n",
        encoding="utf-8",
    )

    indexed = ingest_document(str(note), source_name="project-note")
    assert indexed["status"] == "indexed"
    assert indexed["chunks"] >= 1

    matches = search_document_index("startup")
    assert matches
    assert any("launch on startup" in item["content"].lower() for item in matches)

    recall = recall_document_details("favorite coffee")
    assert "coffee" in recall.lower()


def test_index_codebase_recovers_relevant_file_text(tmp_path):
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Sample repo\n\nThis app tracks user hours and launch events.\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "def launch_on_startup():\n    return 'ready'\n\n\ndef track_hours():\n    print('hours recorded')\n",
        encoding="utf-8",
    )

    index = index_codebase(str(repo))
    assert index["status"] == "indexed"
    assert index["count"] >= 2

    matches = search_document_index("launch_on_startup")
    assert matches
    assert any("launch_on_startup" in item["content"] for item in matches)


def test_index_pptx_file(tmp_path):
    pptx_path = tmp_path / "deck.pptx"
    pptx_path.write_bytes(b"not a real pptx file")

    result = ingest_document(str(pptx_path), source_name="deck")
    assert result["status"] in {"indexed", "empty", "missing"}


def _fake_embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT"):
    if task_type == "RETRIEVAL_QUERY":
        return [0.9, 0.1]
    if "espresso" in text:
        return [1.0, 0.0]
    if "printer" in text:
        return [0.0, 1.0]
    return None


def test_ingest_document_stores_chunk_embeddings(tmp_path, monkeypatch):
    monkeypatch.setattr(document_ingestion, "embed_text", _fake_embed)

    note = tmp_path / "note.md"
    note.write_text("I enjoy a fresh espresso every morning.\n", encoding="utf-8")

    indexed = ingest_document(str(note), source_name="note")
    assert indexed["status"] == "indexed"

    index = document_ingestion._load_index()
    doc = next(d for d in index["documents"] if d["path"] == str(note.resolve()))
    assert doc["chunks"][0]["embedding"] == [1.0, 0.0]


def test_search_document_index_ranks_semantic_match_over_keyword_only(tmp_path, monkeypatch):
    monkeypatch.setattr(document_ingestion, "embed_text", _fake_embed)

    espresso_note = tmp_path / "espresso.md"
    espresso_note.write_text("I enjoy a fresh espresso every morning.\n", encoding="utf-8")
    printer_note = tmp_path / "printer.md"
    printer_note.write_text("The office printer needs a new coffee-colored toner cartridge.\n", encoding="utf-8")

    ingest_document(str(espresso_note), source_name="espresso")
    ingest_document(str(printer_note), source_name="printer")

    matches = search_document_index("beverage preference")
    assert matches
    assert matches[0]["source"] == "espresso"


def test_search_document_index_falls_back_to_keyword_without_query_embedding(tmp_path, monkeypatch):
    monkeypatch.setattr(document_ingestion, "embed_text", _fake_embed)

    note = tmp_path / "printer.md"
    note.write_text("The office printer needs a new coffee-colored toner cartridge.\n", encoding="utf-8")
    ingest_document(str(note), source_name="printer")

    monkeypatch.setattr(document_ingestion, "embed_text", lambda text, task_type="RETRIEVAL_DOCUMENT": None)

    matches = search_document_index("printer")
    assert matches
    assert "printer" in matches[0]["content"].lower()
