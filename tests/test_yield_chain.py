def test():
    print("  test 执行了")
    yield None
    print("  test yield 之后执行了")
    return "sada"

def test2():
    print("  test2 执行了")
    result = yield from test()
    print(f"  test2 拿到 return 值: {result}")

def test3_broken():
    print("  test3 执行了")
    test2()  # 普通调用，链断了

def test3_fixed():
    print("  test3 执行了")
    result = yield from test2()  # yield from，链通了
    print(f"  test3 拿到值: {result}")

print("=" * 40)
print("断掉的情况：test3 普通调用 test2()")
print("=" * 40)
test3_broken()

print()
print("=" * 40)
print("正确的情况：test3 yield from test2()")
print("=" * 40)
gen = test3_fixed()
try:
    while True:
        val = gen.send(None)
        print(f"  [外层拿到 yield]: {val}")
except StopIteration as e:
    print(f"  [最终 return 值]: {e.value}")
