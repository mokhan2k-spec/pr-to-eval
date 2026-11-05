# numeric_range: `==` and `hash()` raise OverflowError for very long ranges

`more_itertools.numeric_range` is meant to behave like the built-in `range`. For ranges that contain more values than `sys.maxsize`, comparing two `numeric_range` objects or hashing one fails, although the built-in `range` handles both:

```python
>>> from more_itertools import numeric_range
>>> a = numeric_range(0, 10**30)
>>> b = numeric_range(0, 10**30)
>>> a == b
OverflowError: Python int too large to convert to C ssize_t
>>> hash(a)
OverflowError: Python int too large to convert to C ssize_t
>>> {a: 1}[b]
OverflowError: Python int too large to convert to C ssize_t

>>> range(0, 10**30) == range(0, 10**30)
True
>>> hash(range(0, 10**30)) == hash(range(0, 10**30))
True
```

## Required behaviour

- `==` and `!=` between two `numeric_range` objects work for any length, including lengths above `sys.maxsize`. Two ranges are equal when they produce the same values, even if their arguments differ. For example `numeric_range(0, 10**30, 2) == numeric_range(0, 10**30 - 1, 2)` is `True`. Ascending and descending ranges are both supported.
- `hash()` works for any length. Equal ranges have equal hashes, so a `numeric_range` can be used as a dict key or set member whatever its length. The hash of an integer `numeric_range` matches the hash of the equal built-in `range`.
- Behaviour for ranges that fit in `sys.maxsize` stays exactly as it is today.
- `len()` of a range longer than `sys.maxsize` keeps raising `OverflowError`, as it does today. That is Python's own rule for `__len__`, the same as `len(range(0, 10**30))`.
- No public API change: no new public names, no changed signatures.

## Tests

The existing tests are in `tests/test_more.py` (the `NumericRangeTests` class covers `numeric_range`). Run them with `python -m pytest tests/test_more.py -k NumericRange`. Hidden tests will be run against your change. They cover the behaviour above and the existing `numeric_range` behaviour.

Keep the change small and limited to what the bug needs.
