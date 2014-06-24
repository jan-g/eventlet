import sys
import errno
import signal
from eventlet import patcher
select = patcher.original('select')
time = patcher.original('time')
sleep = time.sleep

from eventlet.support import get_errno, clear_sys_exc_info
from eventlet.hubs.hub import BaseHub, READ, WRITE, noop, alarm_handler

import traceback

EXC_MASK = select.POLLERR | select.POLLHUP
READ_MASK = select.POLLIN | select.POLLPRI
WRITE_MASK = select.POLLOUT


class Hub(BaseHub):
    def __init__(self, clock=time.time):
        super(Hub, self).__init__(clock)
        self.poll = select.poll()
        # poll.modify is new to 2.6
        try:
            self.modify = self.poll.modify
        except AttributeError:
            self.modify = self.poll.register

    def add(self, evtype, fileno, cb, tb, mac, **kw):
        listener = super(Hub, self).add(evtype, fileno, cb, tb, mac, kw)
        self.register(fileno, new=True)
        return listener

    def remove(self, listener):
        super(Hub, self).remove(listener)
        self.register(listener.fileno)

    def register(self, fileno, new=False):
        mask = 0
        if self.listeners[READ].get(fileno):
            mask |= READ_MASK | EXC_MASK
        if self.listeners[WRITE].get(fileno):
            mask |= WRITE_MASK | EXC_MASK
        try:
            if mask:
                if new:
                    self.poll.register(fileno, mask)
                else:
                    try:
                        self.modify(fileno, mask)
                    except (IOError, OSError):
                        self.poll.register(fileno, mask)
            else:
                try:
                    self.poll.unregister(fileno)
                except (KeyError, IOError, OSError):
                    # raised if we try to remove a fileno that was
                    # already removed/invalid
                    pass
        except ValueError:
            # fileno is bad, issue 74
            self.remove_descriptor(fileno)
            raise

    def remove_descriptor(self, fileno):
        super(Hub, self).remove_descriptor(fileno)
        try:
            self.poll.unregister(fileno)
        except (KeyError, ValueError, IOError, OSError):
            # raised if we try to remove a fileno that was
            # already removed/invalid
            pass

    def do_poll(self, seconds):
        # poll.poll expects integral milliseconds
        return self.poll.poll(int(seconds * 1000.0))

    def wait(self, seconds=None):
        # print " *** KERRIN *** wait %f" % seconds
        # print ''.join(traceback.format_stack())
        readers = self.listeners[READ]
        writers = self.listeners[WRITE]

        if not readers and not writers:
            if seconds:
                import paramiko.transport
                print " *** KERRIN *** sleeping %f in wait - no readers / writers with %s closed(%s), timers(%s), next_timers(%s)" % (seconds, str(paramiko.transport._active_threads), self.closed, self.timers, self.next_timers)
                print >> sys.stderr, " *** KERRIN *** sleeping %f in wait - no readers / writers with %s closed(%s), timers(%s), next_timers(%s)" % (seconds, str(paramiko.transport._active_threads), self.closed, self.timers, self.next_timers)
                print >> sys.stderr, " *** KERRIN called_timers(%d), uncalled_timers(%d) called_next_timers(%d) uncalled_next_timers(%d)" %(
                    len([x for x in self.timers if x[1].called]),
                    len([x for x in self.timers if not x[1].called]),
                    len([x for x in self.next_timers if x[1].called]),
                    len([x for x in self.next_timers if not x[1].called]),
                    )
                print paramiko.transport._active_threads
                sleep(seconds)
            return
        try:
            presult = self.do_poll(seconds)
        except (IOError, select.error) as e:
            if get_errno(e) == errno.EINTR:
                return
            raise
        SYSTEM_EXCEPTIONS = self.SYSTEM_EXCEPTIONS

        if self.debug_blocking:
            self.block_detect_pre()

        callbacks = set()
        for fileno, event in presult:
            if event & READ_MASK:
                callbacks.add((readers.get(fileno, noop), fileno))
            if event & WRITE_MASK:
                callbacks.add((writers.get(fileno, noop), fileno))
            if event & select.POLLNVAL:
                self.remove_descriptor(fileno)
                continue
            if event & EXC_MASK:
                callbacks.add((readers.get(fileno, noop), fileno))
                callbacks.add((writers.get(fileno, noop), fileno))

        for listener, fileno in callbacks:
            try:
                listener.cb(fileno)
            except SYSTEM_EXCEPTIONS:
                raise
            except:
                self.squelch_exception(fileno, sys.exc_info())
                clear_sys_exc_info()

        if self.debug_blocking:
            self.block_detect_post()
