import select

from pulsar.utils.structures import OrderedDict
from pulsar.utils.config import Global
from pulsar.utils.system import Waker
from pulsar.utils.pep import iteritems
from pulsar.utils.exceptions import EventAlreadyRegistered

from .defer import log_failure


_EPOLLIN = 0x001
_EPOLLPRI = 0x002
_EPOLLOUT = 0x004
_EPOLLERR = 0x008
_EPOLLHUP = 0x010
_EPOLLRDHUP = 0x2000
_EPOLLONESHOT = (1 << 30)
_EPOLLET = (1 << 31)

# Events map exactly to the epoll events
READ = _EPOLLIN
WRITE = _EPOLLOUT
ERROR = _EPOLLERR | _EPOLLHUP | _EPOLLRDHUP
_select = select.select

POLLERS = OrderedDict()


class Poller(object):
    '''The Poller interface'''
    def __init__(self):
        self._handlers = {}
        
    def install_waker(self, event_loop):
        # Install event loop wake if possible
        waker = Waker()
        event_loop.add_reader(waker, waker.consume)
        return waker

    def add_reader(self, fd, handler):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if reader:
                raise EventAlreadyRegistered('reader for %s' % fd)
            events = oevents | READ
            self._register(fd, events, oevents)
            self._handlers[fd] = (events, handler, writer, error)
        except KeyError:
            self._register(fd, READ)
            self._handlers[fd] = (READ, handler, None, None)
            
    def add_writer(self, fd, handler):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if writer:
                raise EventAlreadyRegistered('writer for %s' % fd)
            events = oevents | WRITE
            self._register(fd, events, oevents)
            self._handlers[fd] = (events, reader, handler, error)
        except KeyError:
            self._register(fd, WRITE)
            self._handlers[fd] = (WRITE, None, handler, None)
            
    def add_error(self, fd, handler):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if error:
                raise EventAlreadyRegistered('error handler for %s' % fd)
            events = oevents | ERROR
            self._register(fd, events, oevents)
            self._handlers[fd] = (events, reader, handler, handler)
        except KeyError:
            self._register(fd, ERROR)
            self._handlers[fd] = (ERROR, None, None, handler)
    
    def remove_reader(self, fd):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if reader:
                events = oevents ^ READ
                if events:
                    self._register(fd, events, oevents)
                    self._handlers[fd] = (events, None, writer, error)
                else:
                    self.unregister(fd)
                return True
            else:
                return False
        except KeyError:
            return False
        
    def remove_writer(self, fd):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if writer:
                events = oevents ^ WRITE
                if events:
                    self._register(fd, events, oevents)
                    self._handlers[fd] = (events, reader, None, error)
                else:
                    self.unregister(fd)
                return True
            else:
                return False
        except KeyError:
            return False
        
    def remove_error(self, fd):
        try:
            oevents, reader, writer, error = self._handlers[fd]
            if error:
                events = oevents ^ ERROR
                if events:
                    self._register(fd, events, oevents)
                    self._handlers[fd] = (events, reader, writer, None)
                else:
                    self.unregister(fd)
                return True
            else:
                return False
        except KeyError:
            return False
    
    def handle_events(self, loop, fd, events):
        try:
            mask, reader, writer, error = self._handlers[fd]
        except KeyError:
            raise KeyError('Received an event on unregistered file '
                           'descriptor %s' % fd)    
        processed = False
        if events & READ:
            processed = True
            if reader:
                log_failure(reader())
            else:
                loop.logger.warning('Read callback without handler for file'
                                    ' descriptor %s.', fd)
        if events & WRITE:
            processed = True
            if writer:
                log_failure(writer())
            else:
                loop.logger.warning('Write callback without handler for file'
                                    ' descriptor %s.', fd)
        if events & ERROR:
            processed = True
            if error:
                log_failure(error())
            else:
                loop.logger.warning('Error callback without handler for file'
                                    ' descriptor %s.', fd)
        
    def close(self):
        self._handlers.clear()
    
    def fileno(self):
        return 0
    
    def fromfd(self, fd):
        raise NotImplementedError
        
    def unregister(self, fd):
        raise NotImplementedError
    
    def poll(self, timeout=1):
        raise NotImplementedError
    
    def check_stream(self):
        pass
    
    def _register(self, fd, events, old_events=None):
        raise NotImplementedError


if hasattr(select, 'epoll'):
    
    class IOepoll(Poller):
        
        def __init__(self):
            super(IOepoll, self).__init__()
            self._epoll = select.epoll()
            
        def poll(self, timeout=0.5):
            return self._epoll.poll(timeout)
            
            
    POLLERS['epoll'] = IOepoll
    

if hasattr(select, 'kqueue'):
    
    KQ_FILTER_READ = select.KQ_FILTER_READ
    KQ_FILTER_WRITE = select.KQ_FILTER_WRITE
    KQ_EV_ADD = select.KQ_EV_ADD
    KQ_EV_EOF = select.KQ_EV_EOF
    kevent = select.kevent
    
    class IOkqueue(Poller):
        
        def __init__(self):
            super(IOkqueue, self).__init__()
            self._kqueue = select.kqueue()
        
        def fileno(self):
            return self._kqueue.fileno()
            
        def unregister(self, fd):
            if fd in self._handlers:
                events, _, _, _ = self._handlers.pop(fd)
                self._control(fd, events, select.KQ_EV_DELETE)
            else:
                raise IOError("fd %d not registered" % fd)
        
        def poll(self, timeout):
            kevents = self._kqueue.control(None, 1000, timeout)
            events = {}
            for kevent in kevents:
                fd = kevent.ident
                if kevent.filter == KQ_FILTER_READ:
                    events[fd] = events.get(fd, 0) | READ
                if kevent.filter == KQ_FILTER_WRITE:
                    if kevent.flags & select.KQ_EV_EOF:
                        # If an asynchronous connection is refused, kqueue
                        # returns a write event with the EOF flag set.
                        # Turn this into an error for consistency with the
                        # other IOLoop implementations.
                        # Note that for read events, EOF may be returned before
                        # all data has been consumed from the socket buffer,
                        # so we only check for EOF on write events.
                        events[fd] = IOLoop.ERROR
                    else:
                        events[fd] = events.get(fd, 0) | IOLoop.WRITE
                if kevent.flags & KQ_EV_ERROR:
                    events[fd] = events.get(fd, 0) | IOLoop.ERROR
            return events.items()
    
        def _register(self, fd, events, old_events=None):
            if old_events is not None:
                self._control(fd, old_events, select.KQ_EV_DELETE)
            self._control(fd, events, select.KQ_EV_ADD)
    
        def _control(self, fd, events, flags):
            k = None
            if events & WRITE:
                k = kevent(fd, filter=KQ_FILTER_WRITE, flags=flags)
                self._kqueue.control([k], 0)
            if events & READ or not k:
                # Always read when there is not a write
                k = kevent(fd, filter=KQ_FILTER_READ, flags=flags)
                self._kqueue.control([k], 0)
            if events & ERROR:
                k = kevent(fd, filter=KQ_EV_ERROR, flags=flags)
                self._kqueue.control([k], 0)
        
    POLLERS['kqueue'] = IOkqueue
    

class IOselect(Poller):
    '''An epoll like select class.'''
    def __init__(self):
        super(IOselect, self).__init__()
        self.read_fds = set()
        self.write_fds = set()
        self.error_fds = set()
    
    def _register(self, fd, events, old_events=None):
        if old_events is not None:
            self.read_fds.discard(fd)
            self.write_fds.discard(fd)
            self.error_fds.discard(fd)
        if events & READ:
            self.read_fds.add(fd)
        if events & WRITE:
            self.write_fds.add(fd)
        if events & ERROR:
            self.error_fds.add(fd)
            # Closed connections are reported as errors by epoll and kqueue,
            # but as zero-byte reads by select, so when errors are requested
            # we need to listen for both read and error.
            self.read_fds.add(fd)
                
    def unregister(self, fd):
        if fd in self._handlers:
            self._handlers.pop(fd)
            self.read_fds.discard(fd)
            self.write_fds.discard(fd)
            self.error_fds.discard(fd)
        else:
            raise IOError("fd %d not registered" % fd)
            
    def poll(self, timeout=None):
        readable, writeable, errors = _select(
            self.read_fds, self.write_fds, self.error_fds, timeout)
        events = {}
        for fd in readable:
            events[fd] = events.get(fd, 0) | READ
        for fd in writeable:
            events[fd] = events.get(fd, 0) | WRITE
        for fd in errors:
            events[fd] = events.get(fd, 0) | ERROR
        return list(iteritems(events))


POLLERS['select'] = IOselect
DefaultIO = list(POLLERS.values())[0]
    
    
class PollerSetting(Global):
    name = "poller"
    flags = ["--io"]
    choices = tuple(POLLERS)
    default = tuple(POLLERS)[0]
    desc = """\
        Specify the selectors used for I/O event polling.
        """