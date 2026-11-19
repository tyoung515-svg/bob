// Offline response fixture for the native runtime_model_providers boundary.
module.exports = [
  { id: 'openai', label: 'OpenAI (ChatGPT)', method: 'signin' },
  { id: 'openai', label: 'OpenAI', method: 'key' },
  { id: 'anthropic', label: 'Anthropic', method: 'key' },
  { id: 'deepseek', label: 'DeepSeek', method: 'key' },
  { id: 'google', label: 'Google', method: 'key' },
  { id: 'glm', label: 'GLM (Z.ai)', method: 'key' },
  { id: 'kimi', label: 'Kimi (Moonshot)', method: 'key' },
  { id: 'qwen', label: 'Qwen (Alibaba Model Studio)', method: 'key' },
];
