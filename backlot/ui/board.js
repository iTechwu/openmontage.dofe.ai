// Backlot project board — renders BoardState and stays live via SSE.

import {
  STAGE_ICONS, appURL, el, fmtAgo, fmtClock, fmtDuration, fmtMoney,
  getJSON, mediaURL, subscribe, thumbURL, waveBars,
} from "./lib.js";
import { formatToken, label, pipelineLabel, shotLabel, toolLabel } from "./i18n.js";

const projectPrefix = appURL("/p/");
const rawProjectPath = location.pathname.startsWith(projectPrefix)
  ? location.pathname.slice(projectPrefix.length)
  : "";
const projectId = decodeURIComponent(rawProjectPath);
const encodedProjectId = encodeURIComponent(projectId);
const app = document.getElementById("app");
const modal = document.getElementById("modal");
const player = document.getElementById("player");

const THEME_KEY = "backlot.theme";
let currentTheme = localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
let state = null;
let selectedStage = null;   // stage drawer open for this stage name
let activeRender = 0;
let replay = null;          // {t0, t1, t, playing} — replay mode when non-null
let firstPaint = true;

function applyTheme(theme) {
  currentTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = currentTheme;
  localStorage.setItem(THEME_KEY, currentTheme);
}

function renderThemeToggle() {
  const next = currentTheme === "light" ? "dark" : "light";
  const nextLabel = next === "light" ? "浅色" : "深色";
  return el("button", {
    class: "theme-toggle",
    type: "button",
    title: `切换到${nextLabel}主题`,
    "aria-label": `切换到${nextLabel}主题`,
    "aria-pressed": currentTheme === "light" ? "true" : "false",
    onclick: () => {
      applyTheme(next);
      render();
    },
  }, el("span", { class: "theme-toggle-icon", "aria-hidden": "true" }, currentTheme === "light" ? "☾" : "☀"));
}

applyTheme(currentTheme);

// ---------------------------------------------------------------------------
// header slate
// ---------------------------------------------------------------------------

function renderSlate(s) {
  const board = s.storyboard;
  const chips = [
    el("span", { class: "chip" }, pipelineLabel(s.pipeline.pipeline_type)),
    board && board.total_duration_seconds
      ? el("span", { class: "chip" }, `${board.scenes.length} 个场景 · ${fmtDuration(board.total_duration_seconds)}`)
      : null,
    s.style_playbook ? el("span", { class: "chip" }, s.style_playbook) : null,
  ];

  const awaiting = s.stages.find((x) => x.status === "awaiting_human");
  const inProgress = s.stages.find((x) => x.status === "in_progress");
  const stalled = s.stages.find((x) => x.stalled);
  let liveEl;
  if (awaiting) {
    liveEl = el("span", { class: "live" }, el("span", { class: "dot" }), "◈ 等待确认");
  } else if (stalled) {
    liveEl = el("span", { class: "live", style: "color:var(--red)" },
      el("span", { class: "dot", style: "background:var(--red);animation:none" }), "⚠ 是否卡住？");
  } else if (s.live || inProgress) {
    liveEl = el("span", { class: "live" }, el("span", { class: "dot" }), "进行中");
  } else {
    liveEl = el("span", { class: "live idle" }, el("span", { class: "dot" }),
      `空闲${s.last_activity ? " · " + fmtAgo(s.last_activity) : ""}`);
  }

  const cost = el("div", { class: "cost" });
  if (s.cost) {
    const spent = s.cost.total_spent_usd ?? 0;
    const budget = spent + (s.cost.budget_remaining_usd ?? 0);
    const hasBudget = s.cost.budget_remaining_usd != null;
    const pct = hasBudget && budget > 0 ? Math.min(100, (spent / budget) * 100) : 0;
    cost.append(el("div", { class: "nums" }, el("b", {}, fmtMoney(spent)),
      hasBudget ? el("span", {}, ` / ${fmtMoney(budget)}`) : ""));
    if (hasBudget) {
      cost.append(el("div", { class: "bar" }, el("i", {
        class: pct > 90 ? "crit" : pct > 75 ? "warn" : "", style: `width:${pct}%`,
      })));
    }
    cost.append(el("div", { class: "label" }, "生成花费"));
  }

  return el("header", { class: "slate" },
    el("div", { class: "clapper" }),
    el("div", {},
      el("a", { class: "wordmark", href: appURL("/"), style: "text-decoration:none" }, "Backlot"),
      el("h1", {}, s.title),
    ),
    ...chips,
    el("div", { class: "spacer" }),
    renderThemeToggle(),
    liveEl,
    cost,
  );
}

// ---------------------------------------------------------------------------
// stage rail
// ---------------------------------------------------------------------------

function stageSub(st) {
  if (st.status === "awaiting_human") return "等待你的确认\n在对话中回复以继续";
  if (st.status === "in_progress" && st.stalled) {
    return `是否卡住？已 ${st.stalled_minutes} 分钟无活动\n向 Agent 询问状态`;
  }
  if (st.status === "in_progress" && st.partial_progress) {
    const done = st.partial_progress.completed_scene_ids;
    if (Array.isArray(done)) return `${done.length} 个场景完成`;
    return "进行中";
  }
  if (st.status === "in_progress") return "进行中";
  if (st.status === "failed") return st.error ? String(st.error).slice(0, 60) : "失败";
  if (st.timestamp) {
    const approved = st.gated && st.human_approved ? " · 已确认" : "";
    return fmtClock(st.timestamp) + approved;
  }
  return "";
}

function renderRail(s) {
  const rail = el("nav", { class: "rail" });
  let pendingIndex = 1;
  for (const st of s.stages) {
    const cls = st.status === "completed" ? "done"
      : st.status === "in_progress" ? (st.stalled ? "active stalled" : "active")
      : st.status === "awaiting_human" ? "await"
      : st.status === "failed" ? "failed" : "";
    const icon = STAGE_ICONS[st.status] || String(pendingIndex);
    if (!STAGE_ICONS[st.status]) pendingIndex += 1;
    const node = el("div", {
      class: `stage ${cls}${selectedStage === st.name ? " selected" : ""}${st.undeclared ? " undeclared" : ""}`,
      title: st.undeclared ? "该自定义阶段已运行，但未在流水线清单中声明" : null,
      onclick: () => toggleDrawer(st.name),
    },
      el("span", { class: "line" }),
      el("span", { class: "node" }, icon),
      el("span", { class: "name" }, label(st.name)),
      el("span", { class: "sub", style: "white-space:pre-line" },
        st.undeclared ? `${stageSub(st)}\n未列入`.trim() : stageSub(st)),
    );
    rail.append(node);
  }
  return rail;
}

function toggleDrawer(stageName) {
  selectedStage = selectedStage === stageName ? null : stageName;
  render();
}

const STAGE_ARTIFACTS = {
  research: ["research_brief"],
  proposal: ["proposal_packet"],
  idea: ["brief"],
  script: ["script"],
  scene_plan: ["scene_plan"],
  assets: ["asset_manifest"],
  edit: ["edit_decisions"],
  compose: ["render_report", "final_review"],
  publish: ["publish_log"],
};

function artifactNamesForStage(st) {
  const declared = Array.isArray(st.produces) ? st.produces : [];
  const fallback = STAGE_ARTIFACTS[st.name] || [];
  return [...new Set([...declared, ...fallback].filter(Boolean))];
}

function reviewMetrics(review) {
  const nested = review && review.summary && typeof review.summary === "object"
    ? review.summary : {};
  return {
    critical: Number((review && review.critical) ?? nested.critical ?? 0),
    suggestions: Number((review && review.suggestions) ?? nested.suggestions ?? 0),
    nitpicks: Number((review && review.nitpicks) ?? nested.nitpicks ?? 0),
  };
}

function reviewSummaryText(review) {
  if (!review) return "";
  if (typeof review.summary === "string") return review.summary;
  const nested = review.summary && typeof review.summary === "object" ? review.summary : {};
  const counts = reviewMetrics(review);
  return [
    review.decision,
    `${counts.critical} 严重`,
    `${counts.suggestions} 条建议`,
    nested.review_focus_met ? `审查重点 ${nested.review_focus_met}` : null,
    nested.schema_validation,
  ].filter(Boolean).join(" · ");
}

function renderDrawer(s) {
  if (!selectedStage) return null;
  const st = s.stages.find((x) => x.name === selectedStage);
  if (!st) return null;

  const body = el("div", { class: "drawer-body" });

  if (st.review) {
    const metrics = reviewMetrics(st.review);
    const summary = reviewSummaryText(st.review);
    body.append(el("div", { class: "findings", style: "margin-bottom:12px" },
      el("span", { class: `f ${metrics.critical ? "crit" : ""}` }, `${metrics.critical} 严重`),
      el("span", { class: `f ${metrics.suggestions ? "sugg" : ""}` }, `${metrics.suggestions} 条建议`),
      el("span", { class: "f" }, `${metrics.nitpicks} 个细节`),
      summary ? el("span", { style: "font-size:calc(11.5px * var(--fs-scale));color:var(--text-2);margin-left:8px" }, summary) : null,
    ));
  }

  const names = artifactNamesForStage(st);
  let shown = false;
  for (const name of names) {
    const artifact = s.artifacts[name];
    if (!artifact) continue;
    shown = true;
    body.append(
      el("div", { class: "d-cat", style: "font-family:var(--mono);font-size:calc(9.5px * var(--fs-scale));color:var(--text-3);letter-spacing:.1em;text-transform:uppercase;margin:6px 0 4px" }, name),
      el("pre", {}, JSON.stringify(artifact, null, 2)),
    );
  }
  if (!shown) {
    body.append(el("div", { class: "hint" },
      st.status === "pending" ? "该阶段尚未运行。" : "磁盘上未找到该阶段的规范产物。"));
  }

  return el("div", { class: "drawer" },
    el("div", { class: "drawer-head" },
      el("h3", {}, `${humanize(st.name)} — ${label(st.status)}`),
      st.gate_skipped ? el("span", { class: "gate-chip" }, "⚑ 已跳过审查门") : null,
      st.versions > 1 ? el("span", { class: "ver-chip" }, `v${st.versions}`) : null,
      st.timestamp ? el("span", { class: "meta", style: "font-family:var(--mono);font-size:calc(10.5px * var(--fs-scale));color:var(--text-3)" }, st.timestamp) : null,
      el("span", { class: "close", onclick: () => toggleDrawer(st.name) }, "关闭 ✕"),
    ),
    body,
  );
}

// ---------------------------------------------------------------------------
// script card
// ---------------------------------------------------------------------------

function scriptSections(script, limit) {
  const sections = script.sections || [];
  const shown = limit ? sections.slice(0, limit) : sections;
  const nodes = [];
  for (const sec of shown) {
    nodes.push(el("div", { class: "sp-slug" },
      `${(sec.id || "").toUpperCase()} — ${sec.label || "段落"} `,
      el("span", { class: "tc" }, `${fmtDuration(sec.start_seconds)} – ${fmtDuration(sec.end_seconds)}`)));
    if (sec.text) nodes.push(el("div", { class: "sp-action" }, sec.text));
    if (sec.speaker_directions) nodes.push(el("div", { class: "sp-paren" }, `(${sec.speaker_directions})`));
    const cues = sec.enhancement_cues || [];
    if (cues.length) {
      nodes.push(el("div", { style: "margin-left:42px" },
        cues.map((c) => el("span", { class: "sp-cue" }, `▸ ${c.type} · ${String(c.description || "").slice(0, 60)}`))));
    }
  }
  if (limit && sections.length > limit) {
    nodes.push(el("div", { class: "sp-fade" }, `… 还有 ${sections.length - limit} 个段落`));
  }
  return nodes;
}

function renderScriptCard(s) {
  const script = s.artifacts.script;
  if (!script) return null;
  const scriptStage = s.stages.find((x) => x.name === "script");
  const status = scriptStage ? scriptStage.status : "unknown";
  const stamp = status === "completed"
    ? el("span", { class: "script-status script-approved" }, "已确认")
    : status === "awaiting_human"
      ? el("span", { class: "script-status script-pending" }, "待确认")
      : status === "in_progress"
        ? el("span", { class: "script-status script-draft" }, "草拟中")
        : null;

  const card = el("div", { class: "script-card script-preview", title: "点击展开完整脚本", onclick: openScriptModal },
    stamp,
    el("div", { class: "sp-title" }, script.title || s.title),
    el("div", { class: "sp-meta" },
      `脚本 · ${fmtDuration(script.total_duration_seconds)} · ${(script.sections || []).length} 段`),
    ...scriptSections(script, 4),
    el("span", { class: "sp-expand" }, "⤢ 展开脚本"),
  );
  return card;
}

function humanize(value) {
  return label(value || "artifact");
}

function shortText(value, limit = 180) {
  const text = String(value || "").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function reviewFact(label, value) {
  if (value == null || value === "") return null;
  return el("div", { class: "approval-fact" },
    el("span", {}, label),
    el("b", {}, value),
  );
}

function reviewFacts(items) {
  const facts = items.filter(Boolean);
  return facts.length ? el("div", { class: "approval-facts" }, facts) : null;
}

function titledItems(items, selectedId = null) {
  const rows = (items || []).slice(0, 4).map((item, index) => {
    if (item == null) return null;
    if (typeof item !== "object") {
      return el("li", {}, shortText(item));
    }
    const id = item.id || item.concept_id || item.option_id;
    const title = item.title || item.name || item.display_name || item.label || id || item.path || item.platform || item.description || `项目 ${index + 1}`;
    const detail = item.hook || item.why_this_works || item.summary || item.description || item.silhouette_notes;
    return el("li", { class: id && id === selectedId ? "selected" : "" },
      el("div", { class: "approval-item-title" }, shortText(title, 100),
        id && id === selectedId ? el("span", { class: "approval-selected" }, "已选") : null),
      detail && detail !== title ? el("p", {}, shortText(detail)) : null,
    );
  }).filter(Boolean);
  return rows.length ? el("ul", { class: "approval-items" }, rows) : null;
}

function genericArtifactSummary(artifact) {
  const facts = [];
  const items = [];
  for (const [key, value] of Object.entries(artifact || {})) {
    if (["version", "decision_log_ref"].includes(key)) continue;
    if (["string", "number", "boolean"].includes(typeof value)) {
      facts.push(reviewFact(label(key), shortText(value, 90)));
    } else if (Array.isArray(value)) {
      facts.push(reviewFact(humanize(key), `${value.length} 项`));
      if (!items.length && value.length) items.push(titledItems(value));
    }
    if (facts.length >= 6) break;
  }
  return [reviewFacts(facts), ...items].filter(Boolean);
}

function artifactReviewContent(name, artifact) {
  if (name === "brief") {
    return [
      artifact.hook ? el("p", { class: "approval-lead" }, artifact.hook) : null,
      reviewFacts([
        reviewFact("平台", artifact.target_platform),
        reviewFact("时长", artifact.target_duration_seconds != null ? fmtDuration(artifact.target_duration_seconds) : null),
        reviewFact("基调", artifact.tone),
        reviewFact("风格", artifact.style),
      ]),
      titledItems(artifact.key_points),
    ].filter(Boolean);
  }
  if (name === "proposal_packet") {
    const selected = (artifact.selected_concept || {}).concept_id;
    const plan = artifact.production_plan || {};
    const cost = artifact.cost_estimate || {};
    return [
      reviewFacts([
        reviewFact("渲染引擎", plan.render_runtime),
        reviewFact("流水线", plan.pipeline),
        reviewFact("预估成本", cost.total_estimated_usd != null ? fmtMoney(cost.total_estimated_usd) : null),
        reviewFact("方案数", Array.isArray(artifact.concept_options) ? artifact.concept_options.length : null),
      ]),
      titledItems(artifact.concept_options, selected),
      (artifact.selected_concept || {}).rationale
        ? el("p", { class: "approval-rationale" },
          el("b", {}, "为何选择此方案  "), shortText(artifact.selected_concept.rationale))
        : null,
    ].filter(Boolean);
  }
  if (name === "research_brief") {
    return [
      artifact.topic ? el("p", { class: "approval-lead" }, artifact.topic) : null,
      reviewFacts([
        reviewFact("来源", Array.isArray(artifact.sources) ? artifact.sources.length : null),
        reviewFact("数据点", Array.isArray(artifact.data_points) ? artifact.data_points.length : null),
        reviewFact("角度", Array.isArray(artifact.angles_discovered) ? artifact.angles_discovered.length : null),
      ]),
      titledItems(artifact.angles_discovered),
    ].filter(Boolean);
  }
  if (name === "script") {
    const first = (artifact.sections || [])[0];
    return [
      reviewFacts([
        reviewFact("时长", fmtDuration(artifact.total_duration_seconds)),
        reviewFact("段数", (artifact.sections || []).length),
      ]),
      first && first.text ? el("p", { class: "approval-lead" }, shortText(first.text, 220)) : null,
      el("p", { class: "approval-guidance" }, "完整脚本预览见下方。"),
    ].filter(Boolean);
  }
  if (name === "scene_plan") {
    const scenes = artifact.scenes || [];
    const end = scenes.reduce((max, scene) => Math.max(max, Number(scene.end_seconds) || 0), 0);
    return [
      reviewFacts([
        reviewFact("场景数", scenes.length),
        reviewFact("时长", end ? fmtDuration(end) : null),
      ]),
      titledItems(scenes),
      el("p", { class: "approval-guidance" }, "在下方的分镜中查看时长与镜头覆盖。"),
    ].filter(Boolean);
  }
  if (name === "asset_manifest") {
    const assets = artifact.assets || [];
    const types = [...new Set(assets.map((asset) => asset.type).filter(Boolean))];
    return [
      reviewFacts([
        reviewFact("素材数", assets.length),
        reviewFact("类型", types.map(formatToken).join(", ")),
        reviewFact("生成成本", artifact.total_cost_usd != null ? fmtMoney(artifact.total_cost_usd) : null),
      ]),
      titledItems(assets),
      el("p", { class: "approval-guidance" }, "在下方胶片中逐一检查每个生成镜头，确认后再通过合成。"),
    ].filter(Boolean);
  }
  if (name === "edit_decisions") {
    return [
      reviewFacts([
        reviewFact("剪辑数", Array.isArray(artifact.cuts) ? artifact.cuts.length : null),
        reviewFact("渲染引擎", artifact.render_runtime || (artifact.metadata || {}).render_runtime),
      ]),
      titledItems(artifact.cuts),
    ].filter(Boolean);
  }
  if (name === "render_report") {
    return [
      reviewFacts([
        reviewFact("产物数", Array.isArray(artifact.outputs) ? artifact.outputs.length : null),
        reviewFact("时长", artifact.duration_seconds != null ? fmtDuration(artifact.duration_seconds) : null),
      ]),
      titledItems(artifact.outputs),
    ].filter(Boolean);
  }
  if (name === "publish_log") {
    return [
      reviewFacts([reviewFact("目标数", Array.isArray(artifact.entries) ? artifact.entries.length : null)]),
      titledItems((artifact.entries || []).map((entry) => ({
        title: entry.platform || entry.destination || "发布目标",
        description: [entry.status, entry.url].filter(Boolean).join(" · "),
      }))),
    ].filter(Boolean);
  }
  return genericArtifactSummary(artifact);
}

function artifactReviewTitle(name, artifact, s) {
  if (name === "proposal_packet") {
    const selected = (artifact.selected_concept || {}).concept_id;
    const concept = (artifact.concept_options || []).find((item) => item.id === selected);
    return (concept && concept.title) || "制作方案";
  }
  if (name === "research_brief") return artifact.topic || "研究简报";
  if (name === "scene_plan") return "场景计划";
  if (name === "asset_manifest") return "生成的素材";
  if (name === "edit_decisions") return "剪辑决策";
  if (name === "render_report") return "渲染报告";
  if (name === "publish_log") return "发布计划";
  return artifact.title || artifact.name || s.title;
}

function renderApprovalReview(s) {
  const awaiting = s.stages.find((item) => item.status === "awaiting_human");
  if (!awaiting) return null;

  const names = artifactNamesForStage(awaiting);
  const entries = names
    .filter((name) => name !== "decision_log")
    .map((name) => [name, s.artifacts[name]])
    .filter(([, artifact]) => artifact && typeof artifact === "object");
  const stageIndex = s.stages.findIndex((item) => item.name === awaiting.name);
  const nextStage = stageIndex >= 0 ? s.stages[stageIndex + 1] : null;
  const review = awaiting.review || {};
  const reviewSummary = reviewSummaryText(review);

  const artifacts = entries.map(([name, artifact]) => el("article", {
    class: "approval-artifact",
    "data-artifact": name,
  },
    el("div", { class: "approval-artifact-kicker" }, humanize(name)),
    el("h2", {}, artifactReviewTitle(name, artifact, s)),
    ...artifactReviewContent(name, artifact),
  ));

  if (!artifacts.length) {
    artifacts.push(el("div", { class: "approval-missing", role: "alert" },
      el("b", {}, "未找到可审查的内容。"),
      names.length
        ? `${humanize(awaiting.name)} 检查点声明了 ${names.map(humanize).join("、")}，但 Backlot 无法加载它。`
        : `${humanize(awaiting.name)} 检查点未声明产物。`,
    ));
  }

  return el("section", { class: "approval-review", "data-stage": awaiting.name },
    el("div", { class: "approval-review-head" },
      el("div", {},
        el("div", { class: "approval-eyebrow" }, "审查门"),
        el("h2", {}, `${humanize(awaiting.name)} 已就绪，等待你的审查`),
        el("p", {}, "在此审查产物，然后在对话中回复以确认或要求修改。"),
      ),
      el("span", { class: "approval-status" }, "待确认"),
    ),
    reviewSummary ? el("div", { class: "approval-review-note" },
      el("b", {}, "自审  "), shortText(reviewSummary, 260)) : null,
    el("div", { class: "approval-artifacts" }, artifacts),
    el("div", { class: "approval-review-foot" },
      el("span", {}, nextStage
        ? `确认后将解锁 ${humanize(nextStage.name)}。`
        : "这是最后一个确认门。"),
      el("button", { type: "button", onclick: () => toggleDrawer(awaiting.name) }, "打开完整产物"),
    ),
  );
}

function openScriptModal() {
  const script = state && state.artifacts.script;
  if (!script) return;
  modal.innerHTML = "";
  modal.append(
    el("span", { class: "modal-close", onclick: closeModal }, "按 Esc 关闭"),
    el("div", { class: "modal-page" },
      el("div", { class: "script-card", style: "cursor:default" },
        el("div", { class: "sp-title" }, script.title || state.title),
        el("div", { class: "sp-meta" },
          `脚本 · ${fmtDuration(script.total_duration_seconds)} · ${(script.sections || []).length} 段`),
        ...scriptSections(script, 0),
        el("div", { class: "sp-fade" }, "完"),
      )),
  );
  modal.classList.add("open");
}

function openNarrModal(card) {
  modal.innerHTML = "";
  const meta = [sceneLabel(card.id), card.section_label, fmtDuration(card.duration_seconds)]
    .filter(Boolean).join(" · ");
  modal.append(
    el("span", { class: "modal-close", onclick: closeModal }, "按 Esc 关闭"),
    el("div", { class: "modal-page" },
      el("div", { class: "script-card", style: "cursor:default" },
        el("div", { class: "sp-meta" }, meta),
        card.narration ? el("div", { class: "sp-action", style: "margin-left:0" }, card.narration) : null,
        card.shot_intent ? el("div", { class: "sp-paren", style: "margin-left:0" }, `意图 — ${card.shot_intent}`) : null,
        card.description ? el("div", { class: "sp-paren", style: "margin-left:0" }, card.description) : null,
      )),
  );
  modal.classList.add("open");
}

function closeModal() {
  modal.querySelectorAll("video").forEach((video) => { video.pause(); video.removeAttribute("src"); video.load(); });
  modal.innerHTML = "";
  modal.classList.remove("open");
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

function mediaKind(media) {
  if (media && (media.media_type === "image" || media.media_type === "video")) return media.media_type;
  if (media && (media.type === "image" || media.type === "video")) return media.type;
  const path = String((media && media.path) || "").toLowerCase();
  return /\.(mp4|webm|mov)$/.test(path) ? "video" : "image";
}

function mediaTrigger(node, handler, labelText) {
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", labelText);
  node.onclick = handler;
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); handler(); }
  });
  return node;
}

function mediaFileName(media) {
  const path = String((media && media.path) || "");
  return path.split("/").pop() || "media";
}

function openMediaModal(project, media, labelText = "媒体预览") {
  if (!media || !media.path) return;
  const kind = mediaKind(media);
  const src = mediaURL(project, media.path);
  const name = mediaFileName(media);
  const preview = kind === "video"
    ? el("video", { class: "media-modal-media", src, controls: "", preload: "metadata", playsinline: "" })
    : el("img", { class: "media-modal-media", src, alt: name });
  preview.onerror = () => {
    const error = el("div", { class: "media-modal-error", role: "alert" }, "素材暂时无法加载");
    preview.replaceWith(error);
  };
  const download = el("a", { class: "media-action", href: src, download: name }, "下载");
  modal.innerHTML = "";
  modal.append(
    el("button", { class: "modal-close", type: "button", onclick: closeModal, "aria-label": "关闭预览" }, "关闭"),
    el("div", { class: "media-modal" },
      el("div", { class: "media-modal-head" },
        el("div", { class: "media-modal-title" }, labelText),
        el("div", { class: "media-modal-name" }, name)),
      preview,
      el("div", { class: "media-actions" }, download,
        media.size != null ? el("span", { class: "media-modal-meta" }, `${(Number(media.size) / 1048576).toFixed(1)} MB`) : null,
        media.source_tool ? el("span", { class: "media-modal-meta" }, media.source_tool) : null),
    ),
  );
  modal.classList.add("open");
}

// ---------------------------------------------------------------------------
// right rail: decisions, activity
// ---------------------------------------------------------------------------

function renderDecisions(s) {
  const log = s.artifacts.decision_log;
  const decisions = (log && log.decisions) || [];
  if (!decisions.length) return null;
  const body = el("div", { class: "panel-body" });
  // Collapse by category+subject: a decision that changed mid-run (e.g. voice
  // openai_onyx → chirp3) is superseded by the later entry — show the CURRENT
  // choice, not the first one recorded, and mark that it was revised.
  const current = new Map();
  decisions.forEach((d, i) => {
    const key = `${d.category || "decision"}::${d.subject || ""}`;
    const prev = current.get(key);
    current.set(key, { d, order: i, revised: prev ? prev.revised + 1 : 0 });
  });
  const shown = [...current.values()].sort((a, b) => b.order - a.order).slice(0, 8);
  for (const { d, revised } of shown) {
    const selLabel = (() => {
      // Prefer the human label of the selected option over its bare id.
      const opt = (d.options_considered || []).find((o) => (o.option_id ?? o.label) === d.selected);
      return (opt && opt.label) || d.selected || "";
    })();
    const alts = (d.options_considered || [])
      .filter((o) => (o.option_id ?? o.label) !== d.selected && (o.option_id || o.label));
    body.append(el("div", { class: "decision" },
      el("div", { class: "d-cat" }, `${label(d.category || "decision")}${d.confidence ? ` · ${formatToken(d.confidence)}` : ""}`,
        revised ? el("span", { class: "d-revised" }, " · 已修订") : null),
      el("div", { class: "d-pick" }, `${d.subject || ""} `, el("span", { class: "arrow" }, "→"), ` ${selLabel}`),
      d.reason ? el("div", { class: "d-why" }, d.reason) : null,
      alts.length ? el("div", { class: "d-alt" }, "备选：",
        alts.slice(0, 3).map((o, i) => [i ? " · " : "", el("s", {}, o.label || o.option_id)]).flat()) : null,
    ));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, "决策"), el("span", { class: "meta" }, "decision_log.json")),
    body);
}

function renderActivity(s) {
  const events = s.events || [];
  if (!events.length) return null;
  const body = el("div", { class: "panel-body" });
  // A start is "running" only until a later finish/error for the same
  // tool+scene closes it — closed starts are dropped (the finish row tells
  // the story), unmatched starts render as live. Counted (not keyed-single)
  // so parallel runs of the same tool on the same scene stay visible.
  const open = new Map(); // key -> {count, ev}
  const rows = [];
  for (const ev of events) {
    const key = `${ev.tool}:${ev.scene_id || ""}`;
    if (ev.event === "start") {
      const slot = open.get(key) || { count: 0, ev };
      slot.count += 1;
      slot.ev = ev;
      open.set(key, slot);
    } else {
      const slot = open.get(key);
      if (slot) {
        slot.count -= 1;
        if (slot.count <= 0) open.delete(key);
      }
      rows.push(ev);
    }
  }
  for (const slot of open.values()) rows.push(slot.ev);
  rows.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  for (const ev of rows.slice(-10).reverse()) {
    let statusEl;
    if (ev.event === "finish") {
      statusEl = el("span", { class: `status ${ev.success === false ? "err" : "ok"}` },
        `${ev.success === false ? "✕" : "✓"}${ev.duration_s != null ? ` ${ev.duration_s.toFixed ? ev.duration_s.toFixed(1) : ev.duration_s}s` : ""}${ev.cost_usd ? ` ${fmtMoney(ev.cost_usd)}` : ""}`);
    } else if (ev.event === "error") {
      statusEl = el("span", { class: "status err" }, "✕");
    } else {
      statusEl = el("span", { class: "status run" }, "● 运行中");
    }
    body.append(el("div", { class: "act-row" },
      el("span", { class: "t" }, fmtClock(ev.ts)),
      el("span", { class: "tool" }, toolLabel(ev.tool)),
      el("span", { class: "target" }, ev.scene_id || ""),
      statusEl,
    ));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, "活动"), el("span", { class: "meta" }, "events.jsonl")),
    body);
}

// ---------------------------------------------------------------------------
// storyboard filmstrip
// ---------------------------------------------------------------------------

function sceneLabel(id) {
  // "sc4" → "SC 04", "scene-11" → "SC 11", anything else → uppercased id
  const m = String(id).match(/(\d+)\s*$/);
  if (m) return `场景 ${m[1].padStart(2, "0")}`;
  return `场景 ${String(id).toUpperCase().slice(0, 10)}`;
}

function sceneCard(s, card) {
  const dur = card.duration_seconds;
  const width = Math.max(132, Math.min(300, 70 + (dur || 3) * 26));
  const wrap = el("div", { class: "scene-card", style: `width:${width}px` });

  const slate = el("div", { class: "sc-slate" },
    el("span", { class: "num" }, sceneLabel(card.id)),
    card.takes.length > 1 ? el("span", { class: "take" }, `T${card.takes.length}`) : null,
    card.hero_moment ? el("span", { class: "hero" }, "★ 高光") : null,
    el("span", { class: "dur" }, fmtDuration(dur)),
  );
  wrap.append(slate);

  // visual slot
  let thumb;
  if (card.generating) {
    thumb = el("div", { class: "thumb generating" },
      el("div", { class: "shimmer" }),
      el("div", { class: "gen-label" },
        el("span", {}, "◉ 生成中"),
        el("span", { class: "sub" }, card.generating_tool || "")));
  } else if (card.visual && card.visual.exists) {
    const v = card.visual;
    const badge = [v.model || v.source_tool, v.cost_usd != null ? fmtMoney(v.cost_usd) : null,
      v.quality_score != null ? `质量 ${v.quality_score}` : null].filter(Boolean).join(" · ");
    if (mediaKind(v) === "video") {
      thumb = el("div", { class: "thumb approved" },
        el("video", { src: mediaURL(s.project_id, v.path), muted: "", preload: "metadata", playsinline: "" }),
        el("span", { class: "play" }, "▶"),
        badge ? el("span", { class: "badge" }, badge) : null);
      mediaTrigger(thumb, () => openMediaModal(s.project_id, v, `${sceneLabel(card.id)} · 视频`), `${sceneLabel(card.id)} 视频预览`);
    } else {
      const img = el("img", { src: thumbURL(s.project_id, v.path, 640), loading: "lazy", alt: "" });
      // A thumbnail that fails to load must never show a broken-image icon —
      // fall back to the shot spec in place (F: broken links).
      img.onerror = () => {
        const t = img.closest(".thumb");
        if (!t) return;
        t.className = "thumb spec";
        t.innerHTML = "";
        t.append(el("div", { class: "spec-in" },
          el("div", { class: "spec-desc" }, card.description || "素材不可用"),
          el("div", { class: "spec-shot" }, [card.framing, card.movement].filter(Boolean).join(" · ").slice(0, 70))));
      };
      thumb = el("div", { class: "thumb approved" }, img,
        v.snapshot ? el("span", { class: "badge" }, "快照") : (badge ? el("span", { class: "badge" }, badge) : null));
      mediaTrigger(thumb, () => openMediaModal(s.project_id, v, `${sceneLabel(card.id)} · 图片`), `${sceneLabel(card.id)} 图片预览`);
    }
  } else if (card.type === "animation") {
    // Bespoke/atelier scene with no snapshot yet — name it as such rather
    // than "no asset yet" (the composition IS the asset).
    thumb = el("div", { class: "thumb spec bespoke" },
      el("div", { class: "spec-in" },
        el("span", { class: "bespoke-tag" }, "◆ 定制"),
        el("div", { class: "spec-desc" }, card.description || ""),
        el("div", { class: "spec-shot" }, "手工编排的合成")));
  } else if (card.visual && !card.visual.exists) {
    thumb = el("div", { class: "thumb missing" },
      el("div", { class: "spec-in" },
        el("span", { class: "warn-ic" }, "⚑"),
        el("div", { class: "spec-desc" }, "清单中有素材，但文件缺失"),
        el("div", { class: "spec-shot" }, card.visual.path || "")));
  } else if (card.type === "text_card") {
    thumb = el("div", { class: "thumb textcard" },
      el("div", { class: "tc-copy" }, (card.narration || card.description || "").slice(0, 48)));
  } else if (card.required_assets.length) {
    thumb = el("div", { class: "thumb missing" },
      el("div", { class: "spec-in" },
        el("span", { class: "warn-ic" }, "⚑"),
        el("div", { class: "spec-desc" }, "暂无素材"),
        el("div", { class: "spec-shot" }, (card.required_assets[0].description || "").slice(0, 60))));
  } else {
    thumb = el("div", { class: "thumb spec" },
      el("div", { class: "spec-in" },
        el("div", { class: "spec-desc" }, card.description || ""),
        el("div", { class: "spec-shot" }, [card.framing, card.movement].filter(Boolean).join(" · ").slice(0, 70))));
  }
  wrap.append(thumb);

  // shot language chips
  const sl = card.shot_language;
  if (sl) {
    wrap.append(el("div", { class: "shotchips", style: "display:flex;flex-wrap:wrap;gap:4px;padding:7px 2px 0" },
      [sl.shot_size, sl.camera_movement, sl.lens_mm ? `${sl.lens_mm}mm` : null, sl.lighting_key]
        .filter(Boolean)
        .map((t) => el("span", { style: "font-family:var(--mono);font-size:calc(8.5px * var(--fs-scale));letter-spacing:.04em;color:#62626c;border:1px solid #212129;border-radius:3px;padding:1px 5px" }, shotLabel(t)))));
  }

  // Historical media is grouped by actual file type. This also handles old
  // manifests whose type says "animation" while the file is an MP4.
  const history = card.media_history || {
    images: card.takes.filter((t) => mediaKind(t) === "image"),
    videos: card.takes.filter((t) => mediaKind(t) === "video"),
  };
  const historyTotal = (history.images || []).length + (history.videos || []).length;
  if (historyTotal > 1) {
    const takes = el("div", { class: "takes" });
    for (const [kind, items] of [["image", history.images || []], ["video", history.videos || []]]) {
      if (!items.length) continue;
      const group = el("div", { class: "take-group" },
        el("div", { class: "take-group-label" }, `${kind === "image" ? "历史图片" : "历史视频"} · ${items.length}`),
        el("div", { class: "take-group-items" }));
      const list = group.querySelector(".take-group-items");
      items.forEach((t, i) => {
        const isActive = card.visual && ((t.path && t.path === card.visual.path) || (t.id && t.id === card.visual.id));
        const tk = el("button", { class: `tk${isActive ? " active" : ""}`, type: "button", title: `预览${kind === "image" ? "图片" : "视频"} ${i + 1}`, "aria-label": `预览${kind === "image" ? "图片" : "视频"} ${i + 1}` });
        const image = el("img", { src: thumbURL(s.project_id, t.path, 320), loading: "lazy", alt: "" });
        image.onerror = () => { image.replaceWith(el("span", { class: "tk-fallback" }, kind === "video" ? "视频" : "图片")); };
        tk.append(image);
        tk.onclick = () => openMediaModal(s.project_id, t, `${sceneLabel(card.id)} · ${kind === "image" ? "历史图片" : "历史视频"}`);
        list.append(tk);
      });
      takes.append(group);
    }
    takes.append(el("span", { class: "tk-label" }, `${historyTotal} 个历史素材`));
    wrap.append(takes);
  }

  // narration + audio — clickable to read in full (F: narration text cut off)
  if (card.narration) {
    const long = card.narration.length > 90;
    wrap.append(el("div", {
      class: `narr${long ? " clip" : ""}`,
      title: "点击阅读完整旁白",
      onclick: () => openNarrModal(card),
    }, card.narration, long ? el("span", { class: "narr-more" }, "⤢") : null));
  } else if (card.shot_intent || card.description) {
    wrap.append(el("div", { class: "narr tc-note" }, (card.shot_intent || card.description || "").slice(0, 110)));
  }
  const narrAudio = card.audio.find((a) => a.exists && (a.type === "narration" || a.type === "audio"));
  if (narrAudio) {
    const wave = el("div", { class: "wave", style: "cursor:pointer", title: "播放旁白" });
    waveBars(wave, card.id + narrAudio.path);
    wave.append(el("span", { class: "wv-time" }, narrAudio.duration_seconds ? fmtDuration(narrAudio.duration_seconds) : "♪"));
    wave.onclick = () => {
      player.src = mediaURL(s.project_id, narrAudio.path);
      player.play();
    };
    wrap.append(wave);
  }
  return wrap;
}

function renderStoryboard(s) {
  const board = s.storyboard;
  if (!board) return null;
  const strip = el("div", { class: "filmstrip" });
  for (const card of board.scenes) strip.append(sceneCard(s, card));
  return el("div", {},
    el("div", { class: "section-title" }, "分镜",
      el("span", { class: "meta" },
        `${board.scenes.length} 个场景${board.total_duration_seconds ? ` · ${fmtDuration(board.total_duration_seconds)}` : ""} · 卡片宽度 ∝ 时长`)),
    el("div", { class: "strip-outer" }, strip));
}

// ---------------------------------------------------------------------------
// renders + degraded media
// ---------------------------------------------------------------------------

function renderRenders(s) {
  const renders = s.media.renders;
  if (!renders.length) return null;
  if (activeRender >= renders.length) activeRender = 0;
  const current = renders[activeRender];
  // Full re-renders (every SSE refresh) must not reset an in-progress
  // watch: carry playback position/state over to the recreated element.
  const prev = document.querySelector(".render-hero video");
  const src = mediaURL(s.project_id, current.path);
  // preload="metadata" gives the element its intrinsic aspect ratio (and a
  // poster frame) before playback — without it a portrait 9:16 render sits
  // in a letterboxed 100%-wide black box that reads as landscape.
  const video = el("video", { src, controls: "", preload: "metadata" });
  // Click the frame to start playback (controls handle pause/scrub) — the
  // big player was inert to a click on the picture itself.
  video.addEventListener("click", () => { if (video.paused) video.play().catch(() => {}); });
  if (prev && prev.getAttribute("src") === src && (prev.currentTime > 0 || !prev.paused)) {
    const t = prev.currentTime;
    const wasPlaying = !prev.paused && !prev.ended;
    video.addEventListener("loadedmetadata", () => { video.currentTime = t; }, { once: true });
    video.setAttribute("preload", "metadata");
    if (wasPlaying) video.autoplay = true;
  }
  const versions = el("div", { class: "render-meta" },
    renders.map((r, i) => el("span", {
      class: `v${i === activeRender ? " active" : ""}`,
      onclick: () => { activeRender = i; render(); },
    }, `${r.path.split("/").pop()}${r.at_root ? " · 根目录" : ""}`)),
    el("span", { style: "margin-left:auto" }, `${(current.size / 1048576).toFixed(1)} MB`),
    el("button", { class: "media-action", type: "button", onclick: () => openMediaModal(s.project_id, current, "成片预览") }, "放大预览"),
    el("a", { class: "media-action", href: src, download: mediaFileName(current) }, "下载"),
  );
  return el("div", {},
    el("div", { class: "section-title" }, "成片",
      el("span", { class: "meta" }, `${renders.length} 个版本`)),
    el("div", { class: "render-hero" }, video),
    versions);
}

function renderFoundMedia(s) {
  // Degraded view: show discovered snapshots when there's no storyboard.
  if (s.storyboard || !s.media.snapshots.length) return null;
  const grid = el("div", { class: "found-grid" });
  for (const snap of s.media.snapshots.slice(0, 12)) {
    const item = { ...snap, media_type: "image" };
    const thumb = el("div", { class: "thumb approved" },
      el("img", { src: thumbURL(s.project_id, snap.path, 640), loading: "lazy", alt: "" }));
    mediaTrigger(thumb, () => openMediaModal(s.project_id, item, "发现的图片"), "预览发现的图片");
    grid.append(thumb);
  }
  return el("div", {},
    el("div", { class: "section-title" }, "监控发现的画面",
      el("span", { class: "meta" }, "快照 / 校验帧")),
    grid);
}

function renderNoState(s) {
  if (s.has_pipeline_state) return null;
  return el("div", { class: "notice", style: "border-color:#2b2b33;background:var(--surface-2);color:var(--text-3)" },
    el("span", { style: "font-size:calc(15px * var(--fs-scale))" }, "◌"),
    el("span", {},
      el("b", { style: "color:var(--text-2)" }, "无流水线状态。"),
      "该项目没有检查点——Backlot 显示的是磁盘上发现的内容。",
      "遵循检查点协议的运行会获得完整看板。"));
}

function renderAwaitingNotice(s) {
  const awaiting = s.stages.find((x) => x.status === "awaiting_human");
  if (!awaiting) return null;
  return el("div", { class: "notice" },
    el("span", { style: "font-size:calc(16px * var(--fs-scale))" }, "◈"),
    el("span", {},
      el("b", {}, `${humanize(awaiting.name)} 阶段正在等待你的审查。`),
      "Agent 已在此门处暂停——在", el("b", {}, "对话中"), "回复以确认或要求修改。"));
}

// ---------------------------------------------------------------------------
// replay — scrub a completed run from its timestamps
// ---------------------------------------------------------------------------

// Python writers emit tz-aware UTC isoformat, but treat tz-naive strings as
// UTC too — mixing local-parsed and UTC-parsed timestamps would skew replay
// ordering by the user's UTC offset.
const ts = (iso) => {
  if (!iso) return null;
  let s = String(iso);
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(s)) s += "Z";
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
};

function replayBounds(s) {
  const moments = [];
  for (const st of s.stages) {
    for (const h of st.history_entries || []) {
      const t = ts(h.timestamp);
      if (t) moments.push(t);
    }
  }
  for (const ev of s.events || []) {
    const t = ts(ev.ts);
    if (t) moments.push(t);
  }
  if (moments.length < 2) return null;
  return { t0: Math.min(...moments), t1: Math.max(...moments) };
}

function stateAt(s, T) {
  const view = structuredClone(s);
  for (const st of view.stages) {
    const past = (st.history_entries || []).filter((h) => ts(h.timestamp) != null && ts(h.timestamp) <= T);
    if (!past.length) {
      st.status = "pending"; st.review = null; st.timestamp = null;
      st.gate_skipped = false; st.partial_progress = null;
    } else {
      const cur = past[past.length - 1];
      st.status = cur.status || "pending";
      st.timestamp = cur.timestamp;
    }
  }
  view.events = (view.events || []).filter((ev) => ts(ev.ts) != null && ts(ev.ts) <= T);

  // Storyboard: visuals appear as their scene finishes (events) or when the
  // assets stage has completed as of T (legacy runs without events).
  if (view.storyboard) {
    const assetsStage = view.stages.find((x) => x.name === "assets");
    const assetsDone = assetsStage && assetsStage.status === "completed";
    const finished = new Set();
    const startedNow = new Map();
    for (const ev of view.events) {
      if (!ev.scene_id) continue;
      if (ev.event === "finish") { finished.add(ev.scene_id); startedNow.delete(ev.scene_id); }
      else if (ev.event === "start") startedNow.set(ev.scene_id, ev);
      else if (ev.event === "error") startedNow.delete(ev.scene_id);
    }
    const scenePlanStage = view.stages.find((x) => x.name === "scene_plan");
    const scenePlanDone = scenePlanStage && ["completed", "awaiting_human"].includes(scenePlanStage.status);
    if (!scenePlanDone) {
      view.storyboard = null;
    } else {
      for (const card of view.storyboard.scenes) {
        const visible = assetsDone || finished.has(card.id);
        if (!visible) { card.visual = null; card.takes = []; card.media_history = { images: [], videos: [] }; card.audio = []; }
        card.generating = startedNow.has(card.id);
        card.generating_tool = (startedNow.get(card.id) || {}).tool;
      }
    }
  }
  // Final artifacts hide until their stage happened — for every project
  // shape, storyboard or not (a degraded run must not show the finished
  // movie before its stages ran).
  const scriptStage = view.stages.find((x) => x.name === "script");
  if (!(scriptStage && ["completed", "awaiting_human"].includes(scriptStage.status))) {
    delete view.artifacts.script;
  }
  const composeStage = view.stages.find((x) => x.name === "compose");
  if (!(composeStage && composeStage.status === "completed")) {
    view.media.renders = [];
  }
  return view;
}

function renderReplayBar(s) {
  const bounds = replayBounds(s);
  if (!bounds) return null;
  if (!replay) {
    // collapsed: just the entry button
    return el("div", { class: "replay-bar", style: "justify-content:flex-end" },
      el("span", { class: "rp-time" }, "回看整个运行"),
      el("span", { class: "rp-btn", onclick: startReplay }, "▶ 回放运行"));
  }
  const pos = (replay.t - replay.t0) / Math.max(1, replay.t1 - replay.t0);
  const timeLabel = el("span", { class: "rp-time" },
    new Date(replay.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  const setT = (value) => {
    replay.t = replay.t0 + (Number(value) / 1000) * (replay.t1 - replay.t0);
    timeLabel.textContent = new Date(replay.t)
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  return el("div", { class: "replay-bar" },
    el("span", { class: "rp-btn", onclick: toggleReplayPlay }, replay.playing ? "❚❚" : "▶"),
    el("input", {
      type: "range", min: "0", max: "1000", value: String(Math.round(pos * 1000)),
      // A full render() would destroy this slider mid-drag: while dragging,
      // only pause + track the time label; re-render the board on release.
      onpointerdown: () => { replay.playing = false; },
      oninput: (e) => setT(e.target.value),
      onchange: (e) => { setT(e.target.value); render(); },
    }),
    timeLabel,
    el("span", { class: "rp-btn", onclick: stopReplay }, "✕ 实时"),
  );
}

let replayTimer = null;

function startReplay() {
  const bounds = replayBounds(state);
  if (!bounds) return;
  replay = { ...bounds, t: bounds.t0, playing: true };
  document.body.classList.add("replaying");
  scheduleTick();
  render();
}

function stopReplay() {
  replay = null;
  clearTimeout(replayTimer);
  document.body.classList.remove("replaying");
  render();
}

function toggleReplayPlay() {
  if (!replay) return;
  replay.playing = !replay.playing;
  if (replay.playing) scheduleTick();
  render();
}

function scheduleTick() {
  // Single pending tick, ever — rapid pause/play must not stack chains.
  clearTimeout(replayTimer);
  replayTimer = setTimeout(tickReplay, 100);
}

function tickReplay() {
  if (!replay || !replay.playing) return;
  // A full run replays in ~20 seconds regardless of real duration
  // (10 renders/second — full re-render per tick, keep it modest).
  const step = (replay.t1 - replay.t0) / 200;
  replay.t = Math.min(replay.t1, replay.t + step);
  if (replay.t >= replay.t1) replay.playing = false;
  render();
  if (replay.playing) scheduleTick();
}

// ---------------------------------------------------------------------------
// page assembly
// ---------------------------------------------------------------------------

function render() {
  if (!state) return;
  const s = replay ? stateAt(state, replay.t) : state;
  document.title = `Backlot · ${s.title}`;
  document.body.classList.toggle("first", firstPaint);
  firstPaint = false;
  app.innerHTML = "";
  app.append(renderSlate(s));
  app.append(renderRail(s));
  const replayBar = renderReplayBar(state);
  if (replayBar) app.append(replayBar);
  const drawer = renderDrawer(s);
  if (drawer) app.append(drawer);
  const awaitingNotice = renderAwaitingNotice(s);
  if (awaitingNotice) app.append(awaitingNotice);
  const noState = renderNoState(s);
  if (noState) app.append(noState);

  const main = el("div", { class: "main-col" });
  const approvalReview = renderApprovalReview(s);
  if (approvalReview) main.append(approvalReview);
  const script = renderScriptCard(s);
  if (script) main.append(script);
  const aside = el("aside", {});
  const decisions = renderDecisions(s);
  const activity = renderActivity(s);
  if (decisions) aside.append(decisions);
  if (activity) aside.append(activity);

  // Media sections live INSIDE the main column so a tall decisions rail
  // never pushes them below the fold — the column flows beside the rail.
  const storyboard = renderStoryboard(s);
  const found = renderFoundMedia(s);
  const renders = renderRenders(s);

  if (approvalReview || script || decisions || activity) {
    for (const section of [storyboard, found, renders]) {
      if (section) main.append(section);
    }
    const hasAside = Boolean(decisions || activity);
    app.append(el("div", { class: `board${hasAside ? "" : " solo"}` }, main, hasAside ? aside : null));
  } else {
    for (const section of [storyboard, found, renders]) {
      if (section) app.append(section);
    }
  }
}

// Defensive normalization (F-02): the server contract guarantees these
// fields, but a sparse/legacy payload must degrade, never crash the board.
function normalize(s) {
  s.pipeline = s.pipeline || { pipeline_type: "unknown", stages: [], known: false };
  s.stages = Array.isArray(s.stages) ? s.stages : [];
  for (const stage of s.stages) {
    stage.produces = Array.isArray(stage.produces) ? stage.produces : [];
  }
  s.artifacts = s.artifacts || {};
  s.media = s.media || {};
  s.media.renders = Array.isArray(s.media.renders) ? s.media.renders : [];
  s.media.snapshots = Array.isArray(s.media.snapshots) ? s.media.snapshots : [];
  s.media.music = Array.isArray(s.media.music) ? s.media.music : [];
  s.events = Array.isArray(s.events) ? s.events : [];
  if (s.storyboard && Array.isArray(s.storyboard.scenes)) {
    for (const c of s.storyboard.scenes) {
      c.takes = Array.isArray(c.takes) ? c.takes.filter((t) => t && typeof t === "object" && t.path) : [];
      c.media_history = c.media_history && typeof c.media_history === "object" ? c.media_history : {};
      c.media_history.images = Array.isArray(c.media_history.images) ? c.media_history.images.filter((t) => t && typeof t === "object" && t.path) : c.takes.filter((t) => mediaKind(t) === "image");
      c.media_history.videos = Array.isArray(c.media_history.videos) ? c.media_history.videos.filter((t) => t && typeof t === "object" && t.path) : c.takes.filter((t) => mediaKind(t) === "video");
      c.audio = Array.isArray(c.audio) ? c.audio : [];
      c.required_assets = Array.isArray(c.required_assets) ? c.required_assets : [];
    }
  } else {
    s.storyboard = null;
  }
  return s;
}

async function refresh() {
  state = normalize(await getJSON(`/api/project/${encodeURIComponent(projectId)}/state`));
  render();
}

refresh().catch((err) => {
  app.innerHTML = "";
  app.append(el("div", { class: "empty", style: "margin-top:80px" },
    el("div", { class: "big" }, "未找到项目"),
    el("div", {}, String(err))));
});
// ?static=1 disables the live feed (screenshots, static exports).
if (!new URLSearchParams(location.search).has("static")) {
  subscribe(`/api/project/${encodeURIComponent(projectId)}/events`, () => refresh().catch(console.error));
}
