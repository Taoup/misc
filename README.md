# 使用python generator创建一个异步Echo服务器：从砖头开始

使用到的关键库：
* socket:比较底层的通信库
* select:同样比较底层的轮训库，**异步核心**

例子来源于 
"Python Cookbook 12.12: Using Generators As an Alternative to Threads"

听说异步很厉害，不用线程或进程也能实现高并发？不如动动手来一探究竟。

从Server开始。寻常的同步编程模式下，服务器通常都会在一个端口侦听，来了客户端请求后，创建新进程或者线程提供服务（或者使用创建好的线程池里的线程）。异步编程模式下，这个部分差别不大:

```python

def echo_server(addr, scheduler):
    """
    addr: a tuple consisting of (ip, port) for this server
        to listen on
    scheduler: when a client connection comes in, schedule the client handler
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

上述代码中sock是socket.socket的一个wrapper，它代理socket的所有相关阻塞调用（accept, send, recv），返回一个相关的Event(分别对应ConnectEvent, SendEvent, ReceiveEvent)。
在这里，它yield给调度器的是一个ConnectEvent。

那么调度器是如何处理这个Event的呢？<br>
这首先就涉及到创建echo_server任务，以及把这个任务提交给调度器，来看下入口函数：
```python
if __name__ == '__main__':

    sched = Scheduler()
    sched.submit_task(echo_server(('', 8000), sched))
    sched.run()
```
很简单的几个语句。
* 实例化一个调度器；
* 提交echo_server任务；
* 开冲！

提交任务非常简单，就是把这个任务(task)放到调度器的ready队列，等待执行即可。在这里的异步编程框架中，任务其本质就是协程，驱动协程一般通过send函数，例如task.send(None)<sup>1</sup>, 会把task这个协程驱动到第一个yield处，并且给调用者返回yield右边的表达式。
上面的server task就是通过这种方式把AcceptEvent返回给了调度器。接下来我们看下调度器对Event的处理。
```python
class ConnectEvent(BaseEvent):
    def do_schedule(self, scheduler, task):
        scheduler.waiting_for_read(self.sock.fileno(), self, task)

class ReceiveEvent(BaseEvent):
    def do_schedule(self, scheduler, task):
        scheduler.waiting_for_read(self.sock.fileno(), self, task)

class SendEvent(BaseEvent):
    def do_schedule(self, scheduler, task):
        scheduler.waiting_for_write(self.sock.fileno(), self, task)

class Scheduler:
    def _run(self):
            while self._readyq:
                task, data = self._readyq.popleft()
                try:
                    # move the current task to next steps, till another event
                    # is yielded by the task.
                    event = task.send(data)
                    if isinstance(event, BaseEvent):
                        # do event specific scheduling by passing the scheduler in
                        event.do_schedule(self, task)
                    else:
                        raise RuntimeError(f"Unkown event type: {event.__class__}")
                except StopIteration:
                    self._nrunning_tasks -= 1
```
上面代码中把不相关的其他部分给去掉了。\
在Scheduler._run函数中，```event = task.send(data)```将每一个task驱动一遍，得到它们等待的下一个Event。然后通过调用Event的do_schedule函数，把各个(event,task)对添加到调度器相应等待队列上。\
接下来，只需要event对应的条件满足，便可以将task从等待队列中取出，放到ready队列中等待执行即可：
```python
class ConnectEvent(BaseEvent):
    def happen_so_smooth(self, scheduler, task):
        res = self.sock.accept()
        scheduler.add_ready(task, res)

class ReceiveEvent(BaseEvent):
    def happen_so_smooth(self, scheduler, task):
        res = self.sock.recv(self.nbytes)
        scheduler.add_ready(task, res)

class SendEvent(BaseEvent):
    def happen_so_smooth(self, scheduler, task):
        nbytes = self.sock.send(self.data)
        scheduler.add_ready(task, nbytes)

class Scheduler:
    def run(self):
        while self._nrunning_tasks:
            if self._readyq:
                self._run()
            # This is the core of whole asynchronous thing!
            rset, wset, eset = select.select(self._read_waiting, self._write_waiting, [])
            for i, fileno in enumerate(rset+wset):
                which = (self._read_waiting, self._write_waiting)[i>=len(rset)]
                # At this moment, we are sure the event will happen without blocking!
                event, task = which.pop(fileno)
                event.happen_so_smooth(self, task)
```
[select.select](!https://docs.python.org/3/library/select.html)返回的是我们想要的条件---不阻塞的IO。然后我们调用event的happen_so_smooth函数，顺畅、非阻塞地(smoothly， non-blocking)进行**之前可能会阻塞**的操作，得到相应结果res，把（task, res)对放到调度器的ready队列中，等待下一个调度器的调度周期进行执行。\
然后，在调度器下一次调度后，res就会被send到各自的task里，如此循环往复。

处理客户端连接的代码也是类似的原理。更多细节见代码，[generator_async.py](generator_async.py)基本上对着原书敲下来的，[async_echo_server.py](async_echo_server.py)是我自己的一些发挥。

写的不好，有问题或者想改进欢迎提ISSUE、PR。

<sup>1</sup>```task.send(None)```也称为prime a coroutine