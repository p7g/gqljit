"""
References:
- https://eli.thegreenplace.net/2015/calling-back-into-python-from-llvmlite-jited-code/
- http://www.vishalchovatiya.com/memory-layout-of-cpp-object/
- https://adventures.michaelfbryan.com/posts/rust-closures-in-ffi/
- https://adventures.michaelfbryan.com/posts/ffi-safe-polymorphism-in-rust/
"""

import ctypes
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


def _result_struct_to_dict(query: ObjectField, struct):
    result = {}
    for alias, field in query.selection.items():
        value = getattr(struct, alias)
        if isinstance(field, ScalarField):
            result[alias] = value
        elif isinstance(field, ObjectField):
            result[alias] = _result_struct_to_dict(field, value)
        else:
            raise NotImplementedError(field)
    return result


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
        return resolver(source_value, None)


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

    def compile(self, query: ObjectField):
        pyfunc = self._compile(query)
        # print(self.llvm_ir)
        # print(self.asm())
        return pyfunc

    @property
    def engine(self):
        return self._engine

    @property
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

    def _make_ctypes_struct(self, query: ObjectField) -> ctypes.Structure:
        name = query.name
        fields = []
        for alias, field in query.selection.items():
            if isinstance(field, ScalarField):
                fields.append((alias, ctypes.py_object))
            elif isinstance(field, ObjectField):
                fields.append((alias, self._make_ctypes_struct(field)))
            else:
                raise NotImplementedError(field)
        return t.cast(
            ctypes.Structure, type(name, (ctypes.Structure,), {"_fields_": fields})
        )

    # FIXME: maintain path to where we're at for compilation error reporting
    def _compile(self, query: ObjectField):
        top_func, result_ty = self._compile_selection("query", query)
        # Generate a ctypes struct for result_ty
        # Wrap top_func in `def run(root, info, *args, **kwargs)`
        ctypes_struct = self._make_ctypes_struct(query)
        result = ctypes_struct()
        self.finalize()
        execute_func_ptr = self._engine.get_function_address(top_func.name)
        adapter = ctypes.PYFUNCTYPE(
            ctypes.c_int,
            ctypes.POINTER(ctypes_struct),
            ctypes.py_object,
            ctypes.py_object,
        )(execute_func_ptr)

        def wrapper(root, info, *args, **kwargs):
            failed = adapter(ctypes.byref(result), root, info)
            if failed:
                raise Exception()
            return _result_struct_to_dict(query, result)

        return wrapper

    def _compile_selection(self, outer_alias: str, selection: ObjectField):
        struct_fields = []
        functions = {}

        for alias, field in selection.selection.items():
            if isinstance(field, ScalarField):
                struct_fields.append((alias, self._pyapi.PyObject))
            elif isinstance(field, ObjectField):
                functions[alias], struct_ty = self._compile_selection(alias, field)
                struct_fields.append((alias, struct_ty))
            else:
                raise NotImplementedError(field)

        field_indices = {name: i for i, (name, _ty) in enumerate(struct_fields)}
        field_types = [ty for _name, ty in struct_fields]
        struct_ty = ir.LiteralStructType(field_types)

        func_ty = ir.FunctionType(
            _bool_ty,
            (struct_ty.as_pointer(), self._pyapi.PyObject, self._pyapi.PyObject),
        )
        func = ir.Function(self._module, func_ty, f"execute_{outer_alias}")
        irbuilder = ir.IRBuilder(func.append_basic_block("entry"))
        ret_struct, root, info = func.args
        ret_struct.name = "field_values"
        root.name = "root"
        info.name = "info"

        for alias, field in selection.selection.items():
            block = irbuilder.append_basic_block(alias)
            irbuilder.branch(block)
            irbuilder.position_at_start(block)
            if field.resolver is not None:
                resolver = irbuilder.inttoptr(
                    ir.IntType(64)(id(field.resolver)), self._pyapi.PyObject
                )
                args = self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyTuple_Pack,
                    [ir.IntType(64)(2), root, info],
                    ret_on_err=_bool_ty(1),
                )
                val = self._pyapi.guarded_call(
                    irbuilder,
                    self._pyapi.PyObject_Call,
                    [resolver, args, self._pyapi.PyObject(None)],
                    ret_on_err=_bool_ty(1),
                )
            else:
                resolver = self._get_default_resolver()
                val = self._pyapi.guarded_call(
                    irbuilder,
                    resolver,
                    [
                        root,
                        cstr(irbuilder, f"{field.name}\0".encode("ascii")),
                        info,
                        irbuilder.call(self._pyapi.PyDict_New, []),
                    ],
                    ret_on_err=_bool_ty(1),
                )
            field_ptr = irbuilder.gep(
                ret_struct,
                [_i32(0), _i32(field_indices[alias])],
                inbounds=True,
                name=f"{alias}_ptr",
            )
            if isinstance(field, ScalarField):
                irbuilder.store(val, field_ptr)
            elif isinstance(field, ObjectField):
                failed = irbuilder.call(
                    functions[alias], [field_ptr, val, info], name=f"{alias}_ok"
                )
                with irbuilder.if_then(failed, likely=False):
                    irbuilder.ret(failed)
            else:
                raise NotImplementedError(field)

        irbuilder.ret(_bool_ty(0))

        return func, struct_ty

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
            none = self._pyapi.Py_None
            irbuilder.call(self._pyapi.Py_IncRef, [none])
            irbuilder.ret(none)
            irbuilder.block.name = "no_value_for_field"

        irbuilder.block.name = "check_is_callable"
        is_callable = irbuilder.trunc(
            irbuilder.call(self._pyapi.PyCallable_Check, [val]),
            _bool_ty,
            name="is_callable",
        )
        prev_block = irbuilder.block
        with irbuilder.if_then(is_callable):
            args = self._pyapi.guarded_call(
                irbuilder, self._pyapi.PyTuple_Pack, [ir.IntType(64)(1), info]
            )
            val2 = self._pyapi.guarded_call(
                irbuilder, self._pyapi.PyObject_Call, [val, args, kwargs]
            )
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
