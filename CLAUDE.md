# OpenMontage

**MANDATORY: Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) before responding to ANY user message.**

Do not act on the user's request until you have read AGENT_GUIDE.md.
It contains routing rules that determine your first action based on what the user asked.
Skipping it WILL cause you to take the wrong action.

There are no instructions in this file. All instructions are in AGENT_GUIDE.md.

## Git 提交规则（强制）

- **每一轮修改完成即提交**：一轮改动落地（功能实现、问题修复、文档更新等）后，立即提交所有更改，不要攒批。
- **提交信息一律使用中文**，采用 Conventional Commits 风格（`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`），正文说明本次实际完成的内容。
- **提交后推送到远端**；提交前先跑相关测试，确认改动可用。

更详细的约定见 `AGENT_GUIDE.md` 的「Git 提交约定」。
## config.local.yaml 约定

- 本项目当前没有 `config.local.yaml`；如未来引入，须遵守以下约定。
- `config.local.yaml` 属**核心配置文件**（结构、功能开关等运行事实），**不含任何密钥**；密钥只放 `keys/config.json`、`.env` 或环境变量。
- 该文件必须纳入 Git 跟踪，**禁止写入 .gitignore**；配置变更随代码一起提交评审。
- 本仓库 dev 版是唯一上游：`docker-helm.dofe.ai` 的部署副本必须随每次变更从本文件同步，唯一允许差异是环境行（`baseUrl`、`frontendUrl`、`domain`、`subDomain`、`apiSubDomain`、zones 桶名）。
- 线上部署的 `config.local.yaml` 与开发环境保持一致；变更后经 Jenkins 重新部署并重启对应服务容器。
