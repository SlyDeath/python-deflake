from pathlib import Path

from deflake import result_file


def test_a_line_that_is_not_an_object_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "rows"
    path.write_text('{"id": "a"}\n[1, 2]\n')
    assert result_file.read(path) == [{"id": "a"}]
