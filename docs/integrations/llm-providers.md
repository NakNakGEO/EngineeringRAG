# LLM providers used by Engineering OS itself

The primary reasoning model is *your client*. Engineering OS only calls a model for internal
features (the Capability Workshop's generators, curation helpers, evaluations), through the
vendor-neutral `LLMGateway` (`eios_llm`): no vendor SDKs, plain HTTP adapters.

A provider exists **only if configured**; nothing is defaulted or guessed (model names are always
explicit):

| Provider | Variables |
|---|---|
| Local OpenAI-compatible (llama.cpp, vLLM, Ollama `/v1`, LM Studio) | `EIOS_LLM_LOCAL_BASE_URL`, `EIOS_LLM_LOCAL_MODEL` (host must be local/private) |
| OpenAI-compatible (remote) | `EIOS_LLM_OPENAI_API_KEY`, `EIOS_LLM_OPENAI_MODEL`, optional `EIOS_LLM_OPENAI_BASE_URL` |
| Anthropic | `EIOS_LLM_ANTHROPIC_API_KEY`, `EIOS_LLM_ANTHROPIC_MODEL` |
| Default | `EIOS_LLM_DEFAULT_PROVIDER` (`local`, `openai` or `anthropic`) |

Every call is checked by the Policy Engine (`llm.call`): **project/company data never goes to a
remote provider without a human approval**; unconfigured providers are refused; calls are
budgeted, retried at most 3 times on transient errors, and recorded as `LLM_STARTED/LLM_COMPLETED`
events (tokens, latency - never prompt content).
