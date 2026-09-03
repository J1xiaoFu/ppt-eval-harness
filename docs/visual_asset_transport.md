# Visual asset transport

The model-audit transport remains `base64` by default. This keeps a fresh local
or Docker installation self-contained and does not expose any model-facing HTTP
asset route.

An operator may opt in to short-lived signed URLs when the evaluation service is
available through a public HTTPS reverse proxy:

```dotenv
PPT_EVAL_VISUAL_ASSET_TRANSPORT=signed-url
PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL=https://ppt-eval.example.com
PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET=<at-least-32-random-bytes>
PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS=900
```

The public base URL is the external service origin, optionally including its
reverse-proxy path prefix. The service appends `/v1/model-assets/{variant}/{sha256}`.
The default lifetime is 15 minutes and the maximum is one hour. Partial settings,
plain HTTP, local/private literal IP addresses, URL credentials, and secrets shorter
than 32 bytes make service startup fail rather than silently falling back.

Only a rendered PNG, JPEG, or WebP file explicitly registered from the controlled
directory for its `slide`, `atlas`, or `crop` variant can receive a URL. Every GET
revalidates the file size and SHA-256 digest. The HMAC covers the variant, digest,
and expiry; paths and secrets never appear in the URL or persisted report. Original
PPTX, uploaded sources, and arbitrary files cannot be registered through this
transport.

Caller-supplied `slide_images` may originate outside the render cache. In signed-URL
mode the runtime first validates each file's declared digest/media type and actual
image format, rejects links and oversized inputs, and atomically copies the bytes to
the content-addressed `slide-renders/visual-cas` directory. Only this immutable CAS
copy is subsequently registered. The serving allow-list remains the render-cache
root; an arbitrary caller pathname is never made remotely readable.

This capability is infrastructure for provider-side image reuse. When enabled by
the environment-aware runtime, the Qwen and GLM adapters publish each integrity-
checked rendered page through this catalog and place the resulting stable HTTPS URL
in the request. Base64 remains the default, so Profile 8.3 keeps its historical wire
shape unless an operator explicitly opts in.

Qwen's cache-capable visual-prefix wire shape is a separate opt-in:

```dotenv
PPT_EVAL_QWEN_CONTEXT_CACHE_ENABLED=false
```

Release 0.8.7 ships it disabled. Usage records include provider-reported image and
cache tokens plus measured request bytes; a URL alone must never be interpreted as
proof that the provider reused visual computation or reduced billing.
