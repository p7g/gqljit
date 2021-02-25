import ctypes
import typing as t

from llvmlite import ir, binding as llvm
from graphql.execution import ExecutionContext
from graphql.language.ast import FieldNode
from graphql.pyutils.path import Path
from graphql.type import GraphQLSchema, GraphQLObjectType, is_non_null_type

_bool_ty = ir.IntType(1)
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


"""
References:
- https://eli.thegreenplace.net/2015/calling-back-into-python-from-llvmlite-jited-code/
- http://www.vishalchovatiya.com/memory-layout-of-cpp-object/
- https://adventures.michaelfbryan.com/posts/rust-closures-in-ffi/
- https://adventures.michaelfbryan.com/posts/ffi-safe-polymorphism-in-rust/
"""


class JITExecutionContext(ExecutionContext):
    def execute_fields(
        self,
        parent_type: GraphQLObjectType,
        source_value: t.Any,
        path: t.Optional[Path],
        fields: t.Dict[str, t.List[FieldNode]],
    ):
        # FIXME: Cache the compiled queries
        selection = [(alias, fields[0]) for alias, fields in fields.items()]
        compiler = _Compiler(self.schema)
        compiled_ir = compiler.compile(selection)
        print(compiled_ir)


class _Compiler:
    def __init__(self, schema: GraphQLSchema):
        self._engine = _get_execution_engine()
        self._schema = schema
        self._ir_context = ir.Context()
        self._module = ir.Module(context=self._ir_context)

    def compile(self, selection: t.List[t.Tuple[str, FieldNode]]):
        query_type = self._schema.query_type
        assert query_type is not None
        top_func, result_struct = self._compile(query_type, selection)
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
        return ctypes.CFUNCTYPE(ctypes.c_int)(execute_func_ptr)

    @property
    def _py_object(self):
        opaque_struct = self._ir_context.get_identified_type("PyObject")
        return opaque_struct.as_pointer()

    # FIXME: maintain path to where we're at for compilation error reporting
    def _compile(
        self,
        root_type: GraphQLObjectType,
        selection: t.List[t.Tuple[str, FieldNode]],
    ):
        return self._compile_selection("query", root_type, selection)

    def _compile_selection(
        self,
        field_name: str,
        root_type: GraphQLObjectType,
        selection: t.List[t.Tuple[str, FieldNode]],
    ):
        struct_fields = []
        functions = {}

        for alias, field in selection:
            if not field.selection_set:
                struct_fields.append((alias, self._py_object))
            else:
                assert all(
                    isinstance(sel, FieldNode) for sel in field.selection_set.selections
                )
                field_type = root_type.fields[field.name.value].type
                functions[alias], struct_ty = self._compile_selection(
                    alias,
                    field_type.of_type if is_non_null_type(field_type) else field_type,
                    [
                        (f.alias.value if f.alias else f.name.value, f)
                        for f in field.selection_set.selections
                        if isinstance(f, FieldNode)
                    ],
                )
                struct_fields.append((alias, struct_ty))

        field_indices = {name: i for i, (name, _ty) in enumerate(struct_fields)}
        field_types = [ty for _name, ty in struct_fields]
        struct_ty = ir.LiteralStructType(field_types)

        func_ty = ir.FunctionType(
            _bool_ty, (struct_ty.as_pointer(), self._py_object, self._py_object)
        )
        func = ir.Function(self._module, func_ty, f"execute_{field_name}")
        irbuilder = ir.IRBuilder(func.append_basic_block("entry"))
        ret_struct, root, info = func.args
        ret_struct.name = "field_values"
        root.name = "root"
        info.name = "info"

        for alias, field in selection:
            if not field.selection_set:
                val = self._py_object(None)  # FIXME: actually resolve field
            else:
                field_struct_ptr = irbuilder.alloca(
                    field_types[field_indices[alias]], name=f"{alias}_fields_ptr"
                )
                ok = irbuilder.call(
                    functions[alias],
                    [field_struct_ptr, self._py_object(None), self._py_object(None)],
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
