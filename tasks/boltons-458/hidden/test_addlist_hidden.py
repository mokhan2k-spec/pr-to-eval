"""Hidden tests for boltons PR 458: addlist() must accept any iterable.

Every OrderedMultiDict in boltons is covered: the one in dictutils, the one
QueryParamDict normally inherits, and the private copy that urlutils carries
and falls back to when boltons.dictutils cannot be imported.
"""
import importlib.util
import sys

import pytest

from boltons import dictutils, urlutils
from boltons.dictutils import OMD
from boltons.urlutils import URL, QueryParamDict


def _load_urlutils_fallback():
    """Load a private instance of urlutils with boltons.dictutils blocked,
    so the module-local OrderedMultiDict copy is the one in use."""
    saved = sys.modules.get('boltons.dictutils', ())
    # a None entry makes `from .dictutils import ...` raise ImportError
    sys.modules['boltons.dictutils'] = None
    try:
        spec = importlib.util.spec_from_file_location(
            'boltons._urlutils_fallback', urlutils.__file__)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved == ():
            del sys.modules['boltons.dictutils']
        else:
            sys.modules['boltons.dictutils'] = saved
    return mod


_FALLBACK = _load_urlutils_fallback()


FACTORIES = {
    'dictutils.OMD': OMD,
    'urlutils.QueryParamDict': QueryParamDict,
    'urlutils-fallback.OMD': _FALLBACK.OrderedMultiDict,
    'urlutils-fallback.QueryParamDict': _FALLBACK.QueryParamDict,
}


@pytest.fixture(params=sorted(FACTORIES))
def make(request):
    return FACTORIES[request.param]


def test_fallback_copy_is_distinct():
    # guards the test setup itself: the fallback must be the module-local copy
    assert _FALLBACK.OrderedMultiDict is not dictutils.OrderedMultiDict
    assert _FALLBACK.OrderedMultiDict.__module__ == 'boltons._urlutils_fallback'
    assert urlutils.OrderedMultiDict is dictutils.OrderedMultiDict


# ---- the upstream PR's test --------------------------------------------

def test_addlist_iterator():
    omd = OMD()
    omd.addlist('a', iter([1, 2, 3]))
    omd.addlist('b', (x for x in [4, 5]))

    assert omd.keys() == ['a', 'b']
    assert omd.getlist('a') == [1, 2, 3]
    assert omd.getlist('b') == [4, 5]
    assert omd.get('a') == 3
    assert len(list(omd.iteritems(multi=True))) == 5

    e_omd = OMD()
    e_omd.addlist('a', (x for x in []))
    assert len(e_omd) == 0
    assert e_omd.keys() == []
    assert e_omd.get('a') is None


# ---- behaviour, every OrderedMultiDict in boltons ----------------------

def test_addlist_generator(make):
    omd = make()
    omd.addlist('a', (x for x in [1, 2, 3]))
    assert omd.getlist('a') == [1, 2, 3]
    assert omd.items(multi=True) == [('a', 1), ('a', 2), ('a', 3)]
    assert len(omd) == 1


def test_addlist_iterator_obj(make):
    omd = make()
    omd.addlist('a', iter([1, 2]))
    assert omd.getlist('a') == [1, 2]
    assert omd.items(multi=True) == [('a', 1), ('a', 2)]


def test_addlist_map_object(make):
    omd = make()
    omd.addlist('a', map(str, [1, 2, 3]))
    assert omd.getlist('a') == ['1', '2', '3']
    assert omd.items(multi=True) == [('a', '1'), ('a', '2'), ('a', '3')]


def test_addlist_tuple(make):
    omd = make()
    omd.addlist('a', (1, 2, 3))
    assert omd.getlist('a') == [1, 2, 3]
    assert omd.items(multi=True) == [('a', 1), ('a', 2), ('a', 3)]


def test_addlist_list(make):
    omd = make()
    omd.addlist('a', [1, 2, 3])
    omd.addlist('b', [4])
    assert omd.getlist('a') == [1, 2, 3]
    assert omd.keys() == ['a', 'b']
    assert len(list(omd.iteritems(multi=True))) == 4


def test_addlist_range(make):
    omd = make()
    omd.addlist('a', range(3))
    assert omd.getlist('a') == [0, 1, 2]


def test_addlist_set(make):
    omd = make()
    omd.addlist('a', {1, 2, 3})
    assert sorted(omd.getlist('a')) == [1, 2, 3]
    assert sorted(v for _, v in omd.items(multi=True)) == [1, 2, 3]


def test_addlist_empty_iterator_creates_no_key(make):
    omd = make()
    omd.addlist('a', iter([]))
    omd.addlist('b', (x for x in []))
    omd.addlist('c', map(str, []))
    assert 'a' not in omd and 'b' not in omd and 'c' not in omd
    assert len(omd) == 0
    assert omd.keys() == []
    assert omd.items(multi=True) == []
    assert omd.getlist('a') == []


def test_addlist_empty_list_creates_no_key(make):
    omd = make()
    omd.addlist('a', [])
    omd.addlist('b', ())
    assert len(omd) == 0
    assert omd.keys() == []
    assert omd.items(multi=True) == []


def test_addlist_empty_iterator_keeps_existing_values(make):
    omd = make()
    omd.add('a', 1)
    omd.addlist('a', iter([]))
    assert omd.getlist('a') == [1]
    assert omd.items(multi=True) == [('a', 1)]


def test_addlist_mixed_add_order(make):
    omd = make()
    omd.add('a', 1)
    omd.addlist('b', iter([10, 11]))
    omd.add('a', 2)
    omd.addlist('a', (x for x in [3, 4]))
    omd.add('b', 12)
    assert omd.items(multi=True) == [
        ('a', 1), ('b', 10), ('b', 11), ('a', 2), ('a', 3), ('a', 4), ('b', 12)]
    assert omd.getlist('a') == [1, 2, 3, 4]
    assert omd.getlist('b') == [10, 11, 12]
    assert omd.keys() == ['a', 'b']
    assert omd.get('a') == 4
    assert omd.get('b') == 12


def test_addlist_iterator_internal_consistency(make):
    # the linked list and the per-key lists must agree after a one-shot add
    omd = make()
    omd.addlist('a', iter([1, 2, 3]))
    omd.add('b', 9)
    assert list(omd.iteritems(multi=True)) == [
        ('a', 1), ('a', 2), ('a', 3), ('b', 9)]
    assert omd.popall('a') == [1, 2, 3]
    assert omd.items(multi=True) == [('b', 9)]
    omd.addlist('a', iter([7, 8]))
    assert omd.poplast('a') == 8
    assert omd.getlist('a') == [7]
    assert omd.copy().items(multi=True) == [('b', 9), ('a', 7)]


def test_addlist_does_not_alias_input(make):
    omd = make()
    src = [1, 2]
    omd.addlist('a', iter(src))
    src.append(3)
    assert omd.getlist('a') == [1, 2]
    lst = [5, 6]
    omd.addlist('b', lst)
    lst.append(7)
    assert omd.getlist('b') == [5, 6]


# ---- QueryParamDict through URL ----------------------------------------

@pytest.mark.parametrize('urlmod', [urlutils, _FALLBACK],
                         ids=['urlutils', 'urlutils-fallback'])
def test_url_query_params_addlist_one_shot(urlmod):
    qpd = urlmod.URL('http://example.com/?x=0').query_params
    qpd.addlist('y', iter(['1', '2']))
    qpd.addlist('z', (v for v in ['3']))
    assert qpd.getlist('y') == ['1', '2']
    assert qpd.getlist('z') == ['3']
    assert qpd.to_text() == 'x=0&y=1&y=2&z=3'


@pytest.mark.parametrize('urlmod', [urlutils, _FALLBACK],
                         ids=['urlutils', 'urlutils-fallback'])
def test_url_to_text_shows_all_added_values(urlmod):
    url = urlmod.URL('http://example.com/search')
    url.query_params.addlist('tag', iter(['a', 'b', 'c']))
    url.query_params.addlist('none', iter([]))
    assert url.to_text() == 'http://example.com/search?tag=a&tag=b&tag=c'
    assert 'none' not in url.query_params
