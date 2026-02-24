import os
import shutil
import struct
import unittest

from llvmlite import ir
import llvmlite.binding as llvm
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
        old = os.environ.get('WASM_LD')
        os.environ['WASM_LD'] = '/usr/bin/true'
        try:
            path = _find_wasm_ld()
            self.assertEqual(path, '/usr/bin/true')
        finally:
            if old is not None:
                os.environ['WASM_LD'] = old
            else:
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


try:
    import wasmtime as _wt
    _has_wasmtime = True
except ImportError:
    _has_wasmtime = False

needs_wasmtime = unittest.skipUnless(_has_wasmtime, "wasmtime not installed")

# Minimal valid WASM module: exports an `add` function (i32, i32) -> i32
# (wat equivalent):
#   (module
#     (func (export "add") (param i32 i32) (result i32)
#       local.get 0  local.get 1  i32.add)
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
    0x07, 0x10, 0x02,
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
        mod2 = llvm.parse_assembly(_WASM_IR_ADD)
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


class TestPublicAPI(unittest.TestCase):

    def test_importable_from_binding(self):
        from llvmlite.binding import (
            create_wasm_engine,
            WasmExecutionEngine,
            WasmFunction,
            WasmToolNotFoundError,
            WasmRuntimeError,
        )
        self.assertTrue(callable(create_wasm_engine))


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
        block = func.append_basic_block(name="entry")
        builder = ir.IRBuilder(block)
        result = builder.mul(func.args[0], func.args[1])
        builder.ret(result)

        # llvmlite's ir module doesn't support arbitrary string attributes,
        # so we inject the wasm-export-name attribute into the IR text
        llvm_ir = str(module)
        llvm_ir = llvm_ir.replace(
            'define i32 @"multiply"(i32 %".1", i32 %".2")\n',
            'define i32 @"multiply"(i32 %".1", i32 %".2") #0\n',
        )
        llvm_ir += '\nattributes #0 = { "wasm-export-name"="multiply" }\n'

        # Parse, compile, execute
        mod = llvm.parse_assembly(llvm_ir)
        mod.verify()
        engine = create_wasm_engine(mod)
        engine.finalize_object()
        multiply = engine.get_function('multiply')
        self.assertEqual(multiply(6, 7), 42)
