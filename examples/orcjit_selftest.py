"""
Self-contained regression checks for llvmlite's ORC/LLJIT bindings.

Mirrors a subset of ``llvmlite/tests/test_binding.py::TestOrcLLJIT`` but
runs as a plain script so it works against the installed wheel (no source
tree or pytest harness required).

Run:

    python examples/orcjit_selftest.py

Exits 0 if all checks pass, 1 on failure.
"""
from __future__ import annotations

import ctypes
import gc
import sys
import threading
import traceback
from ctypes import CFUNCTYPE, c_int, c_int32

import llvmlite.binding as llvm


llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

TRIPLE = llvm.get_default_triple()

IR_SUM = f"""
target triple = "{TRIPLE}"
define i32 @sum(i32 %a, i32 %b) {{
  %r = add i32 %a, %b
  ret i32 %r
}}
"""

IR_MUL = f"""
target triple = "{TRIPLE}"
define i32 @mul(i32 %a, i32 %b) {{
  %r = mul i32 %a, %b
  ret i32 %r
}}
"""

IR_SQSUM_DEPENDS_ON_SUM = f"""
target triple = "{TRIPLE}"
declare i32 @sum(i32, i32)
define i32 @square_sum(i32 %a, i32 %b) {{
  %s = call i32 @sum(i32 %a, i32 %b)
  %r = mul i32 %s, %s
  ret i32 %r
}}
"""


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def run(self, name, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001 - report and continue
            self.failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
        else:
            self.passed += 1
            print(f"ok    {name}")


def assert_equal(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


# ---- individual checks ----------------------------------------------------

def check_basic_call():
    lljit = llvm.create_lljit_compiler()
    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sum"))
    fn = CFUNCTYPE(c_int, c_int, c_int)(rt["sum"])
    assert_equal(fn(2, -5), -3, "sum(2,-5)")


def check_lookup_returns_same_address():
    lljit = llvm.create_lljit_compiler()
    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sum"))
    rt2 = lljit.lookup("sum", "sum")
    assert_equal(rt2["sum"], rt["sum"], "lookup address mismatch")


def check_lookup_missing_library():
    lljit = llvm.create_lljit_compiler()
    try:
        lljit.lookup("nope", "x")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for missing library")


def check_define_symbol():
    """import_symbol registers a host address that can be re-exported."""
    lljit = llvm.create_lljit_compiler()
    rt = (llvm.JITLibraryBuilder()
          .import_symbol("__xyzzy", 1234)
          .export_symbol("__xyzzy")
          .link(lljit, "xyz"))
    assert_equal(rt["__xyzzy"], 1234, "xyzzy address")


def check_two_libraries():
    lljit = llvm.create_lljit_compiler()
    (llvm.JITLibraryBuilder()
        .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sumlib"))
    rt_mul = (llvm.JITLibraryBuilder()
              .add_ir(IR_MUL).export_symbol("mul").link(lljit, "mullib"))
    assert_equal(CFUNCTYPE(c_int, c_int, c_int)(rt_mul["mul"])(2, -5), -10)

    # cross-library lookup must NOT find symbols from the other dylib
    try:
        lljit.lookup("sumlib", "mul")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError for cross-lib lookup")


def check_inter_library_call():
    lljit = llvm.create_lljit_compiler()
    # The dependency's tracker must stay alive: ORC drops the dylib when
    # the Python tracker is GC'd, even if other libraries still reference it.
    rt_sum = (llvm.JITLibraryBuilder()
              .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sumlib"))
    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_SQSUM_DEPENDS_ON_SUM)
          .add_jit_library("sumlib")
          .export_symbol("square_sum")
          .link(lljit, "sqlib"))
    fn = CFUNCTYPE(c_int, c_int, c_int)(rt["square_sum"])
    assert_equal(fn(2, -5), 9, "square_sum(2,-5)")
    # Keep both alive across the call.
    del rt_sum, rt


def check_tracker_unload_removes_symbol():
    lljit = llvm.create_lljit_compiler()
    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sum"))
    del rt
    gc.collect()
    try:
        lljit.lookup("sum", "sum")
    except RuntimeError:
        return
    raise AssertionError("expected lookup to fail after tracker GC")


def check_host_callback():
    """JITted code calls a Python ctypes trampoline via import_symbol."""
    record = []

    CB_T = CFUNCTYPE(None, c_int32)

    @CB_T
    def cb(n):
        record.append(int(n))

    addr = ctypes.cast(cb, ctypes.c_void_p).value

    ir = f"""
    target triple = "{TRIPLE}"
    declare void @cb(i32)
    define void @go(i32 %x) {{
      call void @cb(i32 %x)
      ret void
    }}
    """
    lljit = llvm.create_lljit_compiler()
    rt = (llvm.JITLibraryBuilder()
          .add_ir(ir)
          .import_symbol("cb", addr)
          .export_symbol("go")
          .link(lljit, "cb_lib"))
    CFUNCTYPE(None, c_int32)(rt["go"])(7)
    assert_equal(record, [7], "callback record")
    # Keep both alive for the duration of the call.
    del cb, rt


def check_thread_safety():
    """Concurrent JITLibraryBuilder.link on shared LLJIT must not crash."""
    lljit = llvm.create_lljit_compiler()
    errors = []

    def worker(tid):
        try:
            for c in range(20):
                llvm.JITLibraryBuilder().add_ir(IR_SUM)\
                    .export_symbol("sum").link(lljit, f"sum_{tid}_{c}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise AssertionError(f"thread errors: {errors!r}")


def check_target_data_singleton():
    lljit = llvm.create_lljit_compiler()
    td1 = lljit.target_data
    td2 = lljit.target_data
    assert td1 is td2, "target_data should be cached"
    str(td1)  # should not crash


def check_close_idempotent():
    lljit = llvm.create_lljit_compiler()
    (llvm.JITLibraryBuilder()
        .add_ir(IR_SUM).export_symbol("sum").link(lljit, "sum"))
    lljit.close()
    lljit.close()
    try:
        lljit.lookup("sum", "sum")
    except AssertionError:
        return
    raise AssertionError("expected AssertionError when looking up on closed JIT")


def main():
    checks = Checks()
    checks.run("basic call",                       check_basic_call)
    checks.run("lookup returns same address",      check_lookup_returns_same_address)
    checks.run("lookup of missing library fails",  check_lookup_missing_library)
    checks.run("import_symbol re-export",          check_define_symbol)
    checks.run("two libraries isolated",           check_two_libraries)
    checks.run("inter-library call via deps",      check_inter_library_call)
    checks.run("tracker GC unloads library",       check_tracker_unload_removes_symbol)
    checks.run("host callback via import_symbol",  check_host_callback)
    checks.run("thread-safe concurrent link",      check_thread_safety)
    checks.run("target_data is cached",            check_target_data_singleton)
    checks.run("close() is idempotent",            check_close_idempotent)

    total = checks.passed + checks.failed
    print(f"\n{checks.passed}/{total} passed, {checks.failed} failed")
    sys.exit(0 if checks.failed == 0 else 1)


if __name__ == "__main__":
    main()
