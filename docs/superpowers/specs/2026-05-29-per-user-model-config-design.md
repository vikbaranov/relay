# Per-User Model Configuration Design

## Goal

Allow each Mattermost user runtime to use a selected LLM model while keeping the provider endpoint and API key global. Model selection is controlled by an administrator-provided allowlist and changed by users through chat commands.

This design applies to per-user runtimes. Shared channel runtimes continue to use the default model unless a separate channel-level model configuration is designed later.

## Configuration

- Replace `OPENAI_MODEL` with `ALLOWED_MODELS`.
- `ALLOWED_MODELS` is required and contains a comma-separated list of model names.
- The first model in `ALLOWED_MODELS` is the default for users without an override.
- `OPENAI_BASE_URL` and `OPENAI_API_KEY` remain global provider settings.

Example:

```env
ALLOWED_MODELS=gpt-4o-mini,gpt-4o,gpt-4.1
```

## User Commands

Add a new `!model` command group:

- `!model list` shows all allowed models and marks the current effective model.
- `!model show` shows the current effective model for the user.
- `!model set MODEL_NAME` validates `MODEL_NAME` against `ALLOWED_MODELS`, stores the override, regenerates that user's ZeroClaw `config.toml` Secret, and restarts the pod if it is running.
- `!model reset` removes the override and returns the user to the first model in `ALLOWED_MODELS`.

## Storage

Store the user's model override in the existing per-user identity ConfigMap using a reserved key named `MODEL`.

This keeps the feature small and reuses the existing per-user state lifecycle. The model name is not secret data, so a ConfigMap is appropriate.

## Runtime Behavior

When `RuntimeManager.ensure_runtime(mm_user_id)` provisions or refreshes a runtime:

1. Read the stored model override from the user's identity ConfigMap.
2. If the stored model is present in `ALLOWED_MODELS`, use it.
3. If no override exists, or the stored model is no longer allowed, use the first model from `ALLOWED_MODELS`.
4. Generate the per-user ZeroClaw `config.toml` Secret with the effective model.
5. Keep the existing per-user Secret mount for `model-config` unchanged.

Changing a user's model updates only that user's config Secret and restarts only that user's runtime if a Deployment exists.

## Error Handling

- App startup/config validation fails when `ALLOWED_MODELS` is empty.
- `!model set` rejects unknown models and returns the valid model list.
- A stale stored model that was removed from `ALLOWED_MODELS` is ignored and the default model is used.
- Kubernetes API failures follow the existing metrics/logging style used by env and workspace file state changes.

## Testing

Add or update tests for:

- Settings parsing and validation for required `ALLOWED_MODELS`.
- Default model selection from the first allowlist entry.
- Per-user override selection in generated ZeroClaw config.
- Stale stored model fallback to the default model.
- `!model list`, `!model show`, `!model set`, `!model reset`, and invalid model handling.
- Restart behavior after model changes.

## Documentation

Update README configuration docs to replace `OPENAI_MODEL` with `ALLOWED_MODELS` and document the first-entry default behavior.

Update command documentation to include the new `!model` commands.
