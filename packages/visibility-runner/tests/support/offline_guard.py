"""Test guard, not an OS security boundary. Installs without importing browser drivers."""
from __future__ import annotations
import importlib.abc
import importlib.machinery
import ipaddress
import os
from pathlib import Path
import socket
import sys
import weakref

_INSTALLED = None
_LISTENERS = weakref.WeakSet()
_LISTENING = weakref.WeakSet()
_NATIVE_DESKTOP_MODULES = frozenset({
    "atomacos", "pyautogui", "Quartz", "AppKit", "ApplicationServices",
})
_BROWSER_TYPE_MODULES = frozenset({
    "playwright._impl._browser_type", "patchright._impl._browser_type",
})

class OfflineViolation(RuntimeError):
    pass

def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return host == "localhost"


def _numeric_loopback(host, family=socket.AF_UNSPEC):
    if host == "localhost":
        return "::1" if family == socket.AF_INET6 else "127.0.0.1"
    if not _loopback(host):
        raise OfflineViolation("offline verification forbids nonloopback name resolution")
    return str(ipaddress.ip_address(host))


def _owned_listener(sock, address):
    if not _loopback(address[0]):
        return False
    target = _numeric_loopback(address[0], sock.family)
    for listener in tuple(_LISTENERS):
        try:
            if listener.fileno() < 0 or listener.family != sock.family or listener.type != sock.type:
                continue
            bound = listener.getsockname()
            if bound[:2] != (target, address[1]):
                continue
            if listener.type == socket.SOCK_STREAM and listener not in _LISTENING:
                continue
            if listener.type in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                return True
        except OSError:
            continue
    return False

def _guard_browser(module):
    async def forbidden(*args, **kwargs):
        raise OfflineViolation("offline verification forbids browser launch or attachment")
    for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
        setattr(module.BrowserType, name, forbidden)

class _BrowserLoader(importlib.abc.Loader):
    def __init__(self, wrapped): self.wrapped = wrapped
    def create_module(self, spec):
        return self.wrapped.create_module(spec) if hasattr(self.wrapped, "create_module") else None
    def exec_module(self, module):
        self.wrapped.exec_module(module)
        _guard_browser(module)

class _BrowserFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _BROWSER_TYPE_MODULES: return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.loader: spec.loader = _BrowserLoader(spec.loader)
        return spec

class _NativeDesktopFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in _NATIVE_DESKTOP_MODULES:
            # Importing PyAutoGUI can initialize its display backend. Stop before
            # executing module code, including direct accessibility/event APIs.
            raise OfflineViolation("offline verification forbids native desktop access: " + fullname)
        return None

def install_guard(tier="fast"):
    global _INSTALLED
    policy = "browser" if tier == "browser" else "fast"
    if any(name.split(".", 1)[0] in _NATIVE_DESKTOP_MODULES for name in sys.modules):
        # Already-imported functions may have escaped by reference; patching the
        # module cannot prove that they are contained. Require a fresh process.
        raise OfflineViolation("native desktop modules were loaded before the offline guard; use a fresh process")
    if _INSTALLED is not None:
        if _INSTALLED != policy: raise OfflineViolation("guard policies require separate processes")
        return
    _INSTALLED = policy
    # Even the isolated-browser and native-process tiers never control the
    # user's desktop. Pure axbridge models remain importable.
    sys.meta_path.insert(0, _NativeDesktopFinder())
    if policy != "browser":
        sys.meta_path.insert(0, _BrowserFinder())
        for name in _BROWSER_TYPE_MODULES:
            loaded = sys.modules.get(name)
            if loaded is not None: _guard_browser(loaded)
    # Resolve only numeric loopback addresses; even create_connection must
    # fail before getaddrinfo can send a DNS query. localhost stays local
    # regardless of the machine's resolver configuration.
    original_getaddrinfo = socket.getaddrinfo
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        host = _numeric_loopback(host, family)
        return original_getaddrinfo(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = lambda host: _numeric_loopback(host, socket.AF_INET)
    socket.gethostbyname_ex = lambda host: ("localhost", [], [_numeric_loopback(host, socket.AF_INET)])
    socket.gethostbyaddr = lambda host: ("localhost", [], [_numeric_loopback(host)])

    # Keep socket identities, not reusable port numbers. Closed sockets and
    # bound-but-not-listening TCP sockets confer no connection permission.
    original_bind = socket.socket.bind
    def bind(sock, address):
        if isinstance(address, tuple) and not _loopback(address[0]):
            raise OfflineViolation("offline verification forbids nonloopback listeners")
        result = original_bind(sock, address)
        if isinstance(address, tuple):
            _LISTENERS.add(sock)
        return result
    socket.socket.bind = bind
    original_listen = socket.socket.listen
    original_shutdown = socket.socket.shutdown
    def listen(sock, *args):
        result = original_listen(sock, *args)
        _LISTENING.add(sock)
        return result
    def shutdown(sock, *args):
        _LISTENING.discard(sock)
        return original_shutdown(sock, *args)
    socket.socket.listen = listen
    socket.socket.shutdown = shutdown
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def validate_address(sock, address):
        if isinstance(address, tuple) and not _owned_listener(sock, address):
            raise OfflineViolation("offline verification forbids network access outside test-owned listeners")
    def connect(sock, address):
        validate_address(sock, address)
        return original_connect(sock, address)
    def connect_ex(sock, address):
        validate_address(sock, address)
        return original_connect_ex(sock, address)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    def audit(event, args):
        if event == "subprocess.Popen":
            executable = os.fsdecode(args[0]).casefold()
            if Path(executable).name == "osascript":
                raise OfflineViolation("offline verification forbids native desktop scripting")
            if policy != "browser" and any(word in executable for word in ("chrome", "chromium", "chromedriver", "osascript")):
                raise OfflineViolation("offline verification forbids browser process startup")
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            validate_address(args[0], args[1])
        if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            _numeric_loopback(args[0])
        if event == "socket.bind" and isinstance(args[1], tuple) and not _loopback(args[1][0]):
            raise OfflineViolation("offline verification forbids nonloopback listeners")
    sys.addaudithook(audit)
    os.environ["VISIBILITY_OFFLINE_GUARD"] = policy

def bootstrap_child(code, *, tier="fast"):
    """Explicit bootstrap works with -I, without sitecustomize or PYTHONPATH."""
    path = str(Path(__file__).resolve())
    return ("import runpy; _guard = runpy.run_path(" + repr(path) + "); "
            "_guard['install_guard'](" + repr(tier) + "); "
            "exec(compile(" + repr(code) + ", '<guarded-child>', 'exec'))")
