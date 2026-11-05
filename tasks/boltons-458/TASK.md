# OrderedMultiDict.addlist() silently drops values when given a one-shot iterator

`OrderedMultiDict.addlist(key, iterable)` is documented as accepting "tuples and other sequences and iterables". With a list it works. With an iterator or a generator, values go missing and the dictionary ends up inconsistent:

```python
from boltons.dictutils import OrderedMultiDict
omd = OrderedMultiDict()
omd.addlist('a', iter([1, 2]))
print(omd.getlist('a'), omd.items(multi=True))   # values are missing or disagree with each other
```

## Required behaviour

- Every `OrderedMultiDict` in boltons accepts any iterable in `addlist`. That includes the one `QueryParamDict` in `boltons/urlutils.py` uses for URL query strings (`URL(...).query_params.addlist(...)`, then `to_text()` must show all the values).
- All values are kept, in the order the iterable yields them, and after any earlier `add`/`addlist` values for the same key.
- `getlist`, `get`, `items(multi=True)` and the other methods agree with each other afterwards.
- An empty iterable adds nothing. In particular it must not create the key.
- Lists, tuples, ranges and other sequences keep working exactly as before.

The existing tests in `tests/` show how to run the suite (`python -m pytest tests`). Add a regression test of your own if you like. Keep the change small: do not touch files outside `boltons/` and `tests/`, and add no new dependencies.
