# WASM Execution Engine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a WASM execution engine to llvmlite that compiles LLVM IR to WebAssembly and executes it via Wasmtime (native) or dlopen (Pyodide).

**Architecture:** Pure Python engine in `llvmlite/binding/wasmengine.py`. Compilation uses existing `emit_object()` for WASM targets + `wasm-ld` subprocess for linking. Execution dispatches to a backend interface with two implementations: WasmtimeBackend (native) and EmscriptenBackend (Pyodide). No C++ FFI changes.

**Tech Stack:** Python, wasmtime-py, wasm-ld (LLVM linker), llvmlite binding layer (ctypes)

**Design Doc:** `docs/plans/2026-02-24-wasm-execution-engine-design.md`

---

### Task 1: Exceptions and wasm-ld discovery

**Files:**
- Create: `llvmlite/binding/wasmengine.py`
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the failing test**

```python
# llvmlite/tests/test_wasmengine.py
import os
import shutil
import unittest

from llvmlite.binding.wasmengine import (
    WasmToolNotFoundError,
    WasmRuntimeError,
    _find_wasm_ld,
)


needs_wasm_ld = unittest.skipUnless(
    shutil.which('wasm-ld'), "wasm-ld not found on PATH"
)


class TestWasmLdDiscovery(unittest.TestCase):

    def test_explicit_path(self):
        path = _find_wasm_ld(wasm_ld_path='/usr/bin/true')
        self.assertEqual(path, '/usr/bin/true')

    def test_env_var(self):
        os.environ['WASM_LD'] = '/usr/bin/true'
        try:
            path = _find_wasm_ld()
            self.assertEqual(path, '/usr/bin/true')
        finally:
            del os.environ['WASM_LD']

    @needs_wasm_ld
    def test_which_fallback(self):
        path = _find_wasm_ld()
        self.assertIn('wasm-ld', path)

    def test_not_found_raises(self):
        old = os.environ.pop('WASM_LD', None)
        try:
            with self.assertRaises(WasmToolNotFoundError):
                _find_wasm_ld(wasm_ld_path=None,
                              _which=lambda x: None)
        finally:
            if old is not None:
                os.environ['WASM_LD'] = old

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(WasmToolNotFoundError, RuntimeError))
        self.assertTrue(issubclass(WasmRuntimeError, RuntimeError))
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmLdDiscovery -v`
Expected: FAIL with `ModuleNotFoundError` (wasmengine.py doesn't exist yet)

**Step 3: Write minimal implementation**

```python
# llvmlite/binding/wasmengine.py
import os
import shutil


class WasmToolNotFoundError(RuntimeError):
    pass


class WasmRuntimeError(RuntimeError):
    pass


def _find_wasm_ld(wasm_ld_path=None, _which=shutil.which):
    if wasm_ld_path is not None:
        return wasm_ld_path
    env = os.environ.get('WASM_LD')
    if env:
        return env
    found = _which('wasm-ld')
    if found:
        return found
    raise WasmToolNotFoundError(
        "wasm-ld not found. Install LLVM/Clang or set the WASM_LD "
        "environment variable to the path of wasm-ld."
    )
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmLdDiscovery -v`
Expected: PASS (all tests, or skip if wasm-ld not on PATH)

**Step 5: Commit**

```bash
git add llvmlite/binding/wasmengine.py llvmlite/tests/test_wasmengine.py
git commit -m "add wasm exceptions and wasm-ld discovery"
```

---

### Task 2: WasmBackend protocol and WasmtimeBackend

**Files:**
- Modify: `llvmlite/binding/wasmengine.py`
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the failing test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
try:
    import wasmtime as _wt
    _has_wasmtime = True
except ImportError:
    _has_wasmtime = False

needs_wasmtime = unittest.skipUnless(_has_wasmtime, "wasmtime not installed")

# Minimal valid WASM module: exports an `add` function (i32, i32) -> i32
# Hand-crafted binary for testing the backend independently of LLVM compilation.
# (wat equivalent):
#   (module
#     (func (export "add") (param i32 i32) (result i32)
#       local.get 0
#       local.get 1
#       i32.add)
#     (memory (export "memory") 1))
_WASM_ADD_MODULE = bytes([
    0x00, 0x61, 0x73, 0x6d,  # magic
    0x01, 0x00, 0x00, 0x00,  # version
    # type section: one func type (i32, i32) -> i32
    0x01, 0x07, 0x01, 0x60, 0x02, 0x7f, 0x7f, 0x01, 0x7f,
    # function section: one func, type index 0
    0x03, 0x02, 0x01, 0x00,
    # memory section: one memory, min 1 page
    0x05, 0x03, 0x01, 0x00, 0x01,
    # export section: "add" -> func 0, "memory" -> memory 0
    0x07, 0x11, 0x02,
    0x03, 0x61, 0x64, 0x64, 0x00, 0x00,
    0x06, 0x6d, 0x65, 0x6d, 0x6f, 0x72, 0x79, 0x02, 0x00,
    # code section: one func body
    0x0a, 0x09, 0x01, 0x07, 0x00, 0x20, 0x00, 0x20, 0x01, 0x6a, 0x0b,
])


@needs_wasmtime
class TestWasmtimeBackend(unittest.TestCase):

    def setUp(self):
        from llvmlite.binding.wasmengine import WasmtimeBackend
        self.backend = WasmtimeBackend()
        self.backend.load(_WASM_ADD_MODULE)

    def test_call_add(self):
        result = self.backend.call('add', 2, 3)
        self.assertEqual(result, 5)

    def test_call_negative(self):
        result = self.backend.call('add', -10, 7)
        self.assertEqual(result, -3)

    def test_get_export_names(self):
        names = self.backend.get_export_names()
        self.assertIn('add', names)

    def test_unknown_function(self):
        with self.assertRaises(KeyError):
            self.backend.call('nonexistent', 1, 2)

    def test_memory_read_write(self):
        self.backend.write_memory(0, b'\x01\x02\x03\x04')
        data = self.backend.read_memory(0, 4)
        self.assertEqual(data, b'\x01\x02\x03\x04')

    def test_get_memory(self):
        mem = self.backend.get_memory()
        self.assertIsNotNone(mem)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmtimeBackend -v`
Expected: FAIL with `ImportError` (WasmtimeBackend doesn't exist)

**Step 3: Write minimal implementation**

Append to `llvmlite/binding/wasmengine.py`:

```python
import sys


def _detect_backend(backend=None):
    if backend == 'wasmtime':
        return WasmtimeBackend()
    if backend == 'emscripten':
        return EmscriptenBackend()
    if backend is not None:
        raise ValueError(f"Unknown backend: {backend!r}")
    if sys.platform == 'emscripten':
        return EmscriptenBackend()
    return WasmtimeBackend()


class WasmtimeBackend:

    def __init__(self):
        try:
            import wasmtime
        except ImportError:
            raise ImportError(
                "wasmtime package is required for WASM execution on native "
                "platforms. Install it with: pip install wasmtime"
            )
        self._engine = wasmtime.Engine()
        self._store = wasmtime.Store(self._engine)
        self._instance = None
        self._module = None
        self._exports = {}

    def load(self, wasm_bytes):
        import wasmtime
        self._module = wasmtime.Module(self._engine, wasm_bytes)
        self._instance = wasmtime.Instance(
            self._store, self._module, []
        )
        self._exports = {}
        for export in self._module.exports:
            self._exports[export.name] = self._instance.exports(self._store).get(export.name)

    def call(self, name, *args):
        func = self._exports.get(name)
        if func is None:
            raise KeyError(f"No exported function named {name!r}")
        import wasmtime
        if not isinstance(func, wasmtime.Func):
            raise KeyError(f"{name!r} is not a function export")
        return func(self._store, *args)

    def get_memory(self):
        import wasmtime
        for val in self._exports.values():
            if isinstance(val, wasmtime.Memory):
                return val
        return None

    def write_memory(self, offset, data):
        mem = self.get_memory()
        if mem is None:
            raise RuntimeError("No memory export in WASM module")
        buf = mem.data_ptr(self._store)
        size = mem.data_len(self._store)
        if offset + len(data) > size:
            raise IndexError(
                f"Write at offset {offset} with length {len(data)} "
                f"exceeds memory size {size}"
            )
        import ctypes
        ctypes.memmove(buf + offset, data, len(data))

    def read_memory(self, offset, size):
        mem = self.get_memory()
        if mem is None:
            raise RuntimeError("No memory export in WASM module")
        ptr = mem.data_ptr(self._store)
        mem_size = mem.data_len(self._store)
        if offset + size > mem_size:
            raise IndexError(
                f"Read at offset {offset} with length {size} "
                f"exceeds memory size {mem_size}"
            )
        import ctypes
        return ctypes.string_at(ptr + offset, size)

    def get_export_names(self):
        return list(self._exports.keys())


class EmscriptenBackend:
    def __init__(self):
        raise NotImplementedError(
            "EmscriptenBackend is not yet implemented. "
            "This backend will use dlopen() on Emscripten's virtual FS."
        )
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmtimeBackend -v`
Expected: PASS (or all skipped if wasmtime not installed)

**Step 5: Commit**

```bash
git add llvmlite/binding/wasmengine.py llvmlite/tests/test_wasmengine.py
git commit -m "add wasmtime backend with load, call, and memory access"
```

---

### Task 3: WasmFunction callable wrapper

**Files:**
- Modify: `llvmlite/binding/wasmengine.py`
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the failing test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
@needs_wasmtime
class TestWasmFunction(unittest.TestCase):

    def setUp(self):
        from llvmlite.binding.wasmengine import WasmtimeBackend, WasmFunction
        self.backend = WasmtimeBackend()
        self.backend.load(_WASM_ADD_MODULE)

    def test_callable(self):
        from llvmlite.binding.wasmengine import WasmFunction
        func = WasmFunction('add', self.backend)
        result = func(10, 20)
        self.assertEqual(result, 30)

    def test_name(self):
        from llvmlite.binding.wasmengine import WasmFunction
        func = WasmFunction('add', self.backend)
        self.assertEqual(func.name, 'add')

    def test_unknown_function(self):
        from llvmlite.binding.wasmengine import WasmFunction
        with self.assertRaises(KeyError):
            WasmFunction('nope', self.backend)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmFunction -v`
Expected: FAIL with `ImportError` (WasmFunction doesn't exist)

**Step 3: Write minimal implementation**

Add to `llvmlite/binding/wasmengine.py`:

```python
class WasmFunction:

    def __init__(self, name, backend):
        # Verify the function exists
        backend.call  # ensure backend is valid
        export_names = backend.get_export_names()
        if name not in export_names:
            raise KeyError(f"No exported function named {name!r}")
        self._name = name
        self._backend = backend

    @property
    def name(self):
        return self._name

    def __call__(self, *args):
        return self._backend.call(self._name, *args)

    def __repr__(self):
        return f"WasmFunction({self._name!r})"
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmFunction -v`
Expected: PASS

**Step 5: Commit**

```bash
git add llvmlite/binding/wasmengine.py llvmlite/tests/test_wasmengine.py
git commit -m "add WasmFunction callable wrapper"
```

---

### Task 4: WasmExecutionEngine — compilation pipeline

This is the core task: wire up `emit_object()` → `wasm-ld` → `backend.load()`.

**Files:**
- Modify: `llvmlite/binding/wasmengine.py`
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the failing test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
import llvmlite.binding as llvm

needs_wasm_toolchain = unittest.skipUnless(
    shutil.which('wasm-ld') and _has_wasmtime,
    "requires wasm-ld and wasmtime"
)

# LLVM IR for a simple i32 add function, targeting wasm32
_WASM_IR_ADD = """\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define i32 @add(i32 %a, i32 %b) #0 {
  %result = add i32 %a, %b
  ret i32 %result
}

attributes #0 = { "wasm-export-name"="add" }
"""


@needs_wasm_toolchain
class TestWasmExecutionEngine(unittest.TestCase):

    def setUp(self):
        llvm.initialize_all_targets()
        llvm.initialize_all_asmprinters()

    def test_compile_and_call(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_ADD)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        func = engine.get_function('add')
        result = func(7, 8)
        self.assertEqual(result, 15)

    def test_get_wasm_bytes(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_ADD)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        wasm_bytes = engine.get_wasm_bytes()
        # WASM magic number
        self.assertEqual(wasm_bytes[:4], b'\x00asm')

    def test_finalize_before_get_function(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_ADD)
        engine = create_wasm_engine(mod)
        with self.assertRaises(RuntimeError):
            engine.get_function('add')

    def test_add_module_after_finalize(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_ADD)
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        mod2 = llvm.parse_assembly(_WASM_IR_ADD.replace('add', 'add2'))
        with self.assertRaises(RuntimeError):
            engine.add_module(mod2)

    def test_finalize_idempotent(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_ADD)
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        engine.finalize_object()  # should be no-op
        func = engine.get_function('add')
        self.assertEqual(func(1, 2), 3)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmExecutionEngine -v`
Expected: FAIL with `ImportError` (create_wasm_engine doesn't exist)

**Step 3: Write minimal implementation**

Add to `llvmlite/binding/wasmengine.py`:

```python
import subprocess
import tempfile
from pathlib import Path

from llvmlite.binding import targets


def _create_wasm_target_machine(triple='wasm32-unknown-unknown'):
    target = targets.Target.from_triple(triple)
    return target.create_target_machine(
        cpu='generic',
        features='',
        opt=2,
        reloc='default',
        codemodel='default',
    )


def _link_wasm(object_files, wasm_ld_path, output_path):
    cmd = [
        wasm_ld_path,
        '--no-entry',
        '--export-all',
        '--allow-undefined',
        '-o', str(output_path),
    ] + [str(f) for f in object_files]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"wasm-ld failed (exit code {result.returncode}):\n"
            f"{result.stderr}"
        )


def create_wasm_engine(module, target_machine=None, wasm_ld_path=None,
                       backend=None):
    if target_machine is None:
        target_machine = _create_wasm_target_machine()
    if wasm_ld_path is None:
        wasm_ld_path = _find_wasm_ld()
    engine = WasmExecutionEngine(
        target_machine=target_machine,
        wasm_ld_path=wasm_ld_path,
        backend=_detect_backend(backend),
    )
    engine.add_module(module)
    return engine


class WasmExecutionEngine:

    def __init__(self, target_machine, wasm_ld_path, backend):
        self._target_machine = target_machine
        self._wasm_ld_path = wasm_ld_path
        self._backend = backend
        self._modules = []
        self._finalized = False
        self._wasm_bytes = None

    def add_module(self, module):
        if self._finalized:
            raise RuntimeError(
                "Cannot add modules after finalize_object(). "
                "Create a new engine instead."
            )
        self._modules.append(module)

    def remove_module(self, module):
        if self._finalized:
            raise RuntimeError(
                "Cannot remove modules after finalize_object()."
            )
        self._modules.remove(module)

    def finalize_object(self):
        if self._finalized:
            return
        if not self._modules:
            raise RuntimeError("No modules to compile")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            object_files = []
            for i, mod in enumerate(self._modules):
                obj_bytes = self._target_machine.emit_object(mod)
                obj_path = tmpdir / f"module_{i}.o"
                obj_path.write_bytes(obj_bytes)
                object_files.append(obj_path)

            wasm_path = tmpdir / "output.wasm"
            _link_wasm(object_files, self._wasm_ld_path, wasm_path)
            self._wasm_bytes = wasm_path.read_bytes()

        self._backend.load(self._wasm_bytes)
        self._finalized = True

    def get_function(self, name):
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before get_function()"
            )
        return WasmFunction(name, self._backend)

    def get_wasm_bytes(self):
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before get_wasm_bytes()"
            )
        return self._wasm_bytes

    def get_memory(self):
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before get_memory()"
            )
        return self._backend.get_memory()

    def write_memory(self, offset, data):
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before write_memory()"
            )
        self._backend.write_memory(offset, data)

    def read_memory(self, offset, size):
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before read_memory()"
            )
        return self._backend.read_memory(offset, size)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmExecutionEngine -v`
Expected: PASS (or skip if wasm-ld/wasmtime missing)

**Step 5: Commit**

```bash
git add llvmlite/binding/wasmengine.py llvmlite/tests/test_wasmengine.py
git commit -m "add WasmExecutionEngine with compile-link-execute pipeline"
```

---

### Task 5: Memory round-trip test (functions + linear memory)

**Files:**
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
import struct

# LLVM IR: reads an i32 from memory address 0, adds 42, stores result at address 4
_WASM_IR_MEMORY = """\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define void @add42() #0 {
  %ptr_in = inttoptr i32 0 to ptr
  %val = load i32, ptr %ptr_in
  %result = add i32 %val, 42
  %ptr_out = inttoptr i32 4 to ptr
  store i32 %result, ptr %ptr_out
  ret void
}

attributes #0 = { "wasm-export-name"="add42" }
"""


@needs_wasm_toolchain
class TestWasmMemory(unittest.TestCase):

    def setUp(self):
        llvm.initialize_all_targets()
        llvm.initialize_all_asmprinters()

    def test_memory_round_trip(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_MEMORY)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()

        # Write input value (100) at offset 0
        engine.write_memory(0, struct.pack('<i', 100))

        # Call function that reads offset 0, adds 42, writes to offset 4
        func = engine.get_function('add42')
        func()

        # Read result from offset 4
        result_bytes = engine.read_memory(4, 4)
        result = struct.unpack('<i', result_bytes)[0]
        self.assertEqual(result, 142)
```

**Step 2: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmMemory -v`
Expected: PASS (no new implementation needed — this exercises the existing pipeline)

**Step 3: Commit**

```bash
git add llvmlite/tests/test_wasmengine.py
git commit -m "add memory round-trip integration test for wasm engine"
```

---

### Task 6: Export from llvmlite.binding

**Files:**
- Modify: `llvmlite/binding/__init__.py` (line 17, after `from .orcjit import *`)

**Step 1: Write the failing test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
class TestPublicAPI(unittest.TestCase):

    def test_importable_from_binding(self):
        from llvmlite.binding import (
            create_wasm_engine,
            WasmExecutionEngine,
            WasmFunction,
            WasmToolNotFoundError,
            WasmRuntimeError,
        )
        # Just verify they're importable
        self.assertTrue(callable(create_wasm_engine))
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestPublicAPI -v`
Expected: FAIL with `ImportError`

**Step 3: Add the export**

In `llvmlite/binding/__init__.py`, after line 17 (`from .orcjit import *`), add:

```python
from .wasmengine import (create_wasm_engine, WasmExecutionEngine,
                         WasmFunction, WasmToolNotFoundError,
                         WasmRuntimeError)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestPublicAPI -v`
Expected: PASS

**Step 5: Run full test suite to check for regressions**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py -v`
Expected: All tests PASS (or skip where tooling is missing)

**Step 6: Commit**

```bash
git add llvmlite/binding/__init__.py llvmlite/tests/test_wasmengine.py
git commit -m "export wasm engine public API from llvmlite.binding"
```

---

### Task 7: f64 function test (type coverage)

**Files:**
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the test**

Append to `llvmlite/tests/test_wasmengine.py`:

```python
_WASM_IR_FPADD = """\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define double @fpadd(double %a, double %b) #0 {
  %result = fadd double %a, %b
  ret double %result
}

attributes #0 = { "wasm-export-name"="fpadd" }
"""


@needs_wasm_toolchain
class TestWasmFloatingPoint(unittest.TestCase):

    def setUp(self):
        llvm.initialize_all_targets()
        llvm.initialize_all_asmprinters()

    def test_f64_add(self):
        from llvmlite.binding.wasmengine import create_wasm_engine
        mod = llvm.parse_assembly(_WASM_IR_FPADD)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        func = engine.get_function('fpadd')
        result = func(1.5, 2.25)
        self.assertAlmostEqual(result, 3.75)
```

**Step 2: Run test**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmFloatingPoint -v`
Expected: PASS

**Step 3: Commit**

```bash
git add llvmlite/tests/test_wasmengine.py
git commit -m "add f64 floating point test for wasm engine"
```

---

### Task 8: Full round-trip from llvmlite.ir

**Files:**
- Test: `llvmlite/tests/test_wasmengine.py`

**Step 1: Write the test**

This test builds IR programmatically using `llvmlite.ir`, converts to string, parses, compiles, and executes — proving the full pipeline works.

Append to `llvmlite/tests/test_wasmengine.py`:

```python
from llvmlite import ir


@needs_wasm_toolchain
class TestWasmIRRoundTrip(unittest.TestCase):

    def setUp(self):
        llvm.initialize_all_targets()
        llvm.initialize_all_asmprinters()

    def test_ir_builder_to_wasm(self):
        from llvmlite.binding.wasmengine import create_wasm_engine

        # Build IR programmatically
        module = ir.Module(name="test_roundtrip")
        module.triple = "wasm32-unknown-unknown"
        module.data_layout = "e-m:e-p:32:32-i64:64-n32:64-S128"

        func_type = ir.FunctionType(ir.IntType(32),
                                    [ir.IntType(32), ir.IntType(32)])
        func = ir.Function(module, func_type, name="multiply")
        func.attributes.add('wasm-export-name="multiply"')
        block = func.append_basic_block(name="entry")
        builder = ir.IRBuilder(block)
        result = builder.mul(func.args[0], func.args[1])
        builder.ret(result)

        # Parse, compile, execute
        llvm_ir = str(module)
        mod = llvm.parse_assembly(llvm_ir)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        multiply = engine.get_function('multiply')
        self.assertEqual(multiply(6, 7), 42)
```

**Step 2: Run test**

Run: `python -m pytest llvmlite/tests/test_wasmengine.py::TestWasmIRRoundTrip -v`
Expected: PASS

**Step 3: Commit**

```bash
git add llvmlite/tests/test_wasmengine.py
git commit -m "add full ir-to-wasm round-trip integration test"
```

---

## Summary

| Task | What it builds | Key files |
|------|---------------|-----------|
| 1 | Exceptions + wasm-ld discovery | `wasmengine.py`, `test_wasmengine.py` |
| 2 | WasmtimeBackend (load, call, memory) | `wasmengine.py`, `test_wasmengine.py` |
| 3 | WasmFunction callable wrapper | `wasmengine.py`, `test_wasmengine.py` |
| 4 | WasmExecutionEngine (full pipeline) | `wasmengine.py`, `test_wasmengine.py` |
| 5 | Memory round-trip integration test | `test_wasmengine.py` |
| 6 | Public API export | `__init__.py`, `test_wasmengine.py` |
| 7 | f64 type coverage test | `test_wasmengine.py` |
| 8 | Full llvmlite.ir round-trip test | `test_wasmengine.py` |
