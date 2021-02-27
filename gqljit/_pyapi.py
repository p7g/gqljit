from llvmlite import ir

int32 = ir.IntType(32)
intptr = ir.IntType(64)  # FIXME
c_str = ir.IntType(8).as_pointer()


def make(ctx, mod):
    py_obj = ctx.get_identified_type("PyObject").as_pointer()
    null_obj = py_obj(None)

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
        PyObject_Call = pyapi_func("PyObject_Call", py_obj, [py_obj, py_obj, py_obj])

        PyCallable_Check = pyapi_func("PyCallable_Check", int32, [py_obj])

        PyTuple_Pack = pyapi_func("PyTuple_Pack", py_obj, [intptr], varargs=True)

        PyDict_New = pyapi_func("PyDict_New", py_obj, [])
        PyDict_SetItemString = pyapi_func(
            "PyDict_SetItemString", int32, [py_obj, c_str, py_obj]
        )

        Py_None = global_var("_Py_NoneStruct", ctx.get_identified_type("PyObject"))

        Py_IncRef = pyapi_func("Py_IncRef", ir.VoidType(), [py_obj])
        Py_DecRef = pyapi_func("Py_DecRef", ir.VoidType(), [py_obj])

        PyErr_Clear = pyapi_func("PyErr_Clear", ir.VoidType(), [])

        @staticmethod
        def guarded_call(b, fn, args, ret_on_err=null_obj):
            result = b.call(fn, args)
            is_null = b.icmp_unsigned("==", result, null_obj)
            with b.if_then(is_null):
                b.ret(ret_on_err)
            return result

    return _pyapi()
