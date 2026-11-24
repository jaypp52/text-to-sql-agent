import datetime as dt
from decimal import Decimal

from eval.compare import compare, is_abstention


def test_identical():
    assert compare([(1, "a")], [(1, "a")]).ok


def test_row_order_ignored_unless_order_matters():
    g = [(1, "a"), (2, "b")]
    p = [(2, "b"), (1, "a")]
    assert compare(g, p).ok
    assert not compare(g, p, order_matters=True).ok
    assert compare(g, g, order_matters=True).ok


def test_float_tolerance_and_decimal():
    assert compare([(0.1 + 0.2,)], [(0.3,)]).ok
    assert compare([(Decimal("4.24"),)], [(4.24,)]).ok
    assert compare([(4.24,)], [(4.2400001,)]).ok  # 2.4e-8 relative: within 1e-6
    assert not compare([(4.24,)], [(4.2401,)]).ok  # 2.4e-5 relative: outside


def test_rounding_is_a_mismatch():
    assert not compare([(4.2391,)], [(4.24,)]).ok


def test_nulls_equal():
    assert compare([(None, 1)], [(None, 1)]).ok
    assert not compare([(None,)], [(0,)]).ok


def test_multiset_counts_duplicates():
    assert not compare([(1,), (1,), (2,)], [(1,), (2,), (2,)]).ok
    assert compare([(1,), (1,), (2,)], [(2,), (1,), (1,)]).ok


def test_column_names_ignored_and_extra_columns_allowed():
    g = [("TX-100", 4.78), ("TX-200", 3.09)]
    p = [(3.09, "TX-200", 166), (4.78, "TX-100", 228)]  # reordered + extra column
    m = compare(g, p)
    assert m.ok and m.extra_columns


def test_fewer_columns_is_wrong():
    assert not compare([("a", 1)], [("a",)]).ok


def test_row_count_differs():
    m = compare([(1,)], [(1,), (2,)])
    assert not m.ok and "row count" in m.reason


def test_types_normalised():
    assert compare([(dt.date(2026, 1, 5), True)], [("2026-01-05", 1)]).ok
    assert compare([(dt.datetime(2026, 1, 5, 12, 0),)], [("2026-01-05T12:00:00",)]).ok
    assert compare([(5,)], [(5.0,)]).ok


def test_empty_results():
    assert compare([], []).ok


def test_abstention_detection():
    assert is_abstention("", "This cannot be answered: the warehouse has no driver data.")
    assert is_abstention("", "The warehouse does not contain fuel consumption.")
    assert not is_abstention("SELECT 1", "cannot be answered")
    assert not is_abstention("", "There were 12 crashes.")
