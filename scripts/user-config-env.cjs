const VALID_LLM_PROVIDERS = new Set([
  "openai",
  "google",
  "custom",
]);

const VALID_IMAGE_PROVIDERS = new Set([
  "gemini_flash",
  "nanobanana_pro",
  "gpt-image-1.5",
  "openai_compatible",
]);

const VALID_WEB_SEARCH_PROVIDERS = new Set([
  "auto",
  "native",
  "tavily",
  "exa",
  "brave",
]);

const USER_CONFIG_ENV_KEYS = [
  "LLM",
  "OPENAI_API_KEY",
  "OPENAI_MODEL",
  "GOOGLE_API_KEY",
  "GOOGLE_MODEL",
  "CUSTOM_LLM_URL",
  "CUSTOM_LLM_API_KEY",
  "CUSTOM_MODEL",
  "IMAGE_PROVIDER",
  "DISABLE_IMAGE_GENERATION",
  "DISABLE_THINKING",
  "EXTENDED_REASONING",
  "WEB_GROUNDING",
  "WEB_SEARCH_PROVIDER",
  "WEB_SEARCH_MAX_RESULTS",
  "TAVILY_API_KEY",
  "EXA_API_KEY",
  "BRAVE_SEARCH_API_KEY",
  "USE_CUSTOM_URL",
  "OPENAI_COMPAT_IMAGE_BASE_URL",
  "OPENAI_COMPAT_IMAGE_API_KEY",
  "OPENAI_COMPAT_IMAGE_MODEL",
  "GPT_IMAGE_1_5_QUALITY",
  "DISABLE_ANONYMOUS_TRACKING",
];

const BOOLEAN_CONFIG_KEYS = new Set([
  "DISABLE_IMAGE_GENERATION",
  "DISABLE_THINKING",
  "EXTENDED_REASONING",
  "WEB_GROUNDING",
  "USE_CUSTOM_URL",
]);

const envValue = (env, key) => {
  const value = env[key];
  return value === undefined || value === "" ? undefined : value;
};

const parseBooleanLike = (value) => {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value !== "string") {
    return undefined;
  }

  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return undefined;
};

const readUserConfigEnv = (env) => {
  const config = {};
  for (const key of USER_CONFIG_ENV_KEYS) {
    const value = envValue(env, key);
    if (value !== undefined) {
      config[key] = value;
    }
  }
  return config;
};

const normalizeConfigTypes = (config) => {
  for (const key of BOOLEAN_CONFIG_KEYS) {
    const parsedValue = parseBooleanLike(config[key]);
    if (parsedValue !== undefined) {
      config[key] = parsedValue;
    }
  }
  return config;
};

const normalizeImageConfig = (config) => {
  if (config.DISABLE_IMAGE_GENERATION || config.IMAGE_PROVIDER) {
    return config;
  }

  if (
    config.OPENAI_COMPAT_IMAGE_BASE_URL &&
    config.OPENAI_COMPAT_IMAGE_API_KEY &&
    config.OPENAI_COMPAT_IMAGE_MODEL
  ) {
    config.IMAGE_PROVIDER = "openai_compatible";
  } else if (config.LLM === "openai" && config.OPENAI_API_KEY) {
    config.IMAGE_PROVIDER = "gpt-image-1.5";
    config.GPT_IMAGE_1_5_QUALITY = config.GPT_IMAGE_1_5_QUALITY || "medium";
  } else if (config.LLM === "google" && config.GOOGLE_API_KEY) {
    config.IMAGE_PROVIDER = "gemini_flash";
  } else {
    config.DISABLE_IMAGE_GENERATION = true;
  }

  return config;
};

const sanitizeExistingConfig = (existingConfig) => {
  const config = { ...existingConfig };
  if (config.LLM && !VALID_LLM_PROVIDERS.has(config.LLM)) {
    delete config.LLM;
  }
  if (config.IMAGE_PROVIDER && !VALID_IMAGE_PROVIDERS.has(config.IMAGE_PROVIDER)) {
    delete config.IMAGE_PROVIDER;
  }
  if (
    config.WEB_SEARCH_PROVIDER &&
    !VALID_WEB_SEARCH_PROVIDERS.has(config.WEB_SEARCH_PROVIDER)
  ) {
    delete config.WEB_SEARCH_PROVIDER;
  }
  return config;
};

const buildUserConfigFromEnv = (existingConfig = {}, env = process.env) =>
  normalizeImageConfig(
    normalizeConfigTypes({
      ...sanitizeExistingConfig(existingConfig),
      ...readUserConfigEnv(env),
    })
  );

module.exports = {
  buildUserConfigFromEnv,
  parseBooleanLike,
  readUserConfigEnv,
  USER_CONFIG_ENV_KEYS,
  VALID_LLM_PROVIDERS,
};
