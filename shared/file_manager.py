
import re
from pathlib import Path
from datetime import datetime


PORTAL_PATTERNS = {
    "prisma":    r'E(?P<merchant>\w+)MX(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_(?P<hour>\d{6})\.(?P<ext>\w+)',
    "cabal":     r'(?P<name>.+)\.(?P<ext>\w+)',
    "naranjax":  r'(?P<name>.+)\.(?P<ext>\w+)',
    "fiserv":    r'(?P<name>.+)\.(?P<ext>\w+)',
    "amex":      r'(?P<name>.+)\.(?P<ext>\w+)',
    "getnet":    r'(?P<name>.+)\.(?P<ext>\w+)',
}


def rename_file(provider: str, original_name: str, output_pattern: str) -> str:
    """
    Renames a file according to the configured pattern.

    Available placeholders in output_pattern:
        {merchant}  — merchant code (Prisma)
        {date}      — date in DD-MM-YYYY format
        {hour}      — time in HH:MM:SS format
        {ext}       — extension in lowercase
        {original}  — original name unchanged
        {provider}  — provider name
        {today}     — today's date DD-MM-YYYY

    Example:
        output_pattern = "{merchant}_{date}.{ext}"
        original_name  = "E10484MX20260320_020132.ZIP"
        result         = "10484_20-03-2026.zip"
    """
    regex_pattern = PORTAL_PATTERNS.get(provider)
    data          = _extract_data(original_name, regex_pattern)

    data["original"] = original_name
    data["provider"] = provider
    data["today"]    = datetime.now().strftime("%d-%m-%Y")

    if "year" in data and "month" in data and "day" in data:
        data["date"] = f"{data['day']}-{data['month']}-{data['year']}"

    if "hour" in data and len(data["hour"]) == 6:
        h = data["hour"]
        data["hour"] = f"{h[:2]}:{h[2:4]}:{h[4:]}"

    if "ext" in data:
        data["ext"] = data["ext"].lower()

    try:
        return output_pattern.format(**data)
    except KeyError:
        return original_name


def _extract_data(name: str, regex_pattern: str) -> dict:
    """Extracts data from the filename using the portal's regex."""
    if not regex_pattern:
        return {}
    m = re.match(regex_pattern, name, re.IGNORECASE)
    return m.groupdict() if m else {}


def create_folder(path: str) -> Path:
    """Creates the destination folder if it does not exist."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
