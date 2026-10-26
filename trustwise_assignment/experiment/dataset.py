"""Read the golden dataset. Only `text` and `mode` ever go to a judge; the rest is for sampling and scoring."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "sanity_subset.csv"


@dataclass(frozen=True)
class Row:
    id: str
    category: str
    mode: str
    label: bool          # True = harmful
    text: str
    subcategory: str | None   # prompt_manipulation only: jailbreak | prompt_injection | pii_output

    @property
    def stratum(self) -> str:
        parts = [self.category, self.mode] + ([self.subcategory] if self.subcategory else [])
        return "/".join(parts)


def dataset_sha256(path: Path = DATASET) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(path: Path = DATASET) -> list[Row]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sub = None
            if r["category"] == "prompt_manipulation":
                sub = json.loads(r["provenance"]).get("source_subcategory")
            rows.append(Row(r["id"], r["category"], r["mode"], r["label"] == "true", r["text"], sub))
    return rows
