# IronAgent 本地 PDF 离线烟测记录

## 任务目标

在容器中的 IronAgent 内，使用 `local-pdf` skill 对一个已存在的 PDF 做离线处理，生成以下验证产物：

1. 提取文本文件 `extracted.txt`
2. 原始 PDF 的校验副本 `verified-copy.pdf`
3. 本地生成的摘要 PDF `summary.pdf`

要求：

- 必须由容器中的 IronAgent 完成
- 不依赖外部模型或 PDF API
- 保留一份 IronAgent 的会话日志截选

---

## 执行环境

- IronAgent 容器：`ironagent-assistant-<assistant-id>`
- Gateway 地址：`http://<gateway-host>:<gateway-port>/`
- 输入 PDF（容器内）：`/home/node/.ironagent/workspace/temp/skill-summary-agent/agent-word.pdf`
- 输出目录（容器内）：`/home/node/.ironagent/workspace/temp/local-pdf-smoke`
- 会话日志文件（容器内）：
  `/home/node/.ironagent/agents/main/sessions/<session-id>.jsonl`

---

## 最终产物

生成结果如下：

- `extracted.txt`：文本提取结果
- `verified-copy.pdf`：输入 PDF 的校验副本
- `summary.pdf`：本地生成的摘要 PDF

本次复验中，示例结果如下：

- `extracted.txt`：53 bytes
- `verified-copy.pdf`：635 bytes
- `summary.pdf`：735 bytes

产物目录（容器内）：

`/home/node/.ironagent/workspace/temp/local-pdf-smoke`

---

## 执行结果总结

本次任务已经完成，容器中的 IronAgent 通过 `local-pdf` skill：

1. 先读取 `skills/local-pdf/SKILL.md`
2. 再执行 `skills/local-pdf/scripts/local_pdf.py --input-pdf ... --out-dir ... --stdout-json`
3. 由脚本生成文本提取结果、校验副本和摘要 PDF
4. 基于 JSON 摘要确认输出路径、PDF 版本、页数和文件尺寸
5. 最终回复 `LOCAL_PDF_OK`

这说明当前离线 PDF 验证主线已经成立：

- IronAgent 能在容器内识别并读取工作区 skill
- IronAgent 能在容器内执行不依赖外部模型的本地 PDF 处理脚本
- IronAgent 能在共享工作区中完成 PDF 校验型产物输出

补充说明：

- `local-pdf` 适合做 smoke / 连通性 / 离线验证
- 它不是复杂 PDF 语义编辑方案
- 当前文本提取更适合简单文本型 PDF，例如由 `office-docs` 生成的 PDF

---

## 日志截选

以下为本次任务相关的关键日志片段，已做最小整理。

### 1. 用户任务输入

```json
{
  "message": {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "... Use the local-pdf skill only. Process ./temp/skill-summary-agent/agent-word.pdf into ./temp/local-pdf-smoke. Create the default verification artifacts there and reply with exactly LOCAL_PDF_OK."
      }
    ]
  }
}
```

### 2. IronAgent 读取 `local-pdf` skill

```json
{
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "toolCall",
        "name": "read",
        "arguments": { "path": "/home/node/.ironagent/workspace/skills/local-pdf/SKILL.md" }
      }
    ]
  }
}
```

### 3. IronAgent 执行 `local-pdf` 脚本

```json
{
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "toolCall",
        "name": "exec",
        "arguments": {
          "command": "cd /home/node/.ironagent/workspace && python3 ./skills/local-pdf/scripts/local_pdf.py --input-pdf ./temp/skill-summary-agent/agent-word.pdf --out-dir ./temp/local-pdf-smoke --stdout-json",
          "timeout": 120
        }
      }
    ]
  }
}
```

### 4. 脚本返回结构化摘要

```json
{
  "message": {
    "role": "toolResult",
    "toolName": "exec",
    "content": [
      {
        "type": "text",
        "text": "{\"inputPdf\":\"/home/node/.ironagent/workspace/temp/skill-summary-agent/agent-word.pdf\",\"outDir\":\"/home/node/.ironagent/workspace/temp/local-pdf-smoke\",\"pdfVersion\":\"1.4\",\"pageCount\":1,\"textPreview\":[\"Reverify Office\",\"Reverify office-docs smoke 20260407.\"],\"textOutput\":{\"name\":\"extracted.txt\",\"sizeBytes\":53},\"copyPdf\":{\"name\":\"verified-copy.pdf\",\"sizeBytes\":635},\"summaryPdf\":{\"name\":\"summary.pdf\",\"sizeBytes\":735}}"
      }
    ]
  }
}
```

### 5. IronAgent 最终回复

```json
{ "message": { "role": "assistant", "content": [{ "type": "text", "text": "LOCAL_PDF_OK" }] } }
```

---

## 当前结论

本次验证说明，当前容器中的 IronAgent 已经能够：

1. 在 fresh 助手工作区中识别 `local-pdf` skill
2. 在容器中执行离线 PDF 校验流程
3. 为现有 PDF 生成文本提取结果、校验副本和摘要 PDF
4. 返回结构化 JSON 摘要
5. 留下可追踪的会话日志

如果后续你还要做“纯本地模型 + 纯本地技能”的离线演示，推荐直接组合：

1. `office-docs`
2. `local-pdf`
3. 宿主机本地 Ollama
