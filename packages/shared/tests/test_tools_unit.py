import pytest

from agentos_core.documents import chunk_markdown, content_type_for
from agentos_core.domain.errors import InvalidInputError, PolicyViolationError, ToolExecutionError
from agentos_core.redaction import redact, redact_text
from agentos_core.storage import Storage, validate_filename
from agentos_core.tools.artifact_writer import ArtifactWriterArgs
from agentos_core.tools.calculator import CalculatorArgs, compute, evaluate_expression
from agentos_core.tools.document_search import build_tsquery
from agentos_core.tools.registry import get_registry


@pytest.mark.parametrize(
    ("op", "values", "expected"),
    [
        ("add", [1, 2, 3], 6),
        ("subtract", [10, 3, 2], 5),
        ("multiply", [2, 3, 4], 24),
        ("divide", [9, 3], 3),
        ("mean", [1, 2, 3, 4], 2.5),
        ("median", [5, 1, 3], 3),
        ("min", [3, 1], 1),
        ("max", [3, 1], 3),
        ("percent_change", [200, 150], -25),
        ("ratio", [3.6, 1.2], 3),
    ],
)
def test_calculator_operations(op, values, expected):
    assert compute(CalculatorArgs(operation=op, values=values)) == pytest.approx(expected)


def test_calculator_expression_parser_is_safe():
    assert evaluate_expression("2 + 3 * (4 - 1) ^ 2") == 29
    assert evaluate_expression("-(1.5e2) / 3") == -50
    for bad in ("__import__('os')", "1; 2", "a + 1", "2 ** 3", "open('x')", "1 / 0", "((((1"):
        with pytest.raises(ToolExecutionError):
            evaluate_expression(bad)


def test_calculator_validation():
    with pytest.raises(ValueError):
        CalculatorArgs(operation="mean")
    with pytest.raises(ValueError):
        CalculatorArgs(operation="mean", values=[float("nan")])
    with pytest.raises(ValueError):
        CalculatorArgs(operation="mean", values=[1], unexpected=True)
    with pytest.raises(ToolExecutionError):
        compute(CalculatorArgs(operation="divide", values=[1, 0]))


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b.md", "/abs.md", ".hidden", "x\x00.md", "", "a" * 200])
def test_filename_validation_blocks_traversal(name):
    with pytest.raises(PolicyViolationError):
        validate_filename(name)


def test_storage_resolve_blocks_escape(tmp_path):
    s = Storage(tmp_path)
    with pytest.raises(PolicyViolationError):
        s.resolve("../outside.txt")
    with pytest.raises(PolicyViolationError):
        s.resolve("/etc/passwd")
    p = s.write_atomic("a/b/c.txt", b"hello")
    assert p.read_bytes() == b"hello" and tmp_path in p.parents
    assert not list(p.parent.glob(".tmp-*"))


def test_artifact_args_validation():
    with pytest.raises(ValueError):
        ArtifactWriterArgs(filename="report.exe", content="x")
    with pytest.raises(ValueError):
        ArtifactWriterArgs(filename="../report.md", content="x")
    args = ArtifactWriterArgs(filename="r.json", content={"b": 1, "a": 2})
    assert args.content == '{\n  "a": 2,\n  "b": 1\n}'


def test_redaction():
    data = {
        "api_key": "sk-ant-abc123456789",
        "nested": {"Authorization": "Bearer x"},
        "input_tokens": 5,
        "note": "key sk-proj-ABCDEFGHIJKLMNOPQRSTUV and postgresql://u:pw@host/db",
    }
    out = redact(data)
    assert out["api_key"] == "[REDACTED]" and out["nested"]["Authorization"] == "[REDACTED]"
    assert out["input_tokens"] == 5
    assert "sk-proj" not in out["note"] and "pw@" not in out["note"]
    assert redact_text("sk-ant-api03-abcdefghij") == "[REDACTED]"


def test_chunking_by_heading_path():
    chunks = chunk_markdown(
        "# Title\n\nIntro.\n\n## Pricing\n\n- A: 1\n\n### GPU\n\nText here.\n\n## Other\n\nMore."
    )
    assert [c.section for c in chunks] == [
        "Title",
        "Title > Pricing",
        "Title > Pricing > GPU",
        "Title > Other",
    ]
    long = chunk_markdown("# T\n\n" + "\n\n".join(["word " * 100] * 10), max_chars=1200)
    assert all(len(c.content) <= 1200 for c in long) and len(long) > 1


def test_tsquery_is_sanitized():
    assert build_tsquery("Nimbus's pricing; DROP TABLE x & | !") == "nimbus | pricing | drop | table"
    assert build_tsquery("the and of") is None


def test_content_types():
    assert content_type_for("x.pdf") == "application/pdf"
    with pytest.raises(InvalidInputError):
        content_type_for("x.exe")


def test_registry_catalog_has_no_shell_and_declares_policy():
    reg = get_registry()
    assert reg.names() == [
        "artifact_writer",
        "calculator",
        "document_search",
        "memory_search",
        "memory_write",
        "structured_data_reader",
    ]
    meta = {m["name"]: m for m in reg.catalog()}
    assert (
        meta["artifact_writer"]["requires_approval"] and meta["artifact_writer"]["side_effect"] == "internal"
    )
    assert not meta["calculator"]["requires_approval"] and meta["calculator"]["side_effect"] == "none"
    assert meta["calculator"]["input_schema"]["properties"]["operation"]


def test_nul_characters_are_stripped_everywhere_persisted_text_is_sanitized():
    from agentos_core.redaction import redact, redact_text, strip_nul

    assert redact_text("ab\x00cd") == "abcd"
    assert strip_nul({"k\x00": ["a\x00b", {"x": "\x00"}], "n": 3}) == {"k": ["ab", {"x": ""}], "n": 3}
    assert redact({"text": "bad\x00text", "api_key": "x"}) == {"text": "badtext", "api_key": "[REDACTED]"}
