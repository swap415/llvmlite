# WASM Execution Engine Design

**Issue:** [numba/llvmlite#836](https://github.com/numba/llvmlite/issues/836)
**Date:** 2026-02-24
**Branch:** `wasm-execution-engine`

## Problem

llvmlite's execution engines (MCJIT, OrcJIT) write machine code to process memory and flip the execute bit. This is impossible inside WebAssembly's sandboxed memory model. We need an alternative execution engine that compiles LLVM IR to WASM and executes it.

## Decisions

| Decision | Choice |
|----------|--------|
| Use case | Both native platforms + Pyodide |
| Native runtime | Wasmtime-py |
| Pyodide runtime | dlopen() on Emscripten virtual FS |
| API style | Hybrid — mirrors ExecutionEngine, returns callable wrappers instead of raw uint64 addresses |
| Scope | Functions + linear memory (no globals initially) |
| Compilation | emit_object() -> wasm-ld subprocess -> .wasm bytes |
| Architecture | Pure Python, no C++ FFI changes |
| Development order | Native-first, Pyodide-aware structure |

## Architecture

```
User Code
    |
    v
WasmExecutionEngine (llvmlite/binding/wasmengine.py)
    |
    +-- finalize_object():
    |     1. target_machine.emit_object() -> .o bytes
    |     2. wasm-ld subprocess -> .wasm bytes
    |     3. backend.load(wasm_bytes)
    |
    +-- get_function(name) -> WasmFunction callable
    +-- get_memory() / read_memory() / write_memory()
    |
    +-- Backend auto-detection:
          |
          +-- WasmtimeBackend (sys.platform != 'emscripten')
          +-- EmscriptenBackend (sys.platform == 'emscripten')
```

## API

### Creation

```python
def create_wasm_engine(module, target_machine=None, wasm_ld_path=None, backend=None):
    """
    module: ModuleRef (parsed IR)
    target_machine: TargetMachine for wasm32/64 (auto-created if None)
    wasm_ld_path: path to wasm-ld binary (auto-detected if None)
    backend: 'wasmtime' | 'emscripten' | None (auto-detect)
    """
```

### WasmExecutionEngine

Core API (mirrors ExecutionEngine):
- `add_module(module)` — add IR module for compilation
- `finalize_object()` — compile all modules: emit -> link -> load
- `get_function(name)` — returns WasmFunction callable
- `remove_module(module)` — remove a previously added module

WASM-specific extensions:
- `get_wasm_bytes()` — return linked .wasm binary
- `get_memory()` — buffer-protocol object over linear memory
- `write_memory(offset, data)` — write bytes into linear memory
- `read_memory(offset, size)` — read bytes from linear memory

Intentionally omitted:
- `get_function_address()` — meaningless in WASM
- `get_global_value_address()` — deferred
- `enable_jit_events()` — no WASM profiler integration yet
- `set_object_cache()` — `get_wasm_bytes()` serves this purpose

### WasmFunction

```python
class WasmFunction:
    def __call__(self, *args):       # call with Python scalars
    name: str                        # export name
    params: tuple                    # WASM param types (i32, i64, f32, f64)
    results: tuple                   # WASM return types
```

## Backend Interface

```python
class WasmBackend:
    def load(self, wasm_bytes): ...
    def call(self, name, *args): ...
    def get_memory(self): ...
    def write_memory(self, offset, data): ...
    def read_memory(self, offset, size): ...
    def get_export_names(self): ...
```

Detection: `sys.platform == 'emscripten'` selects EmscriptenBackend, otherwise WasmtimeBackend.

## Compilation Pipeline

```
ModuleRef -> emit_object() -> .o bytes -> tmpfile
                                             |
                                          wasm-ld --no-entry --export-all --allow-undefined
                                             |
                                          .wasm bytes -> backend.load() -> ready
```

wasm-ld discovery order:
1. Explicit `wasm_ld_path` parameter
2. `WASM_LD` environment variable
3. `shutil.which('wasm-ld')`
4. Adjacent to `llvm-config`
5. Raise `WasmToolNotFoundError`

Multiple modules: each `add_module()` produces a `.o`, all linked in one `wasm-ld` call.

## Error Handling

- `WasmRuntimeError(RuntimeError)` — wraps WASM traps from either backend
- `WasmToolNotFoundError(RuntimeError)` — wasm-ld not found, with install instructions
- `wasmtime` not installed -> `ImportError` with hint
- `get_function()` before `finalize_object()` -> `RuntimeError`
- `add_module()` after `finalize_object()` -> `RuntimeError`
- `finalize_object()` twice -> no-op

## State Machine

```
Created -> [add_module()]* -> finalize_object() -> [get_function() / get_memory()]*
```

## Files

New:
- `llvmlite/binding/wasmengine.py` — engine, backends, WasmFunction
- `llvmlite/tests/test_wasmengine.py` — tests

Modified:
- `llvmlite/binding/__init__.py` — export create_wasm_engine, WasmExecutionEngine

## Dependencies

- `wasmtime` (optional, PyPI, native only)
- `wasm-ld` (system tool, required at compile time)

## Testing

Unit tests (gated behind `pytest.mark.wasm`):
- Compile & execute scalar functions (i32 add, f64 multiply, void)
- Multiple modules with cross-references
- Memory read/write round-trip
- get_wasm_bytes() returns valid WASM magic
- Error cases (wrong state, missing function, type mismatch, missing wasm-ld)
- Explicit backend selection

Integration tests:
- Full round-trip from llvmlite.ir -> string -> parse -> compile -> execute
- Array via linear memory: write floats, sum in WASM, read result

Deferred:
- Emscripten backend tests (needs Pyodide environment)
- Performance benchmarks
- Stress tests
