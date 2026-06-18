# Agentic-RL 项目记忆 / Resume Brief

本文件随项目仓库同步，是跨机器恢复本项目工作的完整记录。任何机器打开本目录时由 Claude Code 自动加载。中文写作遵守用户全局 CLAUDE.md 的文体约束（禁用词表、不用 em dash、正式学术文体）。

## 1. 目标与背景

用户是 next-gen infra engineer，目标是为 **Long-Horizon Agentic RL** 搭建基础设施。RL 组现状：在 **verl 0.8** 上用 tau2 bench 做 Agentic RL，但环境是 **static** 的（每个环境一次性、浪费 pod、缺噪声），对长程不友好。两个核心痛点贯穿全部分析：
1. 把 static 环境转成 **dynamic** 环境；
2. 更好的 **async RL 效率**。
用户倾向全面拥抱前沿模型的 RL infra（slime/miles），做自己的版本。GLM-5.2 发布后，用户关注其对 **PPO with critic 在长程中的作用** 的强调。

## 2. 交付物清单（均在 docs/，全部完成并通过质量闸）

- `docs/AReaL/`：AReaL 逐文件精读 12 课 + 索引（更早完成）。
- `docs/slime/`：slime 逐文件精读：索引 + 第 1-10 课 + 第 11 课（miles 增量）。第 1 课为手工校准样章，其余由 workflow 产出。
- `docs/verl/`：verl 逐文件精读：索引 + 12 课。本地 verl 版本 0.9.0.dev（生产用 0.8）。
- `docs/POLARS/`：Polar(ProRL-Agent-Server) 逐文件精读：索引 + 8 课（与更早的 `polar-sampling-algorithm.html` 并列）。
- `docs/rl-framework-comparison.html`：**五框架大横评**（核心交付）。当前 12 节：定位 / 总览矩阵 / 分维度深入(7 维 Tab) / Async RL 深度剖析 / 各框架的问题(Tab) / 决策矩阵 / 针对你们的明确推荐 / tau2 专项(动态+含噪) / slime 数据自由度·AnyGym·DTA·Async 大 PPO(4 Tab) / Async PPO with critic·长程信用分配 / 自研版本架构建议 / 风险。
- `docs/slime-async-internals.html`：代码级深读：fully_async 在途池 + delta 权重同步。
- `docs/AutoGym-Survey-v1.8-zh.html`：用户自己的 AnyGym/AutoGym 设计稿（更早，非本轮产出）。

本地源码仓库：`AReaL/`、`slime/`、`miles/`、`verl/`、`ProRL-Agent-Server/`(Polar)。

## 3. 核心结论（横评推荐）

两痛点沿“训练侧 / 环境侧”正交分开，无单一框架同时最优。**推荐组合**：
- **训练内核 = slime（超大 MoE / 低精度 / train-infer 逐位一致时用 miles）**：train efficiency 生产验证最硬（GLM-4.5→5.2）、delta 权重同步(约 2% 字节)、fully_async 在途池、流式 partial rollout、原生 PPO-with-critic。
- **环境/rollout 服务层 = Polar（或 Polar 式自研）**：唯一内置容器 runtime 池化(static→dynamic env、省 pod)、八种 harness 零改造、rollout-as-a-service。
- 二者经 Polar 现成的 `src/slime_bridge/rollout.py` 连接（已有可跑的 SWE-Gym GRPO 训练曲线）。
- verl 长处（HybridFlow 放置最灵活、agent_loop/BaseTool、多推理后端、PPO-critic 本源最成熟）值得借鉴，但其异步/agentic 在 experimental，作主内核证据不如 slime/miles。

## 4. 关键技术发现（避免重新推导）

- **slime** = GLM-4.5→5.2 训练框架（Megatron + 单一 SGLang）。**miles** = slime 的 fork（radixark/InfiX + 蚂蚁 + SGLang RL 团队），增量：router(R3 路由重放服务化)、true_on_policy(逐位一致)、统一 FP8、INT4 W4A16 QAT、MrlX；**移除了 slime 的 agent/ 子包**（harness-as-env 只在 slime 主干）。
- **verl 0.9.0.dev**：异步已实现(非 planned)，在 `experimental/`(agent_loop / fully_async_policy / one_step_off_policy / separation) 与 `trainer/ppo/v1/`(trainer_sync / trainer_colocate_async / trainer_separate_async) 双轨，官方计划合并入主库；`separate_async` 当前强制 bypass 修正(Decoupled PPO 仍 TODO)。`recipe/` 已迁为独立 submodule(本地未初始化)。
- **Polar**(NVIDIA-NeMo)：harness-as-env(claude_code/codex/gemini_cli/opencode/openhands_sdk/pi/qwen_code/shell 八种零改造)、runtime 池化(docker/apptainer 会话级容器复用)、Rollout as a Service(Staging + balancer)；trainer-agnostic；2026-05 新仓、slime_bridge 为 demo、推理后端只 SGLang 需打补丁。
- **tau2 必须 dynamic + noisy**：GRPO 组内优势 `A_i=(r_i-mean)/(std+ε)`，static 且确定 → std→0 → 优势→0 → 无梯度；slime 的 `check_reward_nonzero_std`(filter_hub/dynamic_sampling_filters.py:9，std>1e-6 才保留)正据此丢零方差组。tau2 一次 rollout = 被训策略 + 外部 LLM 用户模拟器(gemini，`user_strategy=llm`)多轮 + 工具 + 有状态 DB + 规则奖励。slime 有 `examples/tau-bench`(generate_with_tau.py、trainable_agents.py、openai_tool_adapter、sglang_tool_parser)；AReaL 有 `examples/tau2`；verl 仓内无 tau(DIY)。
- **async 机制(代码级见 docs/slime-async-internals.html)**：slime fully_async 在途池(`rollout/fully_async_rollout.py`：全局常驻线程 + asyncio loop，`max_concurrent = sglang_server_concurrency × 引擎数`，与 batch 解耦；ABORTED 组退回 data_buffer 不送训练) + delta 权重同步(`update_weight/update_weight_from_distributed_delta.py`：pinned-CPU 快照按位 diff、gap 编码 ~2%、双侧流流水线、NCCL 每桶广播或写盘、首次只播种快照、colocate 禁用 delta)。
- **DTA(Dynamic Tree Attention)**：AReaL 有(`areal/models/tree_attn/`：tree/module/module_megatron/module_fsdp/module_archon/triton_kernel + tree_training 文档)，DFS 遍历前缀树、共享前缀只算一次、任一时刻只物化一条 root-to-leaf 路径，对 tau2(长共享前缀 × G 样本 × 多轮)效率高。**slime/miles 无 tree attention**，但数据侧前缀树已在 `slime/agent/trajectory.py`(消息树、CLEAN/REALIGN/FORK)，miles 有可替换 attention 插件；缺 compute 侧实现。横评 DTA tab 有 5 步移植方案，落点 miles。
- **PPO with critic(GLM-5.2 长程重点)**：critic 支持 GAE(per-token TD bootstrap 信用分配)，且省 G 倍 rollout(critic 当基线，每 prompt 可少到 1 条 vs GRPO 需 G 条)。slime **原生支持**(`--use-critic`、独立 critic 训练组、`--num-critic-only-steps` 预热、`vanilla_gae`/`chunked_gae`+CP，`train_async.py` 内 `critic_model.async_train`，delta 同步 actor+critic 两套权重) → GLM-5.2 实证 async PPO-critic 长程。verl critic 本源最成熟(HybridFlow，`one_step_off` 用 `need_critic` 支持异步+critic)。AReaL 有 `PPOCritic`/`PPOCriticControllerV2` + clipped value loss。Polar 不含训练。
- **custom rollout = 一等数据入口**：slime 用 `--rollout-function-path` / `--custom-generate-function-path` / `--custom-rm-path` / `--dynamic-sampling-filter-path`(全用 `load_function` 按字符串路径动态导入)，契约只有“返回 list[Sample]”，训练内核(Megatron + 损失 + 权重同步)与 rollout 怎么生成完全解耦、不 fork 内核。这正契合用户的 AnyGym 愿景：slime 作 AutoGym Core IR(reset/step/grade)背后的 trainer，比“每 gym fork 内核”的框架更合适；与 NeMo Gym(env 组合/服务层 L1)互补而非替代。

## 5. tau2 dynamic+noisy 改法(slime examples/tau-bench)

现状 `run_qwen3_4B.sh` 跑同步 `train.py --colocate`、retail、`n_samples=8`、`--rollout-temperature 1`，已挂 `check_reward_nonzero_std` + `--rollout-shuffle`。改法：
- 噪声：给用户模拟器(gemini)设较高采样温度、可轮换 user_strategy/user_model；`--n-samples-per-prompt 8→16`；保留 `check_reward_nonzero_std`。
- 动态：合并 retail+airline 任务、扩大 split；`--rollout-max-response-len 1024→4096`。
- 异步：去掉 `--colocate`，actor/rollout 解耦放置，`--rollout-function-path slime.rollout.fully_async_rollout.generate_rollout_fully_async`(每样本仍 `--custom-generate-function-path generate_with_tau.generate`)，上调 `--sglang-server-concurrency`(注意 gemini 限流)。
- 注意：`generate_with_tau.generate` 断言 `not args.partial_rollout`(fully_async 兼容)；**fully_async 不支持 eval**(评测走单独同步跑)。
- 验证指标：非零方差组占比(有没有梯度)、吞吐、pod 利用率；与现有 verl 0.8 static 版 A/B。

## 6. 文档脚手架与写作规范

所有 HTML 用同一脚手架：单文件、React 18 UMD + MathJax(tex-svg) + 内联暗色 CSS；辅助组件 `Src`(vscode://file 可点击源码行，path 相对仓库根)、`Ext`(外部文档)、`Formula`(LaTeX)、`Code`(自写语法高亮)、`CourseNav`、Tab 探索器(useState)。脚手架金标准模板：`docs/AReaL/areal-lesson-07-train-engine.html`；slime 课金标准 `docs/slime/slime-lesson-01-foundations.html`。各仓 vscode 基址：slime→`/Users/xinyu/Code/Agentic-RL/slime/`，verl→`.../verl/`，Polar→`.../ProRL-Agent-Server/`，AReaL→`.../AReaL/`，miles→`.../miles/`。

每个 HTML 产出后的质量闸：① 抽出 App `<script>` 跑 `node --check`；② 禁用词扫描(用户全局 CLAUDE.md 的词表)；③ em-dash 扫描(U+2014 单字符与连用形式，含代码注释)；④ 抽查 `Src` 的 path:line 真实存在且命中 def/class。用户偏好：自底向上、初学者可读、信息密集、设计动机、完整数学、可点击 vscode 源码行(详见用户 auto-memory areal-explainer-prefs)。

## 7. 状态与可继续方向

**状态：全部核心交付完成。** 可继续：
- 在 miles 上实现 DTA(横评 DTA tab 的 5 步)。
- 产出可直接跑的 `run_qwen3_4B_dynamic.sh`(tau2 dynamic+noisy)。
- miles mooncake P2P 权重传输、AReaL interrupt-and-resume partial rollout 的代码级深读页(同 slime-async-internals 体例)。
- 启动最小 “slime + Polar + 一个 tau2 类任务” 全流程验证，与 verl 0.8 做 A/B。

## 8. 跨机 resume 说明

- 本文件(项目根 CLAUDE.md)随仓库同步，是跨机器的**可携完整记录**，自动加载。
- 另有本机 auto-memory：`~/.claude/projects/-Users-xinyu-Code-Agentic-RL/memory/rl-infra-framework-study.md`(及索引 MEMORY.md)，仅在原机器自动加载，换机不带过来,以本文件为准。
- 恢复时：先读本文件，再打开 `docs/rl-framework-comparison.html` 总览，需要细节进对应 `docs/<framework>/` 课程或 `docs/slime-async-internals.html`。
