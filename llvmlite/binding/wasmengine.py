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
