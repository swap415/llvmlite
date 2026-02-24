"""
WASM Execution Engine Demo

Two demos in one:
1. Interactive Playground - paste LLVM IR, compile to WASM, execute
2. Browser WASM Demo - pre-compiled WASM running natively in browser

Usage: python demo/app.py
"""
import base64
import json
import time
import traceback

from flask import Flask, render_template, request, jsonify, send_from_directory

import llvmlite.binding as llvm
from llvmlite.binding.wasmengine import create_wasm_engine

llvm.initialize_all_targets()
llvm.initialize_all_asmprinters()

app = Flask(__name__)

# Pre-compiled WASM examples for browser-native demo
EXAMPLES = {}


def _compile_ir_to_wasm(ir_string):
    """Compile LLVM IR string to WASM bytes."""
    mod = llvm.parse_assembly(ir_string)
    mod.verify()
    engine = create_wasm_engine(mod)
    engine.finalize_object()
    return engine


def _precompile_examples():
    """Pre-compile example WASM modules at startup."""
    examples = {
        "add": {
            "ir": '''\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define i32 @add(i32 %a, i32 %b) #0 {
  %result = add i32 %a, %b
  ret i32 %result
}

attributes #0 = { "wasm-export-name"="add" }
''',
            "description": "Integer addition (i32)",
            "function": "add",
            "args": [7, 8],
            "expected": 15,
        },
        "fpadd": {
            "ir": '''\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define double @fpadd(double %a, double %b) #0 {
  %result = fadd double %a, %b
  ret double %result
}

attributes #0 = { "wasm-export-name"="fpadd" }
''',
            "description": "Floating point addition (f64)",
            "function": "fpadd",
            "args": [3.14, 2.71],
            "expected": 5.85,
        },
        "factorial": {
            "ir": '''\
target triple = "wasm32-unknown-unknown"
target datalayout = "e-m:e-p:32:32-i64:64-n32:64-S128"

define i32 @factorial(i32 %n) #0 {
entry:
  %cmp = icmp sle i32 %n, 1
  br i1 %cmp, label %base, label %recurse

base:
  ret i32 1

recurse:
  %n_minus_1 = sub i32 %n, 1
  %sub_result = call i32 @factorial(i32 %n_minus_1)
  %result = mul i32 %n, %sub_result
  ret i32 %result
}

attributes #0 = { "wasm-export-name"="factorial" }
''',
            "description": "Recursive factorial (i32)",
            "function": "factorial",
            "args": [10],
            "expected": 3628800,
        },
    }

    for name, info in examples.items():
        try:
            engine = _compile_ir_to_wasm(info["ir"])
            wasm_bytes = engine.get_wasm_bytes()
            EXAMPLES[name] = {
                **info,
                "wasm_b64": base64.b64encode(wasm_bytes).decode(),
                "wasm_size": len(wasm_bytes),
            }
        except Exception as e:
            print(f"warning: failed to pre-compile {name}: {e}")


@app.route("/")
def index():
    return render_template("index.html", examples=EXAMPLES)


@app.route("/api/compile", methods=["POST"])
def api_compile():
    """Compile LLVM IR to WASM and execute a function."""
    data = request.get_json()
    ir_string = data.get("ir", "")
    func_name = data.get("function", "")
    args_raw = data.get("args", [])

    try:
        t0 = time.perf_counter()
        engine = _compile_ir_to_wasm(ir_string)
        compile_ms = (time.perf_counter() - t0) * 1000

        wasm_bytes = engine.get_wasm_bytes()

        result = None
        exec_ms = 0
        if func_name:
            func = engine.get_function(func_name)
            args = [_parse_arg(a) for a in args_raw]
            t1 = time.perf_counter()
            result = func(*args)
            exec_ms = (time.perf_counter() - t1) * 1000

        return jsonify({
            "ok": True,
            "result": result,
            "wasm_size": len(wasm_bytes),
            "wasm_b64": base64.b64encode(wasm_bytes).decode(),
            "compile_ms": round(compile_ms, 2),
            "exec_ms": round(exec_ms, 4),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), 400


@app.route("/api/examples/<name>.wasm")
def api_example_wasm(name):
    """Serve pre-compiled WASM binary."""
    if name not in EXAMPLES:
        return "not found", 404
    wasm_bytes = base64.b64decode(EXAMPLES[name]["wasm_b64"])
    return wasm_bytes, 200, {"Content-Type": "application/wasm"}


def _parse_arg(val):
    """Parse a string argument to int or float."""
    if isinstance(val, (int, float)):
        return val
    s = str(val).strip()
    if "." in s:
        return float(s)
    return int(s)


if __name__ == "__main__":
    print("pre-compiling WASM examples...")
    _precompile_examples()
    for name, info in EXAMPLES.items():
        print(f"  {name}: {info['wasm_size']} bytes")
    print(f"\nstarting demo at http://localhost:5000")
    app.run(debug=True, port=5000)
