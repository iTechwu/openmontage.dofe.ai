# OpenMontage 客户端 Agent 执行架构实施方案

## 1. 目标

在不改变 OpenMontage 现有功能、Pipeline 顺序、媒体工具调用方式和 CI 文件生成机制的前提下，将原由 runtime/DSH Agent 完成的认知工作迁移到客户端 Agent：

```text
research -> proposal -> script -> scene_plan -> assets -> edit -> compose -> publish
```

新的职责划分：

- 客户端 Agent：读取服务器端 Markdown、YAML、JSON 指令和规范；进行研究、分析、脚本编写、场景规划、提示词编写和编排；调用现有 Gateway 工具；将 JSON artifact、阶段状态和进度同步到 CI。
- CI：保存 Job/Pipeline 状态、checkpoint 和 artifact；执行现有图片、音频、视频、TTS、合成和渲染工具；保存全部媒体和中间文件；执行媒体校验和最终导出。

明确禁止：

- 恢复 DSH Worker、`dsh-agent-run` 或 `OPENMONTAGE_AGENT_EXECUTOR_JSON`；
- 客户端本地生成或渲染媒体；
- 客户端上传图片、音频、视频；
- 客户端直接操作 CI 主机路径或写 checkpoint；
- 修改现有 Gateway 媒体工具契约。

## 2. 根因

旧链路是：

```text
submit_video_job
  -> CI Job 入队
  -> Worker claim_job/start_stage
  -> 生成 StageAssignment
  -> 启动外部 Agent executor
  -> Agent 读取 Markdown/YAML/Skill
  -> Agent 调用工具并写 checkpoint
  -> Worker 校验 checkpoint
  -> complete_stage
```

历史失败任务由 Worker 启动不存在的：

```text
/opt/dsh-plugins/dsh-agent-run/bin/agent-run.js
```

最终触发 `MODULE_NOT_FOUND`。当前 CI 是 MCP-only，DSH Worker 已永久停用，因此必须改为客户端 Agent 显式推进 stage。

## 3. 保持不变

### 3.1 Pipeline 和 Job 状态

Pipeline 顺序保持：

```text
research -> proposal -> script -> scene_plan -> assets -> edit -> compose -> publish
```

保留现有 Job 状态：

```text
QUEUED, RUNNING, WAITING_APPROVAL, CANCEL_REQUESTED,
COMPLETED, FAILED, CANCELLED
```

保留现有 stage 状态：

```text
PENDING, RUNNING, WAITING_APPROVAL, COMPLETED, FAILED
```

### 3.2 审批

继续使用 `approve_video_stage`。审批逐阶段生效，不能用一次审批覆盖所有后续阶段。

### 3.3 Gateway 工具

以下工具的名称、参数、调用顺序和返回结构保持不变：

```text
openmontage_capabilities
video_selector
tts_selector
image_selector
math_animate
diagram_gen
code_snippet
music_gen
video_compose
audio_mixer
video_stitch
composition_validator
audio_probe
```

继续遵守：

- 先 selector，再 provider；
- 先 rank/preflight，再执行；
- 只使用模型目录返回的精确模型 ID；
- 不直接调用供应商 API；
- 不私自更换 provider、model 或 render runtime；
- 读取工具返回的 `required_agent_skills`；
- 保留 cost、approval、review 和 checkpoint 规则。

变化只有：工具由客户端 Agent 发起，实际任务仍在 CI 执行。

## 4. 客户端执行模型

客户端每次只负责一个 stage：

```text
1. get_video_job
2. 确定当前可执行 stage
3. begin_client_stage
4. read_openmontage_file 读取服务器指令
5. read_project_file 读取前置 artifact
6. 完成本阶段认知工作
7. 按现有方式调用 Gateway 工具
8. update_client_stage_progress
9. submit_client_stage 提交 artifact 和结果
10. 审批阶段等待 approve_video_stage
11. 审批通过后重新获取 Job 并开始下一阶段
```

客户端不调用 CI shell、不访问 `/data/projects`、不直接写 checkpoint、不上传媒体，也不通过旧 Worker/DSH 执行阶段。

## 5. 只读文件接口

### 5.1 接口

新增只读 MCP 工具：

```text
read_openmontage_file(path, max_bytes=2000000)
```

它每次从 CI 仓库实时读取，不做客户端缓存，不维护逻辑 Skill ID 映射。可以返回实际服务器路径，便于排查。

请求示例：

```text
read_openmontage_file("AGENT_GUIDE.md")
read_openmontage_file("pipeline_defs/animation.yaml")
read_openmontage_file("skills/pipelines/animation/research-director.md")
read_openmontage_file("skills/creative/seedance-production.md")
read_openmontage_file(".agents/skills/seedance-prompting/SKILL.md")
read_openmontage_file("schemas/artifacts/scene_plan.schema.json")
```

返回示例：

```json
{
  "path": "/app/openmontage/skills/creative/seedance-production.md",
  "relative_path": "skills/creative/seedance-production.md",
  "content": "...",
  "size": 12345,
  "modified_at": "2026-09-01T12:00:00Z",
  "content_hash": "sha256:...",
  "repository_revision": "..."
}
```

### 5.2 允许格式

允许：

```text
.md
.yaml
.yml
.json
```

`.json` 必须支持以下内容：

```text
schemas/artifacts/*.schema.json
schemas/checkpoints/checkpoint.schema.json
schemas/pipelines/pipeline_manifest.schema.json
schemas/styles/playbook.schema.json
remotion-composer/public/demo-props/*.json
docs/*.json
```

拒绝 Python/JavaScript/脚本、凭据、数据库和所有媒体格式。

### 5.3 路径和读取校验

不维护映射表，但服务端必须做边界校验：

1. 拒绝空路径和 NUL 字节；
2. 拒绝 `../` 等路径遍历；
3. 解析路径必须位于 OpenMontage 仓库根目录；
4. 拒绝通过符号链接跳出仓库；
5. 可接受仓库内实际绝对路径，但不能接受仓库外路径；
6. 单次读取默认不超过 2 MB；
7. 文件不存在返回明确 404 类型错误；
8. 不允许的扩展名返回 `UNSUPPORTED_FILE_TYPE`；
9. 越界返回 `PATH_OUTSIDE_REPOSITORY`；
10. 接口只能读取，不能创建、修改、删除或复制文件。

建议允许的仓库目录：

```text
AGENT_GUIDE.md
pipeline_defs/
skills/
.agents/skills/
schemas/
styles/
remotion-composer/public/
docs/
```

### 5.4 与项目文件接口分工

```text
read_openmontage_file
  -> 仓库静态指令、Skill、schema、示例和文档

read_project_file
  -> Job 项目的 artifact、checkpoint、分析结果
```

`video_analysis_brief.json`、`reference_clone_request.json`、`artifacts/*.json` 和 `checkpoint_*.json` 继续使用 `list_project_files/read_project_file`。

## 6. 客户端 Stage 接口

现有 `submit_video_job`、`get_video_job`、`approve_video_stage` 保持兼容。

### 6.1 begin_client_stage

```text
begin_client_stage(job_id, stage, expected_sequence, idempotency_key)
```

服务端验证 workspace 权限、workflow/stage 合法性、前置 stage、并发占用和当前 Job 状态，然后将 stage 设置为 `RUNNING` 并返回不透明的 `lease_token`、`stage_attempt`、`last_sequence` 和过期时间。

### 6.2 update_client_stage_progress

```text
update_client_stage_progress(
  job_id, stage, stage_attempt,
  completed_units, total_units, label_code,
  lease_token, idempotency_key
)
```

验证 lease、stage、attempt，更新进度、续租并写入有序 Job event。重复请求必须幂等。

### 6.3 submit_client_stage

```text
submit_client_stage(
  job_id, stage, stage_attempt, status,
  artifacts, metadata, instruction_provenance,
  lease_token, idempotency_key
)
```

允许的状态：

```text
completed
awaiting_human
failed
in_progress
```

服务端在一次业务操作中完成：

1. lease、Job/stage/attempt 校验；
2. artifact 和 checkpoint schema 校验；
3. 前置 artifact、审批规则和媒体引用校验；
4. 写入标准 checkpoint；
5. 写入 Job event；
6. 推进 Job 状态；
7. 释放或续期 lease；
8. 返回最新 Job snapshot。

不同时暴露独立的 `submit_client_checkpoint` 和 `complete_client_stage`，避免两次调用之间状态不一致。

### 6.4 审批状态

客户端提交 `awaiting_human` 后：

```text
checkpoint = awaiting_human
stage = WAITING_APPROVAL
job = WAITING_APPROVAL
lease = released
```

用户继续调用现有 `approve_video_stage`。审批通过后，客户端重新 `begin_client_stage` 执行下一阶段。

## 7. Checkpoint 服务端实现

标准路径仍为：

```text
projects/<job_id>/checkpoint_<stage>.json
```

服务端自行确定：

```text
project_id = job.job_id
pipeline = job.workflow.name
pipeline_version = job.workflow.version
stage_attempt = 数据库中的当前 attempt
```

客户端不得提交 `project_dir`、`projects_dir`、`checkpoint_path` 或任意绝对路径。

写入前必须校验：

- checkpoint、artifact JSON Schema；
- pipeline、stage、attempt 身份；
- 前置 artifact；
- human approval 规则；
- 媒体路径为当前项目相对路径；
- 不含媒体二进制；
- 不跨 stage、跨 Job 修改。

superseded checkpoint 继续归档到：

```text
projects/<job_id>/history/
```

## 8. 幂等、并发和恢复

### 8.1 幂等

所有有副作用接口都使用 `idempotency_key`：

```text
begin_client_stage
update_client_stage_progress
submit_client_stage
```

相同参数重放返回原结果；参数不同返回 `IDEMPOTENCY_CONFLICT`，不能重复写 artifact、推进 Job 或产生业务事件。

### 8.2 Sequence fencing

请求携带 `expected_sequence`：

```text
匹配 -> 执行
不匹配 -> 拒绝并返回最新 snapshot
```

客户端收到冲突后重新调用 `get_video_job` 和 `list_video_job_events`。

### 8.3 断线和重启

- CI 保留 `in_progress` checkpoint；
- lease 到期后允许重新获取；
- 新客户端从 `metadata.partial_progress` 恢复；
- 不重复生成已成功媒体；
- CI 重启后通过 Job、event 和 checkpoint 恢复；
- 不启动 Worker 或旧 executor。

### 8.4 并发

同一 Job 同一 stage 只能有一个 active lease。其他客户端收到 `STAGE_ALREADY_OWNED`，直到 lease 释放或过期。

## 9. Skill 和 JSON 读取流程

每个 stage 开始前实时读取：

```text
AGENT_GUIDE.md
pipeline_defs/<pipeline>.yaml
skills/pipelines/<pipeline>/<stage>-director.md
skills/meta/checkpoint-protocol.md
skills/meta/reviewer.md
```

按阶段按需读取：

```text
proposal:
  skills/meta/animation-runtime-selector.md
  skills/meta/taste-direction.md

script:
  skills/meta/voice-performance-director.md

scene_plan:
  skills/creative/seedance-production.md
  .agents/skills/seedance-directing/SKILL.md

assets:
  skills/creative/seedance-production.md
  对应工具 Layer 2 Skill
  selector 返回的 Layer 3 Skill

compose:
  skills/core/remotion.md
  skills/core/hyperframes.md
  .agents/skills/remotion-best-practices/SKILL.md
```

客户端不保存 Skill、schema 或 manifest 的本地副本，只允许在当前 Agent turn 内存中暂存。

## 10. 阶段职责

| 阶段 | 客户端 Agent 职责 | CI 职责 |
|---|---|---|
| research | Web Search/Fetch、内容和技术研究、趋势、数据、受众分析；生成 `research_brief` | 保存 checkpoint，不生成媒体 |
| proposal | capabilities/preflight、方案、provider/runtime/音频/预算比较；生成 `proposal_packet`、`decision_log` | 保存 artifact，进入审批 |
| script | beats、旁白、字幕、voice performance、delivery cues；可调用 transcriber | 保存 script |
| scene_plan | 场景、镜头、时长、工具路径、identity registry、Seedance contract | 保存 scene_plan，进入审批 |
| assets | 资产计划、提示词、参数、顺序和预算；调用 selector | CI 生成并保存 TTS、图片、视频、图表、Manim、音乐 |
| edit | 生成时间线、转场、hold、stagger、audio ducking | 校验引用并保存 edit artifact |
| compose | composition plan；调用 `video_compose`、`audio_mixer`、`video_stitch` | 校验、合成、渲染、抽帧并保存 MP4 |
| publish | 标题、描述、平台 metadata、缩略图概念和 publish_log | 保存发布 metadata 和导出包，进入审批 |

## 11. Seedance 2.0

读取链：

```text
scene-director
  -> seedance-production
  -> video_selector(operation=rank)
  -> required_agent_skills
  -> seedance-provider
  -> seedance-directing
  -> seedance-continuity
  -> seedance-prompting
  -> seedance-quality
```

客户端负责生成和记录：

- `generation_contract`、`identity_registry`、`temporal_beats`；
- `prompt_review.compile_spec`；
- provider preflight 真实参数；
- reference binding、duration；
- `take_review`、`observed_state`、`lineage_review`。

CI 负责执行任务、保存视频、返回状态和 metadata，并执行 ffprobe、抽帧和媒体检查。CI 不重新编写客户端 prompt，也不做创意替换。

## 12. 媒体文件约束

媒体始终位于 CI：

```text
projects/<job_id>/assets/
projects/<job_id>/renders/
projects/<job_id>/snapshots/
remotion-composer/public/<project_name>/
```

客户端 artifact 只能引用：

```text
project-relative path
asset_id
artifact_id
media metadata
```

CI 必须验证文件存在、类型正确、SHA-256 正确、位于当前 Job 目录且没有跨 Job 或符号链接越界引用。

## 13. CI 部署

CI 保持 MCP-only：

```text
openmontage-mcp
openmontage-events
openmontage-backlot
```

上线前必须确认：

```text
DSH Worker 已停止
没有 worker 容器
OPENMONTAGE_AGENT_EXECUTOR_JSON 未设置
没有 dsh-agent-run 依赖
没有 DeepSeek Harness executor
```

建议配置：

```text
OPENMONTAGE_CLIENT_STAGE_ONLY=true
```

该配置用于拒绝旧 Worker 推进 stage，确保 Job 只能由客户端 Stage API 推进。Job 创建后保持 `QUEUED`，客户端 `begin_client_stage` 后才进入 `RUNNING`。

## 14. 事件

继续使用有序 Job event，新增或复用：

```text
client_stage.started
client_stage.progressed
client_stage.checkpointed
client_stage.awaiting_approval
client_stage.completed
client_stage.failed
```

事件至少记录 Job、stage、attempt、status、sequence、时间和 instruction provenance（读取文件路径及 hash）。不得包含 API key、token、媒体二进制或无关环境变量。

## 15. 源码改动范围

### 15.1 `openmontage/mcp_server.py`

新增：

```text
read_openmontage_file
begin_client_stage
update_client_stage_progress
submit_client_stage
```

保留现有 Job、审批、项目文件和 artifact 查询接口。

### 15.2 `openmontage/job_service.py`

新增客户端执行方法，复用现有 stage、审批、Job transition 和 checkpoint 逻辑，补充 lease、attempt、sequence、幂等和 client-only 校验。

### 15.3 新增只读文件模块

建议新增 `openmontage/instruction_files.py`，负责扩展名、路径边界、符号链接、大小、UTF-8、hash、metadata 和错误码。

### 15.4 其他模块

- `lib/checkpoint.py`：尽量保持格式，只让服务端安全调用。
- `openmontage/job_worker.py`：第一阶段不删除，只在 CI 禁止启动。
- 媒体工具：不修改调用协议。

### 15.5 实际新增模块（实现时扩展）

实现过程中，除上述计划模块外，还新增了两个模块以闭合契约与客户端编排：

- `openmontage/media_references.py`：校验客户端提交 artifact 中的媒体引用（§7、§12）——必须为项目相对路径、禁止绝对路径 / `..` 遍历 / NUL 字节 / 符号链接越界；`completed`/`awaiting_human` 提交必须引用存在的文件；同字典内的 `sha256` 与 CI 文件实际哈希一致。
- `openmontage/client_stage_driver.py`：客户端侧 stage 驱动（§4、§9），串接 begin → 读指令（含 provenance）→ 读前置 artifact → handler → submit。属于客户端适配层，不在 CI 容器内运行。

## 16. 测试

### 16.1 文件读取

覆盖四种允许格式、代码和媒体拒绝、路径遍历、绝对路径越界、符号链接越界、超限、UTF-8 错误、实际路径/hash 返回和无文件修改。

### 16.2 Stage API

覆盖当前 stage、跳阶段、lease 错误/过期、sequence 冲突、幂等重放、非法 artifact、审批、失败、进度、并发占用和断线恢复。

### 16.3 Checkpoint

覆盖标准路径、客户端不能指定路径、schema/审批规则、历史归档、断点恢复、stage identity 和媒体路径越界。

### 16.4 端到端

使用 fake Gateway，不执行真实生成任务，验证：

```text
创建 Job
  -> research
  -> proposal 审批
  -> script
  -> scene_plan
  -> assets
  -> edit
  -> compose
  -> publish
  -> Job 完成
```

所有测试只能使用 mock response、测试 metadata 和测试文件，不产生真实生成费用。

## 17. 分阶段实施

### 阶段一：只读指令接口

交付 `read_openmontage_file`，完成四种格式、路径安全、实际路径、hash/metadata 和安全测试。

### 阶段二：客户端 Stage 接口

交付 `begin_client_stage`、`update_client_stage_progress`、`submit_client_stage`，完成 lease、sequence、幂等、checkpoint、审批和事件。

### 阶段三：客户端适配

客户端按 stage 实时读取 Skill、schema 和前置 artifact，提交 provenance，并按原方式调用 Gateway 工具。

### 阶段四：媒体观察和 compose

闭合 CI 的媒体 metadata、ffprobe、抽帧、音频探测、Seedance review、compose 和 publish 流程。

### 阶段五：CI-only 上线

确认 DSH Worker、旧 executor、Worker 容器和 `OPENMONTAGE_AGENT_EXECUTOR_JSON` 均禁用，随后观察 stage event、sequence、checkpoint、媒体路径和 compose 结果。

## 18. 故障处理

- Skill 读取失败：返回 `INSTRUCTION_FILE_UNAVAILABLE`，停止当前 stage，不执行工具。
- Artifact 校验失败：返回 `ARTIFACT_SCHEMA_INVALID`，客户端修正后重提。
- 工具失败：区分认证、访问、配额、契约、媒体和 prompt 质量错误，不自动换 provider/model。
- Lease 过期：重新获取 Job 和 lease，从 `in_progress` 恢复。
- CI 重启：从 Job、event 和 checkpoint 恢复，不重复提交已完成阶段。

## 19. 最终架构

```text
客户端 Agent
  -> read_openmontage_file 读取 CI Skill/Schema
  -> 进行 research/proposal/script/scene_plan/edit/publish
  -> 调用现有 Gateway 工具
  -> 提交 JSON artifact、checkpoint 和状态

公网 OpenMontage MCP / CI
  -> Job 状态机、lease、幂等、sequence fencing
  -> checkpoint 校验和写入
  -> Skill 只读服务
  -> 审批、事件和项目文件管理

CI 媒体执行层
  -> tts_selector/image_selector/video_selector
  -> math_animate/diagram_gen/code_snippet/music_gen
  -> video_compose/audio_mixer/video_stitch
  -> ffprobe/抽帧/校验
  -> 所有媒体和中间文件留在 CI
```

> OpenMontage 保留完整 Pipeline、Job、审批、checkpoint 和 CI 媒体生产能力；客户端 Agent 只接替原 runtime Agent 的认知和编排职责。客户端每次通过 `read_openmontage_file` 实时读取 CI 上的 Markdown、YAML 和 JSON，不做本地缓存；现有 Gateway 工具调用方式保持不变；所有图片、音频、视频、合成和渲染文件始终在 CI 完成和保存。

## 20. 实现状态（2026-09-01）

五个阶段已全部实现、测试、经子代理审查并修复后验收。详见
`docs/0901/implementation-acceptance-report.md` 的完整验收报告。

| 阶段 | 交付 | 提交 | 测试 |
|---|---|---|---|
| 一：只读指令接口 | `read_openmontage_file` + `openmontage/instruction_files.py` | `3c8c1f9` | 35 用例 |
| 二：客户端 Stage 接口 | `begin/update/submit_client_stage` + `media_references.py` | `5b3572f` | 34 用例 |
| 三：客户端适配 | `openmontage/client_stage_driver.py` | `ef81385` | 16 用例 |
| 四：媒体观察和 compose | E2E 端到端（fake Gateway） | `26a7a34` | 1 用例 |
| 五：CI-only 上线 | `OPENMONTAGE_CLIENT_STAGE_ONLY` 门禁 | `c0d016a` | 20 用例 |

新增错误码契约（客户端按码分支）：

```text
INSTRUCTION_FILE_NOT_FOUND / INSTRUCTION_FILE_UNAVAILABLE
UNSUPPORTED_FILE_TYPE / PATH_OUTSIDE_REPOSITORY / FILE_TOO_LARGE / INVALID_PATH
STAGE_ALREADY_OWNED / STAGE_LEASE_INVALID / STAGE_LEASE_EXPIRED / STAGE_ATTEMPT_MISMATCH
STAGE_STATE_INVALID / HUMAN_APPROVAL_REQUIRED / IDEMPOTENCY_CONFLICT
ARTIFACT_SCHEMA_INVALID / MEDIA_REFERENCE_INVALID / CHECKPOINT_WRITE_FAILED / JOB_CANCELLED
```

新增事件类型（§14）：`openmontage.client_stage.started / .progressed /
.checkpointed / .awaiting_approval / .completed / .failed`。
