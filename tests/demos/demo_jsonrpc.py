"""
JSON-RPC over HTTP 最简 demo
演示：HTTP 只是传输层，JSON-RPC 只是 body 里的 JSON 格式约定
"""

# ============ 服务端 ============
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/rpc")
def handle_rpc():
    # HTTP 层：拿到 body（一坨 JSON 字节）
    req = request.json

    # JSON-RPC 层：解析格式（就是读 JSON 里的字段，没有任何魔法）
    req_id = req["id"]           # 客户端给的 id，原样返回
    method = req["method"]       # 客户端想调什么方法
    params = req.get("params", {})

    # 业务层：根据 method 做不同的事
    if method == "add":
        result = params["a"] + params["b"]
    elif method == "greet":
        result = f"hello {params['name']}"
    else:
        # JSON-RPC 错误格式（也是约定）
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"未知方法: {method}"}
        })

    # JSON-RPC 响应格式（也是约定）：带上同样的 id
    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,        # ← 原样返回客户端给的 id
        "result": result
    })


# ============ 客户端 ============
import requests

def call_rpc(method, params):
    # 自增 id（每次请求不同）
    call_rpc.counter += 1
    req_id = call_rpc.counter

    # HTTP 层：发 POST，body 是 JSON-RPC 格式的 JSON
    response = requests.post("http://localhost:5000/rpc", json={
        "jsonrpc": "2.0",
        "id": req_id,            # ← 自己生成的 id
        "method": method,
        "params": params
    })

    # HTTP 层：拿到响应 body（又是一坨 JSON）
    resp = response.json()

    # JSON-RPC 层：检查 id 是否匹配
    assert resp["id"] == req_id, "id 不匹配！"

    if "error" in resp:
        print(f"错误: {resp['error']['message']}")
        return None

    return resp["result"]

call_rpc.counter = 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["server"]:
        app.run(port=5000)
    else:
        # 客户端调用
        print(call_rpc("add", {"a": 3, "b": 5}))       # → 8
        print(call_rpc("greet", {"name": "张三"}))       # → hello 张三
        print(call_rpc("unknown", {}))                    # → 错误: 未知方法
