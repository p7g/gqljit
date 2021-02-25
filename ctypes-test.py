import ctypes

import cffi
import llvmlite.ir as ir
import llvmlite.binding as llvm


def create_addrcaller(module, addr):
    i64_ty = ir.IntType(64)

    cb_func_ptr_ty = ir.FunctionType(i64_ty, [i64_ty, i64_ty]).as_pointer()
    addrcaller_func_ty = ir.FunctionType(i64_ty, [i64_ty, i64_ty])

    addrcaller_func = ir.Function(module, addrcaller_func_ty, name="addrcaller")
    a, b = addrcaller_func.args
    a.name = "a"
    b.name = "b"

    irbuilder = ir.IRBuilder(addrcaller_func.append_basic_block("entry"))
    f = irbuilder.inttoptr(ir.values.Constant(i64_ty, addr), cb_func_ptr_ty, name="f")
    call = irbuilder.call(f, [a, b])
    irbuilder.ret(call)


ffibuilder = cffi.FFI()
ffibuilder.cdef(
    """
    extern "Python" int64_t myfunc(int64_t, int64_t);
    """
)
ffibuilder.set_source("_native", "")
ffibuilder.compile(verbose=True)
from _native import ffi, lib


def make_counter():
    i = 0

    def myfunc(a, b):
        nonlocal i
        print(f"I was called with {a} and {b}, counter: {i}")
        i += 1
        return a + b

    return myfunc


ffi.def_extern()(make_counter())
myfunc_addr = int(ffi.cast("intptr_t", lib.myfunc))
print(f"Callback address is 0x{myfunc_addr:x}")

module = ir.Module()
create_addrcaller(module, myfunc_addr)
print(module)

llvm.initialize()
llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

llvm_module = llvm.parse_assembly(str(module))
tm = llvm.Target.from_default_triple().create_target_machine()

ee = llvm.create_mcjit_compiler(llvm_module, tm)
ee.finalize_object()

addrcallerfunc = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_int64, ctypes.c_int64)(
    ee.get_function_address("addrcaller")
)
print("Calling 'addrcaller'")
res = addrcallerfunc(105, 23)
print("  The result is", res)
