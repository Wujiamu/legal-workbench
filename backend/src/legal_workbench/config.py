"""Local configuration; secrets must not appear in representations."""
from dataclasses import dataclass, field
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    migration_url: str = field(repr=False)
    data_root: Path
    import_roots: tuple[Path, ...]
    host: str
    port: int
    ocr_model_dir: Path
    max_file_bytes: int
    parse_timeout_seconds: int
    max_image_pixels: int


def load_settings() -> Settings:
    # Explicit environment variables override the local, permission-restricted file.
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    def get(name: str, default: str = "") -> str:
        return os.environ.get("LWB_" + name, values.get("LWB_" + name, default))
    def local_path(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else ROOT / path
    roots = get("IMPORT_ROOTS")
    return Settings(
        database_url=get("DATABASE_URL"),
        migration_url=get("MIGRATION_URL") or get("DATABASE_URL"),
        data_root=local_path(get("DATA_ROOT", "var/data")),
        import_roots=tuple(local_path(p) for p in roots.split(os.pathsep) if p),
        host=get("HOST", "127.0.0.1"), port=int(get("PORT", "8765")),
        ocr_model_dir=local_path(get("OCR_MODEL_DIR", "var/models")),
        max_file_bytes=int(get("MAX_FILE_BYTES", str(2 * 1024**3))),
        parse_timeout_seconds=int(get("PARSE_TIMEOUT_SECONDS", "120")),
        max_image_pixels=int(get("MAX_IMAGE_PIXELS", "50000000")),
    )
