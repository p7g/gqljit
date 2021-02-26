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
    GraphQLSchema,
    GraphQLObjectType,
    is_non_null_type,
    is_scalar_type,
    get_nullable_type,
    is_object_type,
)

from . import _pyapi

_bool_ty = ir.IntType(1)
_char_p = ir.IntType(8).as_pointer()
_i32 = ir.IntType(32)
_execution_engine = None


def _get_execution_engine():
    global _execution_engine

    if _execution_engine:
        return _execution_engine

    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    target_machine = llvm.Target.from_default_triple().create_target_machine()
    backing_module = llvm.parse_assembly("")
    _execution_engine = llvm.create_mcjit_compiler(backing_module, target_machine)
    return _execution_engine


def cstr(b, bytes_: bytes):
    ty = ir.ArrayType(ir.IntType(8), len(bytes_))
    ptr = b.alloca(ty)
    b.store(ty(bytearray(bytes_)), ptr)
    return b.bitcast(ptr, _char_p)


@dataclass
class Field:
    name: str
    resolver: t.Callable[..., t.Any]
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
        compiler = _Compiler()
        compiled_ir = compiler.compile(selection)
        print(compiled_ir)


class _Compiler:
    def __init__(self):
        self._engine = _get_execution_engine()
        self._ir_context = ir.Context()
        self._module = ir.Module(context=self._ir_context)
        self._pyapi = _pyapi.make(self._ir_context, self._module)

    def compile(self, query: ObjectField):
        top_func, result_struct = self._compile(query)
        print(str(self._module))
        module = llvm.parse_assembly(str(self._module))
        module.verify()
        tm = llvm.Target.from_default_triple().create_target_machine()
        tm.set_asm_verbosity(True)
        print(tm.emit_assembly(module))
        return
        self._engine.add_module(module)
        self._engine.finalize_object()
        self._engine.run_static_constructors()

        execute_func_ptr = module.get_function_address("execute")
        # FIXME: use cffi instead of ctypes
        return ctypes.PYFUNCTYPE(ctypes.c_int)(execute_func_ptr)

    @property
    def _py_object(self):
        opaque_struct = self._ir_context.get_identified_type("PyObject")
        return opaque_struct.as_pointer()

    # FIXME: maintain path to where we're at for compilation error reporting
    def _compile(self, query: ObjectField):
        return self._compile_selection("query", query)

    def _compile_selection(self, outer_alias: str, selection: ObjectField):
        struct_fields = []
        functions = {}

        for alias, field in selection.selection.items():
            if isinstance(field, ScalarField):
                struct_fields.append((alias, self._py_object))
            elif isinstance(field, ObjectField):
                functions[alias], struct_ty = self._compile_selection(alias, field)
                struct_fields.append((alias, struct_ty))
            else:
                raise NotImplementedError(field)

        field_indices = {name: i for i, (name, _ty) in enumerate(struct_fields)}
        field_types = [ty for _name, ty in struct_fields]
        struct_ty = ir.LiteralStructType(field_types)

        func_ty = ir.FunctionType(
            _bool_ty, (struct_ty.as_pointer(), self._py_object, self._py_object)
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
            resolver = self._get_default_resolver()  # FIXME: use real resolver
            val = irbuilder.call(
                resolver,
                [
                    root,
                    cstr(irbuilder, field.name.encode("ascii")),
                    info,
                    irbuilder.call(self._pyapi.PyDict_New, []),
                ],
            )
            if isinstance(field, ObjectField):
                field_struct_ptr = irbuilder.alloca(
                    field_types[field_indices[alias]], name=f"{alias}_fields_ptr"
                )
                ok = irbuilder.call(
                    functions[alias],
                    [field_struct_ptr, val, self._py_object(None)],
                    name=f"{alias}_ok",
                )
                with irbuilder.if_then(irbuilder.not_(ok), likely=False):
                    irbuilder.ret(ok)
                val = irbuilder.load(field_struct_ptr)
            field_ptr = irbuilder.gep(
                ret_struct,
                [_i32(0), _i32(field_indices[alias])],
                inbounds=True,
                name=f"{alias}_ptr",
            )
            irbuilder.store(val, field_ptr)

        irbuilder.ret(_bool_ty(0))

        return func, struct_ty

    def _get_default_resolver(self):
        existing = getattr(self, "_default_resolver", None)
        if existing:
            return existing

        func_ty = ir.FunctionType(
            self._py_object,
            (self._py_object, _char_p, self._py_object, self._py_object),
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
        val = irbuilder.phi(self._py_object, name="value")
        val.add_incoming(then_val, then_block)
        val.add_incoming(else_val, else_block)

        with irbuilder.if_then(
            irbuilder.icmp_unsigned("==", val, self._py_object(None)),
            likely=False,
        ):
            irbuilder.ret(irbuilder.load(self._pyapi.Py_None))
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
            val2 = irbuilder.call(self._pyapi.PyObject_Call, [val, args, kwargs])
            then_block = irbuilder.block
            then_block.name = "call_it"

        irbuilder.block.name = "return_value"
        prev_val = val
        val = irbuilder.phi(self._py_object)
        val.add_incoming(prev_val, prev_block)
        val.add_incoming(val2, then_block)

        irbuilder.ret(val)

        self._default_resolver = func
        return func
