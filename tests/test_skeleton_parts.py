"""ADR-034 §5: split class skeleton parts carry the class header and their source lines."""
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


def test_later_parts_repeat_the_class_header_without_decorators():
    parts = _parts()
    assert len(parts) > 1
    header = "class Big<T> extends Base<T> implements Thing"
    for p in parts[1:]:
        assert p.text.split("Code:\n", 1)[1].startswith(header + "\n")
    assert "@decorated" not in parts[1].text


def test_parts_record_the_source_lines_they_cover():
    lines = TS.split("\n")
    for p in _parts():
        assert 0 < p.start_line <= p.end_line
        code = p.text.split("Code:\n", 1)[1].split("\n")
        body = code[1:] if p.scope != "big.ts::Big_part_1" else code
        # The first and last body lines are the source lines the part claims.
        assert body[0].strip() in lines[p.start_line - 1]
        assert body[-1].strip() in lines[p.end_line - 1]
