#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const W = 1280;
const H = 720;
const C = {
  ink: "#111418", muted: "#5E6570", rule: "#C8CDD3", panel: "#EEF0F2",
  blue: "#1677D2", blueLight: "#DCEEFF", teal: "#087E72", tealLight: "#DDF3EF",
  red: "#B44536", redLight: "#F6E4E1", white: "#FFFFFF", black: "#000000",
};
const FONT = "Microsoft YaHei";

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i += 2) out[argv[i].replace(/^--/, "")] = argv[i + 1];
  if (!out.input || !out.output) throw new Error("usage: build_interview_deck.mjs --input deck-data.json --output deck.pptx");
  return out;
}

function addShape(slide, name, left, top, width, height, fill = "none", lineFill = "none", lineWidth = 0, geometry = "rect") {
  return slide.shapes.add({
    geometry, name, position: { left, top, width, height }, fill,
    line: { style: "solid", fill: lineFill, width: lineWidth },
  });
}

function addText(slide, name, text, left, top, width, height, size = 20, color = C.ink, bold = false, alignment = "left", typeface = FONT) {
  const box = addShape(slide, name, left, top, width, height);
  box.text = String(text);
  box.text.style = { typeface, fontSize: size, color, bold, alignment };
  return box;
}

function addHeader(slide, slideData, index, project) {
  addText(slide, `chapter-${index}`, `${slideData.chapter} / 0${(index % 3) + 1}`, 54, 34, 190, 24, 19, C.blue, true);
  addText(slide, `title-${index}`, slideData.title, 54, 70, 1110, 58, 47, C.ink, true);
  addText(slide, `number-${index}`, String(index + 1).padStart(2, "0"), 1182, 36, 46, 24, 19, C.muted, true, "right");
  addShape(slide, `header-rule-${index}`, 54, 139, 1172, 2, C.ink);
  addText(slide, `footer-${index}`, `${project.id} · ${project.status} · snapshot ${project.as_of}`, 54, 684, 1120, 18, 11, C.muted);
}

function addNotes(slide, slideData, input) {
  const sourceHash = input.source_hashes?.[slideData.source] || "unavailable";
  slide.speakerNotes.textFrame.setText([
    `[Sources]`,
    `- ${slideData.source} (sha256:${sourceHash})`,
    `- audit snapshot: ${input.generated_from.audit} (sha256:${input.generated_from.hashes.audit})`,
    `Status note: planned targets are not observed results.`,
  ]);
}

function chapterOpener(slide, s, accent, accentLight, index) {
  addShape(slide, `phase-band-${index}`, 54, 174, 15, 340, accent);
  addText(slide, `claim-${index}`, s.claim, 105, 184, 1010, 142, 48, C.ink, true);
  addText(slide, `phase-mark-${index}`, s.chapter, 1000, 344, 190, 86, 58, accent, true, "right");
  addShape(slide, `point-panel-${index}`, 105, 485, 1070, 134, accentLight);
  const colW = 330;
  s.points.forEach((point, i) => {
    addText(slide, `point-num-${index}-${i}`, `${i + 1}`, 135 + i * 350, 510, 38, 30, 19, accent, true);
    addText(slide, `point-${index}-${i}`, point, 178 + i * 350, 504, colW - 45, 68, 22, C.ink, true);
  });
}

function relatedWork(slide, s, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 164, 1115, 68, 26, C.muted, false);
  const rows = [
    ["结构证据", "局部/缺失", "PPTX 对象树 + 渲染"],
    ["忠实与视觉", "常由单一 Judge 覆盖", "专项 Oracle + 视觉 Oracle"],
    ["执行错误", "易混入低分", "ERROR 与 FAIL 类型隔离"],
    ["审计复现", "版本信息不完整", "Manifest + append-only 事件"],
  ];
  addText(slide, `matrix-head-a-${index}`, "比较维度", 54, 252, 210, 38, 22, C.muted, true);
  addText(slide, `matrix-head-b-${index}`, "公开基线常见边界", 285, 252, 360, 38, 22, C.red, true);
  addText(slide, `matrix-head-c-${index}`, "工业 Harness", 674, 252, 500, 38, 22, C.teal, true);
  rows.forEach((row, i) => {
    const y = 298 + i * 70;
    addShape(slide, `matrix-row-${index}-${i}`, 54, y, 1120, 58, i % 2 ? C.white : C.panel, C.rule, 1);
    addText(slide, `matrix-a-${index}-${i}`, row[0], 72, y + 15, 190, 28, 22, C.ink, true);
    addText(slide, `matrix-b-${index}-${i}`, row[1], 285, y + 15, 360, 28, 22, C.muted);
    addText(slide, `matrix-c-${index}-${i}`, row[2], 674, y + 15, 480, 28, 22, C.ink);
  });
  addText(slide, `note-${index}`, "PPTEval：模型评分受凭证阻塞；SlidesBench：客观 smoke 已完成，其余评分受公开缺失项与凭证限制。", 54, 590, 1110, 28, 19, C.red, true);
  addText(slide, `dataset-note-${index}`, "数据侧：15 个公开候选已审计；优先 UniPPTBench、SlideAudit 与逐文件许可 PPTX，中文企业金标仍缺。", 54, 624, 1110, 26, 18, C.teal, true);
}

function pdms(slide, s, input, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 42, 26, C.muted);
  addShape(slide, `outer-${index}`, 54, 230, 1120, 218, C.redLight, C.red, 2);
  addText(slide, `m-label-${index}`, "外层：不可补偿且高置信", 82, 250, 300, 28, 22, C.red, true);
  addText(slide, `formula-${index}`, "100 × ∏(M_base) × ∏(M_scene)", 83, 304, 490, 52, 32, C.ink, true, "left", "Cambria Math");
  addText(slide, `times-${index}`, "×", 570, 312, 45, 45, 31, C.red, true, "center");
  addShape(slide, `inner-${index}`, 620, 275, 515, 126, C.white, C.rule, 1);
  addText(slide, `inner-label-${index}`, "内层：允许补偿的性能", 646, 292, 450, 24, 22, C.teal, true);
  addText(slide, `inner-formula-${index}`, "λ·A_base + (1−λ)·A_scene", 646, 333, 450, 40, 28, C.ink, true, "left", "Cambria Math");
  const rules = ["M ∈ {0, 0.5, 1}", "同一缺陷不得重复计罚", "ERROR 不进入公式"];
  rules.forEach((rule, i) => {
    addShape(slide, `rule-box-${index}-${i}`, 54 + i * 374, 500, 350, 92, i === 1 ? C.tealLight : C.panel);
    addText(slide, `rule-num-${index}-${i}`, `${i + 1}`, 76 + i * 374, 519, 35, 24, 19, i === 1 ? C.teal : C.blue, true);
    addText(slide, `rule-${index}-${i}`, rule, 118 + i * 374, 514, 260, 48, 22, C.ink, true);
  });
  addText(slide, `lambda-${index}`, "λ v1：文字 0.55 · 总结 0.40 · 多模态 0.45；待人工金标校准", 54, 621, 1120, 30, 22, C.muted);
}

function architecture(slide, s, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 50, 26, C.muted);
  const layers = ["EvaluationService", "RunSupervisor", "ProfileCompiler + DagScheduler", "Composite / Leaf Oracle", "Calibrator + PPT-PDMS", "DecisionPolicy", "Evidence · Audit · Feedback"];
  layers.forEach((layer, i) => {
    const y = 230 + i * 56;
    const left = 130 + i * 45;
    const width = 930 - i * 90;
    const fill = i === 1 ? C.blueLight : (i === 3 ? C.tealLight : C.panel);
    addShape(slide, `layer-${index}-${i}`, left, y, width, 42, fill, i === 1 ? C.blue : C.rule, i === 1 ? 2 : 1);
    addText(slide, `layer-text-${index}-${i}`, `${String(i + 1).padStart(2, "0")}  ${layer}`, left + 18, y + 9, width - 36, 24, 22, C.ink, i === 1 || i === 3, "center");
  });
  addText(slide, `arch-note-${index}`, "每层只完成既定工作；模型不能生成 DAG、修改规则或决定发布。", 160, 635, 960, 30, 22, C.red, true, "center");
}

function fallback(slide, s, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 50, 26, C.muted);
  const items = [
    ["01", "编译", "强制注入 Baseline", C.blueLight, C.blue],
    ["02", "执行", "基础与专项解耦", C.panel, C.ink],
    ["03", "专项异常", "ERROR / 缺证据", C.redLight, C.red],
    ["04", "收束", "S_base + REVIEW", C.tealLight, C.teal],
  ];
  items.forEach((item, i) => {
    const x = 54 + i * 285;
    addShape(slide, `fallback-node-${index}-${i}`, x, 252, 248, 170, item[3], item[4], 2);
    addText(slide, `fallback-num-${index}-${i}`, item[0], x + 22, 274, 44, 25, 16, item[4], true);
    addText(slide, `fallback-name-${index}-${i}`, item[1], x + 22, 316, 200, 32, 28, C.ink, true);
    addText(slide, `fallback-desc-${index}-${i}`, item[2], x + 22, 363, 200, 34, 22, C.muted);
    if (i < items.length - 1) addText(slide, `fallback-arrow-${index}-${i}`, "→", x + 252, 316, 29, 32, 25, C.muted, true, "center");
  });
  addShape(slide, `baseline-band-${index}`, 54, 466, 1103, 106, C.ink);
  addText(slide, `baseline-label-${index}`, "不可删除的基础质量子图", 82, 486, 420, 31, 28, C.white, true);
  addText(slide, `baseline-dims-${index}`, "内容 · 叙事 · 视觉 · 技术 · 可编辑性 · 兼容性 · 可访问性", 82, 530, 980, 28, 22, C.white);
  addText(slide, `fallback-state-${index}`, "FULL / DEGRADED / BASE_ONLY / UNASSESSABLE", 54, 612, 1103, 30, 22, C.blue, true, "center");
}

function auditChain(slide, s, input, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 50, 26, C.muted);
  const ids = ["REQ", "ADR", "ORC+IMP", "TST", "EXP+RUN", "REL"];
  addShape(slide, `chain-line-${index}`, 130, 324, 950, 4, C.rule);
  ids.forEach((id, i) => {
    const x = 90 + i * 196;
    addShape(slide, `chain-node-${index}-${i}`, x, 280, 96, 96, i === 4 ? C.blue : C.ink);
    addText(slide, `chain-id-${index}-${i}`, id, x, 312, 96, 28, id.includes("+") ? 15 : 19, C.white, true, "center");
    addText(slide, `chain-caption-${index}-${i}`, ["需求", "取舍", "判断/实现", "验证", "实验/运行", "发布"][i], x - 6, 393, 108, 25, 22, C.muted, true, "center");
  });
  const manifest = ["input/output hash", "Git · container · fonts", "model · prompt · profile", "seed · cost · DAG"];
  manifest.forEach((item, i) => addText(slide, `manifest-${index}-${i}`, `• ${item}`, 125 + (i % 2) * 535, 490 + Math.floor(i / 2) * 48, 475, 30, 22, C.ink, i === 0));
  addText(slide, `append-only-${index}`, "机器结果与人工复核分别追加；修订用 supersedes，不覆盖历史。", 90, 611, 1060, 30, 22, C.red, true, "center");
}

function evaluationOpener(slide, s, input, index) {
  chapterOpener(slide, s, C.teal, C.tealLight, index);
  addText(slide, `planned-label-${index}`, "门槛状态：预注册，尚非实测", 835, 166, 340, 24, 20, C.red, true, "right");
}

function degradation(slide, s, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 48, 26, C.muted);
  const headers = ["场景", "专项成功", "专项部分错误", "专项缺失/全错"];
  const rows = [
    ["文字生成", "FULL", "DEGRADED + REVIEW", "BASE_ONLY + REVIEW"],
    ["项目总结", "FULL", "DEGRADED + REVIEW", "BASE_ONLY + REVIEW"],
    ["多模态", "FULL", "DEGRADED + REVIEW", "BASE_ONLY + REVIEW"],
    ["成品 PPT", "FULL (base)", "N/A", "N/A"],
  ];
  const widths = [220, 230, 330, 340];
  let x = 54;
  headers.forEach((header, i) => {
    addShape(slide, `deg-head-${index}-${i}`, x, 248, widths[i], 58, C.ink);
    addText(slide, `deg-head-text-${index}-${i}`, header, x + 12, 266, widths[i] - 24, 25, 22, C.white, true, "center");
    x += widths[i];
  });
  rows.forEach((row, r) => {
    let rx = 54; const y = 306 + r * 66;
    row.forEach((cell, c) => {
      const fill = c === 0 ? C.panel : (cell.includes("BASE") ? C.redLight : cell.includes("DEGRADED") ? C.blueLight : C.white);
      addShape(slide, `deg-cell-${index}-${r}-${c}`, rx, y, widths[c], 66, fill, C.rule, 1);
      addText(slide, `deg-cell-text-${index}-${r}-${c}`, cell, rx + 9, y + 21, widths[c] - 18, 28, 22, cell.includes("BASE") ? C.red : C.ink, c === 0 || cell === "FULL", "center");
      rx += widths[c];
    });
  });
  addText(slide, `deg-rule-${index}`, "不变量：前三场景无论专项如何失败，base_score 与基础证据都必须保留；full_score 为空。", 54, 600, 1120, 42, 22, C.red, true, "center");
}

function flywheel(slide, s, input, index) {
  addText(slide, `claim-${index}`, s.claim, 54, 158, 1110, 50, 26, C.muted);
  const steps = ["生产反馈", "候选生成", "校准回放", "人工审批", "Shadow", "灰度发布"];
  steps.forEach((step, i) => {
    const x = 54 + i * 190;
    const active = i === 3;
    addShape(slide, `fly-node-${index}-${i}`, x, 260, 160, 94, active ? C.tealLight : C.panel, active ? C.teal : C.rule, active ? 2 : 1);
    addText(slide, `fly-num-${index}-${i}`, `${i + 1}`, x + 14, 275, 34, 22, 19, active ? C.teal : C.blue, true);
    addText(slide, `fly-text-${index}-${i}`, step, x + 14, 311, 132, 26, 22, C.ink, true, "center");
    if (i < steps.length - 1) addText(slide, `fly-arrow-${index}-${i}`, "→", x + 162, 292, 26, 28, 20, C.muted, true, "center");
  });
  addText(slide, `shadow-${index}`, "Shadow：≥2 周或 ≥1,000 份，以较晚满足者为准", 54, 402, 1120, 36, 26, C.ink, true, "center");
  const targets = input.acceptance_targets.slice(0, 5);
  const targetLabels = {
    human_objective_alpha: "客观人评 α",
    human_pairwise_agreement: "人工成对一致率",
    high_confidence_block_precision: "高置信拦截精度",
    severe_escape_rate_upper_95: "严重误放行率 95% 上界",
    gate_repeatability: "门禁重复一致率",
  };
  targets.forEach((target, i) => {
    const x = 54 + i * 224;
    const operator = target.operator === ">=" ? "≥" : target.operator === "<=" ? "≤" : target.operator;
    addText(slide, `target-value-${index}-${i}`, `${operator}${Number(target.target).toFixed(2)}`, x, 488, 190, 40, 28, C.blue, true, "center");
    addText(slide, `target-name-${index}-${i}`, targetLabels[target.metric] || target.metric.replaceAll("_", " "), x, 536, 190, 48, 22, C.muted, false, "center");
  });
  addText(slide, `close-${index}`, "可演进，但不能绕过冻结集、Shadow 与人类治理。", 54, 622, 1120, 32, 24, C.teal, true, "center");
}

async function writeBlob(filePath, blob) {
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

export async function buildDeck(inputFile, outputFile) {
  const inputPath = path.resolve(inputFile);
  const outputPath = path.resolve(outputFile);
  const input = JSON.parse(await fs.readFile(inputPath, "utf8"));
  if (!Array.isArray(input.slides) || input.slides.length !== 9) throw new Error("deck input must contain exactly nine slides");

  const presentation = Presentation.create({ slideSize: { width: W, height: H } });
  input.slides.forEach((s, index) => {
    const slide = presentation.slides.add();
    slide.background.fill = C.white;
    addHeader(slide, s, index, input.project);
    addNotes(slide, s, input);
    [
      () => chapterOpener(slide, s, C.blue, C.blueLight, index),
      () => relatedWork(slide, s, index),
      () => pdms(slide, s, input, index),
      () => architecture(slide, s, index),
      () => fallback(slide, s, index),
      () => auditChain(slide, s, input, index),
      () => evaluationOpener(slide, s, input, index),
      () => degradation(slide, s, index),
      () => flywheel(slide, s, input, index),
    ][index]();
  });

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);

  const previewDir = path.join(path.dirname(outputPath), "deck-preview");
  await fs.mkdir(previewDir, { recursive: true });
  for (const [index, slide] of presentation.slides.items.entries()) {
    const png = await presentation.export({ slide, format: "png", scale: 1 });
    await writeBlob(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.png`), png);
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.layout.json`), await layout.text());
  }
  await writeBlob(path.join(previewDir, "montage.webp"), await presentation.export({ format: "webp", montage: true, scale: 1 }));

  const bytes = await fs.readFile(outputPath);
  const deckHash = crypto.createHash("sha256").update(bytes).digest("hex");
  const manifestPath = path.join(path.dirname(outputPath), "build-manifest.json");
  let manifest = {};
  try { manifest = JSON.parse(await fs.readFile(manifestPath, "utf8")); } catch { /* optional */ }
  manifest.outputs = { ...(manifest.outputs || {}), [path.basename(outputPath)]: deckHash };
  manifest.presentation = { slide_count: 9, chapters: ["调研", "开发", "评测"], renderer: "@oai/artifact-tool", preview_dir: "deck-preview" };
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n", "utf8");
  return { outputPath, deckHash, slideCount: 9, previewDir };
}

if (typeof process !== "undefined" && process.argv?.[1]?.endsWith("build_interview_deck.mjs")) {
  const args = parseArgs(process.argv);
  buildDeck(args.input, args.output)
    .then(({ outputPath }) => console.log(`wrote ${outputPath}`))
    .catch((error) => { console.error(error); process.exitCode = 1; });
}
