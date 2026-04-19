# Kiro 可用工具模板

## 1. bash — 执行 Shell 命令

参数：
  command  (必填) — 要执行的 shell 命令
  timeout  (可选) — 超时毫秒数，默认 120000

---

## 2. read_file — 读取文件内容

参数：
  file_path  (必填) — 文件绝对路径
  offset     (可选) — 起始行号（0-based）
  limit      (可选) — 最多读取行数，默认 2000

---

## 3. grep — 正则搜索文件内容

参数：
  pattern  (必填) — 正则表达式
  path     (可选) — 搜索目录或文件，默认当前目录
  glob     (可选) — 文件过滤模式，如 *.py 或 *.{ts,tsx}

---

## 4. Skill — 调用内置技能

参数：
  skill  (必填) — 技能名称
  args   (可选) — 传给技能的参数字符串

---

## 5. agent — 启动子 Agent

参数：
  description  (必填) — 3-5 词的任务描述
  prompt       (必填) — 给 Agent 的详细任务说明
  agent_type   (可选) — Explore 或 general-purpose（默认）

---

## 汇总表

| 工具      | 必填参数              | 可选参数            |
|-----------|-----------------------|---------------------|
| bash      | command               | timeout             |
| read_file | file_path             | offset, limit       |
| grep      | pattern               | path, glob          |
| Skill     | skill                 | args                |
| agent     | description, prompt   | agent_type          |
