# GitHub Agent：加入 Code Search 的改进方案

## 1. 改进目标

当前 `github_agentv1` 已经可以通过以下工具回答仓库问题：

- `repository_overview`
- `list_repository_files`
- `read_repository_file`
- `list_open_issues`
- `list_open_pull_requests`

目前的问题是：当用户询问某个技术、依赖、数据库、框架、API、函数或实现是否存在时，Agent 主要依赖 `list_repository_files` 找文件，再决定读取什么。

本次改进的目标不是简单地增加一个工具，而是让 Agent 建立：

```text
search → locate → inspect → verify → answer
```

即：

```text
用户问题
   ↓
Code Search
   ↓
定位候选文件
   ↓
Read File
   ↓
验证实际实现/使用
   ↓
回答
```

---

## 2. 需要修改的文件

主要修改：

```text
github_api.py
agent.py
```

不需要修改：

```text
cli.py
```

原因是 `cli.py` 已经把自然语言问题传递给 `agent.run()`。新增 Tool 后，Agent 可以直接使用，不需要 CLI 增加新的 command。

---

# 3. 修改 `github_api.py`

## 3.1 当前 API Client

你的 `GitHubClient` 已经有统一的：

```python
def get(
    self,
    path: str,
    params: dict[str, str] | None = None
) -> Any:
```

现有方法包括：

```python
repository()
languages()
tree()
file_text()
readme()
issues()
pull_requests()
```

因此不需要新增 `_request()`。

直接复用现有的 `self.get()` 即可。

## 3.2 新增 `search_code()`

建议将下面的方法放在 `tree()` 后面、`file_text()` 前面：

```python
def search_code(
    self,
    repo: RepositoryRef,
    query: str,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Search code within a GitHub repository."""
    clean_query = query.strip()

    if not clean_query:
        return []

    max_results = max(1, min(max_results, 100))

    payload = self.get(
        "/search/code",
        {
            "q": f"{clean_query} repo:{repo.slug}",
            "per_page": str(max_results),
        },
    )

    return payload.get("items", [])[:max_results]
```

### 为什么这样写

你的 `RepositoryRef` 已经有：

```python
@property
def slug(self) -> str:
    return f"{self.owner}/{self.name}"
```

因此：

```python
repo.slug
```

会得到：

```text
huggingface/smolagents
```

例如搜索：

```python
client.search_code(repo, "MongoDB")
```

会构造：

```text
MongoDB repo:huggingface/smolagents
```

并通过你已有的：

```python
self.get(...)
```

调用 GitHub Code Search API。

---

# 4. 修改 `agent.py`

## 4.1 新增 `search_repository_code`

建议放在 `list_repository_files` 和 `read_repository_file` 附近。

```python
@tool
def search_repository_code(
    query: str,
    max_results: int = 20,
) -> str:
    """
    Search for a keyword or code pattern in the configured GitHub repository.

    Use this tool when the user's question requires finding where a
    technology, dependency, database, framework, API, function, class,
    configuration key, or implementation pattern appears in the repository.

    Do not treat a search match alone as proof of actual usage.
    When necessary, use read_repository_file to inspect the matching file.

    Args:
        query: Keyword or code pattern to search for.
        max_results: Maximum number of matching files to return.
    """

    safe_query = query.strip()

    if not safe_query:
        return (
            "[EVIDENCE_NOT_FOUND]\n"
            "Code search query cannot be empty."
        )

    max_results = max(1, min(max_results, 50))

    matches = client.search_code(
        repo,
        safe_query,
        max_results=max_results,
    )

    if not matches:
        return (
            "[REPOSITORY_EVIDENCE]\n"
            "Evidence type: code search\n"
            f"Query: {safe_query}\n"
            "Result: No matching code found."
        )

    results = []

    for item in matches:
        path = item.get("path", "")
        html_url = item.get("html_url", "")
        repository = item.get("repository", {}).get("full_name", "")

        results.append(
            f"- File: {path}\n"
            f"  Repository: {repository}\n"
            f"  URL: {html_url}"
        )

    return (
        "[REPOSITORY_EVIDENCE]\n"
        "Evidence type: code search\n"
        f"Query: {safe_query}\n"
        "Matches:\n"
        + "\n".join(results)
    )
```

---

# 5. 将新 Tool 加入 `repository_tools`

当前：

```python
repository_tools = [
    repository_overview,
    list_repository_files,
    read_repository_file,
]
```

修改为：

```python
repository_tools = [
    repository_overview,
    list_repository_files,
    search_repository_code,
    read_repository_file,
]
```

后面的：

```python
issue_pr_tools = [
    list_open_issues,
    list_open_pull_requests,
]

all_tools = repository_tools + issue_pr_tools
```

不需要修改。

---

# 6. 修改 Agent Description

在现有 description 中加入：

```python
"Use search_repository_code when the question requires finding "
"a keyword, technology, dependency, database, framework, API, "
"function, class, configuration key, or implementation pattern "
"across the repository and the relevant file is unknown. "

"Prefer search_repository_code over list_repository_files when "
"the question is about whether a specific technology or implementation "
"appears somewhere in the repository. "

"When search_repository_code identifies relevant files, use "
"read_repository_file to inspect the actual implementation before "
"concluding that the technology or feature is used. "

"Do not treat a code search match alone as proof of actual usage. "
"Distinguish between imports, comments, documentation, tests, "
"configuration, and executable implementation code. "
```

---

# 7. 修改 Agent 的 Tool Selection Rules

建议在现有规则后增加：

```text
10. Use search_repository_code when the question requires finding
    occurrences of a keyword, technology, dependency, database,
    framework, API, function, class, configuration key, or
    implementation pattern and the relevant file is unknown.

11. Prefer search_repository_code over list_repository_files when
    the user's question is about whether a specific technology or
    implementation appears somewhere in the repository.

12. When code search returns relevant files and the question requires
    understanding actual usage or behavior, follow the search with
    read_repository_file.

13. Do not treat a code search result alone as proof of implementation
    or actual usage.
```

---

# 8. 最重要：增加 Technology Verification 规则

这是这次改进的核心。

建议加入：

```text
Technology verification:

If the question asks whether a technology, dependency, database,
framework, API, or implementation is used:

1. Search the repository for relevant keywords using
   search_repository_code.

2. Identify relevant candidate files from the search results.

3. Read the most relevant candidate files using
   read_repository_file.

4. Determine whether the retrieved code provides sufficient evidence
   of actual usage or implementation.

5. If the evidence is insufficient, search for additional relevant
   keywords or inspect additional candidate files.

6. Do not conclude that a technology is used merely because its name
   appears in a comment, documentation, test fixture, or unrelated text.

7. If sufficient evidence cannot be found, state that the usage
   cannot be verified from the repository.
```

## 8及以前的内容全部已修改
---

# 9. 为什么 Search 之后还需要 Read

不能把：

```text
search result
```

直接当成：

```text
proof
```

例如搜索：

```text
MongoDB
```

可能返回：

```text
README.md
docs/example.md
tests/test_database.py
src/example.py
```

这些结果的证据强度并不相同。

### README / Documentation

可能只能证明：

```text
MongoDB is mentioned.
```

不能证明：

```text
The repository actually uses MongoDB.
```

### Test

可能证明仓库存在 MongoDB 相关测试，但还需要判断测试针对的是实际实现还是 fixture/example。

### Import

例如：

```python
from pymongo import MongoClient
```

这是更强的使用证据。

### Actual implementation

例如：

```python
client = MongoClient(...)
```

则可以进一步支持实际数据库连接/初始化的判断。

因此：

```text
Search ≠ Proof
```

应该建立：

```text
Search → Read → Verify
```

---

# 10. `list_repository_files` 与 `search_repository_code` 的职责

## `list_repository_files`

用于回答：

> 仓库有哪些文件？

例如：

```text
What files are under src/smolagents?
```

使用：

```text
list_repository_files
```

---

## `search_repository_code`

用于回答：

> 某个关键词、技术、类、函数在哪里出现？

例如：

```text
Where is MongoDB mentioned in the repository?
```

使用：

```text
search_repository_code("MongoDB")
```

---

## `read_repository_file`

用于回答：

> 某个具体文件里的代码做什么？

例如：

```text
What does src/smolagents/memory.py contain?
```

使用：

```text
read_repository_file(
    path="src/smolagents/memory.py"
)
```

---

# 11. 理想的多步 Tool Chain

以前：

```text
Question
   ↓
list_repository_files
   ↓
Answer
```

现在应该：

```text
Question
   ↓
判断是否需要代码定位
   ↓
search_repository_code
   ↓
找到 candidate files
   ↓
read_repository_file
   ↓
分析 evidence
   ↓
Answer
```

例如：

```text
Does the smolagents repository use MongoDB as a database?
```

理想执行：

```text
search_repository_code("MongoDB")
             ↓
       找到候选文件
             ↓
read_repository_file(...)
             ↓
     检查实际代码
             ↓
判断：
- import？
- configuration？
- executable code？
- documentation？
- test？
             ↓
        最终回答
```

---

# 12. `cli.py` 不需要修改

你的 CLI 已经有：

```python
elif args.command == "ask":
    agent = build_agent(client, repo)
    answer = agent.run(...)
    print(answer)
```

因此：

```text
CLI
 ↓
build_agent()
 ↓
all_tools
 ↓
ToolCallingAgent
 ↓
agent.run()
```

新 Tool 会自动进入 Agent。

所以本次不需要修改：

```text
cli.py
```

---

# 13. 测试计划

## Test 1：Code Search 基础测试

```bash
github-agent ask huggingface/smolagents "Where is MongoDB mentioned in the repository?"
```

检查：

- 是否调用 `search_repository_code`
- 是否返回匹配文件
- 是否没有错误地调用 `list_repository_files` 代替搜索

---

## Test 2：搜索类/实现

```bash
github-agent ask huggingface/smolagents "Where is ToolCallingAgent used in the repository?"
```

检查：

- 是否使用 `search_repository_code`
- 是否找到相关文件
- 是否能给出文件路径

---

## Test 3：Search → Read → Verify

```bash
github-agent ask huggingface/smolagents "Does the smolagents repository use MongoDB as a database?"
```

这是最重要的测试。

理想 Tool Chain：

```text
search_repository_code
        ↓
read_repository_file
        ↓
evidence analysis
        ↓
answer
```

重点检查：

- 是否调用 Code Search
- 是否继续读取候选文件
- 是否真正检查代码内容
- 是否区分 documentation / test / import / implementation
- 是否避免仅凭搜索结果下结论

---

## Test 4：不存在的技术

```bash
github-agent ask huggingface/smolagents "Does the repository use Redis?"
```

期望 Agent 能基于搜索证据回答。

如果没有证据，不应该直接说：

```text
The repository definitely does not use Redis.
```

更稳妥的是：

```text
No repository evidence was found for Redis.
```

或者：

```text
Its use cannot be verified from the repository evidence retrieved.
```

---

## Test 5：MCP 实现

```bash
github-agent ask huggingface/smolagents "Does the repository implement MCP support?"
```

这个问题适合测试：

```text
search
  ↓
candidate files
  ↓
read
  ↓
implementation verification
```

---

# 14. 回归测试

新增 Code Search 后，需要确保之前的功能没有被破坏。

## Repository metadata

```bash
github-agent ask huggingface/smolagents "What programming languages does the smolagents repository use?"
```

期望：

```text
repository_overview
```

## Repository structure

```bash
github-agent ask huggingface/smolagents "What files are under the src/smolagents directory?"
```

期望：

```text
list_repository_files
```

## File content

```bash
github-agent ask huggingface/smolagents "What does src/smolagents/memory.py contain?"
```

期望：

```text
read_repository_file
```

## Open issues

```bash
github-agent ask huggingface/smolagents "What are the open issues in the smolagents repository?"
```

期望：

```text
list_open_issues
```

## Open pull requests

```bash
github-agent ask huggingface/smolagents "What are the open pull requests in the smolagents repository?"
```

期望：

```text
list_open_pull_requests
```

---

# 15. 推荐测试矩阵

| Test | 问题类型 | 期望 Tool | 重点 |
|---|---|---|---|
| 1 | Repository metadata | `repository_overview` | 基础 routing |
| 2 | File structure | `list_repository_files` | 文件发现 |
| 3 | File content | `read_repository_file` | 文件读取 |
| 4 | Code search | `search_repository_code` | 新 Tool |
| 5 | Technology verification | `search → read` | 多步 reasoning |
| 6 | Non-existent technology | `search` | Negative evidence |
| 7 | Implementation | `search → read` | 判断真实实现 |
| 8 | MCP | `search → read` | 综合测试 |
| 9 | Issues | `list_open_issues` | Regression |
| 10 | PRs | `list_open_pull_requests` | Regression |

---

# 16. 验收标准

完成后，不应该只检查：

```text
search_repository_code 有没有被调用？
```

而应该检查四层能力。

## ① Tool Routing

Agent 是否知道：

```text
技术/依赖/实现在哪里？
        ↓
search_repository_code
```

## ② Evidence Retrieval

是否真正获得：

```text
candidate files
```

## ③ Evidence Verification

对于：

```text
Does the repository use X?
```

是否执行：

```text
search
   ↓
read
   ↓
verify
```

## ④ Grounded Answer

最终答案是否严格基于：

```text
retrieved repository evidence
```

而不是模型自己的常识。

---

# 17. 最终架构

```text
                         User Question
                              │
                              ▼
                     ┌─────────────────┐
                     │  Tool Selection │
                     └────────┬────────┘
                              │
          ┌───────────────────┼──────────────────┐
          │                   │                  │
          ▼                   ▼                  ▼
 repository_overview   list_repository_files   search_repository_code
          │                   │                  │
          │                   │                  ▼
          │                   │            Candidate Files
          │                   │                  │
          │                   └──────────┐       │
          │                              ▼       ▼
          │                       read_repository_file
          │                              │
          └──────────────────────────────┤
                                         ▼
                                Evidence Analysis
                                         │
                                         ▼
                                  Final Answer
```

核心能力从：

```text
list files → answer
```

升级为：

```text
search → locate → inspect → verify → answer
```

---

# 18. 本次修改清单

### `github_api.py`

新增：

```python
GitHubClient.search_code()
```

复用：

```python
self.get()
```

### `agent.py`

新增：

```python
search_repository_code()
```

并加入：

```python
repository_tools
```

### Agent Description

增加：

```text
search_repository_code
```

的 routing 说明。

### Agent Instructions

增加：

```text
search → read → verify
```

的技术验证规则。

### `cli.py`

无需修改。

---

# 19. 下一阶段建议

完成 Code Search 后，不建议立即继续堆很多 Tool。

当前项目更值得优化的是：

```text
Tool Routing
      ↓
Evidence Retrieval
      ↓
Evidence Verification
      ↓
Citation
      ↓
Final Answer
```

根据目前已有测试：

- Tool Routing：基本正确
- Evidence Retrieval：基本正确
- 基础回答：基本正确
- Citation：仍需改进
- `latest` 的时间语义：仍需改进
- Code Search：本次新增
- Search → Read → Verify：本次重点

因此，完成 Code Search 后，下一步建议重点解决：

1. **Citation / evidence citation**
2. **Search → Read 的多步 Tool Calling**
3. **`latest` / `newest` 等时间语义**
4. **建立更系统的自动测试集**

最终希望你的 Agent 不只是：

```text
会调用 Tool
```

而是：

```text
会选择 Tool
→ 会寻找证据
→ 会验证证据
→ 会引用证据
→ 会在证据不足时拒绝猜测
```
