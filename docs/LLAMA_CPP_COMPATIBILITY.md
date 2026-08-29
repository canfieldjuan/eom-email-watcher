# llama.cpp compatibility and bounded capacity proof

## Status

Mainline llama.cpp is compatible with the current Email Watcher and Document Summarizer model
request shapes when reasoning is disabled for these bounded task lanes. This is a transport and
concurrency proof, not a model-quality promotion, appliance-sizing result, or production cutover.

The committed observation is
[`llama-cpp-v0.3.0-qwen35-4b-compatibility.json`](../benchmarks/runtime-results/llama-cpp-v0.3.0-qwen35-4b-compatibility.json).

Current application behavior remains unchanged: the existing EOM deployment still uses its
configured loopback service and LM Studio service unit. Do not remove that path until a separate
cutover slice installs and verifies an equivalent llama.cpp service.

## Proven contracts

The proof runner exercises both existing OpenAI-compatible contracts as observed at Email Watcher
revision `ea663e1af19fa510d5df0608cc6db9599048e0c4` and Document Summarizer revision
`ca5933a2174f7b08af62a9a032fed261f0afdd02`:

- Email Watcher calls the production `LocalModel.analyze()` path, including the production prompt,
  strict JSON schema, response extraction, and deterministic `Analysis` validation.
- The document workload uses the current Document Summarizer request envelope: system and user
  messages, temperature `0.0`, 512 output tokens, non-streaming, and required plain-text content.

Only aggregate timing, exception class names, model artifact identity, and runtime provenance are
printed. Synthetic prompts and free-form model output are not written to the public result.

## Pinned runtime build

The observed runtime was tag `v0.3.0`, exact revision
`c1d0e7a004015f23bc0233470b747b596f29b264`, built from the official llama.cpp repository:

```bash
git clone --branch v0.3.0 --single-branch https://github.com/ggml-org/llama.cpp.git source
git -C source checkout c1d0e7a004015f23bc0233470b747b596f29b264
cmake -S source -B source/build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build source/build --target llama-server -j6
source/build/bin/llama-server --version
```

For an installed service, distribute a pinned prebuilt artifact; do not make every customer machine
compile llama.cpp.

## Required task-lane profile

Qwen 3.5 defaults to reasoning under this llama.cpp release. With the default, a 512-token document
request ended with `finish_reason=length`, zero content characters, and 1,807 reasoning characters.
The mixed proof then failed every request because neither app received its required final output.

Start bounded Email Watcher and Document Summarizer lanes with reasoning disabled:

```bash
CUDA_VISIBLE_DEVICES=YOUR_DEVICE \
llama-server \
  --model /path/to/Qwen3.5-4B-Q4_K_M.gguf \
  --alias qwen3.5-4b \
  --host 127.0.0.1 \
  --port 18123 \
  --ctx-size 16384 \
  --parallel 4 \
  --n-gpu-layers all \
  --split-mode none \
  --reasoning off \
  --api-key-file /mode-0600/path/to/inference-api-keys \
  --metrics
```

The total context is divided across the configured slots; this observed profile reported four
4,096-token slots. Size the context and slot count from admitted task requirements rather than
copying these proof values blindly.

Run the reproducible request proof from this repository:

```bash
uv run eom-llama-cpp-proof \
  --base-url http://127.0.0.1:18123/v1 \
  --model qwen3.5-4b \
  --model-artifact /path/to/Qwen3.5-4B-Q4_K_M.gguf \
  --runtime-revision c1d0e7a004015f23bc0233470b747b596f29b264 \
  --requests 8 \
  --concurrency 4 \
  --api-token-file /mode-0600/path/to/client-token
```

## Observed boundary

The capacity run used an RTX 4060 Ti because existing Ollama and LM Studio processes occupied the
RTX 3090. All eight mixed requests passed at four-way concurrency after reasoning was disabled.
An authenticated follow-up rejected an unauthenticated model-list request with HTTP 401 and passed
all four mixed authenticated requests at two-way concurrency.

These measurements prove concurrent compatibility on this machine only. They do not establish how
many users a future 4090/5090 appliance supports, validate large documents, exercise vision, or
replace the fixed quality benchmark and human review.

## Deferred from this proof

- installing and supervising a pinned llama.cpp service;
- LAN transport, TLS, per-user authorization, quotas, and the admin surface;
- task-to-lane routing across fast, deep, and vision models;
- changing either application's default endpoint or removing LM Studio deployment files;
- appliance sizing and sustained-load acceptance testing.

Connect capability discovery remains independent of inference transport. Neither app discovers or
selects models through Connect, and this proof adds no app-to-app runtime dependency.

The proposed secure multi-user appliance boundary is documented separately in
[`INFERENCE_GATEWAY_V0.md`](INFERENCE_GATEWAY_V0.md). It keeps worker/model selection behind
administrator policy rather than making this llama.cpp proof an application dependency.
