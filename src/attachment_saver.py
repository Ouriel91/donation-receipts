from datetime import datetime
from pathlib import Path
import re


MONTH_NAMES = {
    1: "01_January",
    2: "02_February",
    3: "03_March",
    4: "04_April",
    5: "05_May",
    6: "06_June",
    7: "07_July",
    8: "08_August",
    9: "09_September",
    10: "10_October",
    11: "11_November",
    12: "12_December",
}


SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def sanitize_filename_part(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9א-ת]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "receipt"


def parse_email_date(date_value: str) -> datetime:
    return datetime.strptime(date_value, "%Y-%m-%d")


def build_receipt_directory(base_dir: Path, account: str, date_value: str) -> Path:
    parsed_date = parse_email_date(date_value)
    return base_dir / account / str(parsed_date.year) / MONTH_NAMES[parsed_date.month]


def build_base_filename(date_value: str, label: str, extension: str) -> str:
    parsed_date = parse_email_date(date_value)
    safe_label = sanitize_filename_part(label)
    return f"{parsed_date:%d_%m_%y}__{safe_label}{extension.lower()}"


def next_available_path(directory: Path, base_filename: str) -> Path:
    candidate = directory / base_filename

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix

    counter = 2
    while True:
        next_candidate = directory / f"{stem}__{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def plan_receipt_path(
    base_dir: Path,
    account: str,
    date_value: str,
    original_filename: str,
    label: str | None = None,
) -> Path:
    extension = Path(original_filename).suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported attachment extension: {extension}")

    effective_label = label or Path(original_filename).stem or "receipt"
    directory = build_receipt_directory(base_dir, account, date_value)
    base_filename = build_base_filename(date_value, effective_label, extension)

    return next_available_path(directory, base_filename)


def save_attachment(
    content: bytes,
    target_path: Path,
    dry_run: bool = True,
) -> Path:
    if dry_run:
        return target_path

    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        raise FileExistsError(f"Target file already exists: {target_path}")

    target_path.write_bytes(content)

    return target_path