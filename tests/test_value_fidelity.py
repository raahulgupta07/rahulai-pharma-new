"""Every quantity in the file must reach the database unchanged.

Zero, negative and blank are all real states of a pharmacy, and each means
something different:

* **0**   — the branch is out. A true, dispensable fact.
* **-3**  — the branch's books disagree with its shelf. A data problem the
            branch needs to SEE; silently dropping the row hides it forever.
* **blank** — nobody counted. Stored as NULL and meaning UNKNOWN, which is not
            the same as zero. A pharmacist reading "0" does not dispense; one
            reading "unknown" goes and looks.

The failure mode these tests exist to prevent is a well-meaning filter or a
``COALESCE(x, 0)`` turning one of those into another. That already happened
twice in this repo — ``admin.catalog_one`` and ``tools.summarize_article`` both
summed NULL as zero — so it is worth pinning at the parse layer where it starts.
"""

import math

import pytest

from app.ingest import parse_inventory

# parse_inventory returns tuples in _INVENTORY_FIELDS order:
# (article_code, site_code, site_name, stock_qty, price, uom)
QTY = 3
PRICE = 4


def _write_csv(tmp_path, rows):
    p = tmp_path / "balance_stock.csv"
    head = "id,site_code,article_code,stock_qty,weighted_cost_price\n"
    p.write_text(head + "".join(rows), encoding="utf-8")
    return str(p)


def _by_code(records):
    return {r[0]: r for r in records}


class TestQuantityFidelity:
    def test_zero_negative_and_positive_all_survive(self, tmp_path):
        path = _write_csv(tmp_path, [
            "1,20015-CCGV,1000000000001,0,1500\n",
            "2,20015-CCGV,1000000000002,-20,1500\n",
            "3,20015-CCGV,1000000000003,6533,1500\n",
        ])
        got = _by_code(parse_inventory(path))

        assert got["1000000000001"][QTY] == 0, "zero must not become NULL"
        assert got["1000000000002"][QTY] == -20, "negative must not be clamped or dropped"
        assert got["1000000000003"][QTY] == 6533

    def test_no_row_is_dropped_for_its_quantity(self, tmp_path):
        """A filter that skipped odd values would lose the row entirely."""

        path = _write_csv(tmp_path, [
            f"{i},20015-CCGV,100000000000{i},{q},1500\n"
            for i, q in enumerate([0, -1, -999, 1, 100000])
        ])
        assert len(parse_inventory(path)) == 5

    def test_blank_quantity_becomes_unknown_not_zero(self, tmp_path):
        """The distinction the whole NULLS LAST invariant rests on."""

        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,,1500\n"])
        rec = parse_inventory(path)[0]

        assert rec[QTY] is None
        assert rec[QTY] != 0

    def test_non_numeric_quantity_becomes_unknown_not_zero(self, tmp_path):
        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,n/a,1500\n"])
        assert parse_inventory(path)[0][QTY] is None

    def test_decimal_quantity_truncates_rather_than_failing(self, tmp_path):
        """Excel writes 12.0 for an integer column; that is still 12."""

        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,12.0,1500\n"])
        assert parse_inventory(path)[0][QTY] == 12


class TestPriceFidelity:
    def test_zero_price_is_kept(self, tmp_path):
        """A free or promotional line is a real price, not a missing one."""

        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,5,0\n"])
        assert parse_inventory(path)[0][PRICE] == 0.0

    def test_negative_price_is_kept(self, tmp_path):
        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,5,-250\n"])
        assert parse_inventory(path)[0][PRICE] == -250.0

    def test_blank_price_becomes_unknown_not_zero(self, tmp_path):
        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,5,\n"])
        rec = parse_inventory(path)[0]
        assert rec[PRICE] is None
        assert rec[PRICE] != 0.0

    def test_nan_price_becomes_none(self, tmp_path):
        path = _write_csv(tmp_path, ["1,20015-CCGV,1000000000001,5,NaN\n"])
        got = parse_inventory(path)[0][PRICE]
        assert got is None or not math.isnan(got)


class TestValidationAcceptsThemAll:
    """Validation must REPORT these values, never reject them."""

    def test_a_file_of_zeros_and_negatives_still_passes(self, tmp_path):
        from app.validation import validate_file

        rows = [
            f"{i},20015-CCGV,100000000{i:04d},{q},1500\n"
            for i, q in enumerate([0, -5, 0, -1, 3, 0, 12, -20, 7, 0, 4, 9])
        ]
        report = validate_file(_write_csv(tmp_path, rows))

        assert report.ok, report.summary
        assert report.errors == []
        assert report.stats["qty_zero"] == 4
        assert report.stats["qty_negative"] == 3

    def test_those_values_are_notes_not_warnings(self, tmp_path):
        """They are facts about the data, not defects in the file."""

        from app.validation import validate_file

        rows = [f"{i},20015-CCGV,100000000{i:04d},{-1 if i % 2 else 0},1500\n"
                for i in range(12)]
        report = validate_file(_write_csv(tmp_path, rows))

        assert report.ok
        joined = " ".join(report.notes)
        assert "negative" in joined and "loaded as-is" in joined
        assert not any("negative" in w for w in report.warnings)

    def test_blank_quantities_are_reported_as_unknown(self, tmp_path):
        from app.validation import validate_file

        rows = [f"{i},20015-CCGV,100000000{i:04d},{'' if i < 3 else 5},1500\n"
                for i in range(12)]
        report = validate_file(_write_csv(tmp_path, rows))

        assert report.ok
        assert report.stats["qty_blank_or_non_numeric"] == 3
        assert any("unknown (NULL), not as zero" in n for n in report.notes)

    def test_all_quantities_unreadable_is_still_a_rejection(self, tmp_path):
        """Storing every value does not mean accepting a mis-mapped column."""

        from app.validation import validate_file

        rows = [f"{i},20015-CCGV,100000000{i:04d},n/a,1500\n" for i in range(12)]
        report = validate_file(_write_csv(tmp_path, rows))

        assert not report.ok
        assert "no readable stock quantity" in " ".join(report.errors)
