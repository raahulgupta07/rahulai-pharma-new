"""Validate an upload BEFORE it is allowed to replace live data.

Both loaders are destructive by design: an article export replaces the whole
catalog, a balance export truncates and reloads the whole inventory. That is the
requested behaviour — the newest file is the world — but it means a bad upload
is not a failed import, it is data loss. So nothing is written until the file
has been read, its columns checked, and its rows sampled.

Three classes of problem, checked in this order because each makes the next
meaningless:

1. **Format** — is it a readable xlsx/csv, and can we tell what it is from the
   filename? An unclassifiable name has no schema to check against.
2. **Columns** — are the required headers present after mapping? This is the
   check that would have caught the state behind the 2026-07-28 field reports,
   where a catalog loaded with no ``Brand Name`` column and every row silently
   took ``brand_name = article_code``.
3. **Data** — do the rows actually carry usable values? A file can have perfect
   headers and still be 4,000 rows of blanks.

Plus one guard that is not about correctness at all: a **shrink check**. A valid
export of 40 rows will happily replace a 5,292-row catalog, because "replace the
world" cannot tell a deliberate small upload from someone exporting a filtered
view by mistake. Demonstrated live on 2026-08-02 — the catalog went 5,292 -> 40
and 5,252 rows were deleted, exactly as designed. A large drop now needs an
explicit override rather than a shrug.

Nothing here talks to the database except the shrink check, which reads counts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from app.ingest import _CATALOG_MAP, detect_kind

logger = logging.getLogger(__name__)

# Columns without which the file cannot do its job. Everything else is optional
# and simply lands as NULL — a catalog with no `Side Effect` column is thin, not
# broken; one with no `Brand Name` is unusable.
REQUIRED_CATALOG_HEADERS = ("Article Code", "Brand Name")
REQUIRED_INVENTORY_HEADERS = ("article_code", "site_code", "stock_qty")

# Columns whose absence is worth saying out loud without blocking the load.
EXPECTED_CATALOG_HEADERS = (
    "Generic Name", "Composition", "Category", "Indication", "Dosage",
)

_CODE_RE = re.compile(r"^\d{6,20}$")
_SITE_RE = re.compile(r"^[0-9]{3,8}-[A-Za-z0-9]{2,12}$")

# A file may not shrink a table by more than this and still load unattended.
MAX_SHRINK = 0.50

# Reject outright below this many usable rows — a real CityCare export is
# thousands of rows; a handful means someone uploaded the wrong thing.
MIN_ROWS = 10

# Share of rows that may carry a blank brand name before the file is refused.
MAX_BLANK_BRAND = 0.20


@dataclass
class ValidationReport:
    """The verdict, plus everything needed to explain it to an operator."""

    file: str
    kind: Optional[str] = None
    ok: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # Neutral observations about the data, kept separate from `warnings`.
    # Zero, negative and blank quantities are all valid states of a pharmacy
    # and are loaded unchanged — calling them "warnings" would suggest the
    # file needs fixing when it does not.
    notes: List[str] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)

    def fail(self, msg: str) -> "ValidationReport":
        self.errors.append(msg)
        self.ok = False
        return self

    def warn(self, msg: str) -> "ValidationReport":
        self.warnings.append(msg)
        return self

    def note(self, msg: str) -> "ValidationReport":
        self.notes.append(msg)
        return self

    def as_dict(self) -> Dict:
        return {
            "file": self.file, "kind": self.kind, "ok": self.ok,
            "errors": self.errors, "warnings": self.warnings,
            "notes": self.notes, "stats": self.stats,
        }

    @property
    def summary(self) -> str:
        if self.ok:
            return f"{self.file}: OK ({self.kind}, {self.stats.get('usable_rows', 0)} rows)"
        return f"{self.file}: REJECTED — " + "; ".join(self.errors)


def _read_frame(path: str, kind: str, stats: Optional[Dict] = None) -> pd.DataFrame:
    """Load the sheet the same way the real parser will.

    BOTH halves now DELEGATE to the loader's reader rather than mirroring it —
    validating a differently-read frame would pass files the loader then chokes
    on (or, as happened with CMHL's 2026-08-18 export, reject one the loader
    could have read).

    ``stats`` is the report's own stats dict, handed down so the sheet the
    reader chose is recorded even when the file is then REJECTED. That is the
    case where it matters most: "missing required column(s) … Found: CityCare
    Article Export" and "read sheet 'Summary' of 3" are the same incident, and
    only together do they say what to do about it.
    """

    if kind == "catalog":
        # Delegated, not mirrored. This function used to hold its own copy of
        # the header logic under a comment telling the next person to keep the
        # two in step; they drifted anyway. One reader now.
        from app.ingest import read_catalog_frame

        df = read_catalog_frame(path, stats)
    else:
        from app.ingest import read_inventory_frame

        df = read_inventory_frame(path, stats)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _validate_catalog(df: pd.DataFrame, r: ValidationReport) -> None:
    present = set(df.columns)
    missing = [c for c in REQUIRED_CATALOG_HEADERS if c not in present]
    if missing:
        # Name what WAS found — "missing Brand Name" plus a list of unrelated
        # headers usually means the banner height changed, not that the column
        # was dropped, and that is a different fix.
        r.fail(
            f"missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(sorted(present)[:12]) or 'nothing'}"
        )
        return

    thin = [c for c in EXPECTED_CATALOG_HEADERS if c not in present]
    if thin:
        r.warn(f"optional column(s) absent, those fields will be empty: {', '.join(thin)}")

    codes = df["Article Code"].astype(str).str.strip()
    brands = df["Brand Name"].astype(str).str.strip()
    valid_code = codes.apply(lambda v: bool(_CODE_RE.match(v)))
    blank_brand = brands.isin(["", "nan", "None"]) | brands.eq(codes)

    usable = int(valid_code.sum())
    dupes = int(codes[valid_code].duplicated().sum())
    blanks = int(blank_brand[valid_code].sum())

    r.stats.update({
        "rows_in_file": int(len(df)),
        "usable_rows": usable,
        "duplicate_codes": dupes,
        "blank_brand_names": blanks,
        "with_generic": int(df["Generic Name"].notna().sum()) if "Generic Name" in present else 0,
        "with_indication": int(df["Indication"].notna().sum()) if "Indication" in present else 0,
        "with_dosage": int(df["Dosage"].notna().sum()) if "Dosage" in present else 0,
    })

    if usable == 0:
        r.fail("no rows have a valid article code")
        return
    if usable < MIN_ROWS:
        r.fail(f"only {usable} usable row(s); expected at least {MIN_ROWS}")
    if blanks / usable > MAX_BLANK_BRAND:
        # The exact failure behind the field reports: rows load, but every
        # brand_name is the article code, so search_by_name can never match.
        r.fail(
            f"{blanks} of {usable} rows ({blanks/usable:.0%}) have no brand name — "
            f"products would be unsearchable by name"
        )
    if dupes:
        r.warn(f"{dupes} duplicate article code(s); the last occurrence wins")


def _validate_inventory(df: pd.DataFrame, r: ValidationReport) -> None:
    present = set(df.columns)
    missing = [c for c in REQUIRED_INVENTORY_HEADERS if c not in present]
    if missing:
        r.fail(
            f"missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(sorted(present)[:12]) or 'nothing'}"
        )
        return
    if "weighted_cost_price" not in present:
        r.warn("no weighted_cost_price column; prices will be empty")

    codes = df["article_code"].astype(str).str.strip()
    sites = df["site_code"].astype(str).str.strip()
    ok_rows = codes.apply(lambda v: bool(_CODE_RE.match(v))) & sites.ne("") & sites.ne("nan")

    usable = int(ok_rows.sum())
    qty = pd.to_numeric(df.loc[ok_rows, "stock_qty"], errors="coerce")
    price = (
        pd.to_numeric(df.loc[ok_rows, "weighted_cost_price"], errors="coerce")
        if "weighted_cost_price" in present
        else pd.Series(dtype="float64")
    )
    bad_site = int((~sites[ok_rows].apply(lambda v: bool(_SITE_RE.match(v)))).sum())

    # Zero, negative and blank are all LOADED — they are real states of a
    # pharmacy, not errors to filter out. A 0 means the branch is out; a
    # negative means its books disagree with its shelf, which the branch needs
    # to see rather than have quietly dropped; a blank means nobody counted,
    # which is stored as NULL (unknown) and must never become 0. They are
    # counted here purely so the operator can see the shape of what they are
    # about to load.
    r.stats.update({
        "rows_in_file": int(len(df)),
        "usable_rows": usable,
        "distinct_sites": int(sites[ok_rows].nunique()),
        "distinct_articles": int(codes[ok_rows].nunique()),
        "qty_zero": int((qty == 0).sum()),
        "qty_negative": int((qty < 0).sum()),
        "qty_blank_or_non_numeric": int(qty.isna().sum()),
        "price_zero": int((price == 0).sum()) if len(price) else 0,
        "price_blank": int(price.isna().sum()) if len(price) else 0,
    })

    if usable == 0:
        r.fail("no rows have both a valid article code and a site code")
        return
    if usable < MIN_ROWS:
        r.fail(f"only {usable} usable row(s); expected at least {MIN_ROWS}")
    if qty.isna().sum() / usable > 0.5:
        # Not "blanks are bad" — a majority of unreadable quantities means the
        # wrong column was mapped, not that the branch never counted.
        r.fail(
            f"{int(qty.isna().sum())} of {usable} rows have no readable stock "
            f"quantity — check that stock_qty is the right column"
        )
    if (qty < 0).any():
        # Informational, never blocking. Loaded as-is.
        r.note(f"{int((qty < 0).sum())} row(s) have a negative stock quantity (loaded as-is)")
    if (qty == 0).any():
        r.note(f"{int((qty == 0).sum())} row(s) have zero stock (loaded as-is)")
    if qty.isna().any():
        r.note(
            f"{int(qty.isna().sum())} row(s) have no stock figure — stored as "
            f"unknown (NULL), not as zero"
        )
    if bad_site:
        # Not fatal: _site_clause tolerates prefix/suffix forms. Worth flagging
        # because an unexpected shape usually means the wrong column was mapped.
        r.warn(f"{bad_site} row(s) have an unusual site_code shape (expected like 20005-CCYK)")


def validate_file(path: str) -> ValidationReport:
    """Check one upload. Never touches the database."""

    name = Path(path).name
    r = ValidationReport(file=name)

    if not name.lower().endswith((".xlsx", ".csv")):
        return r.fail("unsupported file type — only .xlsx and .csv are accepted")

    kind = detect_kind(name)
    if kind is None:
        return r.fail(
            "cannot tell what this file is from its name — an article export must "
            "contain 'article', a stock export 'balance', 'stock' or 'inventory'"
        )
    r.kind = kind

    try:
        df = _read_frame(path, kind, r.stats)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejection
        return r.fail(f"could not read the file: {exc}")

    if r.stats.get("sheet_count", 1) > 1:
        # A NOTE, not a warning: a workbook with several tabs is not a defect,
        # and the file may well be perfectly correct. It is here because notes
        # are rendered verbatim into the file's history (see
        # `watcher._checked_line`), so the one person who can tell whether
        # "Summary" or "Articles" was the right tab gets to read that we took
        # the first one. A file that shrinks the table on top of this is already
        # refused by check_shrink; this is what explains WHY it shrank.
        r.note(
            f"this workbook has {r.stats['sheet_count']} sheets "
            f"({', '.join(r.stats.get('sheet_names', []))}) — read the first, "
            f"'{r.stats.get('sheet_read')}'. Check that is the sheet you meant."
        )

    if df.empty:
        return r.fail("the file contains no rows")

    r.ok = True  # provisional; the checks below flip it back on any failure
    if kind == "catalog":
        _validate_catalog(df, r)
    else:
        _validate_inventory(df, r)
    r.ok = not r.errors
    return r


async def check_shrink(report: ValidationReport, allow_shrink: bool = False) -> ValidationReport:
    """Refuse a valid file that would delete most of the existing table.

    Separate from ``validate_file`` because it is the only check that needs the
    database, and because it is a policy question rather than a correctness one:
    the file is fine, it is just much smaller than what it is about to replace.
    """

    if not report.ok or allow_shrink:
        return report

    from app.db import q

    table = "catalog" if report.kind == "catalog" else "inventory"
    try:
        rows = await q(f"SELECT count(*) AS n FROM {table}")
        current = (rows[0]["n"] if rows else 0) or 0
    except Exception as exc:  # noqa: BLE001 - cannot compare, so do not block
        report.warn(f"could not compare against existing data: {exc}")
        return report

    incoming = report.stats.get("usable_rows", 0)
    report.stats["existing_rows"] = current
    if current and incoming < current * (1 - MAX_SHRINK):
        report.fail(
            f"this file would replace {current} rows with {incoming} "
            f"({1 - incoming / current:.0%} of the data would be deleted). "
            f"If that is intended, re-upload with allow_shrink=true"
        )
    return report
