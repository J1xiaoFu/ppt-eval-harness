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

Profile 8.4 never sends a caller or renderer byte stream directly. At Acquire time,
`canonical-model-image-cas@1.0.0` verifies the declared digest and actual raster
format, limits decoded dimensions, accepts one PNG/JPEG/WebP frame, applies EXIF
orientation, composites transparency on white, and deterministically re-encodes only
the visible pixels as a metadata-free PNG under
`slide-renders/model-image-cas`. This removes ancillary metadata and trailing bytes,
including a valid image followed by an embedded PPTX. Base64 and signed-URL requests
both use this same frozen copy. The provider boundary independently rechecks its
format, exact container end, size and SHA-256 before network I/O.

Explicit Profile 8.3 replay retains its previous input behavior. In its optional
signed-URL mode, external images are copied to `slide-renders/visual-cas` under the
existing contract. The serving allow-list remains the render-cache root; an arbitrary
caller pathname is never made remotely readable.

This capability is infrastructure for provider-side image reuse. When enabled by
the environment-aware runtime, the Qwen and GLM adapters publish each integrity-
checked rendered page through this catalog and place the resulting stable HTTPS URL
in the request. Base64 remains the default transport. Profile 8.4 can reuse its stable
Qwen image prefix without external object storage; explicit Profile 8.3 replay keeps
the cache wire disabled.

Qwen's cache-capable visual-prefix wire shape is a separate opt-in:

```dotenv
PPT_EVAL_QWEN_CONTEXT_CACHE_ENABLED=true
```

Release 0.9.0 enables it only for Profile 8.4 high-resolution criterion calls. Atlas
Scout and explicit Profile 8.3 replay keep it disabled. Usage records include provider-reported image and
cache tokens plus measured request bytes; a URL alone must never be interpreted as
proof that the provider reused visual computation or reduced billing.
