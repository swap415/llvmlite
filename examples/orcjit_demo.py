"""
A guided tour of llvmlite's ORC/LLJIT API.

llvmlite ships two JIT wrappers:

  * ``create_mcjit_compiler`` (legacy MCJIT engine)
  * ``create_lljit_compiler`` (modern ORC/LLJIT engine)

This script focuses on the ORC path. It walks through, in order:

  1. Building a single library from LLVM IR text and calling an exported symbol.
  2. Linking a second library that *depends on* the first (cross-library call).
  3. Importing a host-process symbol (a C callback) so JITted code can call it.
  4. Looking a symbol back up after the fact via ``LLJIT.lookup``.
  5. Showing that a library is unloaded when its ResourceTracker is GC'd.

Run it directly:

    python examples/orcjit_demo.py
"""
from __future__ import annotations

import ctypes
import gc

import llvmlite.binding as llvm


# ---------------------------------------------------------------------------
# One-time native target setup. ``llvm.initialize()`` is deprecated in recent
# llvmlite releases; the native target/asm-printer initializers are still
# required for codegen on the current host.
# ---------------------------------------------------------------------------
llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

TRIPLE = llvm.get_default_triple()


# ---------------------------------------------------------------------------
# IR fragments used below. They are parameterised by triple so the JIT picks
# the host data layout/calling convention.
# ---------------------------------------------------------------------------
IR_SUM = f"""
target triple = "{TRIPLE}"

define i32 @sum(i32 %a, i32 %b) {{
  %r = add i32 %a, %b
  ret i32 %r
}}
"""

# A second library that *calls* @sum from the first one. Note the bare
# ``declare`` — at link time ORC resolves it against the ``sumlib`` dylib
# we register as a dependency.
IR_SQUARE_SUM = f"""
target triple = "{TRIPLE}"

declare i32 @sum(i32, i32)

define i32 @square_sum(i32 %a, i32 %b) {{
  %s = call i32 @sum(i32 %a, i32 %b)
  %r = mul i32 %s, %s
  ret i32 %r
}}
"""

# This module declares an external @host_log function. We don't link any IR
# that defines it; instead we hand its address to the JIT through
# ``import_symbol`` so the linker resolves the reference to a Python-side
# C callback.
IR_GREETER = f"""
target triple = "{TRIPLE}"

declare void @host_log(i32)

define i32 @greet(i32 %n) {{
  call void @host_log(i32 %n)
  %r = add i32 %n, 1
  ret i32 %r
}}
"""


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def demo_single_library(lljit: llvm.LLJIT) -> None:
    section("1) Compile a library and call its exported function")

    rt_sum = (llvm.JITLibraryBuilder()
              .add_ir(IR_SUM)
              .export_symbol("sum")
              .link(lljit, "sumlib"))

    sum_fn = ctypes.CFUNCTYPE(ctypes.c_int32,
                              ctypes.c_int32,
                              ctypes.c_int32)(rt_sum["sum"])
    print(f"sum(2, 3) = {sum_fn(2, 3)}")
    return rt_sum


def demo_cross_library(lljit: llvm.LLJIT, rt_sum) -> None:
    section("2) Link a second library that depends on the first")

    # ``add_jit_library('sumlib')`` tells ORC: when you see an unresolved
    # symbol, also search the dylib named 'sumlib' (the one we built above).
    rt_sq = (llvm.JITLibraryBuilder()
             .add_ir(IR_SQUARE_SUM)
             .add_jit_library("sumlib")
             .export_symbol("square_sum")
             .link(lljit, "squarelib"))

    sq_fn = ctypes.CFUNCTYPE(ctypes.c_int32,
                             ctypes.c_int32,
                             ctypes.c_int32)(rt_sq["square_sum"])
    print(f"square_sum(2, 3) = {sq_fn(2, 3)}  (expect 25)")
    # Keep rt_sum alive only as long as rt_sq needs it; ORC will track the
    # internal reference, but the *Python* tracker for rt_sum must outlive
    # any caller too. Returning rt_sq from this scope is fine; rt_sum is
    # owned by the caller.
    return rt_sq


def demo_host_callback(lljit: llvm.LLJIT):
    section("3) Let JITted code call back into Python via import_symbol")

    log_calls = []

    HOST_LOG_T = ctypes.CFUNCTYPE(None, ctypes.c_int32)

    @HOST_LOG_T
    def host_log(n):
        # This Python callable is invoked from JITted machine code.
        log_calls.append(int(n))
        print(f"  [python] host_log({n}) called from JIT")

    # ctypes.cast(..., c_void_p).value gives the integer address of the
    # trampoline; ``import_symbol`` registers that address as the resolution
    # for the IR's external ``@host_log`` declaration.
    host_log_addr = ctypes.cast(host_log, ctypes.c_void_p).value

    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_GREETER)
          .import_symbol("host_log", host_log_addr)
          .export_symbol("greet")
          .link(lljit, "greetlib"))

    greet_fn = ctypes.CFUNCTYPE(ctypes.c_int32,
                                ctypes.c_int32)(rt["greet"])
    out = greet_fn(41)
    print(f"greet(41) returned {out}; python recorded {log_calls}")

    # Keep both the tracker AND the ctypes trampoline alive on the returned
    # object — if either is GC'd before the next JIT call, you get a crash.
    return rt, host_log


def demo_lookup(lljit: llvm.LLJIT) -> None:
    section("4) Look a symbol up after the library is loaded")

    # ``lljit.lookup`` returns its own ResourceTracker; the address is the
    # same one ``JITLibraryBuilder.link`` already gave us.
    rt = lljit.lookup("sumlib", "sum")
    print(f"  address from lookup:  {hex(rt['sum'])}")


def demo_unload(lljit: llvm.LLJIT) -> None:
    section("5) Dropping a ResourceTracker unloads its library")

    rt = (llvm.JITLibraryBuilder()
          .add_ir(IR_SUM.replace("@sum", "@throwaway"))
          .export_symbol("throwaway")
          .link(lljit, "throwlib"))
    assert rt["throwaway"] != 0
    del rt
    gc.collect()

    # After the tracker is gone, lookup should fail.
    try:
        lljit.lookup("throwlib", "throwaway")
    except RuntimeError as e:
        print(f"  lookup after unload failed as expected: "
              f"{type(e).__name__}: {str(e)[:80]}")
    else:
        raise SystemExit("expected lookup to fail after unload")


def main() -> None:
    lljit = llvm.create_lljit_compiler()
    print(f"created LLJIT, target_data = {lljit.target_data}")

    rt_sum = demo_single_library(lljit)
    rt_sq = demo_cross_library(lljit, rt_sum)         # noqa: F841 (kept alive)
    rt_greet, host_log = demo_host_callback(lljit)    # noqa: F841 (kept alive)
    demo_lookup(lljit)
    demo_unload(lljit)

    print("\nAll demos completed.")


if __name__ == "__main__":
    main()
