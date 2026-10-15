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

## Changing the local model without editing files

Open **Settings** in the web UI: give the address of a server on this PC (Ollama inside Docker:
`http://host.docker.internal:11434/v1`), press *Find models*, pick one, *Save and use it*. The choice
is stored in `platform.setting` and applies to the API and the worker at once, no restart. It needs
the admin token (`EIOS_ADMIN_TOKEN`) and only accepts a server on this machine or a private
network; keys for remote providers stay in `.env`. *Use the .env value again* removes the choice.

## File summaries

The details panel shows three short lines for a file (used for / connects to / worth knowing),
written by the local model **once per version of the file**: they are stored in
`project.file_summary` keyed by the file's content hash and rewritten only when the file changes
(or when the prompt version in `insight.py` is bumped). The text is limited to a few words, comes
only from facts about the file (names, imports, who uses it, the first lines), and is never added
to the knowledge base. Without a configured model nothing is called and the panel says so.

Models that "think" before answering (for example Gemma 4 `e2b` in Ollama) would spend the token
budget on thinking and return nothing; the summary request therefore sends `reasoning_effort:
none`, and retries with a larger budget if a server ignores it. The first call after the server was
idle loads the model and can take a minute: `EIOS_LLM_LOCAL_TIMEOUT_SECONDS` (default 240).
