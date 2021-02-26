from llvmlite import ir

int32 = ir.IntType(32)
intptr = ir.IntType(64)  # FIXME
c_str = ir.IntType(8).as_pointer()


def make(ctx, mod):
    py_obj = ctx.get_identified_type("PyObject").as_pointer()

    def pyapi_func(name, ret, args, varargs=False):
        return ir.Function(mod, ir.FunctionType(ret, args, var_arg=varargs), name)

    def global_var(name, type_):
        return ir.GlobalVariable(mod, type_, name)

    class _pyapi:
        PyMapping_Check = pyapi_func("PyMapping_Check", int32, [py_obj])
        PyMapping_GetItemString = pyapi_func(
            "PyMapping_GetItemString", py_obj, [py_obj, c_str]
        )
        PyObject_GetAttrString = pyapi_func(
            "PyObject_GetAttrString", py_obj, [py_obj, c_str]
        )
        PyCallable_Check = pyapi_func("PyCallable_Check", int32, [py_obj])
        PyObject_Call = pyapi_func("PyObject_Call", py_obj, [py_obj, py_obj, py_obj])
        PyTuple_Pack = pyapi_func("PyTuple_Pack", py_obj, [intptr], varargs=True)
        PyDict_New = pyapi_func("PyDict_New", py_obj, [])
        PyDict_SetItemString = pyapi_func(
            "PyDict_SetItemString", int32, [py_obj, c_str, py_obj]
        )
        Py_None = global_var("Py_None", py_obj)

    return _pyapi()
