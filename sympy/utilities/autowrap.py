"""Module for compiling codegen-output, and wrap it for use in python.

What?

This module provides a common interface for different external backends, such
as f2py, fwrap, Cython, SWIG(?) etc.  The goal is to provide access to compiled
binaries of acceptable performance with a one-button user interface, i.e.

    >>> from sympy.abc import x,y
    >>> from sympy.utilities.autowrap import autowrap
    >>> expr = ((x - y)**(25)).expand()
    >>> binary_callable = autowrap(expr)
    >>> binary_callable(1, 2)           #doctest: +SKIP
    -1.0

The callable returned from autowrap() is a binary python function, not a
Sympy object.  If it is desired to use the compiled function in symbolic
expressions, it is better to use binary_function() which returns a Sympy
Function object.  The binary callable is attached as the _imp_ attribute and
invoked when a numerical evaluation is requested with evalf(), or with
lambdify().

    >>> from sympy.utilities.autowrap import binary_function
    >>> f = binary_function('f', expr)
    >>> 2*f(x, y) + y
    y + 2*f(x, y)
    >>> (2*f(x, y) + y).evalf(2, subs={x: 1, y:2})    #doctest: +SKIP
    0.0

Why?

The idea is that a SymPy user will primarily be interested in working with
mathematical expressions, and should not have to learn details about wrapping
tools in order to evaluate expressions numerically, even if they are
computationally expensive.

When is this useful?

    1) For computations on large arrays, Python iterations may be too slow, and
    depending on the mathematical expression, it may be difficult to exploit
    the advanced index operations provided by NumPy.

    2) For *really* long expressions that will be called repeatedly, the
    compiled binary should be significantly faster than SymPy's .evalf()

    3) If you are generating code with the codegen utility in order to use it
    in another project, the automatic python wrappers let you test the binaries
    immediately from within SymPy.

When is this module NOT the best approach?

    1) If you are really concerned about speed or memory optimizations, you
    will probably get better results by working directly with the wrapper tools
    and the low level code.  However, the files generated by this utility may
    provide a useful starting point and reference code. Temporary files will be
    left intact if you supply the keyword filepath="path/to/files/".

    2) If the array computation can be handled easily by numpy, and you don't
    need the binaries for another project.

"""
import sys
import os
import shutil
import tempfile
import subprocess

from sympy.utilities.codegen import (
        codegen, get_code_generator, Routine, OutputArgument, InOutArgument
        )
from sympy.utilities.lambdify import implemented_function

class CodeWrapError(Exception): pass

class CodeWrapper:
    _filename = "wrapped_code"
    _module_basename = "wrapper_module"
    _module_counter = 0

    @property
    def filename(self):
        return "%s_%s" % (self._filename, CodeWrapper._module_counter)

    @property
    def module_name(self):
        return "%s_%s" % (self._module_basename, CodeWrapper._module_counter)

    def __init__(self, generator, filepath=None, flags=[], quiet=True):
        """
        generator -- the code generator to use
        """
        self.generator = generator
        self.filepath = filepath
        self.flags = flags
        self.quiet = quiet

    @property
    def include_header(self):
        return bool(self.filepath)

    @property
    def include_empty(self):
        return bool(self.filepath)

    def _generate_code(self, main_routine, routines):
        routines.append(main_routine)
        self.generator.write(routines, self.filename, True, self.include_header,
                self.include_empty)

    def wrap_code(self, routine, helpers=[]):

        workdir = self.filepath or tempfile.mkdtemp("_sympy_compile")
        if not os.access(workdir, os.F_OK):
            os.mkdir(workdir)
        oldwork = os.getcwd()
        os.chdir(workdir)
        try:
            sys.path.append(workdir)
            self._generate_code(routine, helpers)
            self._prepare_files(routine)
            self._process_files(routine)
            mod = __import__(self.module_name)
        finally:
            sys.path.pop()
            CodeWrapper._module_counter +=1
            os.chdir(oldwork)
            if not self.filepath:
                shutil.rmtree(workdir)

        return self._get_wrapped_function(mod)

    def _process_files(self, routine):
        command = self.command
        command.extend(self.flags)
        if self.quiet:
            null = open(os.devnull, 'w')
            retcode = subprocess.call(command, stdout=null)
        else:
            retcode = subprocess.call(command)
        if retcode:
            raise CodeWrapError

class DummyWrapper(CodeWrapper):
    """Class used for testing independent of backends """

    template = """# dummy module for testing of Sympy
def %(name)s():
    return "%(expr)s"
"""
    def _prepare_files(self, routine):
        return

    def _generate_code(self, routine, helpers):
        f = file('%s.py' % self.module_name, 'w')
        printed = ", ".join([str(res.expr) for res in routine.result_variables])
        print >> f, DummyWrapper.template % {
                'name': routine.name,
                'expr': printed
                }
        f.close()

    def _process_files(self, routine):
        return

    @classmethod
    def _get_wrapped_function(cls, mod):
        return mod.autofunc

class CythonCodeWrapper(CodeWrapper):
    setup_template = """
from distutils.core import setup
from distutils.extension import Extension
from Cython.Distutils import build_ext

setup(
    cmdclass = {'build_ext': build_ext},
    ext_modules = [Extension(%(args)s)]
        )
"""

    @property
    def command(self):
        command = ["python", "setup.py", "build_ext", "--inplace"]
        return command

    def _prepare_files(self, routine):
        pyxfilename = self.module_name + '.pyx'
        codefilename = "%s.%s" % (self.filename, self.generator.code_extension)

        # pyx
        f = file(pyxfilename, 'w')
        self.dump_pyx([routine], f, self.filename,
                self.include_header, self.include_empty)
        f.close()

        # setup.py
        ext_args = [repr(self.module_name), repr([pyxfilename, codefilename])]
        f = file('setup.py', 'w')
        print >> f, CythonCodeWrapper.setup_template % {'args': ", ".join(ext_args)}
        f.close()

    @classmethod
    def _get_wrapped_function(cls, mod):
        return mod.autofunc_c

    def dump_pyx(self, routines, f, prefix, header=True, empty=True):
        """Write a Cython file with python wrappers

           This file contains all the definitions of the routines in c code and
           refers to the header file.

           Arguments:
             routines  --  a list of Routine instances
             f  --  a file-like object to write the file to
             prefix  --  the filename prefix, used to refer to the proper header
                         file. Only the basename of the prefix is used.

           Optional arguments:
             empty  --  When True, empty lines are included to structure the
                        source files. [DEFAULT=True]
        """
        for routine in routines:
            prototype = self.generator.get_prototype(routine)
            origname = routine.name
            routine.name = "%s_c" % origname
            prototype_c = self.generator.get_prototype(routine)
            routine.name = origname

            # declare
            print >> f, 'cdef extern from "%s.h":' % prefix
            print >> f, '   %s' % prototype
            if empty: print >> f

            # wrap
            ret, args_py = self._split_retvals_inargs(routine.arguments)
            args_c = ", ".join([str(a.name) for a in routine.arguments])
            print >> f, "def %s_c(%s):" % (routine.name,
                    ", ".join(self._declare_arg(arg) for arg in args_py))
            for r in ret:
                if not r in args_py:
                    print >> f, "   cdef %s" % self._declare_arg(r)
            rets = ", ".join([str(r.name) for r in ret])
            if routine.results:
                call = '   return %s(%s)' % (routine.name, args_c)
                if rets:
                    print >> f, call + ', ' + rets
                else:
                    print >> f, call
            else:
                print >> f, '   %s(%s)' % (routine.name, args_c)
                print >> f, '   return %s' % rets

            if empty: print >> f
    dump_pyx.extension = "pyx"

    def _split_retvals_inargs(self, args):
        """Determines arguments and return values for python wrapper"""
        py_args = []
        py_returns = []
        for arg in args:
            if isinstance(arg, OutputArgument):
                py_returns.append(arg)
            elif isinstance(arg, InOutArgument):
                py_returns.append(arg)
                py_args.append(arg)
            else:
                py_args.append(arg)
        return py_returns, py_args

    def _declare_arg(self, arg):
        t = arg.get_datatype('c')
        if arg.dimensions:
            return "%s *%s"%(t, str(arg.name))
        else:
            return "%s %s"%(t, str(arg.name))

class F2PyCodeWrapper(CodeWrapper):

    @property
    def command(self):
        filename = self.filename + '.' + self.generator.code_extension
        command = ["f2py", "-m", self.module_name, "-c" , filename]
        return command

    def _prepare_files(self, routine):
        pass

    @classmethod
    def _get_wrapped_function(cls, mod):
        return mod.autofunc

def _get_code_wrapper_class(backend):
    wrappers = { 'F2PY': F2PyCodeWrapper, 'CYTHON': CythonCodeWrapper, 'DUMMY': DummyWrapper}
    return wrappers[backend.upper()]

def autowrap(expr, language='F95', backend='f2py', tempdir=None, args=None, flags=[],
        quiet=True, help_routines=[]):
    """Generates python callable binaries based on the math expression.

    expr  --  the SymPy expression that should be wrapped as a binary routine

    Otional arguments:
    language  --  the programming language to use, currently C or F95
    backend  --  the wrapper backend to use, currently f2py or Cython
    tempdir  --  path to directory for temporary files.  If this argument is
                 supplied, the generated code and the wrapper input files are
                 left intact in the specified path.
    args --  sequence of the formal parameters of the generated code, if ommited the
             function signature is determined by the code generator.
    help_routines  --  list defining supportive expressions, e.g. user defined
                       functions that are called by the main expression.  Items should
                       be tuples with (<funtion_name>, <sympy_expression>, <arguments>).

    >>> from sympy.abc import x, y, z
    >>> from sympy.utilities.autowrap import autowrap
    >>> expr = ((x - y + z)**(13)).expand()
    >>> binary_func = autowrap(expr)
    >>> binary_func(1, 4, 2)
    -1.0

    """

    code_generator = get_code_generator(language, "autowrap")
    CodeWrapperClass = _get_code_wrapper_class(backend)
    code_wrapper = CodeWrapperClass(code_generator, tempdir, flags, quiet)
    routine  = Routine('autofunc', expr, args)
    helpers = []
    for name, expr, args in help_routines:
        helpers.append(Routine(name, expr, args))

    return code_wrapper.wrap_code(routine, helpers=helpers)

def binary_function(symfunc, expr, **kwargs):
    """Returns a sympy function with expr as binary implementation

    This is a convenience function that relies on autowrap() and implemented_function()

    >>> from sympy.abc import x, y, z
    >>> from sympy.utilities.autowrap import binary_function
    >>> expr = ((x - y)**(25)).expand()
    >>> f = binary_function('f', expr)
    >>> type(f)
    <class 'sympy.core.function.FunctionClass'>
    >>> 2*f(x, y)
    2*f(x, y)
    >>> f(x, y).evalf(2, subs={x: 1, y: 2})
    -1.0
    """
    binary = autowrap(expr, **kwargs)
    return implemented_function(symfunc, binary)
