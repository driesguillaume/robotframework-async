import threading
from concurrent.futures import ThreadPoolExecutor, wait
from functools import wraps
from .scoped_value import ScopedValue, ScopedDescriptor
from robot.api.logger import librarylogger
from robot.libraries.BuiltIn import BuiltIn
from robot.libraries.DateTime import convert_time
from robot.running import Keyword


def only_run_on_robot_thread(func):
    @wraps(func)
    def inner(*args, **kwargs):
        thread = threading.currentThread().getName()
        if thread not in librarylogger.LOGGING_THREADS:
            return

        return func(*args, **kwargs)

    return inner


class ScopedContext:
    _attributes = [
        ['user_keywords'],
        ['namespace', 'variables', '_scopes'],
        ['namespace', 'variables', '_variables_set', '_scopes'],
        ['in_test_teardown'],
        ['in_keyword_teardown'],
    ]

    _construct = {
        'in_test_teardown': False,
        'in_keyword_teardown': 0,
    }

    def __init__(self):
        self._context = BuiltIn()._get_context()
        self._forks = []
        for a in self._attributes:
            current = self._context
            for p in a:
                parent = current
                current = getattr(parent, p)
            try:
                scope = getattr(parent, f'_scoped_{p}')
            except AttributeError:
                scope = None
            finally:
                if not isinstance(scope, ScopedValue):
                    kwargs = {'default': current}
                    if p in self._construct:
                        kwargs['forkvalue'] = self._construct[p]
                    scope = ScopedValue(**kwargs)
                    setattr(parent, f'_scoped_{p}', scope)
                    delattr(parent, p)

                    class PatchedClass(parent.__class__):
                        pass

                    setattr(PatchedClass, p, ScopedDescriptor(f'_scoped_{p}'))
                    PatchedClass.__name__ = parent.__class__.__name__
                    PatchedClass.__doc__ = parent.__class__.__doc__
                    parent.__class__ = PatchedClass

            self._forks.append(scope.fork())

    def activate(self):
        forks = self._forks

        for a, c in zip(self._attributes, forks):
            current = self._context
            for p in a[0:-1]:
                current = getattr(current, p)
            scope = getattr(current, f'_scoped_{a[-1]}')
            scope.activate(c)

    def kill(self):
        forks = self._forks
        self._forks = []
        for a, c in zip(self._attributes, forks):
            if c is not None:
                current = self._context
                for p in a[0:-1]:
                    current = getattr(current, p)
                scope = getattr(current, f'_scoped_{a[-1]}')
                scope.kill(c)
            self._forks.append(None)

    def __enter__(self):
        self.activate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.kill()


class AsyncLibrary:
    ROBOT_LIBRARY_SCOPE = 'SUITE'
    ROBOT_LISTENER_API_VERSION = 2

    def __init__(self):
        self.ROBOT_LIBRARY_LISTENER = [self]
        self._future = {}
        self._last_thread_handle = 0
        self._executor = ThreadPoolExecutor()
        self._lock = threading.Lock()

        context = BuiltIn()._get_context()
        output = getattr(context, 'output', None)
        xmllogger = getattr(output, '_xmllogger', None)
        writer = getattr(xmllogger, '_writer', None)
        if writer:
            writer.start = only_run_on_robot_thread(writer.start)
            writer.end = only_run_on_robot_thread(writer.end)
            writer.element = only_run_on_robot_thread(writer.element)

    def _run(self, scope, fn, *args, **kwargs):
        with scope:
            return fn(*args, **kwargs)

    def async_run(self, keyword, *args):
        '''
        Executes the provided Robot Framework keyword in a separate thread
        and immediately returns a handle to be used with async_get
        '''
        context = BuiltIn()._get_context()
        runner = context.get_runner(keyword)
        scope = ScopedContext()
        future = self._executor.submit(
            self._run, scope, runner.run, Keyword(keyword, args=args), context
        )
        future._scope = scope

        with self._lock:
            handle = self._last_thread_handle
            self._last_thread_handle += 1
            self._future[handle] = future

        return handle

    def async_get(self, handle, timeout=None):
        '''
        Blocks until the future created by async_run includes a result
        '''
        if timeout:
            timeout = convert_time(timeout, result_format='number')
        try:
            future = self._future.pop(handle)
        except KeyError:
            raise ValueError(f'entry with handle {handle} does not exist')
        return future.result(timeout)

    def async_get_all(self, timeout=None):
        '''
        Blocks until all futures created by async_run include a result
        '''
        if timeout:
            timeout = convert_time(timeout, result_format='number')

        with self._lock:
            future = self._future
            self._future = {}

        futures = list(future.values())

        result = wait(futures, timeout)

        if result.not_done:
            self._future.update({k: v for k, v in futures.items()
                                 if v in result.not_done})
            raise TimeoutError(
                f'{len(result.not_done)} (of {len(futures)}) '
                'futures unfinished'
            )

        for f in result.done:
            f.result()

    def _end_suite(self, suite, attrs):
        self._wait_all()

    def _close(self):
        self._wait_all()

    def _wait_all(self):
        futures = []
        with self._lock:
            for f in self._future.values():
                if f.cancel():
                    f._scope.kill()
                else:
                    futures.append(f)
            self._future = {}

        wait(futures)
