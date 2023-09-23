from llvmlite import ir

from ._utils import cstr, printf

int32 = ir.IntType(32)
intptr = ir.IntType(64)  # FIXME
c_str = ir.IntType(8).as_pointer()


def make(ctx, mod):
    py_obj = ctx.get_identified_type("PyObject").as_pointer()
    FILE_p = ctx.get_identified_type("FILE").as_pointer()
    null_obj = py_obj(None)
    stdout = ir.GlobalVariable(mod, FILE_p, "stdout")

    def pyapi_func(name, ret, args, varargs=False):
        return ir.Function(mod, ir.FunctionType(ret, args, var_arg=varargs), name)

    def global_var(name, type_):
        return ir.GlobalVariable(mod, type_, name)

    class _pyapi:
        PyObject = py_obj

        PyMapping_Check = pyapi_func("PyMapping_Check", int32, [py_obj])
        PyMapping_GetItemString = pyapi_func(
            "PyMapping_GetItemString", py_obj, [py_obj, c_str]
        )

        PyObject_GetAttrString = pyapi_func(
            "PyObject_GetAttrString", py_obj, [py_obj, c_str]
        )
        PyObject_Call = pyapi_func("PyObject_Call", py_obj, [py_obj, py_obj, py_obj])
        PyObject_Repr = pyapi_func("PyObject_Repr", py_obj, [py_obj])
        PyObject_Print = pyapi_func("PyObject_Print", int32, [py_obj, FILE_p, int32])
        PyObject_Type = pyapi_func("PyObject_Type", py_obj, [py_obj])
        Py_BuildValue = pyapi_func("Py_BuildValue", py_obj, [c_str], varargs=True)

        PyCallable_Check = pyapi_func("PyCallable_Check", int32, [py_obj])

        PyTuple_Pack = pyapi_func("PyTuple_Pack", py_obj, [intptr], varargs=True)

        PyDict_New = pyapi_func("PyDict_New", py_obj, [])
        PyDict_SetItemString = pyapi_func(
            "PyDict_SetItemString", int32, [py_obj, c_str, py_obj]
        )

        PyList_Append = pyapi_func("PyList_Append", int32, [py_obj, py_obj])

        PyUnicode_AsEncodedString = pyapi_func(
            "PyUnicode_AsEncodedString", py_obj, [py_obj, c_str, c_str]
        )

        PyBytes_AsString = pyapi_func("PyBytes_AsString", c_str, [py_obj])

        Py_None = global_var("_Py_NoneStruct", py_obj.pointee)

        Py_IncRef = pyapi_func("Py_IncRef", ir.VoidType(), [py_obj])
        Py_DecRef = pyapi_func("Py_DecRef", ir.VoidType(), [py_obj])

        PyErr_Clear = pyapi_func("PyErr_Clear", ir.VoidType(), [])
        PyErr_GivenExceptionMatches = pyapi_func(
            "PyErr_GivenExceptionMatches", ir.IntType(1), [py_obj, py_obj]
        )
        PyErr_Restore = pyapi_func(
            "PyErr_Restore", ir.VoidType(), [py_obj, py_obj, py_obj]
        )
        PyException_GetTraceback = pyapi_func(
            "PyException_GetTraceback", py_obj, [py_obj]
        )

        PyExc_BaseException = global_var("PyExc_BaseException", py_obj)
        PyExc_Exception = global_var("PyExc_Exception", py_obj)

        @staticmethod
        def guarded_call(b, fn, args, ret_on_err=null_obj, error_sentinel=null_obj):
            result = b.call(fn, args)
            is_null = b.icmp_unsigned("==", result, error_sentinel)
            with b.if_then(is_null):
                b.ret(ret_on_err)
            return result

        @classmethod
        def repr(cls, b, obj, ret_on_err=null_obj):
            result = b.call(cls.PyObject_Print, [obj, b.load(stdout), int32(0)])
            with b.if_then(b.trunc(result, ir.IntType(1))):
                b.ret(ret_on_err)

        @classmethod
        def incref(cls, b, obj):
            return b.call(cls.Py_IncRef, [obj])

        @classmethod
        def decref(cls, b, obj):
            return b.call(cls.Py_DecRef, [obj])

    return _pyapi()
