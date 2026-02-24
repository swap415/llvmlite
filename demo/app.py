"""llvmlite WASM demo — compile LLVM IR to WebAssembly, run in browser.

Usage: PYTHONPATH=. python demo/app.py
"""
import base64
import subprocess
import tempfile
from pathlib import Path

from flask import Flask, render_template, request, jsonify

import llvmlite.binding as llvm
from llvmlite.binding.wasmengine import _find_wasm_ld, _create_wasm_target_machine

llvm.initialize_all_targets()
llvm.initialize_all_asmprinters()

app = Flask(__name__)


def compile_to_wasm(ir_string):
    mod = llvm.parse_assembly(ir_string)
    mod.verify()
    tm = _create_wasm_target_machine()
    obj = tm.emit_object(mod)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / 'mod.o').write_bytes(obj)
        out = tmp / 'out.wasm'
        r = subprocess.run(
            [_find_wasm_ld(), '--no-entry', '--export-all',
             '--allow-undefined', '-o', str(out), str(tmp / 'mod.o')],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return out.read_bytes()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/compile', methods=['POST'])
def api_compile():
    data = request.get_json()
    try:
        wasm = compile_to_wasm(data['ir'])
        return jsonify(ok=True, wasm=base64.b64encode(wasm).decode(), size=len(wasm))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


if __name__ == '__main__':
    print('http://localhost:8080')
    app.run(port=8080)
