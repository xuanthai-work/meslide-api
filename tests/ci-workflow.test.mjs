import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("FastAPI CI uses the same isolated suite runner as check:fastapi", async () => {
  const workflow = await readFile(
    path.join(repoRoot, ".github/workflows/test.yml"),
    "utf8",
  );
  const pkg = JSON.parse(
    await readFile(path.join(repoRoot, "package.json"), "utf8"),
  );

  assert.match(pkg.scripts["test:fastapi"], /tests\/run_suite\.py/);
  assert.match(pkg.scripts["check:fastapi"], /test:fastapi/);
  assert.match(workflow, /check:fastapi/);
  assert.match(workflow, /uv run --locked python tests\/run_suite\.py --verbose --tb=short/);
  assert.doesNotMatch(workflow, /python -m pytest/);
});
