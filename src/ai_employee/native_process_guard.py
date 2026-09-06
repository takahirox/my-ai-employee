"""Linux seccomp admission guard, executed only inside an owned disposable container.

Counts the initial process plus attempted non-thread fork/vfork/clone admissions.
Failed syscalls still consume an admission (conservative); exec and threads do not.
No syscall argument pointers are inspected. clone3 returns ENOSYS for libc fallback.
The supervisor is non-dumpable; nested notification listeners and ptrace are denied.
The final report is written only after killing/reaping every other worker-UID process.
"""

PROCESS_GUARD_SOURCE = r"""
import array, ctypes as c, errno, json, os, platform, signal, socket, sys, threading, time

libc = c.CDLL(None, use_errno=True)
libc.syscall.restype = c.c_long
limit, report_path = int(sys.argv[1]), sys.argv[2]
argv = sys.argv[3:]
assert os.getuid() == 1000 and limit > 0 and argv
assert sys.byteorder == "little"
arch, sc, clone, fork, vfork, ptrace = {
    "x86_64": (0xc000003e, 317, 56, 57, 58, 101),
    "aarch64": (0xc00000b7, 277, 220, -1, -1, 117),
}[platform.machine()]

def checked(value):
    if value < 0:
        raise OSError(c.get_errno(), "native process guard unavailable")
    return value

class Filter(c.Structure):
    _fields_ = [("code", c.c_ushort), ("jt", c.c_ubyte),
                ("jf", c.c_ubyte), ("k", c.c_uint)]

class Program(c.Structure):
    _fields_ = [("len", c.c_ushort), ("filter", c.POINTER(Filter))]

class Data(c.Structure):
    _fields_ = [("nr", c.c_int), ("arch", c.c_uint), ("ip", c.c_ulonglong),
                ("args", c.c_ulonglong * 6)]

class Notification(c.Structure):
    _fields_ = [("id", c.c_ulonglong), ("pid", c.c_uint),
                ("flags", c.c_uint), ("data", Data)]

class Response(c.Structure):
    _fields_ = [("id", c.c_ulonglong), ("val", c.c_longlong),
                ("error", c.c_int), ("flags", c.c_uint)]

assert c.sizeof(Notification) == 80 and c.sizeof(Response) == 24
sizes = (c.c_ushort * 3)()
checked(libc.syscall(sc, 3, 0, c.byref(sizes)))
assert tuple(sizes) == (80, 24, 64), "unsupported seccomp notification ABI"
ALLOW, NOTIFY, KILL, ERROR = 0x7fff0000, 0x7fc00000, 0x80000000, 0x50000
instructions = [
    (0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, KILL),
    (0x20, 0, 0, 0), (0x45, 0, 1, 0x40000000), (0x06, 0, 0, KILL),
    (0x15, 0, 1, 435), (0x06, 0, 0, ERROR | errno.ENOSYS),
    (0x15, 0, 1, ptrace), (0x06, 0, 0, ERROR | errno.EPERM),
    # A later NEW_LISTENER could otherwise supersede our USER_NOTIF filter.
    (0x15, 0, 4, sc), (0x20, 0, 0, 24), (0x45, 0, 1, 8),
    (0x06, 0, 0, ERROR | errno.EPERM), (0x06, 0, 0, ALLOW),
    (0x15, 0, 4, clone), (0x20, 0, 0, 16), (0x45, 1, 0, 0x10000),
    (0x06, 0, 0, NOTIFY), (0x06, 0, 0, ALLOW),
    (0x15, 0, 1, fork & 0xffffffff), (0x06, 0, 0, NOTIFY),
    (0x15, 0, 1, vfork & 0xffffffff), (0x06, 0, 0, NOTIFY),
    (0x06, 0, 0, ALLOW),
]
filters = (Filter * len(instructions))(*(Filter(*i) for i in instructions))
program = Program(len(filters), filters)
checked(libc.prctl(38, 1, 0, 0, 0))  # no_new_privs
checked(libc.prctl(4, 0, 0, 0, 0))   # non-dumpable supervisor /proc and ptrace
checked(libc.prctl(36, 1, 0, 0, 0))  # adopt detached grandchildren for cleanup
parent, child = socket.socketpair()
pid = os.fork()
if pid == 0:
    try:
        parent.close()
        listener = checked(libc.syscall(sc, 1, 8, c.byref(program)))
        child.sendmsg([b"ready"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  array.array("i", [listener]))])
        os.close(listener)
        assert child.recv(1) == b"!"
        child.close()
        os.execvp(argv[0], argv)
    except BaseException:
        os._exit(126)
child.close()
message, ancillary, flags, address = parent.recvmsg(16, socket.CMSG_SPACE(4))
assert message == b"ready" and len(ancillary) == 1 and flags == 0
level, kind, payload = ancillary[0]
assert level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS
listener = array.array("i", payload)[0]
usage = {"limit": limit, "admitted": 1, "denied": False, "guard_error": False}
usage["syscall_counts"] = {}
lock = threading.Lock()
finishing = False

def terminate_workers():
    # Container PID 1/control/Git are UID 0; all task code is UID 1000.
    # kill(-1) excludes the caller; no host PIDs are visible in this namespace.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass

def supervise():
    try:
        while True:
            notification = Notification()
            result = libc.ioctl(listener, 0xc0502100, c.byref(notification))
            if result < 0 and c.get_errno() in (errno.EINTR, errno.ENOENT):
                continue
            checked(result)
            with lock:
                if finishing:
                    return
                if usage["admitted"] >= limit:
                    usage["denied"] = True
                    terminate_workers()
                    return
                # Never inspect mutable userspace pointers or rewrite arguments.
                response = Response(notification.id, 0, 0, 1)  # CONTINUE
                syscall = str(notification.data.nr)
                usage["syscall_counts"][syscall] = usage["syscall_counts"].get(syscall, 0) + 1
                usage["admitted"] += 1
                result = libc.ioctl(listener, 0xc0182101, c.byref(response))
                if result < 0 and c.get_errno() == errno.ENOENT:
                    continue  # cancelled syscall still consumes its reservation
                checked(result)
    except BaseException:
        with lock:
            usage["guard_error"] = True
        terminate_workers()

threading.Thread(target=supervise, daemon=True).start()
parent.send(b"!")
parent.close()
_, status = os.waitpid(pid, 0)
with lock:
    finishing = True
    terminate_workers()
while True:
    try:
        waited, _ = os.waitpid(-1, os.WNOHANG)
        if waited == 0:
            terminate_workers()
            time.sleep(0.01)
    except ChildProcessError:
        break
# No candidate writer remains. The root-owned report inode and its parent cannot
# be replaced by UID 1000. Overwrite any earlier untrusted bytes only now.
with lock:
    usage["root_exit"] = os.waitstatus_to_exitcode(status)
    usage["cleanup"] = "confirmed"
    with open(report_path, "w") as report:
        json.dump(usage, report)
sys.exit(125 if usage["denied"] else 126 if usage["guard_error"] else 0)
"""
