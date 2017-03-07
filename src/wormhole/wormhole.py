from __future__ import print_function, absolute_import, unicode_literals
import os, sys
from attr import attrs, attrib
from zope.interface import implementer
from twisted.python import failure
from twisted.internet import defer
from ._interfaces import IWormhole
from .util import bytes_to_hexstr
from .timing import DebugTiming
from .journal import ImmediateJournal
from ._boss import Boss
from ._key import derive_key
from .errors import WelcomeError, NoKeyError, WormholeClosed
from .util import to_bytes

# We can provide different APIs to different apps:
# * Deferreds
#   w.when_code().addCallback(print_code)
#   w.send(data)
#   w.when_received().addCallback(got_data)
#   w.close().addCallback(closed)

# * delegate callbacks (better for journaled environments)
#   w = wormhole(delegate=app)
#   w.send(data)
#   app.wormhole_got_code(code)
#   app.wormhole_got_verifier(verifier)
#   app.wormhole_got_version(version)
#   app.wormhole_receive(data)
#   w.close()
#   app.wormhole_closed()
#
# * potential delegate options
#   wormhole(delegate=app, delegate_prefix="wormhole_",
#            delegate_args=(args, kwargs))

def _log(client_name, machine_name, old_state, input, new_state):
    print("%s.%s[%s].%s -> [%s]" % (client_name, machine_name,
                                    old_state, input, new_state))

class _WelcomeHandler:
    def __init__(self, url, current_version, signal_error):
        self._ws_url = url
        self._version_warning_displayed = False
        self._current_version = current_version
        self._signal_error = signal_error

    def handle_welcome(self, welcome):
        if "motd" in welcome:
            motd_lines = welcome["motd"].splitlines()
            motd_formatted = "\n ".join(motd_lines)
            print("Server (at %s) says:\n %s" %
                  (self._ws_url, motd_formatted), file=sys.stderr)

        # Only warn if we're running a release version (e.g. 0.0.6, not
        # 0.0.6-DISTANCE-gHASH). Only warn once.
        if ("current_cli_version" in welcome
            and "-" not in self._current_version
            and not self._version_warning_displayed
            and welcome["current_cli_version"] != self._current_version):
            print("Warning: errors may occur unless both sides are running the same version", file=sys.stderr)
            print("Server claims %s is current, but ours is %s"
                  % (welcome["current_cli_version"], self._current_version),
                  file=sys.stderr)
            self._version_warning_displayed = True

        if "error" in welcome:
            return self._signal_error(WelcomeError(welcome["error"]),
                                      "unwelcome")

@attrs
@implementer(IWormhole)
class _DelegatedWormhole(object):
    _delegate = attrib()

    def __attrs_post_init__(self):
        self._key = None

    def _set_boss(self, boss):
        self._boss = boss

    # from above

    def allocate_code(self, code_length=2):
        self._boss.allocate_code(code_length)
    def input_code(self, stdio):
        self._boss.input_code(stdio)
    def set_code(self, code):
        self._boss.set_code(code)

    def serialize(self):
        s = {"serialized_wormhole_version": 1,
             "boss": self._boss.serialize(),
             }
        return s

    def send(self, plaintext):
        self._boss.send(plaintext)

    def derive_key(self, purpose, length):
        """Derive a new key from the established wormhole channel for some
        other purpose. This is a deterministic randomized function of the
        session key and the 'purpose' string (unicode/py3-string). This
        cannot be called until when_verifier() has fired, nor after close()
        was called.
        """
        if not isinstance(purpose, type("")): raise TypeError(type(purpose))
        if not self._key: raise NoKeyError()
        return derive_key(self._key, to_bytes(purpose), length)

    def close(self):
        self._boss.close()

    def debug_set_trace(self, client_name, which="B N M S O K R RC NL C T",
                           logger=_log):
        self._boss.set_trace(client_name, which, logger)

    # from below
    def got_code(self, code):
        self._delegate.wormhole_got_code(code)
    def got_welcome(self, welcome):
        pass # TODO
    def got_key(self, key):
        self._key = key # for derive_key()
    def got_verifier(self, verifier):
        self._delegate.wormhole_got_verifier(verifier)
    def got_version(self, version):
        self._delegate.wormhole_got_version(version)
    def received(self, plaintext):
        self._delegate.wormhole_received(plaintext)
    def closed(self, result):
        self._delegate.wormhole_closed(result)

@implementer(IWormhole)
class _DeferredWormhole(object):
    def __init__(self):
        self._code = None
        self._code_observers = []
        self._key = None
        self._verifier = None
        self._verifier_observers = []
        self._version = None
        self._version_observers = []
        self._received_data = []
        self._received_observers = []
        self._observer_result = None
        self._closed_result = None
        self._closed_observers = []

    def _set_boss(self, boss):
        self._boss = boss

    # from above
    def when_code(self):
        # TODO: consider throwing error unless one of allocate/set/input_code
        # was called first
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._code is not None:
            return defer.succeed(self._code)
        d = defer.Deferred()
        self._code_observers.append(d)
        return d

    def when_verifier(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._verifier is not None:
            return defer.succeed(self._verifier)
        d = defer.Deferred()
        self._verifier_observers.append(d)
        return d

    def when_version(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._version is not None:
            return defer.succeed(self._version)
        d = defer.Deferred()
        self._version_observers.append(d)
        return d

    def when_received(self):
        if self._observer_result is not None:
            return defer.fail(self._observer_result)
        if self._received_data:
            return defer.succeed(self._received_data.pop(0))
        d = defer.Deferred()
        self._received_observers.append(d)
        return d

    def allocate_code(self, code_length=2):
        self._boss.allocate_code(code_length)
    def input_code(self, stdio): # TODO
        self._boss.input_code(stdio)
    def set_code(self, code):
        self._boss.set_code(code)

    # no .serialize in Deferred-mode
    def send(self, plaintext):
        self._boss.send(plaintext)

    def derive_key(self, purpose, length):
        """Derive a new key from the established wormhole channel for some
        other purpose. This is a deterministic randomized function of the
        session key and the 'purpose' string (unicode/py3-string). This
        cannot be called until when_verifier() has fired, nor after close()
        was called.
        """
        if not isinstance(purpose, type("")): raise TypeError(type(purpose))
        if not self._key: raise NoKeyError()
        return derive_key(self._key, to_bytes(purpose), length)

    def close(self):
        # fails with WormholeError unless we established a connection
        # (state=="happy"). Fails with WrongPasswordError (a subclass of
        # WormholeError) if state=="scary".
        if self._closed_result:
            return defer.succeed(self._closed_result) # maybe Failure
        d = defer.Deferred()
        self._closed_observers.append(d)
        self._boss.close() # only need to close if it wasn't already
        return d

    def debug_set_trace(self, client_name, which="B N M S O K R RC L C T",
                           logger=_log):
        self._boss._set_trace(client_name, which, logger)

    # from below
    def got_code(self, code):
        self._code = code
        for d in self._code_observers:
            d.callback(code)
        self._code_observers[:] = []
    def got_welcome(self, welcome):
        pass # TODO
    def got_key(self, key):
        self._key = key # for derive_key()
    def got_verifier(self, verifier):
        self._verifier = verifier
        for d in self._verifier_observers:
            d.callback(verifier)
        self._verifier_observers[:] = []
    def got_version(self, version):
        self._version = version
        for d in self._version_observers:
            d.callback(version)
        self._version_observers[:] = []

    def received(self, plaintext):
        if self._received_observers:
            self._received_observers.pop(0).callback(plaintext)
            return
        self._received_data.append(plaintext)

    def closed(self, result):
        #print("closed", result, type(result))
        if isinstance(result, Exception):
            self._observer_result = self._closed_result = failure.Failure(result)
        else:
            # pending w.verify()/w.version()/w.read() get an error
            self._observer_result = WormholeClosed(result)
            # but w.close() only gets error if we're unhappy
            self._closed_result = result
        for d in self._verifier_observers:
            d.errback(self._observer_result)
        for d in self._version_observers:
            d.errback(self._observer_result)
        for d in self._received_observers:
            d.errback(self._observer_result)
        for d in self._closed_observers:
            d.callback(self._closed_result)


def create(appid, relay_url, reactor, delegate=None, journal=None,
           tor_manager=None, timing=None, welcome_handler=None,
           stderr=sys.stderr):
    timing = timing or DebugTiming()
    side = bytes_to_hexstr(os.urandom(5))
    journal = journal or ImmediateJournal()
    if not welcome_handler:
        from . import __version__
        signal_error = NotImplemented # TODO
        wh = _WelcomeHandler(relay_url, __version__, signal_error)
        welcome_handler = wh.handle_welcome
    if delegate:
        w = _DelegatedWormhole(delegate)
    else:
        w = _DeferredWormhole()
    b = Boss(w, side, relay_url, appid, welcome_handler, reactor, journal,
             tor_manager, timing)
    w._set_boss(b)
    b.start()
    return w

def from_serialized(serialized, reactor, delegate,
                    journal=None, tor_manager=None,
                    timing=None, stderr=sys.stderr):
    assert serialized["serialized_wormhole_version"] == 1
    timing = timing or DebugTiming()
    w = _DelegatedWormhole(delegate)
    # now unpack state machines, including the SPAKE2 in Key
    b = Boss.from_serialized(w, serialized["boss"], reactor, journal, timing)
    w._set_boss(b)
    b.start() # ??
    raise NotImplemented
    # should the new Wormhole call got_code? only if it wasn't called before.

# after creating the wormhole object, app must call exactly one of:
# set_code(code), generate_code(), helper=type_code(), and then (if they need
# to know the code) wait for delegate.got_code() or d=w.when_code()

# the helper for type_code() can be asked for completions:
# d=helper.get_completions(text_so_far), which will fire with a list of
# strings that could usefully be appended to text_so_far.

# wormhole.type_code_readline(w) is a wrapper that knows how to use
# w.type_code() to drive rlcompleter

