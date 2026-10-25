"""Load private project configuration consistently across launch paths."""

from pathlib import Path


ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_environment() -> None:
    """Fill unset process variables from the project .env, without expansion."""

    # An explicit path prevents loading a different .env from the caller's cwd
    # or a parent directory. Missing configuration remains a supported no-op.
    if not ENV_FILE.is_file():
        return
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False, interpolate=False, encoding="utf-8")
