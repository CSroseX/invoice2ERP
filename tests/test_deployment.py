"""Guards on what the Docker image ships (see Dockerfile and .dockerignore)."""
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ignored(path: str) -> bool:
    """Whether .dockerignore leaves `path` out of the build context (last matching rule wins)."""
    ignored = False
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        pattern = rule.lstrip("!").rstrip("/")
        if fnmatch(path, pattern) or fnmatch(path, pattern + "/*") or path.startswith(pattern + "/"):
            ignored = not negate
    return ignored


def test_image_ships_the_data_the_showcase_reads():
    # The read-only app renders output/*.json, the source PDFs, the OCR benchmark and master data;
    # leaving any of them out of the image deploys an app that silently shows nothing.
    for path in ["output/DU-02.json", "documents/DU-02.pdf",
                 "measurements/ocr_benchmark/current/run_info.txt", "master_data/suppliers.json"]:
        assert (ROOT / path).exists(), path
        assert not _ignored(path), f"{path} is excluded by .dockerignore"


def test_image_leaves_out_secrets_and_local_state():
    for path in [".env", "logs/audit.jsonl", ".venv/bin/python"]:
        assert _ignored(path), f"{path} should be excluded by .dockerignore"
