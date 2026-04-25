"""
SSE vs 普通 JSON 响应 对比 demo
运行: python demo_sse.py server
测试: python demo_sse.py client
"""

from flask import Flask, Response, jsonify, request
import json, time

app = Flask(__name__)


# ============ 普通 JSON 响应（一次性返回） ============

@app.post("/normal")
def normal():
    """模拟一个耗时操作，处理完一次性返回"""
    time.sleep(2)  # 假装在干活
    return jsonify({"id": 1, "result": "done"})
    # 客户端等 2 秒，一次性拿到完整结果


# ============ SSE 响应（分多次返回） ============

@app.post("/sse")
def sse():
    """模拟一个耗时操作，边做边推进度，最后返回结果"""

    def generate():
        # 第 1 秒：推送进度 30%
        time.sleep(1)
        yield f"data: {json.dumps({'progress': '30%'})}\n\n"

        # 第 2 秒：推送进度 70%
        time.sleep(1)
        yield f"data: {json.dumps({'progress': '70%'})}\n\n"

        # 第 3 秒：推送最终结果
        time.sleep(1)
        yield f"data: {json.dumps({'id': 1, 'result': 'done'})}\n\n"

    return Response(generate(), content_type="text/event-stream")
    # 客户端从第 1 秒开始就能陆续收到数据，不用等到全部完成


# ============ 客户端 ============

def test_normal():
    """普通请求：等 2 秒，一次性拿到结果"""
    import requests
    print("=== 普通 JSON 响应 ===")
    print("发送请求...")
    start = time.time()
    resp = requests.post("http://localhost:5000/normal", json={})
    print(f"  {time.time() - start:.1f}s 后收到: {resp.json()}")
    # 输出：2.0s 后收到: {'id': 1, 'result': 'done'}


def test_sse():
    """SSE 请求：逐条收到数据"""
    import requests
    print("\n=== SSE 响应（分多次返回） ===")
    print("发送请求...")
    start = time.time()

    # stream=True：不等响应全部到达，边收边处理
    resp = requests.post("http://localhost:5000/sse", json={}, stream=True)

    # 逐行读取（每条 SSE 消息以 \n\n 结尾）
    buffer = ""
    for chunk in resp.iter_content(decode_unicode=True):
        buffer += chunk
        while "\n\n" in buffer:
            message, buffer = buffer.split("\n\n", 1)
            if message.startswith("data: "):
                data = json.loads(message[6:])  # 去掉 "data: " 前缀
                print(f"  {time.time() - start:.1f}s 收到: {data}")

    # 输出：
    #   1.0s 收到: {'progress': '30%'}    ← 第 1 秒就能看到
    #   2.0s 收到: {'progress': '70%'}    ← 第 2 秒
    #   3.0s 收到: {'id': 1, 'result': 'done'}  ← 第 3 秒


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["server"]:
        app.run(port=5000)
    elif sys.argv[1:] == ["normal"]:
        test_normal()
    elif sys.argv[1:] == ["sse"]:
        test_sse()
    else:
        test_normal()
        test_sse()
