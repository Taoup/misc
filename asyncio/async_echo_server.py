import socket
import select
from collections import deque

class BaseEvent:

    def __init__(self, sock):
        self.sock = sock

    def fileno(self):
        return self.sock.fileno()

    def happen(self):
        raise NotImplemented

class InputEvent(BaseEvent):
    pass

class ConnectEvent(InputEvent):

    def happen(self):
        res = self.sock.accept()
        return res

class ReceiveEvent(InputEvent):

    def __init__(self, sock, nbytes):
        self.nbytes = nbytes
        super().__init__(sock)

    def happen(self):
        res = self.sock.recv(self.nbytes)
        return res

class OutputEvent(BaseEvent):
    
    def __init__(self, sock, data):
        self.data = data
        super().__init__(sock)

    def happen(self):
        nbytes = self.sock.send(self.data)
        return nbytes

class Socket:
    
    def __init__(self, sock):
        self.sock = sock
    
    def __getattr__(self, attr):
        return getattr(self.sock, attr)
    
    def accept(self):
        return ConnectEvent(self.sock)
    
    def recv(self, nbytes):
        return ReceiveEvent(self.sock, nbytes)

    def send(self, data):
        return OutputEvent(self.sock, data)

def echo_server(addr, scheduler):
    """
    addr: a tuple consisting of (ip, port) for this server
        to listen on
    scheduler: when a client connection comes in, schedule the client task
        ro run.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Set this socket as a reusable socket. Typical for server socket
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    sock.bind(addr)
    sock.listen(5)
    sock = Socket(sock)
    while True:
        # synchronous sibling
        # client, addr = sock.accept()
        # asynchronous one
        client, addr = yield sock.accept()
        """
        The difference between thos two sibling is that the first 
        call is a blocking call made directly on a socket, while 
        the second 'sock.accept()' is a delegating call which retures
        an instance of ConnectEvent directly without blocking the thread. 
        Since no client has been connected yet, we need to wait for 'connect'
        event to happen. Thus, the yield statement to yield the ConnectEvent to
        the scheduler and pause the execution of our server.
        """
        print(f"Connection from: {addr!r}")
        scheduler.submit_task(client_handler(client))

def client_handler(client):
    client = Socket(client)
    
    def readline(client):
        chars = []
        while True:
            c = yield client.recv(1)
            if not c:
                break
            chars.append(c)
            if c == b'\n':
                break
        return b''.join(chars)

    while True:
        line = yield from readline(client)
        if not line:
            break
        line = b'Get: ' + line
        while line:
            n = yield client.send(line)
            line = line[n:]
    print(f"Client: {client.getpeername()!r} disconnected")
    client.close()


class Scheduler:

    def __init__(self):
        self._nrunning_tasks = 0
        self._readyq = deque()
        self._read_wait = {}
        self._write_wait = {}
    
    def submit_task(self, task):
        """
        Schedule a task on the ready queue.
        When a new task is submitted to run, it's appended to the ready
        queue. Cause it can't be waiting on anything at the moment of being 
        created. 
        Also, put it in generator's term, task.send(None) is priming the
        generator.
        """
        self._readyq.append((task, None))
        self._nrunning_tasks += 1
    
    def run(self):
        while self._nrunning_tasks:
            self._execute()
            # This is the core of whole asynchronous thing!
            rset, wset, eset = select.select(self._read_wait, self._write_wait, [])
            for i, fileno in enumerate(rset+wset):
                which = (self._read_wait, self._write_wait)[i>=len(rset)]
                event, task = which.pop(fileno)
                # At this moment, we are sure the event will happen without blocking!
                res = event.happen()
                self._readyq.append((task, res))

    def _execute(self):
        while self._readyq:
            task, data = self._readyq.popleft()
            try:
                # move the current task to next steps, till another event
                # is yielded by the task.
                event = task.send(data)
            except StopIteration:
                self._nrunning_tasks -= 1
            else:
                self._schedule(event, task)

    def _schedule(self, event, task):
        if isinstance(event, (InputEvent, OutputEvent)):
            queue = [self._write_wait, self._read_wait][isinstance(event, InputEvent)]
            queue[event.fileno()] = (event, task)
        else:
            raise RuntimeError(f"Unkown event type: {event.__class__}")

if __name__ == '__main__':

    sched = Scheduler()
    sched.submit_task(echo_server(('', 8000), sched))
    sched.run()