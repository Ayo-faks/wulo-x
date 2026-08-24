"""Signature-pinned conformance checks for test fakes.

A fake that accepts ``**kwargs`` will happily absorb a call the real SDK would
reject with ``TypeError``. In production that class of bug manifests as silent
dead air on a live call while every test stays green. ``assert_conforms``
compares a fake callable against the real SDK callable and fails loudly on any
loosening: extra/missing parameters, kind changes (keyword-only dropped),
requiredness changes, ``**kwargs``/``*args`` added, or sync/async mismatch.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Iterable

_P = inspect.Parameter


class ConformanceError(TypeError):
    """Raised when a fake does not match the real SDK callable's contract."""


def _is_async(fn: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(inspect.unwrap(fn))


def _named_params(fn: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    params = inspect.signature(fn).parameters
    return {
        name: p
        for name, p in params.items()
        if name not in ("self", "cls") and p.kind not in (_P.VAR_KEYWORD, _P.VAR_POSITIONAL)
    }


def _has_kind(fn: Callable[..., Any], kind: inspect._ParameterKind) -> bool:
    return any(p.kind is kind for p in inspect.signature(fn).parameters.values())


def assert_conforms(fake: Callable[..., Any], real: Callable[..., Any], *, name: str | None = None) -> None:
    """Assert that ``fake`` presents exactly the same call contract as ``real``.

    Raises :class:`ConformanceError` listing every violation found.
    """
    label = name or getattr(real, "__qualname__", repr(real))
    problems: list[str] = []

    fake_async, real_async = _is_async(fake), _is_async(real)
    if fake_async != real_async:
        problems.append(
            f"async mismatch: fake is {'async' if fake_async else 'sync'}, "
            f"real is {'async' if real_async else 'sync'}"
        )

    if _has_kind(fake, _P.VAR_KEYWORD) and not _has_kind(real, _P.VAR_KEYWORD):
        problems.append("fake accepts **kwargs but the real callable does not — this hides signature bugs")
    if _has_kind(fake, _P.VAR_POSITIONAL) and not _has_kind(real, _P.VAR_POSITIONAL):
        problems.append("fake accepts *args but the real callable does not — this hides signature bugs")

    fake_params, real_params = _named_params(fake), _named_params(real)
    for missing in sorted(real_params.keys() - fake_params.keys()):
        problems.append(f"fake is missing parameter {missing!r}")
    for extra in sorted(fake_params.keys() - real_params.keys()):
        problems.append(f"fake has parameter {extra!r} that the real callable does not accept")

    for shared in sorted(fake_params.keys() & real_params.keys()):
        fp, rp = fake_params[shared], real_params[shared]
        if fp.kind is not rp.kind:
            problems.append(
                f"parameter {shared!r} kind mismatch: fake={fp.kind.description}, real={rp.kind.description}"
            )
        if rp.default is _P.empty and fp.default is not _P.empty:
            problems.append(f"parameter {shared!r} is required in the real callable but optional in the fake")
        if rp.default is not _P.empty and fp.default is _P.empty:
            problems.append(f"parameter {shared!r} is optional in the real callable but required in the fake")

    if problems:
        raise ConformanceError(f"fake does not conform to {label}:\n  - " + "\n  - ".join(problems))


def assert_object_conforms(
    fake: Any,
    real_cls: type,
    *,
    methods: Iterable[str] | None = None,
) -> None:
    """Assert that ``fake`` implements every public method of ``real_cls`` conformantly.

    Extra helper methods on the fake are allowed; missing or loosened real
    methods are not. Pass ``methods`` to restrict the check to a subset.
    """
    if methods is None:
        names = [
            n
            for n, member in inspect.getmembers(real_cls, callable)
            if not n.startswith("_")
        ]
    else:
        names = list(methods)

    problems: list[str] = []
    for method_name in names:
        real_fn = getattr(real_cls, method_name)
        fake_fn = getattr(fake, method_name, None)
        if fake_fn is None or not callable(fake_fn):
            problems.append(f"fake is missing method {method_name!r}")
            continue
        try:
            assert_conforms(fake_fn, real_fn, name=f"{real_cls.__name__}.{method_name}")
        except ConformanceError as exc:
            problems.append(str(exc))

    if problems:
        raise ConformanceError(
            f"fake object does not conform to {real_cls.__name__}:\n" + "\n".join(problems)
        )
