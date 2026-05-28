"""Unit tests for the rserve_client envelope / type-conversion helpers.

These don't require a running Rserve - they exercise the pure-Python
shims (_unwrap, _to_py) against handcrafted inputs that mirror what
pyRserve returns. They protect us from regressions when pyRserve or our
R-side handlers change shape.
"""

from __future__ import annotations

import pytest

from microbiomedb_mcp.rserve_client import RserveCallError, _to_py, _unwrap


class FakeTagged:
    """Minimal stand-in for pyRserve's TaggedList that exposes .astuples()."""
    def __init__(self, items):
        self._items = list(items)
    def astuples(self):
        return list(self._items)


def test_unwrap_ok_dict_payload():
    env = FakeTagged([("ok", True), ("value", FakeTagged([("a", 1)]))])
    out = _unwrap("fn", env)
    assert out is not None  # _unwrap returns the raw 'value' (still tagged)


def test_unwrap_error_envelope_raises():
    env = FakeTagged([("ok", False), ("error", "boom"), ("class", "simpleError")])
    with pytest.raises(RserveCallError) as ei:
        _unwrap("mcp_loadDataset", env)
    assert "boom" in str(ei.value)
    assert ei.value.fn == "mcp_loadDataset"
    assert ei.value.r_class == "simpleError"


def test_unwrap_handles_scalar_array_ok():
    # Older pyRserve returns scalars as length-1 arrays.
    class FakeArr(list):
        def __init__(self, xs):
            super().__init__(xs)

    env = FakeTagged([("ok", FakeArr([True])), ("value", "hi")])
    assert _unwrap("fn", env) == "hi"


def test_to_py_named_list_to_dict():
    obj = FakeTagged([("a", 1), ("b", FakeTagged([("nested", 2)]))])
    assert _to_py(obj) == {"a": 1, "b": {"nested": 2}}


def test_to_py_unnamed_list_to_python_list():
    # All-None keys (unnamed R list) must NOT collapse into one dict key.
    obj = FakeTagged([(None, 1), (None, 2), (None, 3)])
    assert _to_py(obj) == [1, 2, 3]


def test_to_py_scalars_passthrough():
    assert _to_py("hello") == "hello"
    assert _to_py(3) == 3
    assert _to_py(True) is True
