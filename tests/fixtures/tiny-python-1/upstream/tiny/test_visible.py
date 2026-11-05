from tiny.mathx import clamp


def test_clamp_inside():
    assert clamp(5, 0, 10) == 5
