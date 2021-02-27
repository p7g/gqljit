import functools
import typing as t
import llvmlite.ir as ir

_nothing = object()
_once_T = t.TypeVar("_once_T", bound=t.Callable[..., t.Any])


def once(func: _once_T) -> _once_T:
    init_result = _nothing

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        nonlocal init_result

        if init_result is not _nothing:
            return init_result

        init_result = func(*args, **kwargs)
        return init_result

    return t.cast(_once_T, wrapper)


def cstr(b, bytes_: bytes):
    ty = ir.ArrayType(ir.IntType(8), len(bytes_))
    ptr = b.alloca(ty)
    b.store(ty(bytearray(bytes_)), ptr)
    return b.bitcast(ptr, ir.IntType(8).as_pointer())


@once
def _get_printf(mod):
    return ir.Function(
        mod,
        ir.FunctionType(ir.VoidType(), [ir.IntType(8).as_pointer()], var_arg=True),
        "printf",
    )


def printf(b, text, *strs):
    b.call(_get_printf(b.module), [cstr(b, text), *strs])
