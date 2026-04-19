# Kiro 可用工具模板

---

## 1. bash — 执行 Shell 命令



**示例：**


---

## 2. read_file — 读取文件



**示例：**


---

## 3. grep — 正则搜索文件内容



**示例：**


---

## 4. Skill — 调用内置技能



**示例：**


---

## 5. agent — 启动子 Agent



**可用 Agent 类型：**
-  — 只读，专门用于快速探索代码库、搜索文件
-  — 默认，拥有完整工具访问权限

**示例：**


---

## 参数说明

| 工具 | 必填参数 | 可选参数 |
|------|----------|----------|
| bash | command | timeout |
| read_file | file_path | offset, limit |
| grep | pattern | path, glob |
| Skill | skill | args |
| agent | description, prompt | agent_type |

