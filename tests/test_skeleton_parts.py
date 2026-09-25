"""ADR-034 §5: split class skeleton parts record the source lines they cover."""
from ast_chunker import chunk_file_ast

_FIELDS = "\n".join(
    f"  /** Field number {i}: a documented plain field, whose doc stays in the skeleton. */\n"
    f"  field{i}: number = {i};\n"
    for i in range(60)
)
TS = (
    "@decorated()\n"
    "export class Big<T> extends Base<T> implements Thing {\n"
    f"{_FIELDS}\n"
    "  run(): void { return; }\n"
    "}\n"
)


def _parts():
    return [c for c in chunk_file_ast("big.ts", TS) if c.scope.startswith("big.ts::Big_part_")]


def test_later_parts_do_not_repeat_the_class_header():
    """Measured and rejected in ADR-034 arm 4: a repeated header made the parts alike."""
    parts = _parts()
    assert len(parts) > 1
    for p in parts[1:]:
        assert not p.text.split("Code:\n", 1)[1].startswith("class Big")


def test_parts_record_the_source_lines_they_cover():
    lines = TS.split("\n")
    for p in _parts():
        assert 0 < p.start_line <= p.end_line
        code = p.text.split("Code:\n", 1)[1].split("\n")
        # The first and last lines of the part are the source lines it claims.
        assert code[0].strip() in lines[p.start_line - 1]
        assert code[-1].strip() in lines[p.end_line - 1]
