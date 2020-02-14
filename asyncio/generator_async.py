from collections import deque
import socket
import select


class YieldEvent:
    def yield_handle(self, sched, task):
        raise NotImplemented
    def resume_yield(self, sched, task):
        raise NotImplemented



class Scheduler:
    def __init__(self):
        self._ready = deque()
        self._read_wait = {}
        self._write_wait = {}
        self._n_tasks = 0
    
    def _poll(self):
        while self._ready:
            task, msg = self._ready.popleft()
            try:
                r = task.send(msg)
                if isinstance(r, YieldEvent):
                    r.yield_handle(self, task)
                else:
                    raise RuntimeError(f"Unknown event type:{r.__class__}")
            except StopIteration:
                self._n_tasks -= 1

    def run(self):
        while self._n_tasks:
            if self._ready:
                self._poll()
            rlist, wlist, _ = select.select(self._read_wait.keys(), self._write_wait.keys(), [])
            for i, r in enumerate(rlist + wlist):
                wait = (self._read_wait, self._write_wait)[i >= len(rlist)]
                event, task = wait.pop(r)
                event.resume_yield(self, task)

    def new(self, task):
        # None for task.send(None) to prime the task generator
        self._ready.append((task, None))
        self._n_tasks += 1

    def read_wait(self, fd, event, task):
        self._read_wait[fd] = (event, task)

    def write_wait(self, fd, event, task):
        self._write_wait[fd] = (event, task)

    def add_ready(self, task, msg):
        self._ready.append((task, msg))

class AcceptSocket(YieldEvent):
    def __init__(self, sock):
        self.sock = sock
    def yield_handle(self, sched, task):
        sched.read_wait(self.sock.fileno(), self, task)

    def resume_yield(self, sched, task):
        r = self.sock.accept()
        sched.add_ready(task, r)

class ReadSocket(YieldEvent):
    def __init__(self, sock, nbytes):
        self.sock = sock
        self.nbytes = nbytes
        
    def yield_handle(self, sched, task):
        sched.read_wait(self.sock.fileno(), self, task)

    def resume_yield(self, sched, task):
        data = self.sock.recv(self.nbytes)
        sched.add_ready(task, data)

class WriteSocket(YieldEvent):
    def __init__(self, sock, data):
        self.data = data
        self.sock = sock

    def yield_handle(self, sched, task):
        sched.write_wait(self.sock.fileno(), self, task)

    def resume_yield(self, sched, task):
        n = self.sock.send(self.data)
        sched.add_ready(task, n)

class Socket:
    """
    A factory class returning YieldEvent Handlers
    """
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, attr):
        return getattr(self.sock, attr)

    def recv(self, nbytes):
        return ReadSocket(self.sock, nbytes)

    def send(self, data):
        return WriteSocket(self.sock, data)

    def accept(self):
        return AcceptSocket(self.sock)

class EchoServer(YieldEvent):
    
    def __init__(self, addr, sched):
        """
        addr: address to bind for this server
        sched: scheduler, used to schudle recieved clients
        """
        self.sched = sched
        sched.new(self.server_loop(addr))

    def server_loop(self, addr):
        sock = Socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        sock.bind(addr)
        sock.listen(5)
        while True:
            c, a = yield sock.accept()
            print(f'receive connetions from {a!r}')
            client = Socket(c)
            self.sched.new(client_handle(client))


def client_handle(sock):
    while True:
        line = yield from readline(sock)
        if not line:
            break
        line = b'Get: ' + line
        while line:
            n = yield sock.send(line)
            line = line[n:]
    sock.close()
    print('Client closed')

def readline(socket):
    chars = []
    while True:
        c = yield socket.recv(1)
        if not c:
            break
        chars.append(c)
        if c == b'\n':
            break
    return b"".join(chars)


if __name__ == '__main__':
    sched = Scheduler()
    server = EchoServer(("", 8000), sched)
    sched.run()