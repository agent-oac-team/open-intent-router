from pathlib import Path

from app.services.knowledge_excel_parser import parse_workbook

ROOT = Path("/Users/lijingtong/project/data/内容生产")


def test_parser_handles_two_level_headers_and_is_deterministic() -> None:
    first = parse_workbook(ROOT / "01 要素表.xlsx")
    second = parse_workbook(ROOT / "01 要素表.xlsx")

    assert first.chunks
    assert [chunk.chunk_id for chunk in first.chunks] == [chunk.chunk_id for chunk in second.chunks]
    assert "要素类型 / 字段分类" in first.chunks[0].content
    assert first.chunks[0].source_ref.sheet == "01 要素表"
    assert first.chunks[0].source_ref.row_start == 3
    assert first.chunks[0].source_ref.cell_range == "A3:D3"


def test_parser_marks_customer_workbook_empty_without_fake_chunks() -> None:
    parsed = parse_workbook(ROOT / "03 客群表.xlsx")

    assert parsed.empty is True
    assert parsed.chunks == []
    assert parsed.asset_id == "asset_content_production_03"


def test_parser_preserves_long_text_formula_metadata_and_source_rows() -> None:
    activity = parse_workbook(ROOT / "04 活动表.xlsx", max_chunk_chars=500)
    rights = parse_workbook(ROOT / "05 权益表.xlsx", max_chunk_chars=500)
    templates = parse_workbook(ROOT / "06 企微模板.xlsx", max_chunk_chars=500)

    assert len(activity.chunks) > 23
    assert len(rights.chunks) > 25
    assert templates.chunks
    assert all(len(chunk.content) <= 500 for chunk in activity.chunks + rights.chunks)
    assert all(chunk.source_ref.row_start for chunk in templates.chunks)
    assert all("formula_cells" in chunk.source_ref.metadata for chunk in templates.chunks)
