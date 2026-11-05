from tiny.mathx import clamp


def test_clamp_below():
    assert clamp(-3, 0, 10) == 0


def test_clamp_above():
    assert clamp(42, 0, 10) == 10


def test_clamp_inside():
    assert clamp(5, 0, 10) == 5
