"""
References:
- https://eli.thegreenplace.net/2015/calling-back-into-python-from-llvmlite-jited-code/
- http://www.vishalchovatiya.com/memory-layout-of-cpp-object/
- https://adventures.michaelfbryan.com/posts/rust-closures-in-ffi/
- https://adventures.michaelfbryan.com/posts/ffi-safe-polymorphism-in-rust/
"""

import ctypes
import itertools
import typing as t
from dataclasses import dataclass

from llvmlite import ir, binding as llvm
from graphql.execution import ExecutionContext
from graphql.language.ast import FieldNode
from graphql.pyutils.path import Path
from graphql.type import (
    GraphQLObjectType,
    is_non_null_type,
    is_scalar_type,
    get_nullable_type,
    is_object_type,
)

from . import _pyapi
from ._utils import once, cstr

__all__ = [
    "Field",
    "ScalarField",
    "ObjectField",
    "JITExecutionContext",
    "Compiler",
]

_bool_ty = ir.IntType(1)
_char_p = ir.IntType(8).as_pointer()
_i32 = ir.IntType(32)


@once
def _init_llvm_bindings() -> None:
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()


@dataclass
class Field:
    name: str
    resolver: t.Optional[t.Callable[..., t.Any]]
    nullable: bool


@dataclass
class ScalarField(Field):
    pass


@dataclass
class ObjectField(Field):
    selection: t.Dict[str, Field]


def convert_graphql_query(
    root_type: GraphQLObjectType, fields: t.List[FieldNode]
) -> ObjectField:
    def _convert_graphql_query(
        root_type: GraphQLObjectType, fields: t.List[FieldNode]
    ) -> t.Dict[str, Field]:
        selection = {}

        for field in fields:
            name = field.name.value
            alias = field.alias.value if field.alias else name
            field_def = root_type.fields[name]
            type_ = field_def.type
            resolver = field_def.resolve
            nullable = True

            if is_non_null_type(type_):
                type_ = get_nullable_type(type_)
                nullable = False

            sel: Field
            if is_scalar_type(type_):
                assert field.selection_set is None
                sel = ScalarField(
                    name=name,
                    resolver=resolver,
                    nullable=nullable,
                )
            elif is_object_type(type_):
                assert field.selection_set is not None
                sel = ObjectField(
                    name=name,
                    resolver=resolver,
                    nullable=nullable,
                    selection=_convert_graphql_query(
                        type_, field.selection_set.selections
                    ),
                )
            else:
                raise NotImplementedError(type_)

            selection[alias] = sel

        return selection

    return ObjectField(
        name="query",
        resolver=lambda root, info: root,
        nullable=True,
        selection=_convert_graphql_query(root_type, fields),
    )


class JITExecutionContext(ExecutionContext):
    def execute_fields(
        self,
        parent_type: GraphQLObjectType,
        source_value: t.Any,
        path: t.Optional[Path],
        fields: t.Dict[str, t.List[FieldNode]],
    ):
        # FIXME: Cache the compiled queries
        selection = convert_graphql_query(
            parent_type, [field for (field,) in fields.values()]
        )
        compiler = Compiler()
        resolver = compiler.compile(selection)
        return resolver(source_value, None, self.errors)


class Compiler:
    def __init__(self):
        _init_llvm_bindings()
        self._target_machine = llvm.Target.from_default_triple().create_target_machine()
        self._engine = llvm.create_mcjit_compiler(
            llvm.parse_assembly(""), self._target_machine
        )
        self._ir_context = ir.Context()
        self._module = ir.Module(context=self._ir_context)
        self._pyapi = _pyapi.make(self._ir_context, self._module)

        self._do_not_gc = []

    def compile(self, query: ObjectField):
        pyfunc = self._compile(query)
        # print(self.llvm_ir())
        # print(self.asm())
        return pyfunc

    def llvm_ir(self) -> str:
        return str(self._module)

    def asm(self, verbose=True) -> str:
        self._target_machine.set_asm_verbosity(verbose)
        asm: str = self._target_machine.emit_assembly(llvm.parse_assembly(self.llvm_ir))
        return asm

    def finalize(self):
        module = llvm.parse_assembly(str(self._module))
        module.verify()
        self._engine.add_module(module)
        self._engine.finalize_object()
        self._engine.run_static_constructors()

    # FIXME: maintain path to where we're at for compilation error reporting
    def _compile(self, query: ObjectField):
        top_func = self._compile_selection("query", query, ("query",))
        self.finalize()

        execute_func_ptr = self._engine.get_function_address(top_func.name)
        adapter = ctypes.PYFUNCTYPE(
            ctypes.py_object,
            ctypes.py_object,
            ctypes.py_object,
            ctypes.py_object,
        )(execute_func_ptr)

        def wrapper(root, info, errors, *args, **kwargs):
            return adapter(root, info, errors)

        return wrapper

    def _compile_selection(
        self, outer_alias: str, selection: ObjectField, path: tuple[int | str, ...]
    ):
        functions = {}

        for alias, field in selection.selection.items():
            if isinstance(field, ScalarField):
                pass
            elif isinstance(field, ObjectField):
                functions[alias] = self._compile_selection(alias, field, (*path, alias))
            else:
                raise NotImplementedError(field)

        func_ty = ir.FunctionType(
            self._pyapi.PyObject,
            (self._pyapi.PyObject, self._pyapi.PyObject, self._pyapi.PyObject),
        )
        func = ir.Function(self._module, func_ty, f"execute_{outer_alias}")
        irbuilder = ir.IRBuilder(func.append_basic_block("entry"))
        root, info, errors = func.args
        root.name = "root"
        info.name = "info"
        errors.name = "errors"

        aliases = list(selection.selection)
        alias_blocks = {alias: irbuilder.append_basic_block(alias) for alias in aliases}
        end_block = irbuilder.append_basic_block("end")

        result_dict = self._pyapi.guarded_call(irbuilder, self._pyapi.PyDict_New, [])
        irbuilder.branch(alias_blocks[aliases[0]])

        # FIXME: Py_EnterRecursiveCall?
        for alias, next_alias in itertools.pairwise(aliases + [None]):
            assert isinstance(alias, str)
            assert isinstance(next_alias, (str, type(None)))

            block = alias_blocks[alias]
            next_block = alias_blocks[next_alias] if next_alias else end_block
            field = selection.selection[alias]
            irbuilder.position_at_end(block)

            if field.resolver is not None:
                callback_ptr, callback_ty = self._get_resolver_callback(field)
                resolver = irbuilder.inttoptr(ir.IntType(64)(callback_ptr), callback_ty)
                val = irbuilder.call(resolver, [root, info])
            else:
                resolver = self._get_default_resolver()
                val = irbuilder.call(
                    resolver,
                    [
                        root,
                        cstr(irbuilder, f"{field.name}\0".encode("utf-8")),
                        info,
                        self._pyapi.PyObject(None),
                    ],
                )

            resolver_failed = irbuilder.call(
                self._pyapi.PyErr_GivenExceptionMatches,
                [val, irbuilder.load(self._pyapi.PyExc_BaseException)],
            )
            with irbuilder.if_then(resolver_failed, likely=False):
                is_fatal_exception = irbuilder.not_(
                    irbuilder.call(
                        self._pyapi.PyErr_GivenExceptionMatches,
                        [val, irbuilder.load(self._pyapi.PyExc_Exception)],
                    ),
                )
                with irbuilder.if_then(is_fatal_exception, likely=False):
                    self._pyapi.decref(irbuilder, result_dict)
                    irbuilder.call(
                        self._pyapi.PyErr_Restore,
                        [
                            irbuilder.call(self._pyapi.PyObject_Type, [val]),
                            val,
                            irbuilder.call(self._pyapi.PyException_GetTraceback, [val]),
                        ],
                    )
                    irbuilder.ret(self._pyapi.PyObject(None))

                (
                    located_error_func,
                    located_error_ty,
                ) = self._get_graphql_core_located_error_fn()

                path_fmt = "".join(
                    "s" if isinstance(part, str) else "l" for part in path
                )
                path_llvm_parts = [
                    cstr(irbuilder, f"{part}\0".encode("utf-8"))
                    if isinstance(part, str)
                    else ir.IntType(64)(part)
                    for part in path
                ]
                path_sequence = self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.Py_BuildValue,
                    [
                        cstr(irbuilder, f"({path_fmt})\0".encode("ascii")),
                        *path_llvm_parts,
                    ],
                )

                self._pyapi.incref(irbuilder, self._pyapi.Py_None)
                located_error = irbuilder.call(
                    irbuilder.inttoptr(
                        ir.IntType(64)(located_error_func), located_error_ty
                    ),
                    [val, self._pyapi.Py_None, path_sequence],
                )
                self._pyapi.decref(irbuilder, val)
                self._pyapi.decref(irbuilder, path_sequence)

                self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyList_Append,
                    [errors, located_error],
                    error_sentinel=_i32(-1),
                )

                self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyDict_SetItemString,
                    [
                        result_dict,
                        cstr(irbuilder, f"{alias}\0".encode("utf-8")),
                        self._pyapi.Py_None,
                    ],
                    error_sentinel=_i32(-1),
                )
                if field.nullable:
                    irbuilder.branch(next_block)
                else:
                    self._pyapi.decref(irbuilder, result_dict)
                    self._pyapi.incref(irbuilder, self._pyapi.Py_None)
                    irbuilder.ret(self._pyapi.Py_None)

            if isinstance(field, ScalarField):
                self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyDict_SetItemString,
                    [result_dict, cstr(irbuilder, f"{alias}\0".encode("utf-8")), val],
                    error_sentinel=_i32(-1),
                )
                self._pyapi.decref(irbuilder, val)
            elif isinstance(field, ObjectField):
                inner_result = irbuilder.call(
                    functions[alias], [val, info, errors], name=f"{alias}_ok"
                )
                self._pyapi.decref(irbuilder, val)
                # if inner_result is NULL return NULL
                with irbuilder.if_then(
                    irbuilder.icmp_unsigned(
                        "==", inner_result, inner_result.type(None)
                    ),
                    likely=False,
                ):
                    self._pyapi.decref(irbuilder, result_dict)
                    irbuilder.ret(self._pyapi.PyObject(None))
                self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyDict_SetItemString,
                    [
                        result_dict,
                        cstr(irbuilder, f"{alias}\0".encode("utf-8")),
                        inner_result,
                    ],
                    error_sentinel=_i32(-1),
                )
                if not field.nullable:
                    with irbuilder.if_then(
                        irbuilder.icmp_unsigned(
                            "==", inner_result, self._pyapi.Py_None
                        ),
                        likely=False,
                    ):
                        self._pyapi.incref(irbuilder, self._pyapi.Py_None)
                        self._pyapi.decref(irbuilder, result_dict)
                        self._pyapi.decref(irbuilder, inner_result)
                        irbuilder.ret(self._pyapi.Py_None)
                self._pyapi.decref(irbuilder, inner_result)
            else:
                raise NotImplementedError(field)

            irbuilder.branch(next_block)

        irbuilder.position_at_end(end_block)
        irbuilder.ret(result_dict)

        return func

    def _get_graphql_core_located_error_fn(self):
        if hasattr(self, "_graphql_core_located_error"):
            func, ty = self._graphql_core_located_error
        else:
            from graphql.error import located_error

            proto = ctypes.PYFUNCTYPE(
                ctypes.py_object, ctypes.py_object, ctypes.py_object, ctypes.py_object
            )
            func = proto(located_error)
            ty = ir.FunctionType(
                self._pyapi.PyObject,
                [self._pyapi.PyObject, self._pyapi.PyObject, self._pyapi.PyObject],
            ).as_pointer()
            self._do_not_gc.append(func)
            self._graphql_core_located_error = func, ty

        return ctypes.cast(func, ctypes.c_void_p).value, ty

    def _get_resolver_callback(self, field):
        def wrapper(*args):
            try:
                return field.resolver(*args)
            except BaseException as exc:
                return exc

        # FIXME: Field arguments
        proto = ctypes.CFUNCTYPE(ctypes.py_object, ctypes.py_object, ctypes.py_object)
        callback = proto(wrapper)
        self._do_not_gc.append(callback)
        llvm_type = ir.FunctionType(
            self._pyapi.PyObject, (self._pyapi.PyObject, self._pyapi.PyObject)
        )
        return ctypes.cast(callback, ctypes.c_void_p).value, llvm_type.as_pointer()

    def _get_default_resolver(self):
        existing = getattr(self, "_default_resolver", None)
        if existing:
            return existing

        func_ty = ir.FunctionType(
            self._pyapi.PyObject,
            (self._pyapi.PyObject, _char_p, self._pyapi.PyObject, self._pyapi.PyObject),
        )
        func = ir.Function(self._module, func_ty, "default_field_resolver")
        source, field_name, info, kwargs = func.args
        source.name = "source"
        field_name.name = "field_name"
        info.name = "info"
        kwargs.name = "kwargs"

        irbuilder = ir.IRBuilder(func.append_basic_block("entry"))
        is_mapping_block = func.append_basic_block("check_is_mapping")
        irbuilder.branch(is_mapping_block)
        irbuilder.position_at_end(is_mapping_block)

        is_mapping = irbuilder.trunc(
            irbuilder.call(self._pyapi.PyMapping_Check, [source]),
            _bool_ty,
            name="is_mapping",
        )
        with irbuilder.if_else(is_mapping) as (then, else_):
            with then:
                then_val = irbuilder.call(
                    self._pyapi.PyMapping_GetItemString, [source, field_name]
                )
                then_block = irbuilder.block
                then_block.name = "is_mapping"
            with else_:
                else_val = irbuilder.call(
                    self._pyapi.PyObject_GetAttrString, [source, field_name]
                )
                else_block = irbuilder.block
                else_block.name = "is_not_mapping"

        irbuilder.block.name = "check_no_value"
        val = irbuilder.phi(self._pyapi.PyObject, name="value")
        val.add_incoming(then_val, then_block)
        val.add_incoming(else_val, else_block)

        with irbuilder.if_then(
            irbuilder.icmp_unsigned("==", val, self._pyapi.PyObject(None)),
            likely=False,
        ):
            irbuilder.call(self._pyapi.PyErr_Clear, [])
            irbuilder.ret(val)
            irbuilder.block.name = "no_value_for_field"

        irbuilder.block.name = "check_is_callable"
        is_callable = irbuilder.trunc(
            irbuilder.call(self._pyapi.PyCallable_Check, [val]),
            _bool_ty,
            name="is_callable",
        )
        prev_block = irbuilder.block
        with irbuilder.if_then(is_callable):
            args = irbuilder.call(self._pyapi.PyTuple_Pack, [ir.IntType(64)(1), info])
            # FIXME: handle errors
            val2 = irbuilder.call(self._pyapi.PyObject_Call, [val, args, kwargs])
            then_block = irbuilder.block
            then_block.name = "call_it"

        irbuilder.block.name = "return_value"
        prev_val = val
        val = irbuilder.phi(self._pyapi.PyObject)
        val.add_incoming(prev_val, prev_block)
        val.add_incoming(val2, then_block)

        irbuilder.ret(val)

        self._default_resolver = func
        return func
