"""Linux native process lifetime guard, executed only inside an owned disposable container.

The supervisor is non-dumpable; worker ptrace is denied.
The final report is written only after killing/reaping every other worker-UID process.
"""

PROCESS_GUARD_SOURCE = r"""
import ctypes as c, errno, json, os, platform, signal, socket, sys, time

libc = c.CDLL(None, use_errno=True)
libc.syscall.restype = c.c_long
report_path = sys.argv[1]
argv = sys.argv[2:]
assert os.getuid() == 1000 and argv
assert sys.byteorder == "little"
arch, sc, ptrace = {
    "x86_64": (0xc000003e, 317, 101),
    "aarch64": (0xc00000b7, 277, 117),
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

ALLOW, KILL, ERROR = 0x7fff0000, 0x80000000, 0x50000
instructions = [
    (0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, KILL),
    (0x20, 0, 0, 0), (0x45, 0, 1, 0x40000000), (0x06, 0, 0, KILL),
    (0x15, 0, 1, ptrace), (0x06, 0, 0, ERROR | errno.EPERM),
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
        checked(libc.syscall(sc, 1, 0, c.byref(program)))
        child.sendall(b"ready")
        assert child.recv(1) == b"!"
        child.close()
        os.execvp(argv[0], argv)
    except BaseException:
        os._exit(126)
child.close()
assert parent.recv(16) == b"ready", "native guard startup failed"

def terminate_workers():
    # Container PID 1/control/Git are UID 0; all task code is UID 1000.
    # kill(-1) excludes the caller; no host PIDs are visible in this namespace.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass

parent.send(b"!")
parent.close()
_, status = os.waitpid(pid, 0)
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
with open(report_path, "w") as report:
    json.dump({"root_exit": os.waitstatus_to_exitcode(status), "cleanup": "confirmed"}, report)
"""
