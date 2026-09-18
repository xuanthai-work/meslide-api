"""Regression guard: GSlide-owned runtime code must not ship tracking hooks."""

from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[4]
ACTIVE_ROOTS = (
    API_ROOT / "scripts",
    API_ROOT / "servers" / "fastapi" / "api",
    API_ROOT / "servers" / "fastapi" / "services",
    API_ROOT / "servers" / "fastapi" / "utils",
)
BANNED_TOKENS = (
    "DISABLE_ANONYMOUS_TRACKING",
    "SENTRY_DSN",
    "SENTRY_TRACES_SAMPLE_RATE",
    "SENTRY_SEND_DEFAULT_PII",
    "sentry_sdk",
)


def test_gslide_runtime_has_no_tracking_hooks() -> None:
    offenders: list[str] = []
    for root in ACTIVE_ROOTS:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".js", ".cjs", ".mjs"}:
                continue
            source = path.read_text(encoding="utf-8")
            matches = [token for token in BANNED_TOKENS if token in source]
            if matches:
                offenders.append(f"{path.relative_to(API_ROOT)}: {', '.join(matches)}")

    assert offenders == [], "GSlide tracking hooks remain:\n" + "\n".join(offenders)
