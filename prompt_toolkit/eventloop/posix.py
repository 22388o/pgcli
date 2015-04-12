from __future__ import unicode_literals
import os
import fcntl
import select
import signal
import errno
import threading

from ..terminal.vt100_input import InputStream
from .base import EventLoop, INPUT_TIMEOUT
from .callbacks import EventLoopCallbacks
from .posix_utils import PosixStdinReader

__all__ = (
    'PosixEventLoop',
)


class PosixEventLoop(EventLoop):
    def __init__(self):
        self.running = False
        self.closed = False

        self._calls_from_executor = []

        # Create a pipe for inter thread communication.
        self._schedule_pipe = os.pipe()
        fcntl.fcntl(self._schedule_pipe[0], fcntl.F_SETFL, os.O_NONBLOCK)

        self._running = False

    def run(self, stdin, callbacks):
        """
        The input 'event loop'.
        """
        assert isinstance(callbacks, EventLoopCallbacks)

        if self.closed:
            raise Exception('Event loop already closed.')

        self._running = True

        inputstream = InputStream(callbacks.feed_key)
        timeout = INPUT_TIMEOUT

        # Create reader class.
        stdin_reader = PosixStdinReader(stdin)

        def received_winch():
            """
            (We do it asynchronously, because the handler can write to the
            output, and doing this inside the signal handler causes easily
            reentrant calls, giving runtime errors.)
            """
            self.call_from_executor(callbacks.terminal_size_changed)

        with call_on_sigwinch(received_winch):
            while self._running:
                r, _, _ = _select([stdin, self._schedule_pipe[0]], [], [], timeout)

                # If we got a character, feed it to the input stream. If we got
                # none, it means we got a repaint request.
                if stdin in r:
                    # Feed input text.
                    data = stdin_reader.read()
                    inputstream.feed(data)
                    callbacks.redraw()

                    # Set timeout again.
                    timeout = INPUT_TIMEOUT

                # If we receive something on our "call_from_executor" pipe, process
                # these callbacks in a thread safe way.
                elif self._schedule_pipe[0] in r:
                    # Flush all the pipe content.
                    os.read(self._schedule_pipe[0], 1024)

                    # Process calls from executor.
                    calls_from_executor, self._calls_from_executor = self._calls_from_executor, []
                    for c in calls_from_executor:
                        c()
                else:
                    inputstream.flush()

                    # Fire input timeout event.
                    callbacks.input_timeout()
                    timeout = None

    def run_in_executor(self, callback):
        """
        Run a long running function in a background thread.
        (This is recommended for code that could block the `read_input` event
        loop.)
        Similar to Twisted's ``deferToThread``.
        """
        threading.Thread(target=callback).start()

    def call_from_executor(self, callback):
        """
        Call this function in the main event loop.
        Similar to Twisted's ``callFromThread``.
        """
        self._calls_from_executor.append(callback)

        if self._schedule_pipe:
            os.write(self._schedule_pipe[1], b'x')

    def stop(self):
        self._running = False

    def close(self):
        self.closed = True

        # Close pipes.
        schedule_pipe = self._schedule_pipe
        self._schedule_pipe = None

        if schedule_pipe:
            os.close(schedule_pipe[0])
            os.close(schedule_pipe[1])


def _select(*args, **kwargs):
    """
    Wrapper around select.select.

    When the SIGWINCH signal is handled, other system calls, like select
    are aborted in Python. This wrapper will retry the system call.
    """
    while True:
        try:
            return select.select(*args, **kwargs)
        except select.error as e:
            # Retry select call when EINTR
            if e.args and e.args[0] == errno.EINTR:
                continue
            else:
                raise


class call_on_sigwinch(object):
    """
    Context manager which Installs a SIGWINCH callback.
    (This signal occurs when the terminal size changes.)
    """
    def __init__(self, callback):
        self.callback = callback
        self.previous_callback = None

    def __enter__(self):
        self.previous_callback = signal.signal(signal.SIGWINCH, lambda *a: self.callback())

    def __exit__(self, *a, **kw):
        signal.signal(signal.SIGWINCH, self.previous_callback)
