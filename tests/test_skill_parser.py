"""Tests for lib/skill_parser.py — zip and raw-text extraction."""
import io
import zipfile

from lib import skill_parser


def _zip_with(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extract_skill_md_from_zip_root():
    blob = _zip_with({"SKILL.md": "# Saman Skill\nUse this.", "examples/a.md": "x"})
    assert "Saman Skill" in skill_parser.extract_skill_md(blob)


def test_extract_skill_md_prefers_root_over_nested():
    blob = _zip_with({
        "nested/SKILL.md": "WRONG",
        "SKILL.md": "RIGHT",
    })
    assert skill_parser.extract_skill_md(blob) == "RIGHT"


def test_extract_skill_md_from_zip_without_skill():
    blob = _zip_with({"README.md": "hello"})
    assert skill_parser.extract_skill_md(blob) == ""


def test_extract_skill_md_from_raw_markdown():
    blob = b"# Saman Voice Skill\n\nDo X when Y."
    out = skill_parser.extract_skill_md(blob)
    assert "Saman Voice Skill" in out


def test_extract_skill_md_rejects_non_markdown_text():
    blob = b"some random text not markdown"
    assert skill_parser.extract_skill_md(blob) == ""


def test_extract_skill_md_empty():
    assert skill_parser.extract_skill_md(b"") == ""


def test_extract_skill_md_concatenates_referenced_files():
    """SKILL.md plus references/*.md should all reach the prompt."""
    blob = _zip_with({
        "saman-cold-email-voice/SKILL.md": "# Cold Voice\nLoad voice-guide.md",
        "saman-cold-email-voice/references/voice-guide.md": "## TLDR\nNo em-dashes.",
        "saman-cold-email-voice/references/examples.md": "Example A",
    })
    out = skill_parser.extract_skill_md(blob)
    assert "Cold Voice" in out
    assert "No em-dashes" in out
    assert "Example A" in out
    # Non-root files are annotated with their path so Claude knows the source.
    assert "references/voice-guide.md" in out
    assert "references/examples.md" in out
    # SKILL.md content comes before its referenced files.
    assert out.index("Cold Voice") < out.index("No em-dashes")


def test_extract_skill_md_skips_macosx_metadata():
    blob = _zip_with({
        "__MACOSX/SKILL.md": "JUNK",
        "SKILL.md": "REAL",
    })
    assert skill_parser.extract_skill_md(blob) == "REAL"


def test_extract_skill_md_caps_oversized_bundles(monkeypatch):
    """A pathologically large bundle should be truncated, not OOM."""
    monkeypatch.setattr(skill_parser, "_MAX_BUNDLE_BYTES", 1000)
    blob = _zip_with({
        "SKILL.md": "ok",
        "references/big.md": "X" * 5000,
    })
    out = skill_parser.extract_skill_md(blob)
    # SKILL.md still present; oversized reference dropped.
    assert "ok" in out
    assert "X" * 5000 not in out
