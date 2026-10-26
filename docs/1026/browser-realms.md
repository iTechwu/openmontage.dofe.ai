# OpenMontage canonical 与 /hz 浏览器入口

本实现仍以 `https://user.hozonauto.com/api` 为唯一 issuer。Discovery、code/refresh、introspect、workspace 映射、logout confirmation、backchannel 均沿原服务端 canonical 链，不新增身份源。浏览器别名入口固定 `https://ixicai.cn/hz/montage`，浏览器中央 Cookie 操作固定 `https://ixicai.cn/hz/user/api`。

运行时边缘必须覆盖并精确提供以下组合；未知、不完整组合或无元数据的 `/hz/montage` 请求返回 400。Canonical location 清空 External-Base/Forwarded-Prefix。应用监听保持私有边缘后的同一端口，禁止把可直接注入这些元数据的服务暴露公网。

| 头 | 固定值 |
| --- | --- |
| Host | montage.hozonauto.com |
| X-External-Base | https://ixicai.cn/hz/montage |
| X-Forwarded-Host | ixicai.cn |
| X-Forwarded-Prefix | /hz/montage |
| X-Forwarded-Proto | https |

边缘向 Python 保留 `/hz/montage/` 前缀，应用验证组合后才剥离路由前缀。动态 HTML 注入固定 mount，JS、媒体、SSE、Service Worker 路径均保留该 mount。无 Vite/Next 构建变体、额外 Docker build 参数或新秘密。既有 `OPENMONTAGE_OIDC_*` 稳定密钥、SESSION_DB 持久卷不变，BASE_PATH 仍为空。SSO 需登记 exact redirect `https://ixicai.cn/hz/montage/auth/callback`；backchannel 继续登记 canonical URL。

Canonical Cookie 与既有数据库记录兼容。Alias 使用 `__Host-hz-dofe_rp_montage`、独立 state/suppression Cookie，全部 Secure、HttpOnly、Path=/、无 Domain。数据库内 hz namespace 独立，Fernet 子密钥及认证加密负载绑定 issuer/client/realm/base/record，不能通过拷贝密文跨 realm 或 record 复用。共享 sid 撤销标记和 family 唯一约束保留；不把两个 realm 的同 client family 混成同一记录。

授权 state/PKCE/nonce/redirect 存在各自 namespace。两个 realm 的 target bootstrap 均只向 SSO 提交各自服务端固定 `target_redirect_uri`，不采信 body 中的 realm/redirect；旧 canonical 票不能在 alias 兑换。native context 始终来自 SSO 响应，不能由别名或 Origin 推导。历史 Models-key embed 仅保留 canonical 既有开关，alias 明确拒绝。

Alias Service Worker scope/Allowed 都只到 `/hz/montage/`，等待当前页面由该确切 worker 接管后导航。Alias 不清旧 root Cookie、不清 origin storage、不发共享 BroadcastChannel；主题键单列 `hz.montage.backlot.theme`。同域 Path=/ Cookie 仍会随同源其他路径发送，本设计不宣称不同 origin 安全隔离，也不修改原 ixicai 服务。

验证：`OPENMONTAGE_TEST_CHROME_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' <venv>/bin/python -m pytest tests/backlot --ignore=tests/backlot/test_ui_bug_bash.py -q`。新增固定代理、state/CSRF/target、密文搬移、共享撤销、重启恢复，以及真实隔离 HTTPS/Chrome/Service Worker/Cookie 确认与旧站兼容测试。该测试 OP 为本地 fixture；不替代生产飞书或真实用户设备验收。
