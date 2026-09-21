import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function readRepo(relativePath) {
  return readFile(path.join(root, relativePath), "utf8");
}

test("api compose runs only api services on the shared gslide network", async () => {
  const compose = await readRepo("docker-compose.yml");

  assert.match(compose, /^  api:/m);
  assert.match(compose, /^  api-dev:/m);
  assert.doesNotMatch(compose, /^  web:/m);
  assert.doesNotMatch(compose, /^  web-dev:/m);
  assert.doesNotMatch(compose, /^  searxng:/m);
  assert.doesNotMatch(compose, /nginx:1\.27-alpine|image: nginx/i);
  assert.match(compose, /EXPORT_PAGE_BASE_URL=\$\{EXPORT_PAGE_BASE_URL:-http:\/\/web:3000\}/);
  assert.match(compose, /context: \./);
  assert.match(compose, /dockerfile: Dockerfile/);
  assert.match(compose, /dockerfile: Dockerfile\.dev/);
  assert.match(compose, /name: gslide/);
  assert.match(compose, /aliases:\s*\n\s+- api/);
  assert.match(compose, /:8000"/);
  assert.match(
    compose,
    /http:\/\/127\.0\.0\.1:8000\/health\/ready/,
  );
  assert.doesNotMatch(compose, /\/api\/v1\/auth\/status/);
  assert.match(compose, /gslide_app_data:\/app_data$/m);
  assert.match(compose, /WEB_IMAGE_SEARCH_ENABLED=\$\{WEB_IMAGE_SEARCH_ENABLED:-false\}/);
  assert.doesNotMatch(compose, /depends_on:\s*\n\s+searxng/);
});

test("api env example owns LLM and storage; SearXNG is optional and off", async () => {
  const env = await readRepo(".env.example");

  assert.match(env, /^GSLIDE_API_HOST_PORT=8000$/m);
  assert.match(env, /^EXPORT_PAGE_BASE_URL=http:\/\/web:3000$/m);
  assert.match(env, /^LLM=openai$/m);
  assert.match(env, /^WEB_IMAGE_SEARCH_ENABLED=false$/m);
  assert.match(env, /^OBJECT_STORAGE_BACKEND=local$/m);
  assert.match(
    env,
    /IMAGE_PROVIDER: gemini_flash \| nanobanana_pro \| gpt-image-1\.5 \| openai_compatible/,
  );
  assert.doesNotMatch(env, /^SEARXNG_BASE_URL=/m);
  assert.doesNotMatch(env, /docker compose --profile search/);
  assert.doesNotMatch(env, /POSTHOG_/);
  assert.doesNotMatch(env, /DEMO_USERNAME/);
});

test("api kubernetes manifests own PVC and secrets and omit SearXNG", async () => {
  const api = await readRepo("deploy/kubernetes/20-api.yaml");
  const config = await readRepo("deploy/kubernetes/01-configmap.yaml");
  const pvc = await readRepo("deploy/kubernetes/03-pvc.yaml");
  const secret = await readRepo("deploy/kubernetes/02-secret.example.yaml");

  assert.match(api, /containerPort: 8000/);
  assert.match(api, /type: ClusterIP/);
  assert.match(api, /claimName: gslide-app-data/);
  assert.match(api, /secretRef:\s*\n\s+name: gslide-secrets/);
  assert.doesNotMatch(api, /searxng/i);
  assert.match(pvc, /name: gslide-app-data/);
  assert.match(secret, /name: gslide-secrets/);
  assert.match(config, /name: gslide-api-config/);
  assert.match(config, /EXPORT_PAGE_BASE_URL: "http:\/\/web:3000"/);
  assert.match(config, /WEB_IMAGE_SEARCH_ENABLED: "false"/);
  assert.doesNotMatch(config, /SEARXNG_BASE_URL/);
  assert.match(api, /startupProbe:\s*\n\s+httpGet:\s*\n\s+path: \/health\/ready/);
  assert.match(api, /readinessProbe:\s*\n\s+httpGet:\s*\n\s+path: \/health\/ready/);
  assert.match(api, /livenessProbe:\s*\n\s+httpGet:\s*\n\s+path: \/health\/live/);
  assert.doesNotMatch(api, /\/api\/v1\/auth\/status/);
});
