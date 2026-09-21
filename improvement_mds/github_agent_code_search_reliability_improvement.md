# GitHub Agent：代码搜索可靠性与多跳检索改进

> 本文记录本次工作区改动。文中的“改进前”指本次修改前的工作区代码，不等同于 Git 提交历史中的任意版本。

## 1. 改进目标

原有 Agent 已有 `search_repository_code` 工具，但存在以下问题：

1. 搜索工具返回的是拼接的普通文本，不能直接被 `json.loads()` 解析。
2. GitHub API 返回非法 JSON 或非预期结构时，错误类型不稳定，可能出现底层 `JSONDecodeError` 或 `AttributeError`。
3. 相同搜索会重复调用 GitHub Code Search；空结果也会被反复搜索。
4. 单次搜索结果中没有按文件路径去重。
5. “搜索后继续读取多个关联文件”仅靠模型自行决定，步骤数和提示都不足以稳定支持多跳检索。
6. no-result 容易被误解为“仓库绝对没有这个技术/功能”。

本次改动将这些关键行为从“主要依赖模型提示词”调整为“提示词 + 程序化约束”。

## 2. 总体检索流程

改进后的典型流程如下：

```text
问题需要了解实现
  → search_repository_code(关键词)
  → 得到纯 JSON 的候选路径
  → read_repository_file(候选文件 A)
  → 从 A 中发现 import / 调用 / 配置引用
  → search_repository_code(新的定向关键词)
  → read_repository_file(关联文件 B)
  → 基于已读取的证据回答
```

同一个 `query + max_results` 在一次 Agent 生命周期内只会访问 GitHub 一次；即使没有结果也会复用缓存。

---

## 3. 搜索工具输出：从拼接文本改为纯 JSON

### 问题与原因

改进前，工具把多个字符串直接拼在一起。人可以看懂，但整个返回值不是 JSON：它以 `[REPOSITORY_EVIDENCE]` 开头，而不是 `{` 或 `[`。因此其他代码一旦执行 `json.loads(tool_result)` 就会失败。

此外，有结果和无结果使用两套文字版式：有结果时是 `Matches:`，无结果时是 `Result:`。程序需要猜测文本格式，难以稳定提取文件路径或 no-result 状态。

### 改进前：`src/github_agent/agent.py`

```python
@tool
def search_repository_code(
    query: str,
    max_results: int = 20,
) -> str:
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

示例输出：

```text
[REPOSITORY_EVIDENCE]
Evidence type: code search
Query: redis
Matches:
- File: src/cache.py
  Repository: example/repo
  URL: https://github.com/example/repo/blob/main/src/cache.py
```

这是一段文本，不是合法 JSON。

### 改进后：固定字段的 JSON 格式化函数

```python
def format_code_search_evidence(
    query: str,
    matches: list[dict[str, object]],
    *,
    cached: bool,
) -> str:
    results = [
        {
            "path": item["path"],
            "repository": item.get("repository", {}).get("full_name", "")
            if isinstance(item.get("repository"), dict)
            else "",
            "url": item.get("html_url", ""),
        }
        for item in matches
    ]
    data: dict[str, object] = {
        "evidence_type": "code_search",
        "query": query,
        "cache": "hit" if cached else "miss",
        "match_count": len(results),
        "matches": results,
    }
    if not results:
        data["result"] = "no_matches"
        data["note"] = (
            "No matching code was found; this does not prove the repository lacks the term."
        )

    return json.dumps(data, ensure_ascii=False, indent=2)
```

改进后的正常结果示例：

```json
{
  "evidence_type": "code_search",
  "query": "redis",
  "cache": "miss",
  "match_count": 1,
  "matches": [
    {
      "path": "src/cache.py",
      "repository": "example/repo",
      "url": "https://github.com/example/repo/blob/main/src/cache.py"
    }
  ]
}
```

改进后的 no-result 示例：

```json
{
  "evidence_type": "code_search",
  "query": "redis",
  "cache": "miss",
  "match_count": 0,
  "matches": [],
  "result": "no_matches",
  "note": "No matching code was found; this does not prove the repository lacks the term."
}
```

### 改进后：工具调用缓存与格式化

```python
code_search_cache = CodeSearchCache()

@tool
def search_repository_code(query: str, max_results: int = 20) -> str:
    safe_query = query.strip()
    if not safe_query:
        return "[EVIDENCE_NOT_FOUND]\nCode search query cannot be empty."

    max_results = max(1, min(max_results, 50))
    matches, cached = code_search_cache.get_or_search(
        safe_query,
        max_results,
        lambda: client.search_code(repo, safe_query, max_results=max_results),
    )
    return format_code_search_evidence(safe_query, matches, cached=cached)
```

**效果：** 无论搜索有无结果，返回值都能直接用 `json.loads()` 解析，且字段含义稳定。

---

## 4. GitHub API JSON 与响应结构校验

### 改进前：直接解析并假设结构正确

```python
def get(self, path: str, params: dict[str, str] | None = None) -> Any:
    # 省略请求构造
    try:
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(f"GitHub API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise GitHubAPIError(f"Could not reach the GitHub API: {error.reason}") from error


def search_code(self, repo: RepositoryRef, query: str, max_results: int = 20) -> list[dict[str, Any]]:
    clean_query = query.strip()
    if not clean_query:
        return []

    max_results = max(1, min(max_results, 100))
    payload = self.get(
        "/search/code",
        {"q": f"{clean_query} repo:{repo.slug}", "per_page": str(max_results)},
    )
    return payload.get("items", [])[:max_results]
```

存在的问题：

- 服务器返回非 JSON 时，`json.loads()` 抛出的是底层 `JSONDecodeError`，调用方不能用统一的业务异常处理。
- 若 `payload` 是列表、字符串或 `null`，`payload.get(...)` 会报 `AttributeError`。
- 若 `items` 不是列表，或元素缺少 `path`，后续格式化可能出错或给模型无意义结果。

### 改进后：统一异常和 schema 校验

```python
def get(self, path: str, params: dict[str, str] | None = None) -> Any:
    # 省略请求构造
    try:
        with urlopen(request, timeout=self.timeout) as response:
            raw_response = response.read()
        try:
            return json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAPIError("GitHub API returned an invalid JSON response.") from error
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(f"GitHub API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise GitHubAPIError(f"Could not reach the GitHub API: {error.reason}") from error
```

```python
def search_code(self, repo: RepositoryRef, query: str, max_results: int = 20) -> list[dict[str, Any]]:
    clean_query = query.strip()
    if not clean_query:
        return []

    max_results = max(1, min(max_results, 100))
    payload = self.get(
        "/search/code",
        {"q": f"{clean_query} repo:{repo.slug}", "per_page": str(max_results)},
    )
    if not isinstance(payload, dict):
        raise GitHubAPIError("GitHub code search returned an unexpected JSON payload.")

    items = payload.get("items")
    if not isinstance(items, list):
        raise GitHubAPIError("GitHub code search response did not contain a list of items.")

    results: list[dict[str, Any]] = []
    for item in items[:max_results]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise GitHubAPIError("GitHub code search returned an invalid result item.")
        results.append(item)
    return results
```

**效果：** 无效编码、无效 JSON、顶层结构错误、`items` 类型错误及缺失路径，都会转换为稳定、可处理的 `GitHubAPIError`。

---

## 5. 搜索去重与空结果缓存

### 改进前

每次模型调用 `search_repository_code("redis")` 都会直接请求 GitHub：

```python
matches = client.search_code(repo, safe_query, max_results=max_results)
```

这会造成：

- 模型重复思考时，重复消耗 GitHub Search API 配额和工具步骤；
- no-result 会被重复请求；
- 如果 API 异常，重复调用会扩大不稳定性；
- 结果中如果存在重复路径，会重复给模型同一文件。

### 改进后

```python
class CodeSearchCache:
    """Deduplicate normalized code searches during one agent lifetime."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, int], list[dict[str, object]]] = {}

    def get_or_search(
        self,
        query: str,
        max_results: int,
        search: Callable[[], list[dict[str, object]]],
    ) -> tuple[list[dict[str, object]], bool]:
        key = (query, max_results)
        if key in self._results:
            return self._results[key], True

        unique_results: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for item in search():
            path = item.get("path")
            if not isinstance(path, str) or path in seen_paths:
                continue
            seen_paths.add(path)
            unique_results.append(item)

        self._results[key] = unique_results
        return unique_results, False
```

**设计说明：**

- 缓存键使用 `(query, max_results)`，避免“先请求 5 条、再请求 20 条”错误复用不完整的结果。
- 缓存保存空列表，所以 no-result 也不会重复访问网络。
- `seen_paths` 保证相同仓库路径只保留一次。
- 缓存绑定到 `build_agent()` 内创建的 `CodeSearchCache`，因此不会跨仓库或长期跨会话保留旧结果。
- JSON 中的 `cache` 字段标明本次观察是 `miss`（首次请求）还是 `hit`（复用缓存）。

---

## 6. 多跳检索

### 改进前

改进前只有一般性提示：搜索到文件后，在必要时读取该文件。工具上限较低，无法稳定支撑“搜索 + 多次读文件 + 继续搜索”的场景。

```python
return ToolCallingAgent(
    tools=all_tools,
    model=model,
    max_steps=4,
    # ...
)
```

### 改进后：提高工具步数并明确跨文件规则

```python
return ToolCallingAgent(
    tools=all_tools,
    model=model,
    max_steps=6,
    # ...
)
```

Agent 描述新增规则：

```text
For questions spanning multiple components, follow references found
in inspected files with a new, distinct code search and read the
additional relevant files. Reuse prior search evidence instead of
repeating the same query.
```

CLI 用户问题提示新增规则：

```text
For cross-file questions, follow imports, calls, configuration references,
or other evidence into additional distinct files with another targeted code search.

Do not repeat an identical code search; reuse its prior evidence,
including a no-match result.
```

**效果：** Agent 可以把一个复杂问题拆成多个证据跳转，而不是只列文件或只读第一个搜索命中的文件。典型跳转包括：

- 从入口文件查到 service；
- 从 service 的 import 查到 client 或 repository；
- 从配置键查到配置文件和实际读取该配置的代码；
- 从函数调用查到函数定义和测试覆盖。

注意：这仍是由模型在工具调用过程中执行的多跳流程，并不是一个自动解析 Python import 图的确定性检索器。它通过更明确的规则、更多步骤和去重缓存，提高实际成功率。

---

## 7. no-result 的回答边界

改进后的 no-result 只表示：**本次 GitHub Code Search 没有找到匹配项**。它不能证明：仓库绝对没有该技术、功能或实现。

因此，最终回答应使用以下语义：

```text
Code Search 未找到匹配项，因此暂时无法从已检索证据中验证该功能；这不能作为它绝对不存在的结论。
```

不应使用：

```text
仓库没有使用 Redis。
```

除非已经有更完整、可信的证据支持绝对否定。

---

## 8. 验证记录

本次改动后已完成以下离线验证：

| 验证项 | 结果 |
| --- | --- |
| `python -m compileall -q src` | 通过 |
| 相同查询缓存、路径去重、纯 JSON 可解析性 smoke test | 通过 |
| `search_code` 对合法和错误 schema 的处理 smoke test | 通过 |
| `git diff --check` | 通过 |

当前虚拟环境没有安装 `pytest`，因此没有运行 pytest 测试套件；本次也没有新增或保留测试文件。

## 9. 未覆盖的边界

本文修复的是两类 JSON 问题：

1. `search_repository_code` 的**工具返回内容**从拼接文本改为纯 JSON；
2. GitHub REST API 的**响应内容**在解码和 schema 层得到校验。

如果“JSON 格式不对”指的是模型本身在调用工具前生成了非法的 function-call `arguments`（例如模型输出的参数 JSON 语法错误），那属于 smolagents / 模型工具调用解析层。本次没有在该层加入自动 JSON 修复或重试策略，需要作为独立问题继续处理。


---

## 10. 证据引用与防幻觉：从提示词要求到程序化校验

本节记录后续针对以下两个问题的改动：

1. 最终回答可能没有给出可追溯证据；
2. “不要幻觉”此前主要依赖提示词，模型仍可能输出无证据事实或编造文件路径。

### 10.1 改进前：只有软性提示词

改进前，CLI 已经要求模型引用证据并且不要猜测：

```text
Repository-specific factual claims MUST be supported by
information retrieved from the configured repository through tools.

Do NOT guess repository-specific facts.

When making repository-specific claims, cite the relevant
repository file path, issue number, pull request number,
or other retrieved repository source.
```

但这只是自然语言规则，没有程序检查：

- 模型可以回答“项目使用 Redis”，却不写文件路径；
- 模型可以写一个从未读取的路径，例如 `src/cache.py`；
- 模型可以在最终步骤忽略“不要猜测”的提示；
- 工具结果没有稳定的、可验证的引用 ID；
- 到达 Agent 最大步骤时，框架的常规最终答案检查可能不会执行。

### 10.2 改进后：本轮证据账本

新增文件：`src/github_agent/evidence.py`。

```python
class EvidenceLedger:
    """Track evidence sources and validate final-answer citations for one run."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, EvidenceRecord] = OrderedDict()

    def record(self, kind: str, reference: str) -> str:
        clean_kind = kind.strip().lower().replace(" ", "_")
        clean_reference = reference.strip().replace("]", "%5D")
        if not clean_kind or not clean_reference:
            raise ValueError("Evidence kind and reference must be non-empty.")

        source_id = f"{clean_kind}:{clean_reference}"
        self._records.setdefault(
            source_id,
            EvidenceRecord(source_id=source_id, kind=clean_kind, reference=clean_reference),
        )
        return self._records[source_id].citation
```

每个已成功检索的来源会登记为精确的引用 token，例如：

```text
[evidence:repository:owner/repo]
[evidence:file:src/cache.py]
[evidence:code_search:redis]
[evidence:issues:owner/repo]
```

工具结果会把真实 token 暴露给模型。例如读取文件时：

```python
content = client.file_text(repo, path)
if not content:
    return "[EVIDENCE_NOT_FOUND] ..."

citation = evidence_ledger.record("file", path.strip("/"))
return (
    "[REPOSITORY_EVIDENCE]\n"
    f"Citation: {citation}\n"
    "Evidence type: repository file content\n"
    f"File: {path}\n"
    "Content:\n"
    f"{content[:12000]}"
)
```

Code Search 的 JSON 返回也携带该引用：

```python
citation = evidence_ledger.record("code_search", safe_query)
# ...执行搜索并得到 matches...
return format_code_search_evidence(
    safe_query,
    matches,
    cached=cached,
    citation=citation,
)
```

因此，模型不会只看到文件名或一段无来源文本，而是看到可用于最终回答的精确引用。

### 10.3 改进后：拒绝无引用或伪造引用的最终答案

`EvidenceLedger.validate_final_answer()` 会校验最终答案：

```python
def validate_final_answer(self, answer: Any) -> bool:
    text = str(answer).strip()
    if not text:
        raise EvidenceValidationError("Final answer is empty.")

    citations = {f"[evidence:{source_id}]" for source_id in _CITATION_PATTERN.findall(text)}
    if not self._records:
        if any(marker in text.lower() for marker in _UNVERIFIED_MARKERS):
            return True
        raise EvidenceValidationError(
            "No repository evidence was retrieved; state that the answer could not be verified from the repository."
        )

    if not citations:
        raise EvidenceValidationError(
            "Final answer must cite at least one retrieved source using an [evidence:...] token."
        )

    unknown_citations = citations.difference(self.citations())
    if unknown_citations:
        raise EvidenceValidationError(
            "Final answer cited sources that were not retrieved: " + ", ".join(sorted(unknown_citations))
        )
    return True
```

这实现了三条硬规则：

| 场景 | 改进后的行为 |
| --- | --- |
| 没有检索任何仓库证据 | 最终回答必须明确包含“无法从仓库证据中验证”或对应英文语句。 |
| 已检索证据，但最终回答没有引用 | 拒绝该最终答案。 |
| 最终回答引用了未检索的 ID | 拒绝该最终答案，防止伪造引用。 |
| 最终回答引用了本轮真实 ID | 允许继续输出。 |

### 10.4 改进后：接入 smolagents 最终答案检查

smolagents 1.26 的 `ToolCallingAgent` 支持 `final_answer_checks`。本次将证据校验接入此扩展点：

```python
def require_evidence_citations(final_answer: object, _memory: object, *, agent: object) -> bool:
    """Reject final answers that omit or invent evidence citations."""
    return evidence_ledger.validate_final_answer(final_answer)

agent = ToolCallingAgent(
    tools=all_tools,
    model=model,
    max_steps=6,
    final_answer_checks=[require_evidence_citations],
    name="github_repository_analyst",
    # ...
)
agent.evidence_ledger = evidence_ledger
return agent
```

当模型准备提交最终回答时：

```text
没有引用 / 引用了伪造来源
  → final_answer_checks 报错
  → smolagents 拒绝当前最终回答并让 Agent 继续执行
```

### 10.5 改进后：CLI 层兜底，避免最大步骤绕过校验

`final_answer_checks` 仅在模型通过最终工具调用提交答案时运行。Agent 达到最大步骤后，框架会走另一条生成最终答案的路径。因此 CLI 再执行一次校验：

```python
answer = agent.run(prompt)
evidence_ledger = getattr(agent, "evidence_ledger", None)
if evidence_ledger is not None:
    try:
        evidence_ledger.validate_final_answer(answer)
    except ValueError as error:
        answer = evidence_ledger.fallback_answer(error)

print(answer)
```

若最终答案仍不合格，系统不会继续输出其中可能包含的无证据仓库结论，而是输出安全的非事实性提示和本轮可用引用列表。

### 10.6 更新后的模型规则

模型提示词也同步加强为：

```text
Every repository-specific factual claim must have supporting evidence;
never invent a citation token.

If no evidence citation is available, include the exact phrase:
`无法从仓库证据中验证`.
```

### 10.7 防护范围与限制

本次改动保证：

- 最终答案至少有一个本轮真实检索到的引用；
- 引用 ID 不能凭空编造；
- 没有任何证据时不能输出为已验证的仓库事实；
- 无法通过校验时，CLI 不会输出原始的无证据答案。

它**不能仅靠字符串校验就证明每一句话都被对应文件的内容严格蕴含**。例如，模型可能引用了真实的 `src/cache.py`，但对其中逻辑作出不够准确的总结。对此仍通过以下方式降低风险：

1. 提示词要求每个仓库事实都引用支持它的证据；
2. 对实现问题要求先读文件、不能只凭搜索命中下结论；
3. 不足时要求明确说明“无法从仓库证据中验证”。

若需要更严格的“逐句证据蕴含”验证，下一步可加入第二个 LLM/规则审计步骤，对“结论—引用”逐条进行复核。

### 10.8 本轮验证

| 验证项 | 结果 |
| --- | --- |
| `python -m compileall -q src` | 通过 |
| EvidenceLedger：真实引用通过、缺失引用拒绝、伪造引用拒绝 | 通过 |
| Code Search JSON 中包含真实 citation | 通过 |
| `ToolCallingAgent.final_answer_checks` 与 `evidence_ledger` 接线 | 通过 |


---

## 11. Memory 优化：本轮文件缓存与紧凑检索摘要

本节记录对问题 3（Memory 优化）的实现。目标不是把所有源码长期塞进模型上下文，而是在单次仓库问答中复用已读文件、减少重复网络读取和重复全文观察，同时保持最终答案的证据约束。

### 11.1 实现范围与分层策略

Memory 按风险和收益分层：

| 层次 | 状态 | 作用 |
| --- | --- | --- |
| 本轮文件内容缓存 | **本次已实现** | 同一路径只从 GitHub 读取一次。 |
| 本轮结构化文件摘要 | **本次已实现** | 重复读取时只向模型返回路径、大小、行数和符号列表。 |
| Memory 检查工具 | **本次已实现** | Agent 可查询当前已读文件摘要，用于规划多跳检索。 |
| 上下文预算/摘要压缩 | 后续建议 | 对大量不同文件建立 token 预算和全文转摘要策略。 |
| 按 commit 隔离的跨轮持久记忆 | 后续建议 | 复用仓库索引，但避免旧分支/旧提交污染新答案。 |

本次 Memory 的生命周期与一次 `build_agent()` 创建的 Agent 相同。CLI 的每次 `ask` 都重新创建 Agent，因此不会把一个仓库/问题的内存自动带到下一次 CLI 问答。

### 11.2 改进前：每次读取都会访问 GitHub 并重复注入全文

原来的文件读取逻辑如下：

```python
@tool
def read_repository_file(path: str) -> str:
    content = client.file_text(repo, path)

    if not content:
        return (
            "[EVIDENCE_NOT_FOUND]\n"
            f"File: {path}\n"
            "The requested file could not be read or contained no readable content."
        )

    citation = evidence_ledger.record("file", path.strip("/"))
    return (
        "[REPOSITORY_EVIDENCE]\n"
        f"Citation: {citation}\n"
        "Evidence type: repository file content\n"
        f"File: {path}\n"
        "Content:\n"
        f"{content[:12000]}"
    )
```

问题：

- 模型在多跳推理中再次请求同一个路径时，会再次调用 GitHub API；
- 每次都会把最多 12,000 个字符的全文放进 Agent memory；
- 重复内容浪费步骤、API 配额和上下文窗口；
- Agent 没有可查询的“我已经读过哪些文件”的结构化目录。

### 11.3 改进后：`RepositoryMemory` 缓存文件和确定性摘要

新增文件：`src/github_agent/repository_memory.py`。

```python
@dataclass(frozen=True)
class FileMemory:
    path: str
    content: str
    character_count: int
    line_count: int
    symbols: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        return {
            "path": self.path,
            "character_count": self.character_count,
            "line_count": self.line_count,
            "symbols": list(self.symbols),
        }
```

`symbols` 使用确定性正则提取常见 Python 和 TypeScript/JavaScript 声明，例如 `def`、`class`、`function`、`interface`、`type`、`enum`。它是辅助规划信息，不是模型生成的结论。

```python
class RepositoryMemory:
    def __init__(self) -> None:
        self._files: OrderedDict[str, FileMemory] = OrderedDict()

    def read_or_get(self, path: str, loader: Callable[[], str]) -> tuple[FileMemory, bool]:
        clean_path = path.strip("/")
        if clean_path in self._files:
            return self._files[clean_path], True

        content = loader()
        record = FileMemory(
            path=clean_path,
            content=content,
            character_count=len(content),
            line_count=len(content.splitlines()),
            symbols=_extract_symbols(content),
        )
        self._files[clean_path] = record
        return record, False

    def summaries(self, path: str = "") -> list[dict[str, object]]:
        clean_path = path.strip("/")
        if clean_path:
            record = self._files.get(clean_path)
            return [record.summary()] if record else []
        return [record.summary() for record in self._files.values()]
```

关键行为：

- `/src/app.py/` 和 `src/app.py` 归一为同一个缓存键；
- 首次访问运行 `loader()`，后续访问直接返回缓存记录；
- 摘要不包含完整 `content`，所以适合加入模型上下文；
- 缓存仅限当前 Agent 生命周期，不存在跨仓库、跨 commit 的陈旧记忆问题。

### 11.4 改进后：文件读取工具按缓存命中返回摘要

```python
file_memory, cached = retrieval_memory.read_or_get(
    path,
    lambda: client.file_text(repo, path),
)
content = file_memory.content

if not content:
    return "[EVIDENCE_NOT_FOUND] ..."

citation = evidence_ledger.record("file", file_memory.path)
if cached:
    return json.dumps(
        {
            "memory_type": "repository_file_summary",
            "cache": "hit",
            "citation": citation,
            "summary": file_memory.summary(),
            "note": "The file was already read during this agent run; full content is not repeated."
        },
        ensure_ascii=False,
        indent=2,
    )

return (
    "[REPOSITORY_EVIDENCE]\n"
    f"Citation: {citation}\n"
    "Evidence type: repository file content\n"
    f"File: {file_memory.path}\n"
    "Content:\n"
    f"{content[:12000]}"
)
```

首次读取仍返回原始文件内容和 `[evidence:file:...]`，因此可作为最终结论的证据。重复读取则返回类似：

```json
{
  "memory_type": "repository_file_summary",
  "cache": "hit",
  "citation": "[evidence:file:src/app.py]",
  "summary": {
    "path": "src/app.py",
    "character_count": 2450,
    "line_count": 98,
    "symbols": ["App", "run"]
  }
}
```

### 11.5 改进后：增加 Memory 检查工具

新增 `inspect_retrieval_memory`：

```python
@tool
def inspect_retrieval_memory(path: str = "") -> str:
    """Return compact summaries of files already read during this agent run."""
    summaries = retrieval_memory.summaries(path)
    if not summaries:
        return "[EVIDENCE_NOT_FOUND]\nNo matching file memory is available for this agent run."
    return json.dumps(
        {
            "memory_type": "repository_retrieval_memory",
            "files": summaries,
            "note": "Use this summary to plan retrieval; cite the original file evidence in final answers.",
        },
        ensure_ascii=False,
        indent=2,
    )
```

Agent 可以在多跳时先查看已读文件的路径和符号，再决定是否搜索新引用或读取新文件。

**重要边界：** 该工具返回的是 Memory/规划信息，而不是新的仓库证据。最终回答仍必须使用首次读文件时登记的真实 `[evidence:file:...]` 引用，不能将 Memory 摘要本身当作证据来源。

### 11.6 Prompt 与工具路由更新

Agent 和 CLI 均增加以下策略：

```text
Use inspect_retrieval_memory to recall compact metadata for files
already read in this run. It is for planning only; final claims
must cite original repository evidence rather than a memory summary.

Before rereading a file, inspect retrieval memory; repeated file
reads return a compact summary, not full content.
```

### 11.7 尚未实现的后续优化

本次没有实现跨轮持久化，以避免缓存旧代码后把错误版本作为当前证据。下一阶段可以实现：

1. **上下文预算：** 限制完整文件观察数量；超出后把旧全文转换为摘要。
2. **按版本持久化：** 以 `owner/repo + branch/commit` 作为 key 保存文件哈希、符号索引和摘要。
3. **版本失效：** branch 或 commit 变化时自动失效或重新检索。
4. **证据重新确认：** 跨轮摘要只能加快规划；最终结论仍重新读取当前 commit 的文件并登记本轮引用。

### 11.8 本轮验证

| 验证项 | 结果 |
| --- | --- |
| `RepositoryMemory` 首次加载与规范路径缓存复用 | 通过 |
| Memory 摘要包含路径、字符数、行数和符号 | 通过 |
| `ToolCallingAgent` 包含 `inspect_retrieval_memory` 且暴露本轮 Memory | 通过 |
| `python -m compileall -q src` | 通过 |


---

## 12. 通用 API Cache、失败恢复与 Agent Trace

本节记录对以下问题的改进：通用 GitHub API 没有缓存、临时 API 失败不会恢复、没有项目级可持久查看的 Agent trace。`max_steps=6` 已在第 6 节实现，因此这里保留该限制并将实际步数写入 trace，而不重复增加无限执行能力。

### 12.1 改进前：每次 GET 都直接请求，失败立即终止

原来的 `GitHubClient.get()` 每次都直接调用 `urlopen()`：

```python
def get(self, path: str, params: dict[str, str] | None = None) -> Any:
    # 省略 URL 和 headers 构造
    request = Request(f"{self.api_base}{path}{query}", headers=headers, method="GET")
    try:
        with urlopen(request, timeout=self.timeout) as response:
            raw_response = response.read()
        return json.loads(raw_response.decode("utf-8"))
    except HTTPError as error:
        raise GitHubAPIError(...) from error
    except URLError as error:
        raise GitHubAPIError(...) from error
```

问题：

- repository metadata、languages、tree、file content、Issue、PR 等重复 GET 没有统一缓存；
- 短暂网络故障、GitHub `429`、`5xx` 会立即终止检索；
- 没有记录调用次数、缓存命中、退避或失败原因；
- 虽然 smolagents 内存中有步骤信息，但项目没有可在运行结束后查看的结构化 trace 文件。

### 12.2 改进后：每个 `GitHubClient` 的统一内存 GET Cache

`src/github_agent/github_api.py` 新增 `APIResponseCache`：

```python
class APIResponseCache:
    """Small per-client cache for JSON-safe GitHub GET responses."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._entries: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], CachedResponse] = OrderedDict()

    def get(self, key: tuple[str, tuple[tuple[str, str], ...]]) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return copy.deepcopy(entry.payload)

    def put(self, key: tuple[str, tuple[tuple[str, str], ...]], payload: Any, ttl_seconds: float) -> None:
        self._entries[key] = CachedResponse(
            payload=copy.deepcopy(payload),
            expires_at=self._clock() + ttl_seconds,
        )
```

`get()` 使用 `path + 排序后的参数` 作为缓存键：

```python
normalized_params = tuple(sorted((params or {}).items()))
cache_key = (path, normalized_params)
cached_payload = self._cache.get(cache_key)
if cached_payload is not None:
    self._emit("api_cache_hit", path=path, params=dict(normalized_params))
    return cached_payload
```

缓存命中和写入都使用深复制，因此工具调用者即使修改返回 dict，也不会污染缓存中的原始响应。

TTL 策略：

| 端点 | 最大 TTL | 原因 |
| --- | --- | --- |
| Search API | 120 秒 | 搜索结果短期可复用，但可能随仓库更新变化。 |
| Issues / Pull Requests | 60 秒 | 状态和更新时间变化较频繁。 |
| 其他 GET | 300 秒 | 单次问答中通常稳定，可避免重复 metadata/tree/file 请求。 |

缓存只存在于当前 `GitHubClient` 实例。CLI 每次 `ask` 创建新 client，因此不会把旧 commit 的 API 响应自动复用到下一次问答。这与第 11 节的证据版本安全原则一致。

### 12.3 改进后：针对瞬态错误的有限重试与指数退避

`GitHubClient` 增加可配置参数：

```python
def __init__(
    self,
    token: str | None = None,
    timeout: int = 20,
    max_retries: int = 2,
    backoff_seconds: float = 0.25,
    cache_ttl_seconds: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    # ...
) -> None:
```

核心重试逻辑：

```python
for attempt in range(self.max_retries + 1):
    try:
        with urlopen(request, timeout=self.timeout) as response:
            raw_response = response.read()
        payload = json.loads(raw_response.decode("utf-8"))
        self._cache.put(cache_key, payload, self._cache_ttl(path))
        return copy.deepcopy(payload)
    except HTTPError as error:
        if self._is_retryable_status(error.code) and attempt < self.max_retries:
            delay = self._retry_delay(attempt, error.headers.get("Retry-After"))
            self._sleep(delay)
            continue
        raise GitHubAPIError(...) from error
    except URLError as error:
        if attempt < self.max_retries:
            self._sleep(self._retry_delay(attempt))
            continue
        raise GitHubAPIError(...) from error
```

错误策略：

| 错误类型 | 行为 |
| --- | --- |
| `429 Too Many Requests` | 最多重试 2 次；优先使用 `Retry-After`，等待时间上限 30 秒。 |
| `5xx` | 最多重试 2 次，指数退避：0.25 秒、0.5 秒。 |
| `URLError` / 临时网络问题 | 最多重试 2 次，指数退避。 |
| `4xx` 参数、权限、资源错误 | 不重试，直接返回受控 `GitHubAPIError`。 |
| 无效 UTF-8 / 无效 JSON | 不重试，记录 `invalid_json`，避免对错误响应盲目请求。 |

重试只适用于幂等 GET 请求，不会改变仓库内容。

### 12.4 改进后：默认脱敏 JSONL Trace

新增 `src/github_agent/trace.py`。每个 CLI `ask` 默认创建一个 run：

```python
trace = TraceRecorder(args.trace_dir) if args.command == "ask" and not args.no_trace else None
client = GitHubClient(event_sink=trace.api_event if trace is not None else None)
agent = build_agent(client, repo, trace=trace)
```

默认 trace 目录是 `.github_agent_traces/`，可以使用 `--trace-dir <目录>` 修改，或使用 `--no-trace` 关闭。该目录已加入 `.gitignore`。

每一行是一个独立 JSON 对象（JSONL），示例：

```json
{
  "timestamp": "2026-09-17T10:00:00+00:00",
  "run_id": "...",
  "event": "agent_step",
  "step_number": 2,
  "tools": [
    {
      "name": "read_repository_file",
      "arguments": {"path": "src/app.py"}
    }
  ],
  "duration_ms": 42,
  "error_type": null
}
```

记录事件包括：

- `agent_run_started`：仓库、问题长度、最大步骤数；
- `api_request`、`api_cache_hit`、`api_retry`、`api_error`；
- `agent_step`：工具名、脱敏参数、步骤耗时、错误类型、是否最终答案；
- `agent_run_completed`：实际步骤数、最终证据校验状态、引用列表和答案长度；
- `agent_run_failed`：受控异常类型。

### 12.5 脱敏与数据边界

Trace 会递归处理参数：

```python
_SENSITIVE_KEYS = {"authorization", "content", "password", "secret", "token", "api_key"}
_MAX_STRING_LENGTH = 240


def _sanitize(value: Any, key: str = "") -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING_LENGTH else value[:_MAX_STRING_LENGTH] + "…[truncated]"
    # ...
```

因此 trace **不记录**：GitHub Token、模型 API Key、Authorization header、文件正文、工具完整 observation、最终答案正文。它记录的是调试所需的元数据，而不是仓库内容副本。

### 12.6 Agent 步骤接入

`build_agent()` 使用 smolagents 的 `step_callbacks`：

```python
def trace_agent_step(step: object, agent: object) -> None:
    if trace is not None:
        trace.record_agent_step(step)

agent = ToolCallingAgent(
    tools=all_tools,
    model=model,
    max_steps=6,
    final_answer_checks=[require_evidence_citations],
    step_callbacks=[trace_agent_step] if trace is not None else None,
    # ...
)
```

这保留现有 `max_steps=6` 的无限循环防护，同时让 trace 可诊断 Agent 是否因重复工具、重试或证据校验失败耗尽步骤。

### 12.7 本轮验证

| 验证项 | 结果 |
| --- | --- |
| 相同 GET 的 API cache 命中，底层 `urlopen` 仅调用一次 | 通过 |
| 网络 `URLError` 后按 0.25 秒退避重试，并在第二次成功 | 通过 |
| Trace 中 Token/API key 脱敏、步骤耗时正确记录 | 通过 |
| Trace API event 与 smolagents ActionStep callback 注册 | 通过 |
| `.github_agent_traces/` 被 `.gitignore` 忽略 | 通过 |
