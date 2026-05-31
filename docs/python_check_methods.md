# Python 静态检查方法

## 1. 语法检查（最基础）

```bash
python -m py_compile src/agent_loop.py
```

只检查语法是否合法，不检查类型、逻辑。批量检查：

```bash
find src -name "*.py" -exec python -m py_compile {} \;
```

## 2. Import 链验证

```bash
python -c "from src.agent_loop import agent_loop; print('OK')"
```

验证模块能被正确导入，会暴露循环引用、缺失依赖等问题。

## 3. 类型检查（Pyright）

```bash
pip install pyright
pyright src/
```

检查类型标注、参数类型不匹配、未定义变量等。IDE 里实时跑的就是这个。

配置文件：`pyrightconfig.json` 或 `pyproject.toml` 的 `[tool.pyright]` 段。

## 4. 类型检查（mypy）

```bash
pip install mypy
mypy src/ --ignore-missing-imports
```

功能类似 Pyright，社区更老牌，配置更灵活。

## 5. Lint（ruff，推荐）

```bash
pip install ruff
ruff check src/          # 检查
ruff check src/ --fix    # 自动修复
```

速度极快，覆盖：未使用 import、未使用变量、代码风格、常见 bug 模式。

## 6. Lint（flake8，传统）

```bash
pip install flake8
flake8 src/
```

## 7. 组合使用建议

日常开发靠 IDE 的 Pyright 实时检查就够。提交前跑一遍：

```bash
ruff check src/ && pyright src/ && python -c "from src.main import main; print('OK')"
```
