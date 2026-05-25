import asyncio

async def bg_task():
    print("bg_task 执行了！")
    return "done"

# 测试1：纯 while 轮询，永远 False
async def test1():
    task = asyncio.create_task(bg_task())
    for i in range(100000):
        if task.done():
            print(f"第 {i} 次检查：done!")
            return
    print(f"检查了 100000 次，task.done() 始终是 False")

# 测试2：轮询 1000 次后 yield，然后再检查
async def test2():
    task = asyncio.create_task(bg_task())
    for i in range(1000):
        if task.done():
            print(f"yield 之前第 {i} 次：done!")
            break
    else:
        print(f"yield 之前检查了 1000 次，全是 False")

    await asyncio.sleep(0)  # yield 一下

    print(f"yield 之后：task.done() = {task.done()}")

async def main():
    print("=== 测试1：纯轮询，不 yield ===")
    await test1()
    print()
    print("=== 测试2：轮询 1000 次后 yield ===")
    await test2()

asyncio.run(main())
