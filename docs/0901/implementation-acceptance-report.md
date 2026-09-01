# OpenMontage 客户端 Agent 执行架构 — 实现验收报告

日期：2026-09-01
分支：`codex/docs-0901-plan`（未推送，按约定提交于本地）
依据：`docs/0901/openmontage-client-agent-execution-plan.md`

## 1. 结论

五个阶段全部实现、测试、经子代理独立审查并修复后验收通过。核心目标达成：**在不改变现有 Pipeline 顺序、媒体工具调用方式和 CI 文件生成机制的前提下，把原 runtime/DSH Agent 的认知工作迁移到客户端 Agent**，通过 `read_openmontage_file` 实时读取 CI 指令、通过 `begin/update/submit_client_stage` 推进每个 stage、所有媒体与中间文件留在 CI。

最终测试：`pytest tests/openmontage/` → **408 passed，1 skipped，6 failed**。6 个失败均为**改动前就存在**的既有失败（在干净工作树 `git stash` 后复现），与本次改动无关。

## 2. 各阶段交付与验收

### 阶段一：只读指令接口（提交 `3c8c1f9`）

**交付**
- 新增 `openmontage/instruction_files.py`：`read_instruction_file(path, max_bytes)` 每次从 CI 仓库实时读取 `.md/.yaml/.yml/.json`，返回 `path / relative_path / content / size / modified_at / content_hash / repository_revision`。
- 新增 `verify_instruction_provenance(...)`：校验客户端提交的指令读取清单与 CI 当前文件 hash 一致。
- `mcp_server.py` 注册 `read_openmontage_file` 工具。

**安全边界**（§5.3 十项全部满足）：拒绝空路径 / NUL / `..` 遍历 / 绝对路径越界 / 符号链接逃逸；白名单限定 8 个根目录；扩展名白名单 4 种；默认 2MB 上限；UTF-8 校验；只读无写路径。

**审查发现与修复**
- [S] 错误码 `code` 在 MCP 序列化为纯文本时丢失 → 把 code 编入 `InstructionFileError` 的 message。
- [S] `_repository_revision` 用 `lru_cache` 缓存 HEAD，与"实时读取"矛盾 → 去掉缓存。
- [S] `size` 与 `content_hash` 基准不一致（CRLF 归一化）→ 改为 `read_bytes` 一次读取，hash 对原始字节计算，同时消除 stat-then-read 的 TOCTOU。

**测试**：35 用例（四种格式、代码/媒体/数据库拒绝、路径遍历、绝对/符号链接越界、超限、UTF-8、hash、只读、provenance）。

### 阶段二：客户端 Stage 接口（提交 `5b3572f`）

**交付**
- `job_service.py` 新增 `begin_client_stage` / `update_client_stage_progress` / `submit_client_stage`，以及 `ClientStageLease`（opaque lease）、`ClientStageError`（错误码编入 message）、`openmontage_client_stage_lease` 表。
- 新增 `openmontage/media_references.py`：媒体引用校验（相对路径、无遍历/绝对/NUL、符号链接不越界、文件存在、sha256 校验）。
- `contracts.py` 新增 6 个 `client_stage.*` 事件类型。
- `mcp_server.py` 注册三个工具并做 workspace 归属校验。

**关键设计**
- 幂等：begin 用独立 lease 表（`job_id+stage+idempotency_key`）；update/submit 复用 `openmontage_job_command` 表（`job_id+idempotency_key`），冲突抛 `IDEMPOTENCY_CONFLICT`。
- sequence fencing：`expected_sequence` 不匹配拒绝并返回最新 snapshot。
- checkpoint 复用 `lib.checkpoint`，客户端不能指定路径；gated stage 必须先 `awaiting_human` → `approve_video_stage` → `completed`。
- **磁盘 checkpoint 写入放在 SQLite 写事务之外**：避免持 `BEGIN IMMEDIATE` 做慢 I/O，且崩溃时以 SQLite（权威）为准、re-begin 可恢复。

**审查发现与修复**
- [C] checkpoint 写在事务内，崩溃会"磁盘已写但 Job 未推进" → 移到事务外。
- [S] `client_stage.failed` 事件 payload 报 `status: running` → 先置 FAILED 再构建 payload。
- [S] NUL 字节未拒绝 → 补 NUL 检查。
- [S] 幂等 key 命名空间跨 stage 冲突 → 在 docstring 注明 key 需跨 Job 全操作唯一。

**测试**：34 用例（当前 stage/跳阶段/lease 错误与过期/sequence 冲突/幂等重放/非法 artifact/审批/失败/进度/并发占用/断线恢复/取消）。

### 阶段三：客户端适配（提交 `ef81385`）

**交付**
- 新增 `openmontage/client_stage_driver.py`：`ClientStageDriver` 实现 §4 的 10 步执行模型——`resolve_current_stage` / `read_stage_instructions`（§9 文件集 + provenance）/ `read_predecessor_artifact`（走 read_project_file 通道）/ `drive_stage` / `run`。
- gateway 作为不透明对象传给 handler，保持客户端/CI 边界（驱动自身不调用媒体工具）。

**审查发现与修复**
- [C] begin 幂等重放返回已释放的旧 lease，handler 会重复执行付费调用 → 驱动在 begin 后检测 stage 已推进则跳过 handler。
- [S] `resolve_current_stage` docstring 对 WAITING_APPROVAL 描述错误 → 修正。
- [S] per-stage extras 的 best-effort 依赖 `.code` 属性（MCP 序列化后失效）→ 同时匹配错误消息字符串。
- [S] 测试 `test_run_completes_when_no_gates` 名不副实 → 改用 `deterministic-video-smoke`（单 ungated compose stage）真实走完完成路径。
- [N] `run()` 拒绝 `in_progress` handler 返回（进度应走 progress 接口）。

**测试**：16 用例（resolve 各状态、指令读取/provenance、前置 artifact、drive 提交与审批门、幂等重放、run 完成/审批停止/缺 handler/in_progress 拒绝）。

### 阶段四：媒体观察和 compose 闭合（提交 `26a7a34`）

**交付**
- 新增 `tests/openmontage/test_client_stage_e2e.py`：用 fake Gateway（写假字节文件，无真实生成）驱动完整 `animation` pipeline：research → proposal(审批) → script → scene_plan → assets → edit → compose → publish → Job SUCCEEDED。

**验证点**
- 8 阶段全部 SUCCEEDED；审批门 awaiting_human → approve → completed。
- 媒体引用闭合：assets 引用 `assets/images/*.png`、`assets/audio/*.wav`，compose 引用 `renders/*.mp4`，均被 `validate_media_references` 接受并记录在 checkpoint `metadata.media_references`。
- 媒体只生成一次（审批不重复付费，用 `Counter` 精确断言）。
- 事件不泄漏 lease token / apiKey；每阶段 checkpoint 都记录 instruction provenance。

**审查发现与修复**
- [S] `_gated` 两趟都执行 `build`，媒体副作用跑两次 → 缓存 build 结果。
- [S] 事件洁净度断言偏弱 → 断言事件非空 + 遍历检查敏感字段。
- [S] `_run_with_approval` 无迭代上限 → 加上限 fail-fast。
- [N] provenance 只查 proposal → 循环所有 stage checkpoint。

### 阶段五：CI-only 上线（提交 `c0d016a`）

**交付**
- `job_service.py` 新增 `client_stage_only_enabled()`（allow-list 解析，未知值 fail-closed）。
- `claim_job` 在 client-stage-only 下返回 None（旧 Worker 拿不到 lease、无法推进）。
- `start_stage`/`complete_stage` 等 legacy settle 方法在 client-stage-only 下直接拒绝（纵深防御）。
- `cli.py` `_build_job_worker` 启动即 fail-fast。
- `capabilities.py` 报告 `client_stage_only`。
- `compose.yaml` 默认 `OPENMONTAGE_CLIENT_STAGE_ONLY=true`（MCP-only 部署默认启用）。

**审查发现与修复**
- [S] 非 lease settle 方法仍是 fail-open → 在 `start_stage`/`complete_stage` 加门禁。
- [N] env 解析补 fail-closed 说明、补 trim/未知值/disabled 分支测试。

**测试**：20 用例（env 解析、claim_job 拒绝、客户端路径仍推进、legacy settle 拒绝、worker cli fail-fast、capability 报告）。

## 3. 契约汇总

### 错误码（客户端按码分支）

| 域 | 码 |
|---|---|
| 指令文件 | `INSTRUCTION_FILE_NOT_FOUND` `INSTRUCTION_FILE_UNAVAILABLE` `UNSUPPORTED_FILE_TYPE` `PATH_OUTSIDE_REPOSITORY` `FILE_TOO_LARGE` `INVALID_PATH` |
| 客户端 stage | `STAGE_ALREADY_OWNED` `STAGE_LEASE_INVALID` `STAGE_LEASE_EXPIRED` `STAGE_ATTEMPT_MISMATCH` `STAGE_STATE_INVALID` `HUMAN_APPROVAL_REQUIRED` `IDEMPOTENCY_CONFLICT` `JOB_CANCELLED` |
| 提交 | `ARTIFACT_SCHEMA_INVALID` `MEDIA_REFERENCE_INVALID` `CHECKPOINT_WRITE_FAILED` |

### 事件类型（§14）

`openmontage.client_stage.started / .progressed / .checkpointed / .awaiting_approval / .completed / .failed`

## 4. 关键设计决策

1. **checkpoint 磁盘写与 SQLite 状态转换非原子**：checkpoint 先写磁盘（事务外），状态转换在 SQLite 事务内。崩溃窗口 = "磁盘已写但 Job 未推进"，由 re-begin 自愈（begin 的前置门读的是 snapshot 而非磁盘 checkpoint，故 stale checkpoint 不会让客户端跳阶段）。SQLite 是权威。
2. **幂等 key 分表**：begin 走 `openmontage_client_stage_lease`（含 stage 维度），update/submit 走 `openmontage_job_command`（job 维度）。客户端需保证 key 跨 Job 全操作唯一。
3. **provenance 为审计项非硬门禁**：`instruction_provenance` 可选；提供时在写 checkpoint 前校验与 CI 文件 hash 一致，但不作为强制门（并非每阶段都必须读指令）。
4. **gateway 边界**：驱动把 gateway 当不透明对象传给 handler，客户端负责认知 + 编排，CI 负责媒体执行——忠实于 §19 最终架构。

## 5. 测试汇总

| 文件 | 用例数 |
|---|---|
| `test_instruction_files.py` | 35 |
| `test_client_stage_api.py` | 26 |
| `test_media_references.py` | 9 |
| `test_client_stage_driver.py` | 17 |
| `test_client_stage_e2e.py` | 1 |
| `test_client_stage_only.py` | 21 |
| **合计** | **109** |

全量 `pytest tests/openmontage/`：411 passed，1 skipped，6 failed（既有）。

## 6. 复审与一致性修复（第二轮）

首轮验收提交后，复审指出 3 个 P1 + 1 个 P2，均已修复并复测：

- **P1-1 审批恢复重复执行 handler**（重复付费生成）：`ClientStageDriver.drive_stage` 在审批恢复（`approval_required` 且已 `APPROVED`）时复用 `awaiting_human` checkpoint 的 artifacts 直接提交 `completed`，不再重跑 handler；新增 `_read_stage_checkpoint`。
- **P1-2 CI-only 门禁可被旧接口绕过**：新增 `_reject_legacy_mutation_in_client_stage_only` 统一门禁，覆盖**全部** legacy mutation 入口（heartbeat/release/start/complete/progress/approval/fail/confirm/complete_job 的 `_or_confirm_cancel` 与非 lease 变体 + `publish_artifact`）。
- **P1/P2 取消与 checkpoint 写入竞态**：`submit_client_stage` 取消分支调用 `_archive_stage_checkpoint`，把事务外刚写入的 checkpoint 移到 `history/`，避免 CANCELLED Job 残留 `in_progress`/`completed` checkpoint。
- **P2 幂等 key 命名空间不一致**：修正 `submit_client_stage` docstring，明确 begin 走独立 lease 表、update/submit/approve/cancel 共享 command 表。

提交：`69ad6a5`（3 个 P1）、`3e2d05e`（publish_artifact 门禁 + metadata 保留）。

## 7. 已知遗留

**6 个既有失败**（与本改动无关，在改动前的干净工作树上同样失败）：

- `test_docker_job_bridge.py::test_compose_ships_no_in_container_job_worker` — 断言 `"worker" not in compose_text` 匹配到 compose 注释中的 "worker" 字样。
- `test_event_outbox.py::test_publisher_from_environment_requires_a_complete_bridge_configuration`
- `test_integration_contracts.py::test_docker_contract_exposes_mcp_and_persists_projects` — 断言 `http://dofe-models-api:3101`，实际 compose 用 `host.docker.internal`。
- `test_integration_contracts.py::test_mcp_server_publishes_reference_clone_surface`
- `test_job_worker.py::test_worker_pauses_at_approval_and_resumes_with_latest_job_snapshot`
- `test_reference_clone.py::test_prepare_creates_agent_ready_airouter_project`

**一个架构后续项（非本次范围）**：客户端 Stage API 目前没有把最终视频发布为 durable `PublishedArtifact` 的入口——E2E 的 publish 阶段产出 `publish_log` checkpoint，`list_video_artifacts` 对客户端驱动 Job 返回空。`publish_artifact` 现已被 client-stage-only 门禁（作为 legacy mutation），因此最终视频 artifact 发布需要一条独立的客户端发布路径（可在后续阶段补充）。

## 8. 部署注意事项

- MCP-only 部署默认启用 `OPENMONTAGE_CLIENT_STAGE_ONLY=true`（compose 默认）。若要在容器内跑 worker，需显式 `OPENMONTAGE_CLIENT_STAGE_ONLY=false`。
- 上线前确认（§13）：DSH Worker 已停止、无 worker 容器、`OPENMONTAGE_AGENT_EXECUTOR_JSON` 未设置、无 dsh-agent-run 依赖、无 DeepSeek Harness executor。
