from __future__ import print_function, division, absolute_import

import ast
import contextlib
import fnmatch
import importlib
import os
import pathlib
import shlex
import sys
import tempfile
import threading

from typing import Any, Dict

import packaging.version
import pytest

from IPython import get_ipython

from ._config import current_config, default


def run(
    *args,
    module=None,
    plugins=(),
    run_in_thread=default,
    raise_on_error=default,
    addopts=default,
    defopts=default,
    display_columns=default,
):
    """Execute all tests in the passed module (defaults to `__main__`) with pytest.

    **Parameters:**

    - `args`: additional commandline options passed to pytest
    - `module`: the module containing the tests. If not given, `__main__` will
      be used.
    - `plugins`: additional plugins passed to pytest.

    The following parameters override the config options set with
    [`ipytest.config()`][ipytest.config] or
    [`ipytest.autoconfig()`][ipytest.autoconfig].

    - `run_in_thread`: if given, override the config option "run_in_thread".
    - `raise_on_error`: if given, override the config option "raise_on_error".
    - `addopts`: if given, override the config option "addopts".
    - `defopts`: if given, override the config option "defopts".
    - `display_columns`: if given, override the config option "display_columns".

    **Returns**: the exit code of `pytest.main`.
    """
    import ipytest

    run_in_thread = default.unwrap(run_in_thread, current_config["run_in_thread"])
    raise_on_error = default.unwrap(raise_on_error, current_config["raise_on_error"])
    addopts = default.unwrap(addopts, current_config["addopts"])
    defopts = default.unwrap(defopts, current_config["defopts"])
    display_columns = default.unwrap(display_columns, current_config["display_columns"])

    run = run_func_in_thread if run_in_thread else run_func_direct
    exit_code = run(
        _run_impl,
        *args,
        module=module,
        plugins=plugins,
        addopts=addopts,
        defopts=defopts,
        display_columns=display_columns,
    )

    ipytest.exit_code = exit_code

    if raise_on_error is True and exit_code != 0:
        raise Error(exit_code)

    return exit_code


class Error(RuntimeError):
    """Error raised by ipytest on test failure"""

    def __init__(self, exit_code):
        super().__init__(exit_code)

    def __str__(self):
        return f"ipytest failed with exit_code {self.args[0]}"


def pytest_magic(line, cell, module=None):
    """IPython magic to first execute the cell, then execute [`ipytest.run()`][ipytest.run].

    **Note:** the magics are only available after running
    [`ipytest.autoconfig()`][ipytest.autoconfig] or
    [`ipytest.config(magics=True)`][ipytest.config].

    It cleans any previously found tests, i.e., only tests defined in the
    current cell are executed. To disable this behavior, use
    [`ipytest.config(clean=False)`][ipytest.config].

    Any arguments passed on the magic line are interpreted as command line
    arguments to to pytest. For example calling the magic as

    ```python
    %%ipytest -qq
    ```

    is equivalent to passing `-qq` to pytest. See also the section ["How does it
    work"](#how-does-it-work) for further details.

    The keyword arguments passed to [`ipytest.run()`][ipytest.run] can be
    customized by including a comment of the form `# ipytest: arg1=value1,
    arg=value2` in the cell source. For example:

    ```python
    %%ipytest {MODULE}::test1
    # ipytest: defopts=False
    ```

    is equivalent to `ipytest.run("{MODULE}::test1", defopts=False)`. In this
    case, it deactivates default arguments and then instructs pytest to only
    execute `test1`.
    """
    run_args = shlex.split(line)
    run_kwargs = eval_run_kwargs(cell, module=module)

    clean_tests(module=run_kwargs.get("module"))

    try:
        get_ipython().run_cell(cell)

    except TypeError as e:
        if "raw_cell" in str(e):
            raise RuntimeError(
                "The ipytest magic cannot evaluate the cell. Most likely you "
                "are running a modified ipython version. Consider using "
                "`ipytest.run` and `ipytest.clean_tests` directly."
            ) from e

        else:
            raise e

    run(*run_args, **run_kwargs)


def clean_tests(pattern=default, *, module=None):
    """Delete tests with names matching the given pattern.

    In IPython the results of all evaluations are kept in global variables
    unless explicitly deleted. This behavior implies that when tests are renamed
    the previous definitions will still be found if not deleted. This method
    aims to simply this process.

    An effective pattern is to start with the cell containing tests with a call
    to [`ipytest.clean_tests()`][ipytest.clean_tests], then defined all test
    cases, and finally call [`ipytest.run()`][ipytest.run]. This way renaming
    tests works as expected.

    **Parameters:**

    - `pattern`: a glob pattern used to match the tests to delete. If not given,
      the `"clean"` config option is used.
    - `items`: the globals object containing the tests. If `None` is given, the
        globals object is determined from the call stack.
    """
    pattern = default.unwrap(pattern, current_config["clean"])

    if pattern is False:
        return

    if module is None:
        import __main__ as module

    items = vars(module)
    to_delete = [key for key in items.keys() if fnmatch.fnmatchcase(key, pattern)]

    for key in to_delete:
        del items[key]


def reload(*mods):
    """Reload all modules passed as strings.

    This function may be useful, when mixing code in external modules and
    notebooks.

    Usage:

    ```python
    ipytest.reload("ipytest._util", "ipytest")
    ```
    """
    for mod in mods:
        importlib.reload(importlib.import_module(mod))


def _run_impl(*args, module, plugins, addopts, defopts, display_columns):
    with _prepared_env(module, display_columns=display_columns) as filename:
        full_args = _build_full_args(args, filename, addopts=addopts, defopts=defopts)
        return pytest.main(full_args, plugins=[*plugins, FixProgramNamePlugin()])


def _build_full_args(args, filename, *, addopts, defopts):
    def _fmt(arg):
        return arg.format(MODULE=filename)

    return [
        *(_fmt(arg) for arg in addopts),
        *(_fmt(arg) for arg in args),
        *([filename] if defopts else []),
    ]


@contextlib.contextmanager
def _prepared_env(module, *, display_columns):
    if module is None:  # pragma: no cover
        import __main__ as module

    with tempfile.NamedTemporaryFile(dir=".", suffix=".py") as f:
        path = pathlib.Path(f.name)
        module_name = path.stem

        if not is_valid_module_name(module_name):
            raise RuntimeError(
                f"Invalid module name {module_name!r} generated by tempfile. "
                "This should not happen, please open an issue at "
                "'https://github.com/chmp/ipytest/issues' to report a bug."
            )

        if module_name in sys.modules:
            raise RuntimeError(
                f"Cannot register module with name {module_name!r}. It would "
                "override an existing module. This should not happen. Please "
                "report a bug at 'https://github.com/chmp/ipytest/issues'."
            )

        with patch(module, "__file__", str(path)):
            with register_module(module, module_name):
                with patched_columns(display_columns=display_columns):
                    yield str(path)


class RewriteAssertTransformer(ast.NodeTransformer):
    def register_with_shell(self, shell):
        shell.ast_transformers.append(self)

    def unregister_with_shell(self, shell):
        shell.ast_transformers[:] = [
            transformer
            for transformer in shell.ast_transformers
            if transformer is not self
        ]

    def visit(self, node):
        from _pytest.assertion.rewrite import rewrite_asserts

        pytest_version = get_pytest_version()
        if pytest_version.release[0] >= 5:
            # TODO: re-create a pseudo code to include the asserts?
            rewrite_asserts(node, b"")

        else:
            rewrite_asserts(node)
        return node


class FixProgramNamePlugin:
    def pytest_addoption(self, parser):
        # Explanation:
        #
        # - the prog instance variable is defined, but never overwritten [1]
        # - this variable is passed to the the underlying argparse Parser [2]
        #   via [3]
        # - with a `None` value argparse uses sys.argv array to determine the
        #   program name
        #
        # [1]: https://github.com/pytest-dev/pytest/blob/6d6bc97231f2d9a68002f1d191828fd3476ca8b8/src/_pytest/config/argparsing.py#L41
        # [2]: https://github.com/pytest-dev/pytest/blob/6d6bc97231f2d9a68002f1d191828fd3476ca8b8/src/_pytest/config/argparsing.py#L397
        # [3]: https://github.com/pytest-dev/pytest/blob/6d6bc97231f2d9a68002f1d191828fd3476ca8b8/src/_pytest/config/argparsing.py#L119
        #
        parser.prog = "%%ipytest"


def get_pytest_version():
    return packaging.version.parse(pytest.__version__)


@contextlib.contextmanager
def patch(obj, attr, val):
    had_attr = hasattr(obj, attr)
    prev_val = getattr(obj, attr, None)

    setattr(obj, attr, val)

    try:
        yield

    finally:
        if not had_attr:
            delattr(obj, attr)

        else:
            setattr(obj, attr, prev_val)


@contextlib.contextmanager
def register_module(obj, name):
    if name in sys.modules:
        raise RuntimeError(f"Cannot overwrite existing module {name}")

    sys.modules[name] = obj
    try:
        yield

    finally:
        del sys.modules[name]


@contextlib.contextmanager
def patched_columns(*, display_columns):
    if not display_columns:
        yield
        return

    # NOTE: since values have to be strings, None identifies unset values
    prev_columns = os.environ.get("COLUMNS")

    os.environ["COLUMNS"] = str(display_columns)
    yield

    if prev_columns is not None:
        os.environ["COLUMNS"] = prev_columns

    else:
        del os.environ["COLUMNS"]


def run_func_direct(func, *args, **kwargs):
    return func(*args, **kwargs)


def run_func_in_thread(func, *args, **kwargs):
    res = None

    def _thread():
        nonlocal res
        res = func(*args, **kwargs)

    t = threading.Thread(target=_thread)
    t.start()
    t.join()

    return res


def is_valid_module_name(name):
    return all(c not in name for c in ".- ")


RUN_OPTIONS_MARKER = "# ipytest:"


def eval_run_kwargs(cell: str, module=None) -> Dict[str, Any]:
    """Parse the `ipytest:` comment inside a cell

    If the cell does not start with `# ipytest:` and empty dict is returned.
    Otherwise, the rest of the line is evaluated as keyword args. Any references
    to variables are evaluated to the module object. If not given it defaults to
    `__main__`.

    If the module is given and not overwritten inside the comment, it is also
    returned as keyword argument.
    """
    if module is not None:
        eval_module = module

    else:
        import __main__ as eval_module

    lines = cell.splitlines()
    if not lines:
        return {}

    first_line = lines[0]
    if not first_line.startswith(RUN_OPTIONS_MARKER):
        return {}

    run_options = first_line[len(RUN_OPTIONS_MARKER) :]
    kwargs = eval(f"dict({run_options!s})", eval_module.__dict__, eval_module.__dict__)

    if "module" not in kwargs and module is not None:
        kwargs["module"] = module

    return kwargs
