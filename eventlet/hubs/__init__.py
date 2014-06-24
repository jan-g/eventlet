import sys
import os
from eventlet.support import greenlets as greenlet
from eventlet import patcher
import uuid

try:
    # try and import pkg_resources ...
    import pkg_resources
except ImportError:
    # ... but do not depend on it
    pkg_resources = None


__all__ = ["use_hub", "get_hub", "get_default_hub", "trampoline"]

threading = patcher.original('threading')
_threadlocal = threading.local()


def get_default_hub():
    """Select the default hub implementation based on what multiplexing
    libraries are installed.  The order that the hubs are tried is:

    * epoll
    * kqueue
    * poll
    * select

    It won't automatically select the pyevent hub, because it's not
    python-thread-safe.

    .. include:: ../doc/common.txt
    .. note :: |internal|
    """

    # pyevent hub disabled for now because it is not thread-safe
    # try:
    #    import eventlet.hubs.pyevent
    #    return eventlet.hubs.pyevent
    # except:
    #    pass

    select = patcher.original('select')
    try:
        import eventlet.hubs.epolls
        return eventlet.hubs.epolls
    except ImportError:
        try:
            import eventlet.hubs.kqueue
            return eventlet.hubs.kqueue
        except ImportError:
            if hasattr(select, 'poll'):
                import eventlet.hubs.poll
                return eventlet.hubs.poll
            else:
                import eventlet.hubs.selects
                return eventlet.hubs.selects


def use_hub(mod=None):
    """Use the module *mod*, containing a class called Hub, as the
    event hub. Usually not required; the default hub is usually fine.

    Mod can be an actual module, a string, or None.  If *mod* is a module,
    it uses it directly.   If *mod* is a string and contains either '.' or ':'
    use_hub tries to import the hub using the 'package.subpackage.module:Class'
    convention, otherwise use_hub looks for a matching setuptools entry point
    in the 'eventlet.hubs' group to load or finally tries to import
    `eventlet.hubs.mod` and use that as the hub module.  If *mod* is None,
    use_hub uses the default hub.  Only call use_hub during application
    initialization,  because it resets the hub's state and any existing
    timers or listeners will never be resumed.
    """
    if mod is None:
        mod = os.environ.get('EVENTLET_HUB', None)
    if mod is None:
        mod = get_default_hub()
    if hasattr(_threadlocal, 'hub'):
        del _threadlocal.hub
    if isinstance(mod, str):
        assert mod.strip(), "Need to specify a hub"
        if '.' in mod or ':' in mod:
            modulename, _, classname = mod.strip().partition(':')
            mod = __import__(modulename, globals(), locals(), [classname])
            if classname:
                mod = getattr(mod, classname)
        else:
            found = False
            if pkg_resources is not None:
                for entry in pkg_resources.iter_entry_points(
                        group='eventlet.hubs', name=mod):
                    mod, found = entry.load(), True
                    break
            if not found:
                mod = __import__(
                    'eventlet.hubs.' + mod, globals(), locals(), ['Hub'])
    if hasattr(mod, 'Hub'):
        _threadlocal.Hub = mod.Hub
    else:
        _threadlocal.Hub = mod


def get_hub():
    """Get the current event hub singleton object.

    .. note :: |internal|
    """
    try:
        hub = _threadlocal.hub
    except AttributeError:
        try:
            _threadlocal.Hub
        except AttributeError:
            use_hub()
        hub = _threadlocal.hub = _threadlocal.Hub()
    return hub

from eventlet import timeout


def trampoline(fd, read=None, write=None, timeout=None,
               timeout_exc=timeout.Timeout,
               mark_as_closed = None):
    """Suspend the current coroutine until the given socket object or file
    descriptor is ready to *read*, ready to *write*, or the specified
    *timeout* elapses, depending on arguments specified.

    To wait for *fd* to be ready to read, pass *read* ``=True``; ready to
    write, pass *write* ``=True``. To specify a timeout, pass the *timeout*
    argument in seconds.

    If the specified *timeout* elapses before the socket is ready to read or
    write, *timeout_exc* will be raised instead of ``trampoline()``
    returning normally.

    .. note :: |internal|
    """
    print >> sys.stderr, "*** DEBUG calling trampoline on fileno(%d), read(%s) write(%s)" %(fd.fileno(), read, write)
    t = None
    hub = get_hub()
    current = greenlet.getcurrent()
    assert hub.greenlet is not current, 'do not call blocking functions from the mainloop'
    assert not (
        read and write), 'not allowed to trampoline for reading and writing'
    try:
        fileno = fd.fileno()
    except AttributeError:
        fileno = fd

    if timeout is not None:
        t_debug = uuid.uuid4()
        print >> sys.stderr, "*** DEBUG setting timeout_call on fileno(%d) in timeut(%s) at t_debug(%s)" %(fileno, timeout, t_debug)
        t = hub.schedule_call_global(timeout, timeout_call, fileno, t_debug, current.throw, timeout_exc)

    try:
        if read:
            listener = hub.add(hub.READ, fileno, current.switch, current.throw, mark_as_closed, t_debug=t_debug)
        elif write:
            listener = hub.add(hub.WRITE, fileno, current.switch, current.throw, mark_as_closed, t_debug=t_debug)

        # do_removelistener - when an IOClosed exception is received the
        # current 
        do_removelistener = True
        try:
            try:
                returned = hub.switch()
                print >> sys.stderr, "*** DEBUG hub.switch returned(%s)" % returned
                return returned
            except IOClosed as e:
                closedfds = [list.fileno for list in hub.closed]
                listening = fileno in hub.listeners[read and hub.READ or hub.WRITE]
                print >> sys.stderr, "*** DEBUG hub.switch throw IOClosed(%s) on fileno(%s) closed(%s) listening(%s)" % (str(e), fileno, fileno in closedfds, listening)
                do_removelistener = False
                raise
            except Exception as e:
                closedfds = [list.fileno for list in hub.closed]
                listening = fileno in hub.listeners[read and hub.READ or hub.WRITE]
                print >> sys.stderr, "*** DEBUG hub.switch throw exc(%s) on fileno(%s) closed(%d) listening(%s)" % (str(e), fileno, fileno in closedfds, listening)
                if listener.spent:
                    print >> sys.stderr, "*** DEBUG listener spent, extra(%s), not removing" % (str(listener.extra))
                    do_removelistener = False
                raise
        finally:
            print >> sys.stderr, "*** DEBUG Calling hub.remove for listener %d" % (fileno)
            # do_removelistener - 
            if do_removelistener:
                hub.remove(listener)

    finally:
        print >> sys.stderr, "*** DEBUG Cancelling timer %s on fileno(%s) timers(%d) next_timers(%d) t.called(%s) timer(%r)" % (t is not None, fileno, len(hub.timers), len(hub.next_timers), t.called, t)
        if t is not None:
            print >> sys.stderr, "*** DEBUG Cancel the fer"
            t.cancel()
            print >> sys.stderr, "*** DEBUG cancel timer on fileno(%s) timers(%d) next_timers(%d) t.called(%s) timer(%r)" %(fileno, len(hub.timers), len(hub.next_timers), t.called, t)

def timeout_call(fileno, t_debug, tb, *args, **kw):
    print >> sys.stderr, "*** DEBUG trampoline callback called on fileno(%s) t_debug(%s)" %(fileno, t_debug)
    tb(*args, **kw)

def notify_close(fd):
    """
    A particular file descriptor has been explicitly closed. Register for any
    waiting listeners to be notified on the next run loop.
    """
    hub = get_hub()
    hub.notify_close(fd)

def notify_opened(fd):
    """
    Some file descriptors may be closed 'silently' - that is, by the garbage
    collector, by an external library, etc. When the OS returns a file descriptor
    from an open call (or something similar), this may be the only indication we
    have that the FD has been closed and then recycled.
    We let the hub know that the old file descriptor is dead; any stuck listeners
    will be disabled and notified in turn.
    """
    hub = get_hub()
    hub.mark_as_reopened(fd)


class IOClosed(IOError):
    pass
