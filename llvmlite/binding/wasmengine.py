import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class WasmToolNotFoundError(RuntimeError):
    """Raised when wasm-ld cannot be found."""
    pass


class WasmRuntimeError(RuntimeError):
    """Raised when a WASM runtime trap occurs during execution."""
    pass


def _find_wasm_ld(wasm_ld_path=None, _which=shutil.which):
    """Locate wasm-ld: explicit path > WASM_LD env var > PATH lookup."""
    # In Emscripten, linking is done via JavaScript - no native wasm-ld needed
    if sys.platform == 'emscripten':
        return None
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
                "platforms. Install the wasmtime package."
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
        """Call an exported WASM function by name."""
        func = self._exports.get(name)
        if func is None:
            raise KeyError(f"No exported function named {name!r}")
        import wasmtime
        if not isinstance(func, wasmtime.Func):
            raise KeyError(f"{name!r} is not a function export")
        try:
            return func(self._store, *args)
        except wasmtime.WasmtimeError as e:
            raise WasmRuntimeError(str(e)) from e

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
        """Write bytes into WASM linear memory at offset."""
        base, size = self._memory_base()
        if offset < 0 or offset + len(data) > size:
            raise IndexError(
                f"Write at offset {offset} with length {len(data)} "
                f"exceeds memory size {size}"
            )
        ctypes.memmove(base + offset, data, len(data))

    def read_memory(self, offset, size):
        """Read bytes from WASM linear memory at offset."""
        base, mem_size = self._memory_base()
        if offset < 0 or offset + size > mem_size:
            raise IndexError(
                f"Read at offset {offset} with length {size} "
                f"exceeds memory size {mem_size}"
            )
        return ctypes.string_at(base + offset, size)

    def get_export_names(self):
        return list(self._exports.keys())


class WasmFunction:
    """A callable wrapper around an exported WASM function."""

    def __init__(self, name, backend):
        export_names = backend.get_export_names()
        if name not in export_names:
            raise KeyError(f"No exported function named {name!r}")
        self._name = name
        self._backend = backend

    @property
    def name(self):
        """The export name of this function."""
        return self._name

    def __call__(self, *args):
        """Call the WASM function with the given arguments."""
        return self._backend.call(self._name, *args)

    def __repr__(self):
        return f"WasmFunction({self._name!r})"


def _create_wasm_target_machine(triple='wasm32-unknown-unknown', features=''):
    from llvmlite.binding import targets
    target = targets.Target.from_triple(triple)
    return target.create_target_machine(
        cpu='generic',
        features=features,
        opt=2,
        reloc='default',
        codemodel='default',
    )


def _link_wasm(object_files, wasm_ld_path, output_path):
    """Link object files into a WASM binary using wasm-ld."""
    if sys.platform == 'emscripten':
        _link_wasm_emscripten(object_files, output_path)
    else:
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


def _link_wasm_emscripten(object_files, output_path):
    """
    Link WASM in Emscripten/Pyodide using JavaScript wasm-ld.

    Requires globalThis.wasmLd to be loaded (from llvm-wasm or similar).
    """
    from js import globalThis, Uint8Array
    from pyodide.ffi import to_js

    if not hasattr(globalThis, 'wasmLd'):
        raise RuntimeError(
            "Browser WASM linking requires globalThis.wasmLd. "
            "Load wasm-ld.wasm from https://github.com/aspect-build/aspect-workflows-llvm "
            "or similar before using llvmlite in the browser."
        )

    # Read all object files
    obj_bytes_list = [Path(f).read_bytes() for f in object_files]

    # Call JavaScript wasm-ld
    wasm_bytes = globalThis.wasmLd(to_js(obj_bytes_list))
    Path(output_path).write_bytes(bytes(wasm_bytes.to_py()))


def create_wasm_engine(module, target_machine=None, wasm_ld_path=None,
                       backend=None):
    """
    Create a WASM execution engine from the given *module*.

    *target_machine* is a TargetMachine for wasm32/64 (auto-created if None).
    *wasm_ld_path* is the path to wasm-ld (auto-detected if None).
    *backend* selects the runtime: 'wasmtime', 'emscripten', or None
    (auto-detect).
    """
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
    """An execution engine that compiles LLVM IR to WebAssembly and runs it."""

    def __init__(self, target_machine, wasm_ld_path, backend):
        self._target_machine = target_machine
        self._wasm_ld_path = wasm_ld_path
        self._backend = backend
        self._modules = []
        self._finalized = False
        self._wasm_bytes = None

    def add_module(self, module):
        """Add an IR module for compilation."""
        if self._finalized:
            raise RuntimeError(
                "Cannot add modules after finalize_object(). "
                "Create a new engine instead."
            )
        self._modules.append(module)

    def remove_module(self, module):
        """Remove a previously added module."""
        if self._finalized:
            raise RuntimeError(
                "Cannot remove modules after finalize_object()."
            )
        self._modules.remove(module)

    def finalize_object(self):
        """Compile all modules: emit object code, link with wasm-ld, load."""
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
        """Return a callable WasmFunction for the named export."""
        if not self._finalized:
            raise RuntimeError(
                "Must call finalize_object() before get_function()"
            )
        return WasmFunction(name, self._backend)

    def get_wasm_bytes(self):
        """Return the linked .wasm binary as bytes."""
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
    """
    WASM execution backend for Pyodide/Emscripten environments.

    Uses the browser's WebAssembly API via Pyodide's JavaScript interop.
    This enables llvmlite to compile and run WASM entirely in the browser.
    """

    def __init__(self):
        if sys.platform != 'emscripten':
            raise RuntimeError(
                "EmscriptenBackend is only available in Pyodide/Emscripten. "
                "Use WasmtimeBackend for native platforms."
            )
        # Import Pyodide's JavaScript interop
        try:
            from js import WebAssembly, Uint8Array
            from pyodide.ffi import to_js
        except ImportError:
            raise ImportError(
                "EmscriptenBackend requires Pyodide. "
                "This backend only works in browser environments."
            )
        self._WebAssembly = WebAssembly
        self._Uint8Array = Uint8Array
        self._to_js = to_js
        self._instance = None
        self._module = None
        self._exports = {}
        self._memory = None

    def load(self, wasm_bytes):
        """Load WASM bytes using browser's WebAssembly.instantiate()."""
        import asyncio
        from pyodide.ffi import to_js

        # Convert Python bytes to JavaScript Uint8Array
        js_bytes = self._Uint8Array.new(to_js(wasm_bytes))

        # WebAssembly.instantiate is async, use Pyodide's event loop
        async def _load():
            result = await self._WebAssembly.instantiate(js_bytes)
            return result

        # Run the async load
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(_load())

        self._module = result.module
        self._instance = result.instance

        # Cache exports
        self._exports = {}
        exports_obj = self._instance.exports
        # Get export names via Object.keys()
        from js import Object
        for name in Object.keys(exports_obj):
            self._exports[name] = getattr(exports_obj, name)

        # Cache memory reference if available
        if 'memory' in self._exports:
            self._memory = self._exports['memory']

    def call(self, name, *args):
        """Call an exported WASM function by name."""
        func = self._exports.get(name)
        if func is None:
            raise KeyError(f"No exported function named {name!r}")

        # Convert Python ints to JavaScript BigInt for i64 params
        from pyodide.ffi import to_js
        js_args = []
        for arg in args:
            if isinstance(arg, int) and (arg > 2**31 - 1 or arg < -2**31):
                # Large int needs BigInt
                from js import BigInt
                js_args.append(BigInt(arg))
            else:
                js_args.append(to_js(arg))

        try:
            result = func(*js_args)
            # Convert BigInt result back to Python int if needed
            if hasattr(result, 'valueOf'):
                return int(result.valueOf())
            return result
        except Exception as e:
            raise WasmRuntimeError(str(e)) from e

    def get_memory(self):
        """Return the WASM memory export."""
        return self._memory

    def write_memory(self, offset, data):
        """Write bytes into WASM linear memory at offset."""
        if self._memory is None:
            raise RuntimeError("No memory export in WASM module")

        from js import Uint8Array
        from pyodide.ffi import to_js

        # Get memory buffer
        buffer = self._memory.buffer
        mem_view = Uint8Array.new(buffer)
        mem_size = mem_view.length

        if offset < 0 or offset + len(data) > mem_size:
            raise IndexError(
                f"Write at offset {offset} with length {len(data)} "
                f"exceeds memory size {mem_size}"
            )

        # Write data byte by byte (could optimize with subarray.set)
        js_data = Uint8Array.new(to_js(data))
        mem_view.set(js_data, offset)

    def read_memory(self, offset, size):
        """Read bytes from WASM linear memory at offset."""
        if self._memory is None:
            raise RuntimeError("No memory export in WASM module")

        from js import Uint8Array

        # Get memory buffer
        buffer = self._memory.buffer
        mem_view = Uint8Array.new(buffer)
        mem_size = mem_view.length

        if offset < 0 or offset + size > mem_size:
            raise IndexError(
                f"Read at offset {offset} with length {size} "
                f"exceeds memory size {mem_size}"
            )

        # Read slice and convert to Python bytes
        slice_view = mem_view.subarray(offset, offset + size)
        return bytes(slice_view.to_py())

    def get_export_names(self):
        """Return list of exported names."""
        return list(self._exports.keys())
