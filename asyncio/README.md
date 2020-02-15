# 使用python generator创建一个异步Echo服务器：从砖头开始

使用到的关键库：
* socket:比较底层的通信库
* select:同样比较底层的轮训库，**异步核心**

听说异步很厉害，不用线程或进程也能实现高并发？不如动动手来一探究竟。

## Event

异步编程框架通常都是用来处理IO的---网络IO，文件系统IO，数据库IO，大概有这么3类。在类Unix系统中，网络IO、文件系统IO通常可以被抽象为文件IO，在linux系统中，甚至设备管理、进程管理也可以通过抽象的文件IO来进行。具体表现为所有操作围绕的中心为文件描述符(file descriptor)，读、写以及一些元操作都围绕文件描述符展开。此文主要针对网络IO，在类Unix系统中，文件系统IO应该也同样适用，数据库IO的操作应该需要特殊处理，以后再来探查。

在以socket为中心的网络编程中，主要有网络读、写两大类操作。另外一个涉及服务器的端口侦听操作也可以归类到读操作里面去---从侦听端口上读一个连接。所以在异步编程中，我们可以把网络IO里所有的**阻塞操作**抽象为**两大类事件（Event）**---输入事件、输出事件。在这里，我给事件一个定义：\
事件（Event）：在将来某个时间点发生、并返回一个结果。\
更具体而言：
* 连接事件（ConnectEvent）是服务器端口侦听返回的事件，表示在一个客户端连接到来的时候，返回一个连接好的端口。
* 接收事件（ReceiveEvent) 是读端口返回的事件，表示当端口有内容可供读取的时候，返回读到的内容。
* 输出事件 (OutputEvent) 是写端口时返回的事件，表示可以往端口写内容时，往端口写，并返回写了多少字节。

上述讨论映射到如下代码。其中BaseEvent是个抽象类，为所有Event的基类，定义了Event需要支持的API。其中fileno返回这个Event所代表的端口的文件描述符，是整个异步编程的核心。通过调用[select.select](!https://docs.python.org/3/library/select.html)函数可以判断该端口是否ready，即可进行不阻塞IO。而happen函数则在该Event代表的端口ready时由调度器调用，并返回结果。
```python
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
```

事件(Event)是各个任务(task)和调度器(scheduler)交互的中心。任务需要进行阻塞操作时，yield一个相关事件给调度器，暂停自己的执行。调度器把事件放到等待队列上，当事件等待的条件满足，调用事件的happen接口，得到这个事件发生的结果，到下一个调度周期，把结果发送回任务，于是任务得以继续执行。这就是一个任务完整的异步执行周期。

## Server task

同步编程模式下，服务器通常都会在一个端口侦听，来了客户端请求后，创建新进程或者线程提供服务，或者使用创建好的线程池里的线程。异步编程模式下，只有一个线（进）程，但是却要运行多个task---1个server task以及任意多个client task。所以在异步编程模式下，如果有任何阻塞IO操作或者非常耗时的CPU运算，都会导致其他task得不到及时响应。这是异步编程的难点也是它解决的关键问题。下面从代码角度来看一下server task如何解决阻塞问题。

```python

def echo_server(addr, scheduler):
    """
    addr: a tuple consisting of (ip, port) for this server
        to listen on
    scheduler: when a client connection comes in, schedule the client task
        ro run.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # set this socket as a reusable socket, a typical for server socket
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
```
echo_server 由于里面由于```client, addr = yield sock.accept()```, 成为了一个协程(coroutine)，直接调用它并不会执行它里面的代码，而是返回一个协程对象，这就是我们的server task。 它通过yield主动暂停自己的执行并返回给调度器一个Event对象。这就相当于告诉调度器Event准备好时，再继续server的执行。

上述代码中sock是socket.socket的一个wrapper，它代理socket的所有相关阻塞调用（accept, send, recv），返回一个相关的Event(分别对应ConnectEvent, OutputEvent, ReceiveEvent)。
在这里，它yield给调度器的是一个ConnectEvent。

## Scheduler

调度器是如何处理从任务返回的事件的呢？<br>
事件从任务返回后，调度器需要根据事件的类别---读事件或者写事件---来将事件放到不同的等待队列上。然后调度器会使用select来轮询当前是否有事件发生条件满足，将ready的事件从等待队列取出，放到ready队列中，等待执行。
```python
class Scheduler:

    def __init__(self):
        self._nrunning_tasks = 0
        self._readyq = deque()
        self._read_wait = {}
        self._write_wait = {}
        
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
```
## 总结
client task和server task与调度器的交互原理是一样的，不过它是由server task在接收到客户端连接后创建，并提交调度器执行，详细的代码见[generator_async.py](generator_async.py)和[async_echo_server.py](async_echo_server.py)。异步编程的里涉及的几个重要的概念---task，event，scheduler，就基本覆盖到了。是不是很想一个小操作系统？


代码样例来源于 
"Python Cookbook 12.12: Using Generators As an Alternative to Threads"
[generator_async.py](generator_async.py)基本上对着原书敲下来的，[async_echo_server.py](async_echo_server.py)是我自己的一些发挥。

写的不好，有问题或者想改进欢迎提ISSUE、PR。
