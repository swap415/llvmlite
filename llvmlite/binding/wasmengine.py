import ctypes
import os
import shutil
import sys


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
            self._exports[export.name] = (
                self._instance.exports(self._store).get(export.name)
            )

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

    def _memory_base(self):
        """Return (base_address, byte_length) for the exported memory."""
        mem = self.get_memory()
        if mem is None:
            raise RuntimeError("No memory export in WASM module")
        base = ctypes.addressof(mem.data_ptr(self._store).contents)
        return base, mem.data_len(self._store)

    def write_memory(self, offset, data):
        base, size = self._memory_base()
        if offset + len(data) > size:
            raise IndexError(
                f"Write at offset {offset} with length {len(data)} "
                f"exceeds memory size {size}"
            )
        ctypes.memmove(base + offset, data, len(data))

    def read_memory(self, offset, size):
        base, mem_size = self._memory_base()
        if offset + size > mem_size:
            raise IndexError(
                f"Read at offset {offset} with length {size} "
                f"exceeds memory size {mem_size}"
            )
        return ctypes.string_at(base + offset, size)

    def get_export_names(self):
        return list(self._exports.keys())


class EmscriptenBackend:
    def __init__(self):
        raise NotImplementedError(
            "EmscriptenBackend is not yet implemented. "
            "This backend will use dlopen() on Emscripten's virtual FS."
        )
