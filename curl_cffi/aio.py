import asyncio
import sys
import warnings
from typing import Any
from weakref import WeakSet, WeakKeyDictionary

from ._wrapper import ffi, lib  # type: ignore
from .const import CurlMOpt
from .curl import Curl, DEFAULT_CACERT

__all__ = ["AsyncCurl"]

# registry of asyncio loop : selector thread
_selectors: WeakKeyDictionary = WeakKeyDictionary()
PROACTOR_WARNING = """
Proactor event loop does not implement add_reader family of methods required.
Registering an additional selector thread for add_reader support.
To avoid this warning use:
    asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
"""

def _get_selector_windows(asyncio_loop) -> asyncio.AbstractEventLoop:
    """Get selector-compatible loop

    Returns an object with ``add_reader`` family of methods,
    either the loop itself or a SelectorThread instance.

    Workaround Windows proactor removal of *reader methods.
    """

    if asyncio_loop in _selectors:
        return _selectors[asyncio_loop]

    if not isinstance(asyncio_loop, getattr(asyncio, "ProactorEventLoop", type(None))):
        return asyncio_loop

    from ._asyncio_selector import AddThreadSelectorEventLoop

    warnings.warn(PROACTOR_WARNING, RuntimeWarning)

    selector_loop = _selectors[asyncio_loop] = AddThreadSelectorEventLoop(asyncio_loop)  # type: ignore

    # patch loop.close to also close the selector thread
    loop_close = asyncio_loop.close

    def _close_selector_and_loop():
        # restore original before calling selector.close,
        # which in turn calls eventloop.close!
        asyncio_loop.close = loop_close
        _selectors.pop(asyncio_loop, None)
        selector_loop.close()

    asyncio_loop.close = _close_selector_and_loop  # type: ignore # mypy bug - assign a function to method
    return selector_loop


def _get_selector_noop(loop) -> asyncio.AbstractEventLoop:
    """no-op on non-Windows"""
    return loop


if sys.platform == "win32":
    _get_selector = _get_selector_windows
else:
    _get_selector = _get_selector_noop


CURL_POLL_NONE = 0
CURL_POLL_IN = 1
CURL_POLL_OUT = 2
CURL_POLL_INOUT = 3
CURL_POLL_REMOVE = 4

CURL_SOCKET_TIMEOUT = -1
CURL_SOCKET_BAD = -1

CURL_CSELECT_IN = 0x01
CURL_CSELECT_OUT = 0x02
CURL_CSELECT_ERR = 0x04

CURLMSG_DONE = 1


@ffi.def_extern()
def timer_function(curlm, timeout_ms: int, clientp: Any):
    """
    see: https://curl.se/libcurl/c/CURLMOPT_TIMERFUNCTION.html
    """
    async_curl = ffi.from_handle(clientp)
    # print("time out in %sms" % timeout_ms)
    if timeout_ms == -1:
        for timer in async_curl._timers:
            timer.cancel()
        async_curl._timers = WeakSet()
    else:
        timer = async_curl.loop.call_later(
            timeout_ms / 1000,
            async_curl.process_data,
            CURL_SOCKET_TIMEOUT,  # -1
            CURL_POLL_NONE,  # 0
        )
        async_curl._timers.add(timer)


@ffi.def_extern()
def socket_function(curl, sockfd: int, what: int, clientp: Any, data: Any):
    async_curl = ffi.from_handle(clientp)
    loop = async_curl.loop

    if what & CURL_POLL_IN or what & CURL_POLL_OUT or what & CURL_POLL_REMOVE:
        if sockfd in async_curl._sockfds:
            loop.remove_reader(sockfd)
            loop.remove_writer(sockfd)
            async_curl._sockfds.remove(sockfd)
        elif what & CURL_POLL_REMOVE:
            message = f"File descriptor {sockfd} not found."
            raise TypeError(message)

    if what & CURL_POLL_IN:
        loop.add_reader(sockfd, async_curl.process_data, sockfd, CURL_CSELECT_IN)
        async_curl._sockfds.add(sockfd)
    if what & CURL_POLL_OUT:
        loop.add_writer(sockfd, async_curl.process_data, sockfd, CURL_CSELECT_OUT)
        async_curl._sockfds.add(sockfd)

class AsyncCurl:
    """Wrapper around curl_multi handle to provide asyncio support. It uses the libcurl
    socket_action APIs."""

    def __init__(self, cacert: str = DEFAULT_CACERT, loop=None):
        self._curlm = lib.curl_multi_init()
        self._cacert = cacert
        self._curl2future = {}  # curl to future map
        self._curl2curl = {}  # c curl to Curl
        self._sockfds = set()  # sockfds
        self.loop = _get_selector(
            loop if loop is not None else asyncio.get_running_loop()
        )
        self._checker = self.loop.create_task(self._force_timeout())
        self._timers = WeakSet()
        self._setup()

    def _setup(self):
        self.setopt(CurlMOpt.TIMERFUNCTION, lib.timer_function)
        self.setopt(CurlMOpt.SOCKETFUNCTION, lib.socket_function)
        self._self_handle = ffi.new_handle(self)
        self.setopt(CurlMOpt.SOCKETDATA, self._self_handle)
        self.setopt(CurlMOpt.TIMERDATA, self._self_handle)

    def close(self):
        """Close and cleanup running timers, readers, writers and handles."""
        # Close force timeout checker
        self._checker.cancel()
        # Close all pending futures
        for curl, future in self._curl2future.items():
            lib.curl_multi_remove_handle(self._curlm, curl._curl)
            if not future.done() and not future.cancelled():
                future.set_result(None)
        # Cleanup curl_multi handle
        lib.curl_multi_cleanup(self._curlm)
        self._curlm = None
        # Remove add readers and writers
        for sockfd in self._sockfds:
            self.loop.remove_reader(sockfd)
            self.loop.remove_writer(sockfd)
        # Cancel all time functions
        for timer in self._timers:
            timer.cancel()

    async def _force_timeout(self):
        while True:
            if not self._curlm:
                break
            await asyncio.sleep(1)
            # print("force timeout")
            self.socket_action(CURL_SOCKET_TIMEOUT, CURL_POLL_NONE)

    def add_handle(self, curl: Curl):
        """Add a curl handle to be managed by curl_multi. This is the equivalent of
        `perform` in the async world."""
        # import pdb; pdb.set_trace()
        curl._ensure_cacert()
        lib.curl_multi_add_handle(self._curlm, curl._curl)
        future = self.loop.create_future()
        self._curl2future[curl] = future
        self._curl2curl[curl._curl] = curl
        return future

    def socket_action(self, sockfd: int, ev_bitmask: int) -> int:
        """Call libcurl socket_action function"""
        running_handle = ffi.new("int *")
        lib.curl_multi_socket_action(self._curlm, sockfd, ev_bitmask, running_handle)
        return running_handle[0]

    def process_data(self, sockfd: int, ev_bitmask: int):
        """Call curl_multi_info_read to read data for given socket."""
        if not self._curlm:
            warnings.warn("Curlm alread closed! quitting from process_data")
            return

        self.socket_action(sockfd, ev_bitmask)

        msg_in_queue = ffi.new("int *")
        while True:
            curl_msg = lib.curl_multi_info_read(self._curlm, msg_in_queue)
            # print("message in queue", msg_in_queue[0], curl_msg)
            if curl_msg == ffi.NULL:
                break
            if curl_msg.msg == CURLMSG_DONE:
                # print("curl_message", curl_msg.msg, curl_msg.data.result)
                curl = self._curl2curl[curl_msg.easy_handle]
                retcode = curl_msg.data.result
                if retcode == 0:
                    self.set_result(curl)
                else:
                    # import pdb; pdb.set_trace()
                    self.set_exception(curl, curl._get_error(retcode, "perform"))
            else:
                print("NOT DONE")  # Will not reach, for no other code being defined.

    def _pop_future(self, curl: Curl):
        lib.curl_multi_remove_handle(self._curlm, curl._curl)
        self._curl2curl.pop(curl._curl, None)
        return self._curl2future.pop(curl, None)

    def remove_handle(self, curl: Curl):
        """Cancel a future for given curl handle."""
        future = self._pop_future(curl)
        if future and not future.done() and not future.cancelled():
            future.cancel()

    def set_result(self, curl: Curl):
        """Mark a future as done for given curl handle."""
        future = self._pop_future(curl)
        if future and not future.done() and not future.cancelled():
            future.set_result(None)

    def set_exception(self, curl: Curl, exception):
        """Raise exception of a future for given curl handle."""
        future = self._pop_future(curl)
        if future and not future.done() and not future.cancelled():
            future.set_exception(exception)

    def setopt(self, option, value):
        """Wrapper around curl_multi_setopt."""
        return lib.curl_multi_setopt(self._curlm, option, value)
