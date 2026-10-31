"""Add validated option maxima to the existing HIF catalog without changing cards."""

import argparse
import csv
import io
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c091aeae9f9637c25c4894340704f5dbad430c72"
OUTPUT = ROOT / "extensions/hif/catalog/hif_customization_catalog.json"


def parse_csv_records(text):
    """Mirror upstream utils/csv.js: TRUE/FALSE become booleans and digit-only fields become ints."""

    def value(field):
        if field in ("TRUE", "FALSE"):
            return field == "TRUE"
        return int(field) if field.isascii() and field.isdigit() else field

    return [{key: value(field) for key, field in row.items()} for row in csv.DictReader(io.StringIO(text))]


def build_catalog(catalog, customizations):
    by_id = {item["id"]: item for item in customizations}
    for card in catalog["cards"]:
        if card.get("sourceEvidence", {}).get("type") == "mumu-screenshot":
            continue  # Screenshot branch lengths stay authoritative until upstream IDs are confirmed.
        for option in card["options"]:
            maximum = by_id[option["id"]]["max"]
            if type(maximum) is not int or maximum < 1:
                raise ValueError(f"Invalid customization maximum: {option['id']}")
            option["max"] = maximum
    catalog["customizationRevision"] = REVISION
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customizations", type=Path, help="Local pinned upstream customizations.csv")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.customizations:
        customizations = parse_csv_records(args.customizations.read_text(encoding="utf-8-sig"))
    else:
        request = Request(
            f"https://raw.githubusercontent.com/gakumas-tools/gakumas-tools/{REVISION}"
            "/packages/gakumas-data/csv/customizations.csv",
            headers={"User-Agent": "MaaGakumasu-HIF-catalog"},
        )
        with urlopen(request, timeout=30) as response:
            customizations = parse_csv_records(response.read().decode("utf-8-sig"))
    original = OUTPUT.read_text(encoding="utf-8")
    content = json.dumps(build_catalog(json.loads(original), customizations), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if content != original:
            raise SystemExit("HIF customization maxima differ from pinned upstream data")
    else:
        OUTPUT.write_text(content, encoding="utf-8")
    print("HIF customization maxima verified")


if __name__ == "__main__":
    main()
