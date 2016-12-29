import pytest

from pyegs.compiler import compile as compile_


def test_assign():
    with pytest.raises(NotImplementedError):
        compile_('x, y = 1, 2')


def test_consts():
    assert compile_('x = const(4)') == ''
    assert compile_('x = const(4); y = x') == 'p1z 4'


def test_numbers():
    assert compile_('x = 4') == 'p1z 4'
    assert compile_('x = 4.0') == 'p1z 4,0'
    assert compile_('x = 4; y = 5') == 'p1z 4 p2z 5'


def test_other_names():
    assert compile_('x = 4; y = x') == 'p1z 4 p2z p1z'
    assert compile_('x = 4; y = x; z = y; y = 5') == 'p1z 4 p2z p1z p3z p2z p2z 5'
    assert compile_('x = 4; y = x; x = y') == 'p1z 4 p2z p1z p1z p2z'


def test_strings():
    assert compile_('s = "Hello World"') == 's0z Hello_World'


def test_undefined():
    with pytest.raises(NameError) as exc_info:
        compile_('x = y')
    assert str(exc_info.value) == "name 'y' is not defined"


def test_tuples():
    with pytest.raises(TypeError) as exc_info:
        assert compile_('x = (1, "2")')
    assert str(exc_info.value) == 'tuple items must be of the same type'

    assert compile_('x = (1, 2)') == 'p1z 2 p2z 1 p3z 2'
    assert compile_('x = 1; y = (2, 3)') == 'p1z 1 p2z 3 p3z 2 p4z 3'

    assert compile_('x = 1; y = ("1", "2")') == 'p1z 1 p2z 0 s0z 1 s1z 2'

    assert compile_('x = (1, 2, 3); y = x') == 'p1z 2 p2z 1 p3z 2 p4z 3 p5z p1z'

    with pytest.raises(NotImplementedError) as exc_info:
        compile_('x = ((1, 2), (3, 4))')
    assert 'cannot declare item' in str(exc_info.value)

    assert compile_('x = (1, 2); y = (3, 4); z = (x, y)') == 'p1z 2 p2z 1 p3z 2 p4z 5 p5z 3 p6z 4 p7z 8 p8z p1z p9z p4z'


@pytest.mark.skip
def test_static_type():
    with pytest.raises(TypeError) as exc_info:
        assert compile_('x = 1; x = "s"')
    assert str(exc_info.value) == "cannot assign string 's' to numeric variable"
