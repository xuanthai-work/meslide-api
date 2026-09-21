import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("api Dockerfiles use start-api.js and do not copy Next standalone", async () => {
  const api = await readFile(path.join(root, "Dockerfile"), "utf8");
  const apiDev = await readFile(path.join(root, "Dockerfile.dev"), "utf8");
  assert.match(api, /start-api\.js/);
  assert.match(apiDev, /start-api\.js/);
  assert.doesNotMatch(api, /\.next-build\/standalone/);
  assert.doesNotMatch(apiDev, /\.next-build\/standalone/);
  assert.match(api, /COPY servers\/fastapi/);
  assert.match(await readFile(path.join(root, "scripts/start-api.js"), "utf8"), /server\.py/);
  assert.doesNotMatch(await readFile(path.join(root, "scripts/start-api.js"), "utf8"), /nextjsDir/);
  const startApi = await readFile(path.join(root, "scripts/start-api.js"), "utf8");
  assert.match(startApi, /\/health\/ready/);
  assert.doesNotMatch(startApi, /\/api\/v1\/auth\/status/);
  assert.match(startApi, /statusCode/);
});

test("export_utils reads EXPORT_PAGE_BASE_URL", async () => {
  const src = await readFile(
    path.join(root, "servers/fastapi/utils/export_utils.py"),
    "utf8",
  );
  assert.match(src, /EXPORT_PAGE_BASE_URL/);
  assert.match(src, /exportSession/);
});