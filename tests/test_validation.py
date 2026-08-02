"""Upload validation: reject before replacing, never after.

Both loaders are destructive — an article export replaces the whole catalog, a
balance export truncates and reloads inventory. So a file that gets through
validation and turns out to be wrong does not fail an import, it deletes data.
These tests pin what must be refused and, just as importantly, what must NOT be
(a validator that rejects good files is an outage of its own).

The shapes here are the real ones: the header-shifted export that produced a
catalog of nameless codes on the customer host, and the 40-row file that took
the local catalog from 5,292 rows to 40 during the 2026-08-02 live run.
"""

from pathlib import Path

import pandas as pd
import pytest

from app.validation import (
    MAX_BLANK_BRAND,
    MIN_ROWS,
    ValidationReport,
    validate_file,
)

REAL_EXPORT = Path("data/articles-export 1.xlsx")


def _catalog_csv(rows=20, brand=True, blank_from=None):
    head = "Article Code,Brand Name,Generic Name,Indication\n" if brand \
        else "Article Code,Generic Name,Indication\n"
    out = [head]
    for i in range(rows):
        code = f"10000000{i:05d}"
        name = "" if (blank_from is not None and i >= blank_from) else f"BRAND {i}"
        out.append(f"{code},{name},Testogen,fever\n" if brand
                   else f"{code},Testogen,fever\n")
    return "".join(out)


def _inventory_csv(rows=20, qty="10"):
    out = ["id,site_code,article_code,stock_qty,weighted_cost_price\n"]
    for i in range(rows):
        out.append(f"{i},20015-CCGV,10000000{i:05d},{qty},1500\n")
    return "".join(out)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---- format ---------------------------------------------------------------


class TestFormat:
    def test_unsupported_extension_is_rejected(self, tmp_path):
        r = validate_file(_write(tmp_path, "articles-export.txt", "whatever"))
        assert not r.ok
        assert "only .xlsx and .csv" in r.errors[0]

    def test_unclassifiable_name_is_rejected(self, tmp_path):
        """No keyword in the name means no schema to check it against."""

        r = validate_file(_write(tmp_path, "pharmacy_data_july.csv", _catalog_csv()))
        assert not r.ok
        assert r.kind is None
        assert "cannot tell what this file is" in r.errors[0]

    def test_unreadable_file_is_rejected_not_crashed(self, tmp_path):
        p = tmp_path / "articles-export.xlsx"
        p.write_bytes(b"this is not a spreadsheet")
        r = validate_file(str(p))
        assert not r.ok
        assert "could not read the file" in r.errors[0]

    def test_empty_file_is_rejected(self, tmp_path):
        r = validate_file(_write(tmp_path, "articles-export.csv", "Article Code,Brand Name\n"))
        assert not r.ok


# ---- columns --------------------------------------------------------------


class TestColumns:
    def test_missing_brand_name_column_is_rejected(self, tmp_path):
        """The exact shape behind the 14 field tickets.

        Without this column every row loads as brand_name = article_code, the
        catalog looks healthy by row count, and search_by_name can never match.
        """

        r = validate_file(_write(tmp_path, "articles-export.csv",
                                 _catalog_csv(brand=False)))
        assert not r.ok
        assert "Brand Name" in r.errors[0]

    def test_rejection_names_the_columns_it_did_find(self, tmp_path):
        """A shifted banner and a renamed column need different fixes."""

        r = validate_file(_write(tmp_path, "articles-export.csv",
                                 _catalog_csv(brand=False)))
        assert "Found:" in r.errors[0]
        assert "Article Code" in r.errors[0]

    def test_missing_optional_columns_only_warn(self, tmp_path):
        text = "Article Code,Brand Name\n" + "".join(
            f"10000000{i:05d},BRAND {i}\n" for i in range(20))
        r = validate_file(_write(tmp_path, "articles-export.csv", text))
        assert r.ok
        assert any("Dosage" in w for w in r.warnings)

    def test_inventory_missing_site_code_is_rejected(self, tmp_path):
        text = "id,article_code,stock_qty\n" + "".join(
            f"{i},10000000{i:05d},5\n" for i in range(20))
        r = validate_file(_write(tmp_path, "balance_stock.csv", text))
        assert not r.ok
        assert "site_code" in r.errors[0]


# ---- data -----------------------------------------------------------------


class TestData:
    def test_mostly_blank_brand_names_are_rejected(self, tmp_path):
        """Right columns, useless contents — still unsearchable by name."""

        r = validate_file(_write(tmp_path, "articles-export.csv",
                                 _catalog_csv(rows=20, blank_from=4)))
        assert not r.ok
        assert "no brand name" in " ".join(r.errors)

    def test_a_few_blank_brands_are_tolerated(self, tmp_path):
        """Real exports have gaps; only a majority is disqualifying."""

        r = validate_file(_write(tmp_path, "articles-export.csv",
                                 _catalog_csv(rows=20, blank_from=18)))
        assert r.ok
        assert r.stats["blank_brand_names"] == 2
        assert r.stats["blank_brand_names"] / r.stats["usable_rows"] <= MAX_BLANK_BRAND

    def test_too_few_rows_is_rejected(self, tmp_path):
        r = validate_file(_write(tmp_path, "articles-export.csv", _catalog_csv(rows=3)))
        assert not r.ok
        assert f"at least {MIN_ROWS}" in " ".join(r.errors)

    def test_non_numeric_quantities_are_rejected(self, tmp_path):
        r = validate_file(_write(tmp_path, "balance_stock.csv",
                                 _inventory_csv(qty="n/a")))
        assert not r.ok
        assert "non-numeric stock quantity" in " ".join(r.errors)

    def test_negative_quantity_warns_but_loads(self, tmp_path):
        r = validate_file(_write(tmp_path, "balance_stock.csv", _inventory_csv(qty="-5")))
        assert r.ok
        assert any("negative" in w for w in r.warnings)

    def test_duplicate_codes_warn_but_load(self, tmp_path):
        text = "Article Code,Brand Name\n" + "".join(
            f"1000000000001,BRAND {i}\n" for i in range(20))
        r = validate_file(_write(tmp_path, "articles-export.csv", text))
        assert r.ok
        assert r.stats["duplicate_codes"] == 19


# ---- happy path -----------------------------------------------------------


class TestAccepts:
    def test_a_good_catalog_passes(self, tmp_path):
        r = validate_file(_write(tmp_path, "articles-export.csv", _catalog_csv()))
        assert r.ok and r.errors == []
        assert r.kind == "catalog"
        assert r.stats["usable_rows"] == 20

    def test_a_good_inventory_passes(self, tmp_path):
        r = validate_file(_write(tmp_path, "balance_stock.csv", _inventory_csv()))
        assert r.ok and r.errors == []
        assert r.kind == "inventory"
        assert r.stats["distinct_sites"] == 1

    @pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not in data/")
    def test_the_real_customer_export_passes(self):
        """The control. The shipped file must never be refused.

        A validator that rejects the genuine monthly export would be worse than
        no validator: it would block every legitimate update while the broken
        state it was written to catch stayed in place.
        """

        r = validate_file(str(REAL_EXPORT))
        assert r.ok, r.summary
        assert r.stats["usable_rows"] > 4000
        assert r.stats["blank_brand_names"] == 0

    @pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not in data/")
    def test_the_real_export_with_a_shifted_banner_is_refused(self, tmp_path):
        """Same data, header two rows off — the customer-host shape."""

        src = pd.read_excel(REAL_EXPORT, skiprows=4).head(60)
        broken = tmp_path / "articles-export-broken.xlsx"
        with pd.ExcelWriter(broken) as xl:
            pd.DataFrame([[""]] * 6).to_excel(xl, index=False, header=False, startrow=0)
            src.to_excel(xl, index=False, startrow=6)

        r = validate_file(str(broken))
        assert not r.ok
        assert "Brand Name" in " ".join(r.errors)


# ---- report shape ---------------------------------------------------------


class TestReport:
    def test_summary_reads_as_an_operator_message(self, tmp_path):
        r = validate_file(_write(tmp_path, "articles-export.csv",
                                 _catalog_csv(brand=False)))
        assert r.summary.startswith("articles-export.csv: REJECTED — ")

    def test_as_dict_is_json_safe(self, tmp_path):
        import json

        r = validate_file(_write(tmp_path, "balance_stock.csv", _inventory_csv()))
        json.dumps(r.as_dict())  # must not raise on numpy ints

    def test_fail_flips_ok_off(self):
        r = ValidationReport(file="x.csv", ok=True)
        r.fail("nope")
        assert not r.ok and r.errors == ["nope"]
