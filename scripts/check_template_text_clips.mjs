#!/usr/bin/env node
/**
 * Standing regression guard for template V2 body copy.
 *
 * Why this exists
 * ---------------
 * A description box clipped by ``fitTextToBox`` cuts its last line on the canvas
 * (``verticalGlyphClipBounds``) and in PDF/HTML export. Nothing in the build
 * catches that: a template edit that shortens a box, or a typography pass that
 * raises a font without growing its box, ships silently. This script measures
 * every description element in every V2 template and fails when one regresses.
 *
 * It is the source of truth for *clipping*, so it drives the real fitter --
 * ``gslide-web/components/slide-editor/text/template-v2-text-fit.mjs`` -- rather
 * than a re-implementation that can drift from it.
 *
 * The snake_case trap
 * -------------------
 * The fitter reads ``font.lineHeight`` and ``font.letterSpacing``; the template
 * JSON stores ``line_height`` and ``letter_spacing``. Handing raw records to
 * ``fitTextToBox`` silently drops both, which drops the tracking (making every
 * line read wider than it paints) and falls back to a different line height.
 * ``normalizeFont`` below mirrors the render path's ``fontFromRecord`` so this
 * script measures what the user actually sees.
 *
 * Usage
 * -----
 *   node gslide-api/scripts/check_template_text_clips.mjs
 *   node gslide-api/scripts/check_template_text_clips.mjs --bottlenecks
 *   node gslide-api/scripts/check_template_text_clips.mjs --templates nova,pulse
 *   node gslide-api/scripts/check_template_text_clips.mjs --base-root <dir>
 *   node gslide-api/scripts/check_template_text_clips.mjs --update-baseline
 *
 * Exit status: 0 clean, 1 regression (new clip / new parent overflow / more
 * under-sized elements than the baseline), 2 bad usage.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const DEFAULT_ROOT = path.join(REPO_ROOT, "gslide-api", "templates");
const DEFAULT_BASELINE = path.join(HERE, "check_template_text_clips.baseline.json");
const FITTER = path.join(
  REPO_ROOT,
  "gslide-web",
  "components",
  "slide-editor",
  "text",
  "template-v2-text-fit.mjs",
);

/** Element names below this designed size count as under-sized body copy. */
const FONT_TRIGGER = 13.5;
/**
 * The copy this gate owns: every body-text box whose name marks it as descriptive
 * or as a lead paragraph. Descriptions alone left the lead boxes -- a bare
 * ``summary`` is the largest text on many layouts -- outside the gate entirely,
 * so a font change there could clip with the report still reading zero.
 *
 * The lead names match whole, not by substring: ``summary_label`` is a metric
 * card's caption and ``summary_change`` a delta chip, both sized as labels by
 * design, and folding them in would report their tight boxes as body-copy
 * clipping. Bare ``summary`` is the name the lead tier actually uses.
 */
const MEASURED_NAME = /description|^(summary|introductory_summary|lead_paragraph)$/i;
/** Headroom the planner leaves over the measured copy height; mirrored here so
 *  the bottleneck report speaks the same units the normalizer reasons in. */
const HEIGHT_MARGIN = 1.08;

const { fitTextToBox, floorFontPx } = await import(pathToFileUrl(FITTER));

function pathToFileUrl(p) {
  return new URL(`file:///${p.replace(/\\/g, "/")}`).href;
}

// --- Font normalization (mirrors fontFromRecord) -------------------------

const DEFAULT_FONT = {
  family: "Poppins",
  size: 18,
  color: "#111827",
  bold: false,
  italic: false,
  underline: false,
  lineHeight: 1.15,
  letterSpacing: 0,
};

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function normalizeFont(source, base) {
  const fallback = base ?? DEFAULT_FONT;
  const raw = source ?? {};
  return {
    family: typeof raw.family === "string" ? raw.family : fallback.family,
    size: num(raw.size) ?? fallback.size,
    color: typeof raw.color === "string" ? raw.color : fallback.color,
    bold: typeof raw.bold === "boolean" ? raw.bold : fallback.bold,
    italic: typeof raw.italic === "boolean" ? raw.italic : fallback.italic,
    underline: typeof raw.underline === "boolean" ? raw.underline : fallback.underline,
    lineHeight: num(raw.line_height) ?? num(raw.lineHeight) ?? fallback.lineHeight,
    letterSpacing: num(raw.letter_spacing) ?? num(raw.letterSpacing) ?? fallback.letterSpacing,
  };
}

// --- Geometry (mirrors the container sizes the normalizer reasons over) ---

const readSize = (node) => (node && typeof node.size === "object" ? node.size : null);
const heightOf = (node) => num(readSize(node)?.height);
const positionY = (node) => num(node?.position?.y) ?? 0;
const flexChildren = (parent) => (Array.isArray(parent?.children) ? parent.children.filter(Boolean) : []);

/**
 * Vertical room an element may occupy without leaving its parent, or null when
 * the parent does not constrain its children (components have no declared size).
 */
function freeHeight(parent, element) {
  if (!parent) return null;
  const parentHeight = heightOf(parent);
  if (!(parentHeight > 0)) return null;

  if (parent.type === "flex" && flexChildren(parent).length) {
    if (parent.direction === "row") {
      // Children sit side by side, so a taller one does not push the others
      // down: it may use the row's whole height.
      return parentHeight - positionY(element);
    }
    const gap = num(parent.gap) ?? 0;
    const siblings = flexChildren(parent);
    const occupied = siblings.reduce((sum, child) => sum + (heightOf(child) ?? 0), 0);
    return parentHeight - (occupied + gap * Math.max(0, siblings.length - 1)) + (heightOf(element) ?? 0);
  }
  return parentHeight - positionY(element);
}

// --- Measurement ---------------------------------------------------------

/**
 * Copy height at the shrink floor -- what a box must hold to never clip.
 *
 * `widthSafety` narrows the column the plan is measured against. The editor
 * measures glyphs through a canvas that runs a little wider than the fitter's
 * unit model, so planning to the last fraction of a line can still wrap on
 * screen. Callers that are deciding how tall to make a box pass < 1; the
 * regression gate itself always measures at 1 so it reports what ships.
 */
function heightAtFloor(runs, width, widthSafety = 1) {
  const designed = runs.reduce((max, run) => Math.max(max, run.font?.size ?? 0), 0);
  if (!(designed > 0)) return null;
  const scale = floorFontPx(designed) / designed;
  const floored = runs.map((run) => ({ ...run, font: { ...run.font, size: run.font.size * scale } }));
  return fitTextToBox(floored, { width: width * widthSafety, height: Number.MAX_SAFE_INTEGER }).height;
}

function walk(node, parent, visit) {
  if (Array.isArray(node)) {
    for (const child of node) walk(child, parent, visit);
    return;
  }
  if (!node || typeof node !== "object") return;
  visit(node, parent);
  const next = ["group", "flex", "container"].includes(node.type) ? node : parent;
  for (const key of ["components", "elements", "children"]) {
    if (key in node) walk(node[key], next, visit);
  }
}

function measure(templateRoot, template, widthSafety = 1) {
  const file = path.join(templateRoot, template, "template.json");
  if (!fs.existsSync(file)) return null;
  const data = JSON.parse(fs.readFileSync(file, "utf8"));
  const rows = [];

  for (const layout of data.layouts ?? []) {
    const seen = new Map();
    walk(layout.components ?? [], null, (node, parent) => {
      if (node.type !== "text" || !MEASURED_NAME.test(node.name ?? "")) return;
      const base = normalizeFont(node.font, null);
      const width = num(readSize(node)?.width);
      const height = num(readSize(node)?.height);
      if (!(base.size > 0) || !(width > 0) || !(height > 0)) return;
      const runs = (node.runs ?? []).map((run) => ({ text: run.text ?? "", font: normalizeFont(run.font, base) }));
      if (!runs.length) return;

      const index = seen.get(node.name) ?? 0;
      seen.set(node.name, index + 1);

      const fitted = fitTextToBox(runs, { width, height });
      const needed = heightAtFloor(runs, width, widthSafety);
      const room = freeHeight(parent, node);
      const parentHeight = heightOf(parent);

      rows.push({
        key: `${layout.id}|${node.name}|${index}`,
        template,
        layout: layout.id,
        name: node.name,
        index,
        font: +base.size.toFixed(2),
        width: +width.toFixed(2),
        height: +height.toFixed(2),
        renderedPx: +(base.size * fitted.fontScale).toFixed(2),
        lines: fitted.lines.length,
        content: +fitted.height.toFixed(2),
        clipped: fitted.height > height + 0.01,
        parent: parent?.name ?? null,
        parentHeight: parentHeight === null ? null : +parentHeight.toFixed(2),
        room: room === null ? null : +room.toFixed(2),
        need: needed === null ? null : +(needed * HEIGHT_MARGIN).toFixed(2),
        underSized: base.size < FONT_TRIGGER,
        text: runs.map((run) => run.text).join("").replace(/\s+/g, " ").trim().slice(0, 48),
      });
    });
  }
  return rows;
}

// --- Text overlap --------------------------------------------------------

/**
 * Keys of every pair of text nodes whose boxes intersect, per layout.
 *
 * These layouts deliberately overlap cards, so an absolute count says nothing --
 * the only meaningful question is whether a pair appeared that was not there
 * before. Callers therefore diff this against the base root or the baseline.
 */
function textOverlaps(templateRoot, template) {
  const file = path.join(templateRoot, template, "template.json");
  if (!fs.existsSync(file)) return null;
  const data = JSON.parse(fs.readFileSync(file, "utf8"));
  const keys = new Set();

  const visit = (node, offsetX, offsetY, out) => {
    if (Array.isArray(node)) {
      for (const child of node) visit(child, offsetX, offsetY, out);
      return;
    }
    if (!node || typeof node !== "object") return;
    const size = readSize(node) ?? {};
    const x = offsetX + (num(node.position?.x) ?? 0);
    const y = offsetY + (num(node.position?.y) ?? 0);
    if (node.type === "text") {
      const width = num(size.width);
      const height = num(size.height);
      if (width > 0 && height > 0) out.push({ name: node.name ?? "?", x, y, width, height });
    }
    const key = ["children", "elements", "components"].find((k) => Array.isArray(node[k]));
    if (!key) return;
    for (const child of node[key]) visit(child, x, y, out);
  };

  for (const layout of data.layouts ?? []) {
    const boxes = [];
    visit(layout, 0, 0, boxes);
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i];
        const b = boxes[j];
        const wide = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
        const tall = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
        if (wide > 1 && tall > 1) {
          keys.add(`${layout.id}|${[a.name, b.name].sort().join("+")}`);
        }
      }
    }
  }
  return [...keys].sort();
}

// --- Baseline ------------------------------------------------------------

function readBaseline(file) {
  if (!fs.existsSync(file)) return null;
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return data?.templates ? data : null;
  } catch {
    return null;
  }
}

function buildBaseline(perTemplate, perTemplateOverlaps) {
  const templates = {};
  for (const [name, rows] of perTemplate) {
    templates[name] = {
      underSized: rows.filter((row) => row.underSized).length,
      clipped: rows.filter((row) => row.clipped).length,
      clippedKeys: rows.filter((row) => row.clipped).map((row) => row.key).sort(),
      overlapKeys: perTemplateOverlaps.get(name) ?? [],
    };
  }
  return { version: 1, source: "gslide-api/templates", templates };
}

// --- Reporting -----------------------------------------------------------

function analyse(perTemplate, options, overlaps = new Map()) {
  const problems = [];
  const warnings = [];
  const rows = [...perTemplate.values()].flat();

  const clipped = rows.filter((row) => row.clipped);
  const underSized = rows.filter((row) => row.underSized);
  const parentOverflow = rows.filter((row) => row.room !== null && row.content > row.room + 0.01);
  const bottlenecks = underSized
    .filter((row) => row.need !== null && (row.room === null ? row.need > row.height : row.need > row.room + 0.5))
    .sort((a, b) => (b.need - Math.min(b.height, b.room ?? Infinity)) - (a.need - Math.min(a.height, a.room ?? Infinity)));

  const baseline = options.baseline ? readBaseline(options.baseline) : null;
  if (baseline) {
    for (const [name, rows_] of perTemplate) {
      const known = baseline.templates?.[name];
      if (!known) {
        warnings.push(`baseline has no entry for template "${name}" -- run --update-baseline`);
        continue;
      }
      const knownKeys = new Set(known.clippedKeys ?? []);
      for (const row of rows_) {
        if (row.clipped && !knownKeys.has(row.key)) {
          problems.push(`NEW-CLIP ${row.template} ${row.layout} ${row.name}[${row.index}] ${row.content}px > ${row.height}px "${row.text}"`);
        }
      }
      const nowUnder = rows_.filter((row) => row.underSized).length;
      if (nowUnder > (known.underSized ?? 0)) {
        problems.push(`NEW-UNDER-SIZED ${name}: ${known.underSized} -> ${nowUnder}`);
      }
      const knownOverlaps = new Set(known.overlapKeys ?? []);
      for (const pair of overlaps.get(name) ?? []) {
        if (!knownOverlaps.has(pair)) problems.push(`NEW-OVERLAP ${name} ${pair}`);
      }
    }
  } else {
    warnings.push(`no baseline at ${options.baseline} -- reporting only, run --update-baseline to freeze one`);
  }

  const overlapCount = [...overlaps.values()].reduce((n, keys) => n + keys.length, 0);
  return { clipped, underSized, parentOverflow, bottlenecks, problems, warnings, baseline, overlapCount };
}

function report(result, perTemplate, options) {
  const out = options.json ? [] : null;
  const say = (line) => (out ? out.push(line) : console.log(line));

  if (!options.json) {
    say("");
    for (const [name, rows] of perTemplate) {
      const clipped = rows.filter((row) => row.clipped).length;
      const under = rows.filter((row) => row.underSized).length;
      say(
        `  ${name.padEnd(12)} elements ${String(rows.length).padStart(3)}   ` +
          `under-sized ${String(under).padStart(3)}   clipped ${String(clipped).padStart(3)}`,
      );
    }
    say("");
  }

  const line = (label, value) => say(`${label.padEnd(30)}: ${value}`);
  line("text boxes measured", [...perTemplate.values()].reduce((n, r) => n + r.length, 0));
  line(`under-sized (< ${FONT_TRIGGER}px)`, result.underSized.length);
  line("clipped boxes", result.clipped.length);
  line("boxes past parent room", result.parentOverflow.length);
  line("parent-capped (need > room)", result.bottlenecks.length);
  line("overlapping text pairs", result.overlapCount);

  if (options.bottlenecks && result.bottlenecks.length) {
    const byLayout = new Map();
    for (const row of result.bottlenecks) {
      const key = `${row.template}|${row.layout}`;
      const group = byLayout.get(key) ?? { row, count: 0, worst: 0, parents: new Set() };
      const ceiling = row.room === null ? row.height : Math.min(row.height, row.room);
      group.count += 1;
      group.worst = Math.max(group.worst, row.need - ceiling);
      if (row.parent) group.parents.add(`${row.parent}(h=${row.parentHeight})`);
      byLayout.set(key, group);
    }
    say("");
    say(`parent-capped layouts (${byLayout.size}) -- each needs its container grown before the copy can reach ${FONT_TRIGGER}px:`);
    const ordered = [...byLayout.entries()].sort((a, b) => b[1].worst - a[1].worst);
    for (const [key, group] of ordered) {
      const [template, layout] = key.split("|");
      say(
        `  ${template.padEnd(10)} ${layout.slice(0, 44).padEnd(44)} blocked ${String(group.count).padStart(2)}  ` +
          `short by ${group.worst.toFixed(1).padStart(6)}px  via ${[...group.parents].join(", ")}`,
      );
    }
    if (options.detail) {
      say("");
      for (const row of result.bottlenecks) {
        const room = row.room === null ? "unbounded" : `${row.room}`;
        say(
          `  ${row.template.padEnd(10)} ${row.layout.slice(0, 40).padEnd(40)} ${row.name.padEnd(24)} ` +
            `h=${row.height} room=${room} need=${row.need} parent=${row.parent} "${row.text}"`,
        );
      }
    }
  }

  for (const warning of result.warnings) say(`warning: ${warning}`);
  for (const problem of result.problems) say(`FAIL: ${problem}`);
  if (!result.problems.length) say("OK: no new clipping, no new parent overflow, no new under-sized elements");

  if (options.json) process.stdout.write(JSON.stringify({ perTemplate: Object.fromEntries(perTemplate), result }, null, 2) + "\n");
}

// --- CLI -----------------------------------------------------------------

function parseArgs(argv) {
  const options = {
    root: DEFAULT_ROOT,
    baseRoot: null,
    baseline: DEFAULT_BASELINE,
    templates: null,
    updateBaseline: false,
    bottlenecks: false,
    json: false,
    widthSafety: 1,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--root") options.root = path.resolve(argv[++i]);
    else if (arg === "--base-root") options.baseRoot = path.resolve(argv[++i]);
    else if (arg === "--baseline") options.baseline = path.resolve(argv[++i]);
    else if (arg === "--templates") options.templates = argv[++i].split(",").map((t) => t.trim()).filter(Boolean);
    else if (arg === "--update-baseline") options.updateBaseline = true;
    else if (arg === "--bottlenecks") options.bottlenecks = true;
    else if (arg === "--detail") options.detail = true;
    else if (arg === "--json") options.json = true;
    else if (arg === "--width-safety") options.widthSafety = Number(argv[++i]);
    else if (arg === "-h" || arg === "--help") options.help = true;
    else {
      console.error(`unknown option: ${arg}`);
      process.exit(2);
    }
  }
  return options;
}

const HELP = `Measure description-box clipping across template V2 layouts.

  --root <dir>         template root (default gslide-api/templates)
  --base-root <dir>    also report what changed against a second template root
  --baseline <file>    baseline JSON to compare against
  --update-baseline    write the current measurement as the baseline
  --templates a,b,c    limit to these template folders
  --bottlenecks        list elements whose parent cannot host the needed height
  --width-safety <n>   narrow the column the needed height is measured against
                       (< 1 makes the plan conservative; clipping is still
                       reported at the full width)
  --json               machine-readable output
`;

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(HELP);
    return 0;
  }
  if (!fs.existsSync(FITTER)) {
    console.error(`fitter not found: ${FITTER}`);
    return 2;
  }

  const templates =
    options.templates ??
    fs
      .readdirSync(options.root, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(options.root, entry.name, "template.json")))
      .map((entry) => entry.name)
      .sort();

  const perTemplate = new Map();
  const overlaps = new Map();
  for (const name of templates) {
    const rows = measure(options.root, name, options.widthSafety);
    if (!rows) continue;
    perTemplate.set(name, rows);
    overlaps.set(name, textOverlaps(options.root, name) ?? []);
  }
  if (!perTemplate.size) {
    console.error(`no template.json found under ${options.root}`);
    return 2;
  }

  if (options.updateBaseline) {
    const baseline = buildBaseline(perTemplate, overlaps);
    fs.writeFileSync(options.baseline, JSON.stringify(baseline, null, 2) + "\n", "utf8");
    const clipped = [...perTemplate.values()].reduce((n, rows) => n + rows.filter((r) => r.clipped).length, 0);
    console.log(`baseline written: ${options.baseline}`);
    console.log(`  ${perTemplate.size} templates, ${clipped} clipped boxes recorded as known`);
    return 0;
  }

  if (options.baseRoot) {
    const regressions = [];
    for (const [name, rows] of perTemplate) {
      const before = measure(options.baseRoot, name);
      if (!before) continue;
      const beforeByKey = new Map(before.map((row) => [row.key, row]));
      for (const row of rows) {
        const old = beforeByKey.get(row.key);
        if (!old) continue;
        if (row.clipped && !old.clipped) {
          regressions.push(`NEW-CLIP ${name} ${row.layout} ${row.name}[${row.index}] ${row.content}px > ${row.height}px "${row.text}"`);
        }
        if (row.room !== null && row.content > row.room + 0.01 && old.room !== null && old.content <= old.room + 0.01) {
          regressions.push(`NEW-OVERFLOW ${name} ${row.layout} ${row.name}[${row.index}] content ${row.content} > room ${row.room} "${row.text}"`);
        }
      }
      const beforeOverlaps = new Set(textOverlaps(options.baseRoot, name) ?? []);
      for (const pair of overlaps.get(name) ?? []) {
        if (!beforeOverlaps.has(pair)) regressions.push(`NEW-OVERLAP ${name} ${pair}`);
      }
    }
    const result = analyse(perTemplate, { ...options, baseline: null }, overlaps);
    report({ ...result, problems: regressions }, perTemplate, options);
    return regressions.length ? 1 : 0;
  }

  const result = analyse(perTemplate, options, overlaps);
  report(result, perTemplate, options);
  return result.problems.length ? 1 : 0;
}

process.exit(main());
