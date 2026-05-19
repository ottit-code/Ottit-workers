"""
Extract skill content from an Anthropic Skill bundle.

A `.skill` file is either:
  - a zip archive whose root contains `SKILL.md` (and optional referenced files
    under `references/`, `assets/`, etc.), or
  - a plain text file whose contents are SKILL.md directly (legacy/loose form).

Anthropic Skills use "progressive disclosure": `SKILL.md` references other files
that the agent loads on demand. Our drafter is a single-shot endpoint with no
follow-up turns, so we eagerly concatenate `SKILL.md` plus every other `.md`
file in the bundle and hand the whole thing to Claude as one document. Claude's
context window has plenty of room (~35 KB for the saman cold-email skill, well
under the system-prompt budget).

Any failure returns an empty string — Layer 2 is optional in v1.
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Cap the bundle expansion so a malicious or oversized .skill cannot blow up
# our system prompt. 200 KB is ~50k tokens — generous for any sane voice guide.
_MAX_BUNDLE_BYTES = 200_000


def extract_skill_md(blob: bytes) -> str:
    """Return concatenated skill content from a `.skill` blob.

    For zip bundles: SKILL.md first, then every other `.md` file sorted by path.
    For raw text: returned verbatim if it looks like markdown.
    Empty string on failure or unrecognized content.
    """
    if not blob:
        return ""

    if zipped := _extract_from_zip(blob):
        return zipped

    return _extract_from_text(blob)


def _extract_from_zip(blob: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return ""
    except Exception as exc:
        logger.warning("skill_parser.zip_open_error: %s", exc)
        return ""

    with zf:
        md_files = _list_markdown_files(zf)
        # SKILL.md is the required entry point per Anthropic's convention. A
        # bundle without one isn't a real Skill — refuse so we don't accidentally
        # feed an unrelated README into the prompt.
        if not md_files or not _is_root_skill_md(md_files[0]):
            logger.warning(
                "skill_parser.no_skill_md_in_zip names=%s",
                zf.namelist()[:5],
            )
            return ""

        sections: List[str] = []
        total = 0
        for name in md_files:
            try:
                with zf.open(name) as fh:
                    raw = fh.read()
            except Exception as exc:
                logger.warning("skill_parser.zip_read_error name=%s err=%s", name, exc)
                continue

            if total + len(raw) > _MAX_BUNDLE_BYTES:
                logger.warning(
                    "skill_parser.bundle_truncated_at name=%s read=%d cap=%d",
                    name, total, _MAX_BUNDLE_BYTES,
                )
                break
            total += len(raw)

            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # Prefix every non-root file with its path so Claude can see how
            # the skill references it (e.g. "references/voice-guide.md").
            sections.append(text if _is_root_skill_md(name) else f"<!-- {name} -->\n{text}")

        return "\n\n---\n\n".join(sections)


def _list_markdown_files(zf: zipfile.ZipFile) -> List[str]:
    """Return all `.md` files in the bundle, SKILL.md first, then sorted by path.

    Skips directories, hidden files, and `__MACOSX/*` metadata.
    """
    names = [
        n for n in zf.namelist()
        if n.lower().endswith(".md")
        and not n.endswith("/")
        and not n.startswith("__MACOSX/")
        and "/." not in n
        and not n.lstrip("/").startswith(".")
    ]

    skill_files: List[Tuple[int, int, str]] = []
    other_files: List[str] = []
    for n in names:
        if _is_root_skill_md(n):
            # Prefer the shallowest SKILL.md if multiple exist.
            skill_files.append((n.count("/"), len(n), n))
        else:
            other_files.append(n)

    skill_files.sort()
    other_files.sort()
    return [s[2] for s in skill_files[:1]] + other_files


def _is_root_skill_md(name: str) -> bool:
    """True for `SKILL.md` at any depth (case-insensitive on filename)."""
    return name.rsplit("/", 1)[-1].lower() == "skill.md"


def _extract_from_text(blob: bytes) -> str:
    try:
        text = blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        logger.warning("skill_parser.binary_blob_not_zip_not_text")
        return ""

    if text.lstrip().startswith(("---", "#")):
        return text

    logger.warning("skill_parser.text_did_not_look_like_skill_md")
    return ""
