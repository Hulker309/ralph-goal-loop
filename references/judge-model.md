# Judge Model — Routing, Cost & Failure Modes

> 配 `SKILL.md §Cost & Iteration Caps` 段。本文件是它的事实展开:judge 调哪个模型、怎么配、cost 几何、哪 6 个坑必看。

## 1. 默认: 主对话模型(同 worker world-view)

`GoalManager.evaluate_after_turn()` → `judge_goal()` → `_call_goal_judge_llm(call_llm, system, user, timeout)` 这条链在 `hermes_cli/goals.py:848`。当 `config.yaml::auxiliary.goal_judge` **没有配置**时,`call_llm` 直接走主对话模型的 credentials(per `agent/auxiliary_client.py` 的 fallthrough 默认),judge 跟 worker 共享同一个 model + base_url + api_key。

**设计意图**(per SKILL.md §Cost & Iteration Caps 直接引用):

> 当 judge 跟 worker 是同一个模型时,它们共享同一个"worldview"——同套 domain knowledge,同套"什么算 done"直觉。不同模型 judge 可能在边界情况下不同意,不是 work 错,而是 default 不同。

**事实依据**(从 SKILL.md §Cost & Iteration Caps "judge model 设计意图"段):

- "judge LLM uses the SAME model as the worker (the main conversation model)"
- "Shared world-view" 是显式写出来的 rationale
- 用户覆盖入口: `auxiliary.goal_judge.{provider,model,base_url,timeout,max_tokens}` — 这是 `_goal_judge_setting(key, default, cast)` 在 `goals.py:723` 的 helper 直接读 config 的那套

## 2. 三个配置 example

下面 3 段 yaml 都是 `~/.hermes/config.yaml` 的 `auxiliary.goal_judge` 子树。Per `_goal_judge_setting()` 的 helper,任何缺失字段 fall through 到主对话模型的同名字段。

### 2a. Default(走主对话,显式不写)

```yaml
# auxiliary:  # ← 不写这一节就走 default
#   goal_judge:  # ← 没配 → fallback 主对话模型
```

行为: judge LLM = 主对话模型(provider/model/base_url/api_key 全继承)。适合"judge 必须跟 worker 同一世界观"场景。

### 2b. OpenRouter Gemini Flash(便宜 + 强 JSON)

```yaml
auxiliary:
  goal_judge:
    provider: openrouter
    model: google/gemini-3-flash-preview
    timeout: 10       # seconds, judge call hard ceiling
    max_tokens: 500   # judge output token cap (verdict JSON)
```

行为: judge call 走 OpenRouter + Gemini Flash。`timeout` 经 `_goal_judge_timeout()` 在 `goals.py:741` 读,缺省 fallback 主对话默认。`max_tokens` 经 `_goal_judge_max_tokens()` 在 `goals.py:737` 读,缺省 fallback 主对话默认。OpenRouter key 走 `OPENROUTER_API_KEY` env 或 config `providers.openrouter.api_key`。

### 2c. 本地 Ollama qwen2.5:1.5b(零 cost,judge 退化风险高)

```yaml
auxiliary:
  goal_judge:
    provider: openai   # Ollama 走 OpenAI-compatible /v1
    model: qwen2.5:1.5b
    base_url: http://localhost:11434/v1
    timeout: 30        # 本地推理慢,timeout 给宽
    max_tokens: 200
```

行为: judge 走本地 Ollama daemon,所有成本 = 0。**但 1.5b 模型做 JSON verdict 解析不稳**(per `_parse_judge_response` 在 `goals.py:768` 的多层 fallback 暗示 1.5b 经常吐出非 JSON,需要 `_extract_json_object` 救)。这条 path 会让 `state.consecutive_parse_failures` 累加,到 `DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES`(per `goals.py:1506`)自动 pause 报"judge model returned unparseable output"。

## 3. fail-OPEN 继承自 /goal 原生语义

per SKILL.md §Cost & Iteration Caps "Failure mode" 段:

> if `auxiliary.goal_judge` is configured but the call fails (network, auth, rate-limit), `/goal` official behavior is **fail-OPEN** — the judge returns `("continue", ...)` and the turn budget is the backstop.

**事实依据**(per `goals.py:864` `judge_goal()` 跟 `goals.py:848` `_call_goal_judge_llm()`):

- transport exception → `transport_failed=True` 返回
- `evaluate_after_turn` 在 `goals.py:1473` 把 `consecutive_transport_failures += 1`
- **但** verdict 直接 `continue`(即 fail-OPEN,不因单次 transport error 强 abort)
- **硬卡点**: 连续 `DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES` 次 transport failure 才在 `goals.py:1499` 走 `_pause_decision` 让用户检查 config

ralph-goal-loop orchestrator 继承这层语义: judge 偶发挂不影响主循环,主循环靠 `max_iterations` + `cost_cap_usd` 兜底。

## 4. Cost 测算表格

Per SKILL.md §Cost & Iteration Caps "Cost note" 段:~200 input token + ~50 output token / turn。每个 goal loop 跑 20 turns default。Cost / turn 三档:

| 模型 | Provider | 输入 $/1k | 输出 $/1k | $/turn | 20 turns $/loop | 100 loops $/1000 turns |
|---|---|---|---|---|---|---|
| **主对话**(M2.7-class 估测) | 跟随主 provider | 0.003 | 0.015 | **0.0003** | 0.006 | 0.30 |
| **Gemini Flash** | OpenRouter | 0.000075 | 0.0003 | **0.00003** | 0.0006 | 0.03 |
| **本地 Ollama 1.5b** | 本地 | 0 | 0 | **0** | 0 | 0 |

**Worker cost**(跟 judge 完全分开,本 skill orchestrator 不替 worker 算):worker cost 走主对话 + 子 agent tool calls,典型 1 个 story $0.05-$0.30,3-story prd 默认 ~$0.5-$1.0。worker cost 触发 `cost_cap_usd`(默认 $5)就 abort。

**数字来源**:
- "judge call is ~200 input tokens + ~50 output tokens per turn" — 直接抄自 SKILL.md §Cost & Iteration Caps
- "Same model: ~$0.0003/turn (M2.7-class)" — 同上
- "Gemini Flash: ~$0.00003/turn" — 同上
- "Local SLM: $0" — 同上
- "Loop budget: 20 turns default → per-loop cost in the cents" — 同上

输入/输出 $/1k 是按上述 $/turn 反推的近似(用于表里填字段),**不是**provider 官方价目(老板实操时应按当前 provider 价目校准)。

## 5. 6 个已知坑(judge model 专属)

Per SKILL.md §Pitfalls 第 8 条(`auxiliary.goal_judge` 配错)为本节根,本节展开成 6 个具体坑:

### 5.1 配错 provider 名

`config.yaml::auxiliary.goal_judge.provider` 写成 `gemini`(应是 `openrouter` 配 `model: google/gemini-...`)或 `ollama`(应是 `openai` + `base_url`)。`_goal_judge_setting()` 不校验 provider 名,只在 call 时 fail → consecutive_transport_failures 累加。**Mitigation**: orchestrator 启动时调 `_goal_judge_setting("provider", None, str)` 看返回是不是预期 provider 名。

### 5.2 model 名 typo

`model: google/gemini-3-flash-preview` typo 成 `gemini-3-flash-preview`(漏 `google/` 前缀)。OpenRouter 直接 404,本地 daemon 直接 model_not_found。**Mitigation**: orchestrator 不在 v0.1.0 实做 ping test(成本),只 warn 一次。

### 5.3 timeout 太短

本地 Ollama 1.5b cold start 5-15s。`timeout: 5` 必 fail。**Mitigation**: 配本地模型时 `timeout: 30+`,见 §2c。

### 5.4 max_tokens 太小

judge 输出是 JSON verdict(per `goals.py:768` `_parse_judge_response` 期望 `{verdict, reason, wait}` 结构),`max_tokens: 50` 必截断 → `consecutive_parse_failures` 累加。**Mitigation**: `max_tokens: 200+`,见 §2c。

### 5.5 judge 跟 worker 不同 model 但不同 base_url

例: worker 走 Anthropic(`base_url: https://api.anthropic.com`),judge 走 OpenRouter(`base_url: https://openrouter.ai/api/v1`)。API key 没设对会 fail。**Mitigation**: `_goal_judge_setting()` 不会校验 api_key 是否存在,只在 call 时 fail → fail-OPEN 兜底,20 turns 后才 hit hard cap。

### 5.6 配了 judge 但没配 base_url / api_key

例: judge 用 `provider: openrouter` 但 config 没 `providers.openrouter.api_key`,也没 `OPENROUTER_API_KEY` env。`call_llm` 内部 fallback 到主 provider 但 provider 名不匹配 → fail。**Mitigation**: 实操前先 `hermes auxiliary test-judge` 之类(若该命令存在);否则准备一份 config sanity check 脚本。

## 6. 不在范围内

- ❌ 改 `_call_goal_judge_llm` / `_parse_judge_response` / `evaluate_after_turn` 任何 hermes_cli/goals.py 的代码(0 修改核心)
- ❌ 加 retry 逻辑(consecutive_failures pause 已够用)
- ❌ 加 streaming(judge 输出 ~50 token,无意义)
- ❌ 加 judge 多 model 投票(成本 ↑、复杂度 ↑,per /goal 原生 single-judge 已稳定)
