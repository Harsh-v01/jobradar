"""Resume text handling for JobRadar."""

from pathlib import Path


def extract_text(path: Path) -> str:
    """Read a plain-text resume."""
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")

    return path.read_text(
        encoding="utf-8",
        errors="replace"
    )


def build_profile(text: str, summarizer=None) -> str:
    """Return the resume text as the candidate profile."""

    lines = []

    for line in text.splitlines():
        line = " ".join(line.split())

        if line:
            lines.append(line)

    profile = "\n".join(lines)

    # Keep the profile reasonably sized for the LLM.
    return profile[:12000]


def parse_details(text: str) -> dict:
    """Basic resume details for the optional web UI."""

    lines = [
        " ".join(line.split())
        for line in text.splitlines()
        if line.strip()
    ]

    name = lines[0] if lines else ""

    return {
        "name": name,
        "headline": "",
        "location": "",
        "experience": [],
        "error": "",
    }


def build_details(text: str, summarizer=None) -> dict:
    """Return basic parsed resume details."""

    return parse_details(text)
