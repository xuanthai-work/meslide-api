import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

async function readJson(relativePath) {
  return JSON.parse(await readFile(path.join(repoRoot, relativePath), "utf8"));
}

test("Docker pins the presentation export version", async () => {
  const [rootPackage, dockerfile, dockerfileDev] = await Promise.all([
    readJson("package.json"),
    readFile(path.join(repoRoot, "Dockerfile"), "utf8"),
    readFile(path.join(repoRoot, "Dockerfile.dev"), "utf8"),
  ]);

  assert.match(dockerfile, /COPY package\.json \/app\//);
  assert.match(
    dockerfile,
    /sync-presentation-export\.cjs --force/,
  );
  assert.match(dockerfileDev, /COPY package\.json package-lock\.json \/app\//);
  assert.match(
    dockerfileDev,
    /sync-presentation-export\.cjs --force/,
  );
  assert.ok(rootPackage.presentationExportVersion, "presentationExportVersion is set");
});
