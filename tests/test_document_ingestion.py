from pathlib import Path

from memory.document_ingestion import (
    index_codebase,
    ingest_document,
    recall_document_details,
    search_document_index,
)


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
