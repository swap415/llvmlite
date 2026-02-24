import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


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


class WasmFunction:

    def __init__(self, name, backend):
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


def _create_wasm_target_machine(triple='wasm32-unknown-unknown'):
    from llvmlite.binding import targets
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
    result = subprocess.run(cmd, capture_output=True, text=True)
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


class EmscriptenBackend:
    def __init__(self):
        raise NotImplementedError(
            "EmscriptenBackend is not yet implemented. "
            "This backend will use dlopen() on Emscripten's virtual FS."
        )
