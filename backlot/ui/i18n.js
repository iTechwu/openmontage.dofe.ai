// Chinese labels for values that come from pipeline manifests and event logs.
// User-authored titles, narration, and descriptions are intentionally kept as-is.

const PIPELINE_LABELS = {
  "animated-explainer": "动态解说",
  "talking-head": "口播视频",
  "screen-demo": "屏幕演示",
  "clip-factory": "批量短片",
  "podcast-repurpose": "播客再利用",
  cinematic: "电影感制作",
  animation: "动画制作",
  "character-animation": "角色动画",
  hybrid: "混合制作",
  "avatar-spokesperson": "虚拟主持人",
  "localization-dub": "本地化配音",
  "documentary-montage": "纪录片蒙太奇",
  "framework-smoke": "框架冒烟测试",
  unknown: "未知流水线",
};

const LABELS = {
  research: "调研", research_brief: "研究简报", proposal: "提案", proposal_packet: "制作方案",
  idea: "构思", brief: "创意简报", script: "脚本", scene_plan: "场景计划", scene: "场景",
  character_design: "角色设计", rig_plan: "绑定计划", assets: "素材", asset_manifest: "素材清单",
  edit: "剪辑", edit_decisions: "剪辑决策", compose: "合成", render_report: "渲染报告",
  final_review: "终审", publish: "发布", publish_log: "发布日志", decision_log: "决策日志",
  artifact: "产物", unknown: "未知", pending: "待运行", in_progress: "进行中",
  awaiting_human: "等待确认", completed: "已完成", failed: "失败", error: "错误",
  decision: "决策", pass: "通过", passed: "通过", suggestion: "建议", suggestions: "建议",
  nitpick: "细节", nitpicks: "细节", critical: "严重问题", high: "高", medium: "中", low: "低",
  provider_selection: "服务商选择", voice_selection: "语音选择", render_runtime_selection: "渲染引擎选择",
  composition_mode: "合成模式", approval_policy: "确认策略", music_selection: "音乐选择",
  visual_style: "视觉风格", asset_strategy: "素材策略", pipeline_selection: "流水线选择",
  text_card: "文字卡片", stat_card: "数据卡片", hero_title: "主标题", terminal_scene: "终端场景",
  bar_chart: "柱状图", line_chart: "折线图", pie_chart: "饼图", kpi_grid: "指标网格",
  progress_bar: "进度条", image: "图片", video: "视频", audio: "音频", narration: "旁白",
  animation: "动画",
};

const TOOL_LABELS = {
  tts_selector: "语音合成", image_selector: "图片生成", video_selector: "视频生成",
  music_gen: "音乐生成", video_compose: "视频合成", hyperframes_compose: "HyperFrames 合成",
  video_stitch: "视频拼接", audio_mixer: "音频混音", transcriber: "语音转文字",
  scene_detect: "场景检测", frame_sampler: "画面采样", diagram_gen: "图表生成", code_snippet: "代码片段",
};

const SHOT_LABELS = {
  extreme_wide: "超远景", wide: "远景", full: "全景", medium: "中景", medium_close_up: "中近景",
  close_up: "近景", extreme_close_up: "特写", static: "固定镜头", slow_push: "缓慢推进",
  push_in: "推进", pull_out: "拉远", pan: "横摇", tilt: "纵摇", tracking: "跟拍",
  handheld: "手持", crane: "升降", low_key: "低调光", high_key: "高调光", natural: "自然光", rim: "轮廓光",
};

export function pipelineLabel(value) {
  return PIPELINE_LABELS[String(value || "unknown")] || "自定义流水线";
}

export function toolLabel(value) {
  const key = String(value || "");
  return TOOL_LABELS[key] || (key ? "制作工具" : "");
}

export function label(value) {
  return LABELS[String(value || "unknown")] || "自定义字段";
}

export function shotLabel(value) {
  return SHOT_LABELS[String(value || "")] || "镜头参数";
}

export function formatToken(value, fallback = "") {
  if (value == null || value === "") return fallback;
  const key = String(value);
  return LABELS[key] || SHOT_LABELS[key] || key;
}
