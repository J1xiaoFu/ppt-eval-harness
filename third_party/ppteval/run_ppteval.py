"""Reproducible PPTEval runner pinned to the PPTAgent paper implementation.

The upstream experiment code couples evaluation to private Qwen endpoints and a
large generation environment. This runner preserves the published GPT-4o judge
model, prompts, message formatting, score aggregation, and resumable cache while
using the public Chat Completions endpoint instead of OpenAI Batch transport.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UPSTREAM_COMMIT = "88c29f045ab5b7db331bd8b76cf6efc5f9ea7eee"
PAPER_MODEL = "gpt-4o-2024-08-06"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_path(path: Path, *, repository_root: Path = REPOSITORY_ROOT) -> str:
    """Return stable evidence paths without persisting the runner host root."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return f"<external>/{resolved.name}"


def natural_key(path: Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def find_slide_images(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES),
        key=natural_key,
    )


def extract_presentation_text(pptx_path: Path) -> tuple[str, int]:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError(
            "python-pptx is required for coherence input extraction; install python-pptx>=0.6.21"
        ) from exc

    presentation = Presentation(str(pptx_path))
    rendered: list[str] = []
    slide_count = len(presentation.slides)
    for index, slide in enumerate(presentation.slides, 1):
        blocks: list[str] = []
        title = ""
        if slide.shapes.title is not None:
            title = slide.shapes.title.text.strip()
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text:
                    blocks.append(text)
        prefix = f"Slide {index} of {slide_count}\n"
        if title:
            prefix += f"Title:{title}\n"
        rendered.append(prefix + "\n".join(blocks))
    return "\n----\n".join(rendered), slide_count


def load_prompts(prompt_dir: Path) -> dict[str, str]:
    names = {
        "content_descriptor": "ppteval_describe_content.txt",
        "style_descriptor": "ppteval_describe_style.txt",
        "content_scorer": "ppteval_content.txt",
        "style_scorer": "ppteval_style.txt",
        "extractor": "ppteval_extract.txt",
        "coherence_scorer": "ppteval_coherence.txt",
    }
    prompts = {
        key: (prompt_dir / filename).read_text(encoding="utf-8")
        for key, filename in names.items()
    }
    required_placeholders = {
        "content_scorer": "{{descr}}",
        "style_scorer": "{{descr}}",
        "extractor": "{{presentation}}",
        "coherence_scorer": "{{presentation}}",
    }
    for key, placeholder in required_placeholders.items():
        if placeholder not in prompts[key]:
            raise ValueError(f"official prompt {key} is missing {placeholder}")
    return prompts


def render_prompt(template: str, **values: Any) -> str:
    rendered = template
    for key, value in values.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def official_messages(prompt: str, image_path: Path | None = None) -> list[dict[str, Any]]:
    system_message = "You are a helpful assistant"
    if prompt.startswith("You are") and "\n" in prompt:
        system_message, prompt = prompt.split("\n", 1)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_path is not None:
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
    return [
        {"role": "system", "content": [{"type": "text", "text": system_message}]},
        {"role": "user", "content": user_content},
    ]


def chat_completion(
    *, api_key: str, base_url: str, model: str, prompt: str, image_path: Path | None = None
) -> str:
    body = json.dumps(
        {"model": model, "messages": official_messages(prompt, image_path)},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(request, timeout=360) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model response content must be a string")
            return content
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(3)
    raise RuntimeError(f"model request failed after five attempts: {last_error}")


def parse_json_response(response: str) -> dict[str, Any]:
    fenced = re.findall(r"```json\s*(\{.*?\})\s*```", response, flags=re.DOTALL | re.IGNORECASE)
    candidates = list(reversed(fenced))
    starts = [index for index, char in enumerate(response) if char == "{"]
    ends = [index for index, char in enumerate(response) if char == "}"]
    candidates.extend(response[start : end + 1] for start in starts for end in reversed(ends) if start < end)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    raise ValueError(f"JSON object not found in response: {response[:500]}")


def score_value(value: dict[str, Any]) -> float:
    score = float(value["score"])
    if not 1 <= score <= 5:
        raise ValueError(f"PPTEval score must be within [1, 5], got {score}")
    return score


def build_preflight(
    *, pptx_path: Path, slide_images: list[Path], prompt_dir: Path, model: str, text: str, slide_count: int
) -> dict[str, Any]:
    return {
        "status": "READY" if len(slide_images) == slide_count else "INVALID_INPUT",
        "upstream_commit": UPSTREAM_COMMIT,
        "paper_model": PAPER_MODEL,
        "selected_model": model,
        "transport_deviation": "public synchronous Chat Completions; paper code uses OpenAI Batch",
        "pptx": {
            "path": portable_path(pptx_path),
            "sha256": sha256(pptx_path),
            "slides": slide_count,
        },
        "slide_images": [
            {
                "path": portable_path(path),
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in slide_images
        ],
        "presentation_text_chars": len(text),
        "prompt_files": [
            {"path": portable_path(path), "sha256": sha256(path)}
            for path in sorted(prompt_dir.glob("ppteval_*.txt"))
        ],
        "expected_model_calls": 4 * len(slide_images) + 2,
        "aggregation": "mean slide content; mean slide design; one deck coherence; arithmetic mean of dimensions",
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    pptx_path = args.pptx.resolve()
    slides_dir = args.slides.resolve()
    prompt_dir = args.prompt_dir.resolve()
    if not pptx_path.is_file():
        raise FileNotFoundError(pptx_path)
    if not slides_dir.is_dir():
        raise NotADirectoryError(slides_dir)
    prompts = load_prompts(prompt_dir)
    presentation_text, slide_count = extract_presentation_text(pptx_path)
    slide_images = find_slide_images(slides_dir)
    if args.max_slides is not None:
        slide_images = slide_images[: args.max_slides]
    preflight = build_preflight(
        pptx_path=pptx_path,
        slide_images=slide_images,
        prompt_dir=prompt_dir,
        model=args.model,
        text=presentation_text,
        slide_count=slide_count,
    )
    result: dict[str, Any] = {"preflight": preflight, "content": {}, "design": {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        result["status"] = "PREFLIGHT_ONLY"
        return result
    if preflight["status"] != "READY" and args.max_slides is None:
        raise ValueError(
            f"PPTX has {slide_count} slides but {len(slide_images)} rendered images were found"
        )
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        result["status"] = "BLOCKED_CREDENTIAL"
        result["blocked_by"] = f"environment variable {args.api_key_env} is not set"
        return result

    if args.output.exists():
        cached = json.loads(args.output.read_text(encoding="utf-8"))
        if cached.get("preflight", {}).get("pptx", {}).get("sha256") == preflight["pptx"]["sha256"]:
            result["content"] = cached.get("content", {})
            result["design"] = cached.get("design", {})
            if "coherence" in cached:
                result["coherence"] = cached["coherence"]

    def call(prompt: str, image: Path | None = None, expect_json: bool = False) -> Any:
        response = chat_completion(
            api_key=api_key,
            base_url=args.base_url,
            model=args.model,
            prompt=prompt,
            image_path=image,
        )
        return parse_json_response(response) if expect_json else response

    if "coherence" not in result:
        extracted = call(render_prompt(prompts["extractor"], presentation=presentation_text), expect_json=True)
        result["coherence"] = call(
            render_prompt(prompts["coherence_scorer"], presentation=extracted), expect_json=True
        )
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for image in slide_images:
        key = image.name
        if key in result["content"] and key in result["design"]:
            continue
        style_description = call(prompts["style_descriptor"], image)
        content_description = call(prompts["content_descriptor"], image)
        result["design"][key] = call(
            render_prompt(prompts["style_scorer"], descr=style_description), expect_json=True
        )
        result["content"][key] = call(
            render_prompt(prompts["content_scorer"], descr=content_description), expect_json=True
        )
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    content_mean = sum(score_value(value) for value in result["content"].values()) / len(result["content"])
    design_mean = sum(score_value(value) for value in result["design"].values()) / len(result["design"])
    coherence = score_value(result["coherence"])
    result["scores"] = {
        "content": content_mean,
        "design": design_mean,
        "coherence": coherence,
        "overall": (content_mean + design_mean + coherence) / 3,
    }
    result["status"] = "COMPLETE"
    return result


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run the paper-version PPTEval baseline")
    parser.add_argument("--pptx", type=Path, required=True)
    parser.add_argument("--slides", type=Path, required=True, help="folder containing one rendered image per slide")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-dir", type=Path, default=root / "paper_experiment" / "prompts")
    parser.add_argument("--model", default=PAPER_MODEL)
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-slides", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        result = evaluate(args)
    except Exception as exc:
        result = {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    result["runtime"] = {
        "started_at_utc": started_at,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "BLOCKED_CREDENTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
