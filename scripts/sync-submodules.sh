#!/usr/bin/env bash
# 事件驱动子模块同步 + 源码行漂移检测。
#
# 流程：bump（AReaL/slime/miles/verl 拉到各自上游 main 最新，detached，绝不产生本地提交）
#       → 检测哪些指针真的变了 → 对变化的子模块跑 check_src_drift 基线比对（只报“回归”：
#       confirmed→非confirmed 或新增硬伤）→ 提交 bump（提交信息含漂移摘要）→ PUSH=1 时推送。
# 设计取舍（float + 事件驱动修复）：即使检测到漂移回归，bump 仍会提交并（PUSH=1）推送到 main——
#   因为云端 workspace 是临时的，不推送则 bump 丢失。回归只是“文档源码行号待人工修”，不是代码错误。
#   main 可能短暂含已知漂移，ACTION_REQUIRED 之后由有人监督的语义修复补上。语义修复不在本脚本内。
# ProRL-Agent-Server 是用户自己的 fork，不在同步范围内。
#
# 用法：
#   bash scripts/sync-submodules.sh          # 本地：bump+检测+本地提交，不 push
#   PUSH=1 bash scripts/sync-submodules.sh   # 云端 routine：额外推送到 origin/main
set -euo pipefail
export GIT_TERMINAL_PROMPT=0          # 缺凭据时快速失败，不交互式挂起
cd "$(git rev-parse --show-toplevel)"

SUBMODULES=(AReaL slime miles verl)
PUSH="${PUSH:-0}"
BASELINE="scripts/.src-drift-baseline.json"
BOT_NAME="agentic-rl-sync"
BOT_EMAIL="agentic-rl-sync@users.noreply.github.com"

# bump 前记录各子模块指针短 SHA
declare -A BEFORE
for s in "${SUBMODULES[@]}"; do
  BEFORE[$s]=$(git rev-parse --short "HEAD:$s" 2>/dev/null || echo "none")
done

echo "==> 拉取上游最新 commit（detached，不产生本地提交）: ${SUBMODULES[*]}"
# 仅 --remote（不 --merge/--rebase）：checkout 到上游跟踪分支最新 commit，detached HEAD，
# 绝不在子模块里生成本地提交，从而避免 super-repo 指向仅存在于本地的不可达 gitlink。
git submodule update --init --remote "${SUBMODULES[@]}"

# 检测指针真正变化（--ignore-submodules=dirty：忽略脏工作区，只看 gitlink commit 是否变）
CHANGED=()
for s in "${SUBMODULES[@]}"; do
  git diff --quiet --ignore-submodules=dirty -- "$s" || CHANGED+=("$s")
done

if [ ${#CHANGED[@]} -eq 0 ]; then
  echo "NO_CHANGE: 子模块指针无变化，结束。"
  exit 0
fi

echo "CHANGED_SUBMODULES: ${CHANGED[*]}"
for s in "${CHANGED[@]}"; do
  AFTER=$(git rev-parse --short "HEAD:$s" 2>/dev/null || echo "none")
  echo "MOVED: $s ${BEFORE[$s]}->${AFTER}"
done

# ---- 源码行漂移基线比对（仅变化的子模块）----
DRIFT_RC=0
REG="baseline_regressions=unknown"
if [ -f "$BASELINE" ]; then
  set +e
  DRIFT_OUT=$(python3 scripts/check_src_drift.py --submodules "${CHANGED[@]}" --baseline "$BASELINE" --report)
  DRIFT_RC=$?
  set -e
  echo "----- src-drift 基线比对 -----"
  echo "$DRIFT_OUT"
  echo "------------------------------"
  REG=$(printf '%s\n' "$DRIFT_OUT" | grep -oE 'baseline_regressions=[0-9]+' | head -1 || true)
  REG=${REG:-baseline_regressions=unparsed}
  if [ "$DRIFT_RC" -ne 0 ] && [ "$DRIFT_RC" -ne 2 ]; then
    echo "DRIFT_TOOL_FAILED rc=$DRIFT_RC（漂移检测异常，bump 仍照常提交，漂移状态未知）"
  fi
else
  echo "NEEDS_BASELINE: 缺基线 $BASELINE，本次未做漂移检测（bump 仍提交）。先在干净状态跑 --write-baseline。"
fi

# ---- 提交 bump ----
git add "${CHANGED[@]}"
if git diff --cached --quiet; then
  echo "NO_STAGED_CHANGE: 指针未实际变化（脏工作区误报），跳过提交。"
  exit 0
fi
git -c user.name="$BOT_NAME" -c user.email="$BOT_EMAIL" \
    commit -q -m "chore: bump submodules (${CHANGED[*]}) to upstream main" \
              -m "src-drift 基线比对: ${REG}"
echo "COMMITTED: $(git rev-parse --short HEAD)"

# ---- 可选推送（失败时重试一次，且不吞掉后续 ACTION_REQUIRED）----
if [ "$PUSH" = "1" ]; then
  if git push origin HEAD:main; then
    echo "PUSHED to origin/main"
  else
    echo "PUSH_REJECTED: 尝试 rebase 到最新 origin/main 后重推"
    if git pull --rebase origin main && git push origin HEAD:main; then
      echo "PUSHED to origin/main (after rebase)"
    else
      echo "PUSH_FAILED: 自动推送失败（可能 non-fast-forward 或缺凭据），需人工处理。"
    fi
  fi
fi

# ---- 漂移回归上报（放在最后，确保即便 push 失败也已先打印检测结果）----
if [ "$DRIFT_RC" -eq 2 ]; then
  echo "ACTION_REQUIRED: 检测到源码行漂移回归（详见上方 src-drift 比对）。含已知漂移的 bump 已提交"\
"（PUSH=1 时已推送到 main，docs 受影响链接此刻指向旧行号）。需对受影响文件重跑有人监督的漂移修复 workflow，"\
"完成后用 'python3 scripts/check_src_drift.py --write-baseline scripts/.src-drift-baseline.json' 重建基线并提交。"
fi
exit 0
