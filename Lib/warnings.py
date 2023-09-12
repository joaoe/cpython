"""Python part of the warnings subsystem."""

import contextvars
import threading
import sys


__all__ = ["warn", "warn_explicit", "showwarning",
           "formatwarning", "filterwarnings", "simplefilter",
           "resetwarnings", "catch_warnings"]

def showwarning(message, category, filename, lineno, file=None, line=None):
    """Hook to write a warning to a file; replace if you like."""
    msg = WarningMessage(message, category, filename, lineno, file, line)
    if _catch_message_global(msg):
        return
    _showwarnmsg_impl(msg)

def formatwarning(message, category, filename, lineno, line=None):
    """Function to format a warning the standard way."""
    msg = WarningMessage(message, category, filename, lineno, None, line)
    return _formatwarnmsg_impl(msg)

def _showwarnmsg_impl(msg):
    file = msg.file
    if file is None:
        file = sys.stderr
        if file is None:
            # sys.stderr is None when run with pythonw.exe:
            # warnings get lost
            return
    text = _formatwarnmsg(msg)
    try:
        file.write(text)
    except OSError:
        # the file (probably stderr) is invalid - this warning gets lost.
        pass

def _formatwarnmsg_impl(msg):
    category = msg.category.__name__
    s =  f"{msg.filename}:{msg.lineno}: {category}: {msg.message}\n"

    if msg.line is None:
        try:
            import linecache
            line = linecache.getline(msg.filename, msg.lineno)
        except Exception:
            # When a warning is logged during Python shutdown, linecache
            # and the import machinery don't work anymore
            line = None
            linecache = None
    else:
        line = msg.line
    if line:
        line = line.strip()
        s += "  %s\n" % line

    if msg.source is not None:
        try:
            import tracemalloc
        # Logging a warning should not raise a new exception:
        # catch Exception, not only ImportError and RecursionError.
        except Exception:
            # don't suggest to enable tracemalloc if it's not available
            tracing = True
            tb = None
        else:
            tracing = tracemalloc.is_tracing()
            try:
                tb = tracemalloc.get_object_traceback(msg.source)
            except Exception:
                # When a warning is logged during Python shutdown, tracemalloc
                # and the import machinery don't work anymore
                tb = None

        if tb is not None:
            s += 'Object allocated at (most recent call last):\n'
            for frame in tb:
                s += ('  File "%s", lineno %s\n'
                      % (frame.filename, frame.lineno))

                try:
                    if linecache is not None:
                        line = linecache.getline(frame.filename, frame.lineno)
                    else:
                        line = None
                except Exception:
                    line = None
                if line:
                    line = line.strip()
                    s += '    %s\n' % line
        elif not tracing:
            s += (f'{category}: Enable tracemalloc to get the object '
                  f'allocation traceback\n')
    return s

# Keep a reference to check if the function was replaced
_showwarning_orig = showwarning

def _showwarnmsg(msg):
    """Hook to write a warning to a file; replace if you like."""
    if _catch_message_global(msg):
        return
    try:
        sw = showwarning
    except NameError:
        pass
    else:
        if sw is not _showwarning_orig:
            # warnings.showwarning() was replaced
            if not callable(sw):
                raise TypeError("warnings.showwarning() must be set to a "
                                "function or method")

            sw(msg.message, msg.category, msg.filename, msg.lineno,
               msg.file, msg.line)
            return
    _showwarnmsg_impl(msg)

# Keep a reference to check if the function was replaced
_formatwarning_orig = formatwarning

def _formatwarnmsg(msg):
    """Function to format a warning the standard way."""
    try:
        fw = formatwarning
    except NameError:
        pass
    else:
        if fw is not _formatwarning_orig:
            # warnings.formatwarning() was replaced
            return fw(msg.message, msg.category,
                      msg.filename, msg.lineno, msg.line)
    return _formatwarnmsg_impl(msg)

def filterwarnings(action, message="", category=Warning, module="", lineno=0,
                   append=False):
    """Insert an entry into the list of warnings filters (at the front).

    'action' -- one of "error", "ignore", "always", "default", "module",
                or "once"
    'message' -- a regex that the warning message must match
    'category' -- a class that the warning must be a subclass of
    'module' -- a regex that the module name must match
    'lineno' -- an integer line number, 0 matches all warnings
    'append' -- if true, append to the list of filters
    """
    assert action in ("error", "ignore", "always", "default", "module",
                      "once"), "invalid action: %r" % (action,)
    assert isinstance(message, str), "message must be a string"
    assert isinstance(category, type), "category must be a class"
    assert issubclass(category, Warning), "category must be a Warning subclass"
    assert isinstance(module, str), "module must be a string"
    assert isinstance(lineno, int) and lineno >= 0, \
           "lineno must be an int >= 0"

    if message or module:
        import re

    if message:
        message = re.compile(message, re.I)
    else:
        message = None
    if module:
        module = re.compile(module)
    else:
        module = None

    _add_filter(action, message, category, module, lineno, append=append)

def simplefilter(action, category=Warning, lineno=0, append=False):
    """Insert a simple entry into the list of warnings filters (at the front).

    A simple filter matches all modules and messages.
    'action' -- one of "error", "ignore", "always", "default", "module",
                or "once"
    'category' -- a class that the warning must be a subclass of
    'lineno' -- an integer line number, 0 matches all warnings
    'append' -- if true, append to the list of filters
    """
    assert action in ("error", "ignore", "always", "default", "module",
                      "once"), "invalid action: %r" % (action,)
    assert isinstance(lineno, int) and lineno >= 0, \
           f"lineno {lineno} must be an int >= 0"
    _add_filter(action, None, category, None, lineno, append=append)


class ProtectedListGlobal:
    def __init__(self, obj):
        self.lock = threading.RLock()
        self.value = obj

    def __enter__(self):
        self.lock.__enter__()
        return self.value

    def __exit__(self, *a):
        self.lock.__exit__(*a)


class ProtectedListLocal:
    def __init__(self, name, factory):
        self.factory = factory
        self.value = contextvars.ContextVar(name, default=None)

    def __enter__(self):
        if (stack := self.value.get()) is None:
            self.value.set(stack := self.factory())
            assert stack is not None
        return stack

    def __exit__(self, *a):
        pass

def _add_filter(*item, append):
    # Remove possible duplicate filters, so new one will be placed
    # in correct place. If append=True and duplicate exists, do nothing.
    filters = get_filters()
    if not append:
        try:
            filters.remove(item)
        except ValueError:
            pass
        filters.insert(0, item)
    else:
        if item not in filters:
            filters.append(item)
    _filters_mutated()

def resetwarnings():
    """Clear the list of warning filters, so that no filters are active."""
    get_filters()[:] = []
    _filters_mutated()

class _OptionError(Exception):
    """Exception used by option processing helpers."""
    pass

# Helper to process -W options passed via sys.warnoptions
def _processoptions(args):
    for arg in args:
        try:
            _setoption(arg)
        except _OptionError as msg:
            print("Invalid -W option ignored:", msg, file=sys.stderr)

# Helper for _processoptions()
def _setoption(arg):
    parts = arg.split(':')
    if len(parts) > 5:
        raise _OptionError("too many fields (max 5): %r" % (arg,))
    while len(parts) < 5:
        parts.append('')
    action, message, category, module, lineno = [s.strip()
                                                 for s in parts]
    action = _getaction(action)
    category = _getcategory(category)
    if message or module:
        import re
    if message:
        message = re.escape(message)
    if module:
        module = re.escape(module) + r'\Z'
    if lineno:
        try:
            lineno = int(lineno)
            if lineno < 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise _OptionError("invalid lineno %r" % (lineno,)) from None
    else:
        lineno = 0
    filterwarnings(action, message, category, module, lineno)

# Helper for _setoption()
def _getaction(action):
    if not action:
        return "default"
    if action == "all": return "always" # Alias
    for a in ('default', 'always', 'ignore', 'module', 'once', 'error'):
        if a.startswith(action):
            return a
    raise _OptionError("invalid action: %r" % (action,))

# Helper for _setoption()
def _getcategory(category):
    if not category:
        return Warning
    if '.' not in category:
        import builtins as m
        klass = category
    else:
        module, _, klass = category.rpartition('.')
        try:
            m = __import__(module, None, None, [klass])
        except ImportError:
            raise _OptionError("invalid module name: %r" % (module,)) from None
    try:
        cat = getattr(m, klass)
    except AttributeError:
        raise _OptionError("unknown warning category: %r" % (category,)) from None
    if not issubclass(cat, Warning):
        raise _OptionError("invalid warning category: %r" % (category,))
    return cat


def _is_internal_filename(filename):
    return 'importlib' in filename and '_bootstrap' in filename


def _is_filename_to_skip(filename, skip_file_prefixes):
    return any(filename.startswith(prefix) for prefix in skip_file_prefixes)


def _is_internal_frame(frame):
    """Signal whether the frame is an internal CPython implementation detail."""
    return _is_internal_filename(frame.f_code.co_filename)


def _next_external_frame(frame, skip_file_prefixes):
    """Find the next frame that doesn't involve Python or user internals."""
    frame = frame.f_back
    while frame is not None and (
            _is_internal_filename(filename := frame.f_code.co_filename) or
            _is_filename_to_skip(filename, skip_file_prefixes)):
        frame = frame.f_back
    return frame


# Code typically replaced by _warnings
def warn(message, category=None, stacklevel=1, source=None,
         *, skip_file_prefixes=()):
    """Issue a warning, or maybe ignore it or raise an exception."""
    # Check if message is already a Warning object
    if isinstance(message, Warning):
        category = message.__class__
    # Check category argument
    if category is None:
        category = UserWarning
    if not (isinstance(category, type) and issubclass(category, Warning)):
        raise TypeError("category must be a Warning subclass, "
                        "not '{:s}'".format(type(category).__name__))
    if not isinstance(skip_file_prefixes, tuple):
        # The C version demands a tuple for implementation performance.
        raise TypeError('skip_file_prefixes must be a tuple of strs.')
    if skip_file_prefixes:
        stacklevel = max(2, stacklevel)
    # Get context information
    try:
        if stacklevel <= 1 or _is_internal_frame(sys._getframe(1)):
            # If frame is too small to care or if the warning originated in
            # internal code, then do not try to hide any frames.
            frame = sys._getframe(stacklevel)
        else:
            frame = sys._getframe(1)
            # Look for one frame less since the above line starts us off.
            for x in range(stacklevel-1):
                frame = _next_external_frame(frame, skip_file_prefixes)
                if frame is None:
                    raise ValueError
    except ValueError:
        globals = sys.__dict__
        filename = "sys"
        lineno = 1
    else:
        globals = frame.f_globals
        filename = frame.f_code.co_filename
        lineno = frame.f_lineno
    if '__name__' in globals:
        module = globals['__name__']
    else:
        module = "<string>"
    registry = globals.setdefault("__warningregistry__", {})
    warn_explicit(message, category, filename, lineno, module, registry,
                  globals, source)

def warn_explicit(message, category, filename, lineno,
                  module=None, registry=None, module_globals=None,
                  source=None):
    lineno = int(lineno)
    if module is None:
        module = filename or "<unknown>"
        if module[-3:].lower() == ".py":
            module = module[:-3] # XXX What about leading pathname?
    if registry is None:
        registry = {}
    if registry.get('version', 0) != _filters_version:
        registry.clear()
        registry['version'] = _filters_version
    if isinstance(message, Warning):
        text = str(message)
        category = message.__class__
    else:
        text = message
        message = category(message)
    key = (text, category, lineno)
    # Quick test for common case
    if registry.get(key):
        return
    # Search the filters
    for item in get_filters():
        action, msg, cat, mod, ln = item
        if ((msg is None or msg.match(text)) and
            issubclass(category, cat) and
            (mod is None or mod.match(module)) and
            (ln == 0 or lineno == ln)):
            break
    else:
        action = defaultaction
    # Early exit actions
    if action == "ignore":
        return

    # Prime the linecache for formatting, in case the
    # "file" is actually in a zipfile or something.
    import linecache
    linecache.getlines(filename, module_globals)

    if action == "error":
        raise message
    # Other actions
    if action == "once":
        registry[key] = 1
        oncekey = (text, category)
        if onceregistry.get(oncekey):
            return
        onceregistry[oncekey] = 1
    elif action == "always":
        pass
    elif action == "module":
        registry[key] = 1
        altkey = (text, category, 0)
        if registry.get(altkey):
            return
        registry[altkey] = 1
    elif action == "default":
        registry[key] = 1
    else:
        # Unrecognized actions are errors
        raise RuntimeError(
              "Unrecognized action (%r) in warnings.filters:\n %s" %
              (action, item))
    # Print message and context
    msg = WarningMessage(message, category, filename, lineno, source)
    _showwarnmsg(msg)


class WarningMessage(object):

    _WARNING_DETAILS = ("message", "category", "filename", "lineno", "file",
                        "line", "source")

    def __init__(self, message, category, filename, lineno, file=None,
                 line=None, source=None):
        self.message = message
        self.category = category
        self.filename = filename
        self.lineno = lineno
        self.file = file
        self.line = line
        self.source = source
        self._category_name = category.__name__ if category else None

    def __str__(self):
        return ("{message : %r, category : %r, filename : %r, lineno : %s, "
                    "line : %r}" % (self.message, self._category_name,
                                    self.filename, self.lineno, self.line))


class catch_warnings(object):

    """A context manager that copies and restores the warnings filter upon
    exiting the context.

    The 'record' argument specifies whether warnings should be captured and be
    appended to a list returned by the context manager. Otherwise 'None' is
    returned by the context manager. The objects appended to the list are of the
    type 'warnings.WarningMessage'.

    The 'module' argument is to specify an alternative module to the module
    named 'warnings' and imported under that name. This argument is only useful
    when testing the 'warnings' module itself.

    If the 'action' argument is not 'None', the remaining arguments are passed
    to 'warnings.simplefilter()' as if it were called immediately on entering
    the context.

    Multiple 'catch_warnings' can be created and enabled or entered
    simultaneously in the same call chain. When a 'catch_warnings' is enabled
    it goes to the top of a stack. The top-most 'catch_warnings' in the stack,
    the latest one that was activated, will be the first to receive warnings
    and may choose to handle the warning or ignore it so it is passed to the
    next in the stack.
    """

    GLOBAL_STACK = ProtectedListGlobal([])
    LOCAL_STACK = ProtectedListLocal("catch_warnings_stack", list)

    def __init__(self, *, record=False, module=None,
                 action=None, category=Warning, lineno=0, append=False,
                 local=False):
        """Specify whether to record warnings and if an alternative module
        should be used other than sys.modules['warnings'].

        For compatibility with Python 3.0, please consider all arguments to be
        keyword-only.

        Args:
            ...
            `local` controls whether to catch warnings for the local call stack
            or globally. `local=False` is most useful for generic trap all
            libraries like unit test suites and logging libraries, which most
            likely want to catch all warnings globally. This is the historical
            default behavior in Python. `local=True` is the new behavior that
            traps warnings only for the current call stack, e.g., when running
            inside an asyncio task or thread.
        """
        self._record = record
        self._module = sys.modules[__name__] if module is None else module
        self._entered = False
        self._filters = None
        self._local = local
        self._stack = self.LOCAL_STACK if local else self.GLOBAL_STACK
        self._log = None
        if action is None:
            self._filter = None
        else:
            self._filter = (action, category, lineno, append)

    def __repr__(self):
        args = []
        if self._record:
            args.append("record=True")
        if self._module is not sys.modules[__name__]:
            args.append("module=%r" % self._module)
        name = type(self).__name__
        return "%s(%s)" % (name, ", ".join(args))

    def is_enabled(self):
        """Tells if this 'catch_warnings' is currently enabled.

        'catch_warnings' can be enabled in a 'with' block or by calling
        '.enable()'. 'catch_warnings' can be disabled when exiting a 'with'
        block or by calling '.disable()'.

        The return value of this method is not affected by 'record=True' or
        'record=False'.
        """
        return self._entered

    def toggle(self, value):
        """Calls '.enable()' if 'value' is true else calls '.disable()'."""
        if value:
            return self.enable()
        else:
            return self.disable()

    def __enter__(self):
        """Enables this 'catch_warnings' with a ' with' block.

        Returns a 'list' of strings, initially empty, which will be filled with
        all catched warnings.
        """
        if self.is_enabled():
            raise RuntimeError("Cannot enter %r twice" % self)
        return self.enable()

    def enable(self):
        """Enables this 'catch_warnings' to receive warnings.

        If this object was created with 'record=False', this methods sets up local
        filters and returns.

        If this object was created with 'record=True', the newly enabled
        'catch_warnings' is pushed to the top of the 'catch_warnings' stack.
        When receiving warnings, the stack is traversed from top to bottom,
        until the message is consumed. If this 'catch_warnings' is enabled, this
        call is a no-op.
        """

        if self.is_enabled():
            return self._log

        with self._stack as stack:
            assert self not in stack  # Ensured by self.is_enabled() check
            stack.append(self)

            self._entered = True
            self._filters = list(get_filters())
            if self._filter is not None:
                simplefilter(*self._filter)
            if self._record:
                self._log = []

        return self._log

    def __exit__(self, *exc_info):
        if not self.is_enabled():
            raise RuntimeError("Cannot exit %r without entering first" % self)
        self.disable()

    def disable(self):
        """Disables this 'catch_warnings' so it won't receive warnings nor
        filter messages.

        This 'catch_warnings' is removed from the stack, even if it is not at
        the top. If this 'catch_warnings' is disabled, this call is a no-op.
        """

        if not self.is_enabled():
            return

        with self._stack as stack:
            self._log = None
            self._filters = None
            self._entered = False
            self._module._filters_mutated()

            assert stack and self in stack
            stack.remove(self)

    def log(self, message):
        """Method that catches one message.

        If this object is enabled and was created with 'record=True', the
        'message' is appended to the '.messages' list and the method returns
        'True'. Else it returns 'False' to tell the message was not captured.
        """
        if self.is_enabled() and self._log is not None:
            self._log.append(message)
            return True
        return False

    @classmethod
    def acquire_stacks(cls):
        for stack_lock in (cls.LOCAL_STACK, cls.GLOBAL_STACK):
            with stack_lock as stack:
                yield stack

    messages = property(lambda self: self._log)
    """Returns list with captured messages, if this object is enabled and was
    created with 'record=True'. Else returns 'None'."""


def _catch_message_global(message) -> bool:
    for stack in catch_warnings.acquire_stacks():
        for catcher in reversed(stack):
            if catcher.log(message):
                return True
    return False


def get_filters():
    for stack in catch_warnings.acquire_stacks():
        for catcher in reversed(stack):
            if catcher.is_enabled() and catcher._filters is not None:
                return catcher._filters

    return filters  # The global


_DEPRECATED_MSG = "{name!r} is deprecated and slated for removal in Python {remove}"

def _deprecated(name, message=_DEPRECATED_MSG, *, remove, _version=sys.version_info):
    """Warn that *name* is deprecated or should be removed.

    RuntimeError is raised if *remove* specifies a major/minor tuple older than
    the current Python version or the same version but past the alpha.

    The *message* argument is formatted with *name* and *remove* as a Python
    version tuple (e.g. (3, 11)).

    """
    remove_formatted = f"{remove[0]}.{remove[1]}"
    if (_version[:2] > remove) or (_version[:2] == remove and _version[3] != "alpha"):
        msg = f"{name!r} was slated for removal after Python {remove_formatted} alpha"
        raise RuntimeError(msg)
    else:
        msg = message.format(name=name, remove=remove_formatted)
        warn(msg, DeprecationWarning, stacklevel=3)


# Private utility function called by _PyErr_WarnUnawaitedCoroutine
def _warn_unawaited_coroutine(coro):
    msg_lines = [
        f"coroutine '{coro.__qualname__}' was never awaited\n"
    ]
    if coro.cr_origin is not None:
        import linecache, traceback
        def extract():
            for filename, lineno, funcname in reversed(coro.cr_origin):
                line = linecache.getline(filename, lineno)
                yield (filename, lineno, funcname, line)
        msg_lines.append("Coroutine created at (most recent call last)\n")
        msg_lines += traceback.format_list(list(extract()))
    msg = "".join(msg_lines).rstrip("\n")
    # Passing source= here means that if the user happens to have tracemalloc
    # enabled and tracking where the coroutine was created, the warning will
    # contain that traceback. This does mean that if they have *both*
    # coroutine origin tracking *and* tracemalloc enabled, they'll get two
    # partially-redundant tracebacks. If we wanted to be clever we could
    # probably detect this case and avoid it, but for now we don't bother.
    warn(msg, category=RuntimeWarning, stacklevel=2, source=coro)


# filters contains a sequence of filter 5-tuples
# The components of the 5-tuple are:
# - an action: error, ignore, always, default, module, or once
# - a compiled regex that must match the warning message
# - a class representing the warning category
# - a compiled regex that must match the module that is being warned
# - a line number for the line being warning, or 0 to mean any line
# If either if the compiled regexs are None, match anything.
try:
    from _warnings import (filters, _defaultaction, _onceregistry,
                           warn, warn_explicit, _filters_mutated)
    defaultaction = _defaultaction
    onceregistry = _onceregistry
    _warnings_defaults = True
except ImportError:
    filters = []
    defaultaction = "default"
    onceregistry = {}

    _filters_version = 1

    def _filters_mutated():
        global _filters_version
        _filters_version += 1

    _warnings_defaults = False


# Module initialization
_processoptions(sys.warnoptions)
if not _warnings_defaults:
    # Several warning categories are ignored by default in regular builds
    if not hasattr(sys, 'gettotalrefcount'):
        filterwarnings("default", category=DeprecationWarning,
                       module="__main__", append=1)
        simplefilter("ignore", category=DeprecationWarning, append=1)
        simplefilter("ignore", category=PendingDeprecationWarning, append=1)
        simplefilter("ignore", category=ImportWarning, append=1)
        simplefilter("ignore", category=ResourceWarning, append=1)

del _warnings_defaults

import types

class Protect(types.ModuleType):
    module = sys.modules[__name__]

    def __init__(self):
        super().__init__(__name__)

    def __getattribute__(self, item):
        return getattr(Protect.module, item)

    def __setattr__(self, key, value):
        if key in ("filters", "showwarning", "_showwarning_orig", "_showwarnmsg", "_showwarnmsg_impl", "warn", "warn_explicit"):
            raise AttributeError(f"Read-only attribute {key}")
        return setattr(Protect.module, key, value)

    def __delattr__(self, key):
        raise AttributeError(f"Read-only attribute {key}")


sys.modules[__name__] = Protect()
