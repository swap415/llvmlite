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
