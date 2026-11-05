"""Hidden tests for numeric_range equality and hashing above sys.maxsize.

Part 1: the upstream PR's regression tests.
Part 2: extra behavioural tests.
Part 3: a copy of the base NumericRangeTests class (regression guard).
"""

from __future__ import annotations

import sys

from collections.abc import Hashable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import count
from pickle import dumps, loads
from unittest import TestCase

import more_itertools as mi

STOP = 10**30


class AboveMaxsizeUpstreamTests(TestCase):
    def test_eq_above_maxsize(self):
        stop = 10**30
        cases = [
            ((0, stop, 1), (0, stop, 1), True),
            ((0, stop, 2), (0, stop - 1, 2), True),
            ((stop, 0, -2), (stop, 1, -2), True),
            ((0, stop, 1), (0, stop + 1, 1), False),
            ((0, stop, 1), (1, stop + 1, 1), False),
            ((0, stop, 1), (0, stop * 2, 2), False),
        ]
        for args1, args2, expected in cases:
            with self.subTest(args1=args1, args2=args2):
                range1 = mi.numeric_range(*args1)
                range2 = mi.numeric_range(*args2)
                self.assertEqual(range(*args1) == range(*args2), expected)
                self.assertEqual(range1 == range2, expected)

    def test_hash_above_maxsize(self):
        stop = 10**30
        for args in [(0, stop, 1), (0, stop, 2), (stop, 0, -2)]:
            with self.subTest(args=args):
                r = mi.numeric_range(*args)
                self.assertEqual(hash(r), hash(range(*args)))
                self.assertEqual(
                    {r: 'value'}[mi.numeric_range(*args)], 'value'
                )
                self.assertRaises(OverflowError, len, r)


class AboveMaxsizeBehaviourTests(TestCase):
    def test_equal_content_different_stop(self):
        a = mi.numeric_range(0, STOP, 2)
        b = mi.numeric_range(0, STOP - 1, 2)
        self.assertTrue(a == b)
        self.assertFalse(a != b)
        self.assertEqual(hash(a), hash(b))

    def test_equal_content_different_stop_negative_step(self):
        a = mi.numeric_range(STOP, 0, -3)
        b = mi.numeric_range(STOP, -1, -3)
        self.assertTrue(a == b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(hash(a), hash(range(STOP, 0, -3)))

    def test_unequal_above_maxsize(self):
        base = mi.numeric_range(0, STOP)
        others = [
            mi.numeric_range(0, STOP + 1),
            mi.numeric_range(1, STOP + 1),
            mi.numeric_range(0, STOP * 2, 2),
            mi.numeric_range(0, STOP, 3),
            mi.numeric_range(STOP, 0, -1),
        ]
        for other in others:
            with self.subTest(other=other):
                self.assertFalse(base == other)
                self.assertTrue(base != other)
                self.assertFalse(other == base)

    def test_same_length_different_start(self):
        a = mi.numeric_range(0, STOP, 7)
        b = mi.numeric_range(1, STOP + 1, 7)
        self.assertNotEqual(a, b)

    def test_same_length_different_step(self):
        a = mi.numeric_range(0, STOP, 2)
        b = mi.numeric_range(0, STOP, 4)
        self.assertNotEqual(a, b)

    def test_identity_and_copy_equal(self):
        a = mi.numeric_range(0, STOP, 5)
        self.assertTrue(a == a)
        self.assertEqual(a, loads(dumps(a)))
        self.assertEqual(hash(a), hash(loads(dumps(a))))

    def test_hash_matches_builtin_range(self):
        for args in [
            (0, STOP),
            (5, STOP, 3),
            (STOP, -STOP, -7),
            (-STOP, STOP, 11),
        ]:
            with self.subTest(args=args):
                self.assertEqual(
                    hash(mi.numeric_range(*args)), hash(range(*args))
                )

    def test_hashable_dict_and_set(self):
        a = mi.numeric_range(0, STOP, 2)
        b = mi.numeric_range(0, STOP - 1, 2)
        self.assertIsInstance(a, Hashable)
        self.assertEqual(len({a, b}), 1)
        self.assertEqual({a: 1}[b], 1)

    def test_mixed_numeric_types_above_maxsize(self):
        a = mi.numeric_range(0, STOP, 1)
        b = mi.numeric_range(Fraction(0), Fraction(STOP), Fraction(1))
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_empty_ranges_equal(self):
        empties = [
            mi.numeric_range(0),
            mi.numeric_range(5, 5),
            mi.numeric_range(STOP, 0),
            mi.numeric_range(0, STOP, -1),
            mi.numeric_range(STOP, STOP, 3),
        ]
        for left in empties:
            for right in empties:
                with self.subTest(left=left, right=right):
                    self.assertEqual(left, right)
                    self.assertEqual(hash(left), hash(right))
        self.assertEqual(hash(empties[0]), hash(range(0)))

    def test_empty_not_equal_to_huge(self):
        self.assertNotEqual(mi.numeric_range(0), mi.numeric_range(0, STOP))
        self.assertNotEqual(mi.numeric_range(0, STOP), mi.numeric_range(0))

    def test_single_element_ranges_ignore_step(self):
        a = mi.numeric_range(STOP, STOP + 1, 1)
        b = mi.numeric_range(STOP, STOP + 10, 10)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_non_numeric_range_comparison(self):
        r = mi.numeric_range(0, STOP)
        for other in [range(0, 10), list(range(3)), 5, None, 'abc']:
            with self.subTest(other=other):
                self.assertFalse(r == other)
                self.assertTrue(r != other)
                # exactly False, as built-in range returns, not merely falsy
                self.assertIs(r.__eq__(other), False)
        self.assertFalse(mi.numeric_range(0, STOP) == range(0, STOP))

    def test_len_still_overflows(self):
        for args in [(0, STOP), (STOP, 0, -1), (0, STOP, 2)]:
            with self.subTest(args=args):
                self.assertRaises(OverflowError, len, mi.numeric_range(*args))

    def test_len_below_maxsize_unchanged(self):
        self.assertEqual(len(mi.numeric_range(0, 10)), 10)
        self.assertEqual(len(mi.numeric_range(10, 0, -2)), 5)
        self.assertEqual(len(mi.numeric_range(0)), 0)
        self.assertEqual(len(mi.numeric_range(0, sys.maxsize)), sys.maxsize)

    def test_huge_range_other_apis_still_work(self):
        r = mi.numeric_range(0, STOP, 3)
        self.assertEqual(r[0], 0)
        self.assertEqual(r[1], 3)
        self.assertEqual(r[-1], 3 * ((STOP - 1) // 3))
        self.assertIn(3 * 10**20, r)
        self.assertNotIn(3 * 10**20 + 1, r)

    def test_below_maxsize_eq_hash_unchanged(self):
        a = mi.numeric_range(0, 10, 2)
        b = mi.numeric_range(0, 9, 2)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(hash(a), hash(range(0, 10, 2)))
        self.assertNotEqual(a, mi.numeric_range(0, 10, 3))


# --- Part 3: copy of the base NumericRangeTests (regression guard) ---


class NumericRangeTests(TestCase):
    def test_basic(self):
        for args, expected in [
            ((4,), [0, 1, 2, 3]),
            ((4.0,), [0.0, 1.0, 2.0, 3.0]),
            ((1.0, 4), [1.0, 2.0, 3.0]),
            ((1, 4.0), [1.0, 2.0, 3.0]),
            ((1.0, 5), [1.0, 2.0, 3.0, 4.0]),
            ((0, 20, 5), [0, 5, 10, 15]),
            ((0, 20, 5.0), [0.0, 5.0, 10.0, 15.0]),
            ((0, 10, 3), [0, 3, 6, 9]),
            ((0, 10, 3.0), [0.0, 3.0, 6.0, 9.0]),
            ((0, -5, -1), [0, -1, -2, -3, -4]),
            ((0.0, -5, -1), [0.0, -1.0, -2.0, -3.0, -4.0]),
            ((1, 2, Fraction(1, 2)), [Fraction(1, 1), Fraction(3, 2)]),
            ((0,), []),
            ((0.0,), []),
            ((1, 0), []),
            ((1.0, 0.0), []),
            ((0.1, 0.30000000000000001, 0.2), [0.1]),  # IEEE 754 !
            (
                (
                    Decimal("0.1"),
                    Decimal("0.30000000000000001"),
                    Decimal("0.2"),
                ),
                [Decimal("0.1"), Decimal("0.3")],
            ),  # okay with Decimal
            (
                (
                    Fraction(1, 10),
                    Fraction(30000000000000001, 100000000000000000),
                    Fraction(2, 10),
                ),
                [Fraction(1, 10), Fraction(3, 10)],
            ),  # okay with Fraction
            ((Fraction(2, 1),), [Fraction(0, 1), Fraction(1, 1)]),
            ((Decimal('2.0'),), [Decimal('0.0'), Decimal('1.0')]),
            (
                (
                    datetime(2019, 3, 29, 12, 34, 56),
                    datetime(2019, 3, 29, 12, 37, 55),
                    timedelta(minutes=1),
                ),
                [
                    datetime(2019, 3, 29, 12, 34, 56),
                    datetime(2019, 3, 29, 12, 35, 56),
                    datetime(2019, 3, 29, 12, 36, 56),
                ],
            ),
        ]:
            actual = list(mi.numeric_range(*args))
            self.assertEqual(expected, actual)
            self.assertTrue(
                all(type(a) is type(e) for a, e in zip(actual, expected))
            )

    def test_arg_count(self):
        for args, message in [
            ((), 'numeric_range expected at least 1 argument, got 0'),
            (
                (0, 1, 2, 3),
                'numeric_range expected at most 3 arguments, got 4',
            ),
        ]:
            with self.assertRaisesRegex(TypeError, message):
                mi.numeric_range(*args)

    def test_zero_step(self):
        for args in [
            (1, 2, 0),
            (
                datetime(2019, 3, 29, 12, 34, 56),
                datetime(2019, 3, 29, 12, 37, 55),
                timedelta(minutes=0),
            ),
            (1.0, 2.0, 0.0),
            (Decimal("1.0"), Decimal("2.0"), Decimal("0.0")),
            (Fraction(2, 2), Fraction(4, 2), Fraction(0, 2)),
        ]:
            with self.assertRaises(ValueError):
                list(mi.numeric_range(*args))

    def test_bool(self):
        for args, expected in [
            ((1.0, 3.0, 1.5), True),
            ((1.0, 2.0, 1.5), True),
            ((1.0, 1.0, 1.5), False),
            ((1.0, 0.0, 1.5), False),
            ((3.0, 1.0, -1.5), True),
            ((2.0, 1.0, -1.5), True),
            ((1.0, 1.0, -1.5), False),
            ((0.0, 1.0, -1.5), False),
            ((Decimal("1.0"), Decimal("2.0"), Decimal("1.5")), True),
            ((Decimal("1.0"), Decimal("0.0"), Decimal("1.5")), False),
            ((Fraction(2, 2), Fraction(4, 2), Fraction(3, 2)), True),
            ((Fraction(2, 2), Fraction(0, 2), Fraction(3, 2)), False),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=1),
                ),
                True,
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 28),
                    timedelta(hours=1),
                ),
                False,
            ),
        ]:
            self.assertEqual(expected, bool(mi.numeric_range(*args)))

    def test_contains(self):
        for args, expected_in, expected_not_in in [
            ((10,), range(10), (0.5,)),
            ((1.0, 9.9, 1.5), (1.0, 2.5, 4.0, 5.5, 7.0, 8.5), (0.9,)),
            ((9.0, 1.0, -1.5), (1.5, 3.0, 4.5, 6.0, 7.5, 9.0), (0.0, 0.9)),
            (
                (Decimal("1.0"), Decimal("9.9"), Decimal("1.5")),
                (
                    Decimal("1.0"),
                    Decimal("2.5"),
                    Decimal("4.0"),
                    Decimal("5.5"),
                    Decimal("7.0"),
                    Decimal("8.5"),
                ),
                (Decimal("0.9"),),
            ),
            (
                (Fraction(0, 1), Fraction(5, 1), Fraction(1, 2)),
                (Fraction(0, 1), Fraction(1, 2), Fraction(9, 2)),
                (Fraction(10, 2),),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=1),
                ),
                (datetime(2019, 3, 29, 15),),
                (datetime(2019, 3, 29, 15, 30),),
            ),
        ]:
            r = mi.numeric_range(*args)
            for v in expected_in:
                self.assertTrue(v in r)
                self.assertFalse(v not in r)

            for v in expected_not_in:
                self.assertFalse(v in r)
                self.assertTrue(v not in r)

    def test_contains_items_with_inexact_step(self):
        # The items are produced by repeated multiplication, so with a step
        # that has no exact float representation they do not land on an exact
        # multiple of it. Every item must still be a member, and index() must
        # locate it where __getitem__ puts it.
        for args in [
            (0.0, 1.0, 0.1),
            (0.0, 3.0, 0.7),
            (1.0, 0.0, -0.1),
            (2.1, 5.1, 0.5),
            (-1.0, 1.0, 0.25),
        ]:
            r = mi.numeric_range(*args)
            for i, v in enumerate(r):
                self.assertIn(v, r)
                self.assertEqual(i, r.index(v))
                self.assertEqual(v, r[i])
                self.assertEqual(1, r.count(v))

    def test_contains_non_items_with_inexact_step(self):
        # Values the range does not produce stay out of it.
        r = mi.numeric_range(0.0, 1.0, 0.1)
        items = set(r)
        for i in range(1000):
            v = i / 1000
            if v not in items:
                self.assertNotIn(v, r)
                self.assertRaises(ValueError, r.index, v)

    def test_eq(self):
        # numeric_range objects are equal to themselves
        range_1 = mi.numeric_range(2, 4, 1)
        self.assertEqual(range_1, range_1)

        # ...and to distinct objects covering the same values
        self.assertEqual(range_1, mi.numeric_range(2, 4, 1))

        # numeric_range objects are not equal other types of objects
        self.assertNotEqual(mi.numeric_range(7.0), 1)
        self.assertNotEqual(mi.numeric_range(7.0), "abc")

        for args1, args2 in [
            ((0, 5, 2), (0, 6, 2)),
            ((1.0, 9.9, 1.5), (1.0, 8.6, 1.5)),
            ((8.5, 0.0, -1.5), (8.5, 0.7, -1.5)),
            ((7.0, 0.0, 1.0), (17.0, 7.0, 0.5)),
            (
                (Decimal("1.0"), Decimal("9.9"), Decimal("1.5")),
                (Decimal("1.0"), Decimal("8.6"), Decimal("1.5")),
            ),
            (
                (Fraction(1, 1), Fraction(10, 1), Fraction(3, 2)),
                (Fraction(1, 1), Fraction(9, 1), Fraction(3, 2)),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30, 1),
                    timedelta(hours=10),
                ),
            ),
            ((2, 3, 1), (2, 3, 5)),
        ]:
            range1 = mi.numeric_range(*args1)
            range2 = mi.numeric_range(*args2)
            self.assertEqual(range1, range2)
            self.assertEqual(hash(range1), hash(range2))

        for args1, args2 in [
            ((0, 5, 2), (0, 7, 2)),
            ((1.0, 9.9, 1.5), (1.2, 9.9, 1.5)),
            ((1.0, 9.9, 1.5), (1.0, 10.3, 1.5)),
            ((1.0, 9.9, 1.5), (1.0, 9.9, 1.4)),
            ((8.5, 0.0, -1.5), (8.4, 0.0, -1.5)),
            ((8.5, 0.0, -1.5), (8.5, -0.7, -1.5)),
            ((8.5, 0.0, -1.5), (8.5, 0.0, -1.4)),
            ((0.0, 7.0, 1.0), (7.0, 0.0, 1.0)),
            (
                (Decimal("1.0"), Decimal("10.0"), Decimal("1.5")),
                (Decimal("1.0"), Decimal("10.5"), Decimal("1.5")),
            ),
            (
                (Fraction(1, 1), Fraction(10, 1), Fraction(3, 2)),
                (Fraction(1, 1), Fraction(21, 2), Fraction(3, 2)),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30, 15),
                    timedelta(hours=10),
                ),
            ),
        ]:
            range1 = mi.numeric_range(*args1)
            range2 = mi.numeric_range(*args2)
            self.assertNotEqual(range1, range2)
            self.assertNotEqual(hash(range1), hash(range2))

    def test_get_item_by_index(self):
        for args, index, expected in [
            ((1, 6), 2, 3),
            ((1.0, 6.0, 1.5), 0, 1.0),
            ((1.0, 6.0, 1.5), 1, 2.5),
            ((1.0, 6.0, 1.5), 2, 4.0),
            ((1.0, 6.0, 1.5), 3, 5.5),
            ((1.0, 6.0, 1.5), -1, 5.5),
            ((1.0, 6.0, 1.5), -2, 4.0),
            (
                (Decimal("1.0"), Decimal("9.0"), Decimal("1.5")),
                -1,
                Decimal("8.5"),
            ),
            (
                (Fraction(1, 1), Fraction(10, 1), Fraction(3, 2)),
                2,
                Fraction(4, 1),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                1,
                datetime(2019, 3, 29, 10),
            ),
        ]:
            self.assertEqual(expected, mi.numeric_range(*args)[index])

        for args, index in [
            ((1.0, 6.0, 1.5), 4),
            ((1.0, 6.0, 1.5), -5),
            ((6.0, 1.0, 1.5), 0),
            ((6.0, 1.0, 1.5), -1),
            ((Decimal("1.0"), Decimal("9.0"), Decimal("-1.5")), -1),
            ((Fraction(1, 1), Fraction(2, 1), Fraction(3, 2)), 2),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                8,
            ),
        ]:
            with self.assertRaises(IndexError):
                mi.numeric_range(*args)[index]

    def test_get_item_by_slice(self):
        for args, sl, expected_args in [
            ((1.0, 9.0, 1.5), slice(None, None, None), (1.0, 9.0, 1.5)),
            ((1.0, 9.0, 1.5), slice(None, 1, None), (1.0, 2.5, 1.5)),
            ((1.0, 9.0, 1.5), slice(None, None, 2), (1.0, 9.0, 3.0)),
            ((1.0, 9.0, 1.5), slice(None, 2, None), (1.0, 4.0, 1.5)),
            ((1.0, 9.0, 1.5), slice(1, 2, None), (2.5, 4.0, 1.5)),
            ((1.0, 9.0, 1.5), slice(1, -1, None), (2.5, 8.5, 1.5)),
            ((1.0, 9.0, 1.5), slice(10, None, 3), (9.0, 9.0, 4.5)),
            ((1.0, 9.0, 1.5), slice(-10, None, 3), (1.0, 9.0, 4.5)),
            ((1.0, 9.0, 1.5), slice(None, -10, 3), (1.0, 1.0, 4.5)),
            ((1.0, 9.0, 1.5), slice(None, 10, 3), (1.0, 9.0, 4.5)),
            (
                (10.0, 0.0, -2.0),
                slice(None, None, -1),
                (2.0, 12.0, 2.0),
            ),
            (
                (Decimal("1.0"), Decimal("9.0"), Decimal("1.5")),
                slice(1, -1, None),
                (Decimal("2.5"), Decimal("8.5"), Decimal("1.5")),
            ),
            (
                (Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)),
                slice(1, -1, None),
                (Fraction(5, 2), Fraction(4, 1), Fraction(3, 2)),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                slice(1, -1, None),
                (
                    datetime(2019, 3, 29, 10),
                    datetime(2019, 3, 29, 20),
                    timedelta(hours=10),
                ),
            ),
        ]:
            self.assertEqual(
                mi.numeric_range(*expected_args), mi.numeric_range(*args)[sl]
            )

    def test_iter_twice(self):
        r1 = mi.numeric_range(1.0, 9.9, 1.5)
        r2 = mi.numeric_range(8.5, 0.0, -1.5)
        self.assertEqual([1.0, 2.5, 4.0, 5.5, 7.0, 8.5], list(r1))
        self.assertEqual([1.0, 2.5, 4.0, 5.5, 7.0, 8.5], list(r1))
        self.assertEqual([8.5, 7.0, 5.5, 4.0, 2.5, 1.0], list(r2))
        self.assertEqual([8.5, 7.0, 5.5, 4.0, 2.5, 1.0], list(r2))

    def test_len(self):
        for args, expected in [
            ((1.0, 7.0, 1.5), 4),
            ((1.0, 7.01, 1.5), 5),
            ((7.0, 1.0, -1.5), 4),
            ((7.01, 1.0, -1.5), 5),
            ((0.1, 0.30000000000000001, 0.2), 1),  # IEEE 754 !
            (
                (
                    Decimal("0.1"),
                    Decimal("0.30000000000000001"),
                    Decimal("0.2"),
                ),
                2,
            ),  # works with Decimal
            ((Decimal("1.0"), Decimal("9.0"), Decimal("1.5")), 6),
            ((Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)), 3),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                3,
            ),
        ]:
            self.assertEqual(expected, len(mi.numeric_range(*args)))

    def test_len_matches_iteration(self):
        # len() is derived by division while the items come from repeated
        # multiplication; the two must not disagree, or the last index reports
        # an item the range never yields.
        for args in [
            # the division overshoots: len() counted an item past the stop
            (-8.732, -13.532, -2.4),
            (4.51, 10.375, 0.255),
            # the division falls short: len() missed an item the range yields
            (-69.37276490678778, -29.8339073344664, 4.942357196540172),
            (32.50227965239458, 9.003418351482358, -1.9582384417426848),
            (0.0, 1.0, 0.1),
            (0.0, 3.0, 0.7),
            (10.0, -10.0, -0.7),
            (2.1, 5.1, 0.5),
        ]:
            r = mi.numeric_range(*args)
            items = list(r)
            self.assertEqual(len(items), len(r))
            if items:
                self.assertEqual(items[-1], r[len(r) - 1])

    def test_repr(self):
        for args, *expected in [
            ((7.0,), "numeric_range(0.0, 7.0)"),
            ((1.0, 7.0), "numeric_range(1.0, 7.0)"),
            ((7.0, 1.0, -1.5), "numeric_range(7.0, 1.0, -1.5)"),
            (
                (Decimal("1.0"), Decimal("9.0"), Decimal("1.5")),
                (
                    "numeric_range(Decimal('1.0'), Decimal('9.0'), "
                    "Decimal('1.5'))"
                ),
            ),
            (
                (Fraction(7, 7), Fraction(10, 2), Fraction(3, 2)),
                (
                    "numeric_range(Fraction(1, 1), Fraction(5, 1), "
                    "Fraction(3, 2))"
                ),
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                "numeric_range(datetime.datetime(2019, 3, 29, 0, 0), "
                "datetime.datetime(2019, 3, 30, 0, 0), "
                "datetime.timedelta(seconds=36000))",
                "numeric_range(datetime.datetime(2019, 3, 29, 0, 0), "
                "datetime.datetime(2019, 3, 30, 0, 0), "
                "datetime.timedelta(0, 36000))",
            ),
        ]:
            with self.subTest(args=args):
                self.assertIn(repr(mi.numeric_range(*args)), expected)

    def test_reversed(self):
        for args, expected in [
            ((7.0,), [6.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0]),
            ((1.0, 7.0), [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]),
            ((7.0, 1.0, -1.5), [2.5, 4.0, 5.5, 7.0]),
            ((7.0, 0.9, -1.5), [1.0, 2.5, 4.0, 5.5, 7.0]),
            (
                (Decimal("1.0"), Decimal("5.0"), Decimal("1.5")),
                [Decimal('4.0'), Decimal('2.5'), Decimal('1.0')],
            ),
            (
                (Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)),
                [Fraction(4, 1), Fraction(5, 2), Fraction(1, 1)],
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                [
                    datetime(2019, 3, 29, 20),
                    datetime(2019, 3, 29, 10),
                    datetime(2019, 3, 29),
                ],
            ),
        ]:
            self.assertEqual(expected, list(reversed(mi.numeric_range(*args))))

    def test_reversed_preserves_float_values(self):
        for args in [
            (0.0, 1.0, 0.1),
            (1.0, 0.0, -0.1),
            (0.1, 0.5, 0.1),
        ]:
            with self.subTest(args=args):
                values = mi.numeric_range(*args)
                self.assertEqual(list(values)[::-1], list(reversed(values)))

    def test_reversed_datetime_limits(self):
        for start, step in [
            (datetime.min, timedelta(days=1)),
            (datetime.max, -timedelta(days=1)),
        ]:
            with self.subTest(start=start, step=step):
                values = mi.numeric_range(start, start + 2 * step, step)
                self.assertEqual(list(values)[::-1], list(reversed(values)))

    def test_count(self):
        for args, v, c in [
            ((7.0,), 0.0, 1),
            ((7.0,), 0.5, 0),
            ((7.0,), 6.0, 1),
            ((7.0,), 7.0, 0),
            ((7.0,), 10.0, 0),
            (
                (Decimal("1.0"), Decimal("5.0"), Decimal("1.5")),
                Decimal('4.0'),
                1,
            ),
            (
                (Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)),
                Fraction(5, 2),
                1,
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                datetime(2019, 3, 29, 20),
                1,
            ),
        ]:
            self.assertEqual(c, mi.numeric_range(*args).count(v))

    def test_index(self):
        for args, v, i in [
            ((7.0,), 0.0, 0),
            ((7.0,), 6.0, 6),
            ((7.0, 0.0, -1.0), 7.0, 0),
            ((7.0, 0.0, -1.0), 1.0, 6),
            (
                (Decimal("1.0"), Decimal("5.0"), Decimal("1.5")),
                Decimal('4.0'),
                2,
            ),
            (
                (Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)),
                Fraction(5, 2),
                1,
            ),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                datetime(2019, 3, 29, 20),
                2,
            ),
        ]:
            self.assertEqual(i, mi.numeric_range(*args).index(v))

        for args, v in [
            ((0.7,), 0.5),
            ((0.7,), 7.0),
            ((0.7,), 10.0),
            ((7.0, 0.0, -1.0), 0.5),
            ((7.0, 0.0, -1.0), 0.0),
            ((7.0, 0.0, -1.0), 10.0),
            ((7.0, 0.0), 5.0),
            ((Decimal("1.0"), Decimal("5.0"), Decimal("1.5")), Decimal('4.5')),
            ((Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)), Fraction(5, 3)),
            (
                (
                    datetime(2019, 3, 29),
                    datetime(2019, 3, 30),
                    timedelta(hours=10),
                ),
                datetime(2019, 3, 30),
            ),
        ]:
            with self.assertRaises(ValueError):
                mi.numeric_range(*args).index(v)

    def test_parent_classes(self):
        r = mi.numeric_range(7.0)
        self.assertTrue(isinstance(r, Iterable))
        self.assertFalse(isinstance(r, Iterator))
        self.assertTrue(isinstance(r, Sequence))
        self.assertTrue(isinstance(r, Hashable))

    def test_bad_key(self):
        r = mi.numeric_range(7.0)
        for arg, message in [
            ('a', 'numeric range indices must be integers or slices, not str'),
            (
                (),
                'numeric range indices must be integers or slices, not tuple',
            ),
        ]:
            with self.assertRaisesRegex(TypeError, message):
                r[arg]

    def test_pickle(self):
        for args in [
            (7.0,),
            (5.0, 7.0),
            (5.0, 7.0, 3.0),
            (7.0, 5.0),
            (7.0, 5.0, 4.0),
            (7.0, 5.0, -1.0),
            (Decimal("1.0"), Decimal("5.0"), Decimal("1.5")),
            (Fraction(1, 1), Fraction(5, 1), Fraction(3, 2)),
            (datetime(2019, 3, 29), datetime(2019, 3, 30)),
        ]:
            r = mi.numeric_range(*args)
            self.assertTrue(dumps(r))  # assert not empty
            self.assertEqual(r, loads(dumps(r)))

    def test_empty_reversed(self):
        self.assertEqual(list(reversed(mi.numeric_range(0))), [])
