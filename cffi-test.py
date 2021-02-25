from cffi import FFI

builder = FFI()
builder.cdef(
    """
    void *make_a_dict(void);
    """
)

builder.set_source(
    "_test2",
    """
    #include <assert.h>
    #include <Python.h>

    PyObject *make_a_dict(void)
    {
        PyObject *dict = PyDict_New();
        assert(dict);

        PyObject *test_val = PyUnicode_FromString("Hello world");
        assert(test_val);
        PyDict_SetItemString(dict, "Test key", test_val);

        return dict;
    }
    """,
)

builder.compile()

from _test2 import lib

# from ctypes import PYFUNCTYPE, py_object, c_char_p
#
# PyDict_New = PYFUNCTYPE(py_object, [])
# PyUnicode_FromString = PYFUNCTYPE(py_object, [c_char_p])

print(lib.make_a_dict())
