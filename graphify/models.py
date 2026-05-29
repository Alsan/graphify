# Call LLM APIs (OpenAI-compatible) for semantic extraction by file type.
# Used by skill.md Step B2 to route different artifact types to different models.
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path


# ── prompts ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPTS: dict[str, str] = {
    "code": (
        "You are a graphify extraction subagent for CODE. "
        "Extract semantic knowledge graph edges that tree-sitter AST cannot find. "
        "Output ONLY valid JSON matching the schema below — no explanation, no markdown fences, no preamble.\n\n"
        "Rules:\n"
        "- EXTRACTED: relationship explicit in source (import, call, citation)\n"
        "- INFERRED: reasonable inference (shared data structure, implied dependency)\n"
        "- AMBIGUOUS: uncertain — flag for review, do not omit\n"
        "- Focus on semantic edges AST cannot find (call relationships, shared data, arch patterns).\n"
        "- Do NOT re-extract imports or structural edges — AST already has those."
    ),
    "doc": (
        "You are a graphify extraction subagent for DOCUMENTS. "
        "Read the files listed and extract a knowledge graph fragment. "
        "Output ONLY valid JSON matching the schema below — no explanation, no markdown fences, no preamble.\n\n"
        "Rules:\n"
        "- EXTRACTED: relationship explicit in source (import, call, citation, \"see §3.2\")\n"
        "- INFERRED: reasonable inference (shared data structure, implied dependency)\n"
        "- AMBIGUOUS: uncertain — flag for review, do not omit\n"
        "- Extract named concepts, entities, citations, themes, and relationships between them."
    ),
    "paper": (
        "You are a graphify extraction subagent for PAPERS. "
        "Read the files listed and extract a knowledge graph fragment. "
        "Output ONLY valid JSON matching the schema below — no explanation, no markdown fences, no preamble.\n\n"
        "Rules:\n"
        "- EXTRACTED: relationship explicit in source (import, call, citation, \"see §3.2\")\n"
        "- INFERRED: reasonable inference (shared data structure, implied dependency)\n"
        "- AMBIGUOUS: uncertain — flag for review, do not omit\n"
        "- Extract named concepts, entities, citations, and key results.\n"
        "- Pay attention to citations and references between papers."
    ),
    "image": (
        "You are a graphify extraction subagent for IMAGES. "
        "Use vision to extract concepts, entities, and relationships from the image(s). "
        "Output ONLY valid JSON matching the schema below — no explanation, no markdown fences, no preamble.\n\n"
        "Rules:\n"
        "- EXTRACTED: relationship explicit in source\n"
        "- INFERRED: reasonable inference\n"
        "- AMBIGUOUS: uncertain — flag for review, do not omit\n"
        "- Use vision to understand what the image IS — do not just OCR.\n"
        "  UI screenshot: layout patterns, design decisions, key elements, purpose.\n"
        "  Chart: metric, trend/insight, data source.\n"
        "  Tweet/post: claim as node, author, concepts mentioned.\n"
        "  Diagram: components and connections.\n"
        "  Research figure: what it demonstrates, method, result.\n"
        "  Handwritten/whiteboard: ideas and arrows, mark uncertain readings AMBIGUOUS."
    ),
}

_SHARED_RULES = (
    "DEEP_MODE (if --mode deep was given): be aggressive with INFERRED edges - indirect deps, "
    "shared assumptions, latent couplings. Mark uncertain ones AMBIGUOUS instead of omitting.\n\n"
    "Semantic similarity: if two concepts in this chunk solve the same problem or represent the same "
    "idea without any structural link (no import, no call, no citation), add a `semantically_similar_to` "
    "edge marked INFERRED with a confidence_score reflecting how similar they are (0.6-0.95). Examples:\n"
    "- Two functions that both validate user input but never call each other\n"
    "- A class in code and a concept in a paper that describe the same algorithm\n"
    "- Two error types that handle the same failure mode differently\n"
    "Only add these when the similarity is genuinely non-obvious and cross-cutting. Do not add them "
    "for trivially similar things.\n\n"
    "Hyperedges: if 3 or more nodes clearly participate together in a shared concept, flow, or pattern "
    "that is not captured by pairwise edges alone, add a hyperedge to a top-level `hyperedges` array. "
    "Examples:\n"
    "- All classes that implement a common protocol or interface\n"
    "- All functions in an authentication flow (even if they don't all call each other)\n"
    "- All concepts from a paper section that form one coherent idea\n"
    "Use sparingly — only when the group relationship adds information beyond the pairwise edges. "
    "Maximum 3 hyperedges per chunk.\n\n"
    "If a file has YAML frontmatter (--- ... ---), copy source_url, captured_at, author, "
    "contributor onto every node from that file.\n\n"
    "confidence_score rules:\n"
    "- EXTRACTED edges: confidence_score must be 1.0\n"
    "- INFERRED edges: score 0.4-0.9 based on how certain you are.\n"
    "  Strong structural inference (e.g. two classes clearly share data): 0.8-0.9.\n"
    "  Reasonable but not certain: 0.6-0.7. Weak inference: 0.4-0.5.\n"
    "- AMBIGUOUS edges: score 0.1-0.3\n\n"
    "Output exactly this JSON (no other text):\n"
    '{"nodes":[{"id":"filestem_entityname","label":"Human Readable Name","file_type":"code|document|paper|image",'
    '"source_file":"relative/path","source_location":null,"source_url":null,"captured_at":null,"author":null,'
    '"contributor":null}],"edges":[{"source":"node_id","target":"node_id",'
    '"relation":"calls|implements|references|cites|conceptually_related_to|shares_data_with|semantically_similar_to",'
    '"confidence":"EXTRACTED|INFERRED|AMBIGUOUS","confidence_score":1.0,"source_file":"relative/path",'
    '"source_location":null,"weight":1.0}],"hyperedges":[{"id":"snake_case_id","label":"Human Readable Label",'
    '"nodes":["node_id1","node_id2","node_id3"],"relation":"participate_in|implement|form",'
    '"confidence":"EXTRACTED|INFERRED","confidence_score":0.75,"source_file":"relative/path"}],'
    '"input_tokens":0,"output_tokens":0}'
)


# ── config resolution ────────────────────────────────────────────────────────

def _resolve_api_key(ftype: str) -> str:
    """Per-type API key, falling back to global GRAPHIFY_API_KEY then OPENAI_API_KEY."""
    type_key = os.environ.get(f"GRAPHIFY_{ftype.upper()}_API_KEY", "")
    if type_key:
        return type_key
    return os.environ.get("GRAPHIFY_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")


_MODEL_CONFIG = {
    "code": {
        "provider": os.environ.get("GRAPHIFY_CODE_SEMANTIC_PROVIDER", ""),
        "url": os.environ.get("GRAPHIFY_CODE_SEMANTIC_URL", "").rstrip("/"),
        "model": os.environ.get("GRAPHIFY_CODE_SEMANTIC_MODEL", ""),
        "api_key": _resolve_api_key("CODE_SEMANTIC"),
    },
    "doc": {
        "provider": os.environ.get("GRAPHIFY_DOC_PROVIDER", ""),
        "url": os.environ.get("GRAPHIFY_DOC_URL", "").rstrip("/"),
        "model": os.environ.get("GRAPHIFY_DOC_MODEL", ""),
        "api_key": _resolve_api_key("DOC"),
    },
    "paper": {
        "provider": os.environ.get("GRAPHIFY_DOC_PROVIDER", ""),
        "url": os.environ.get("GRAPHIFY_DOC_URL", "").rstrip("/"),
        "model": os.environ.get("GRAPHIFY_DOC_MODEL", ""),
        "api_key": _resolve_api_key("DOC"),
    },
    "image": {
        "provider": os.environ.get("GRAPHIFY_IMAGE_PROVIDER", ""),
        "url": os.environ.get("GRAPHIFY_IMAGE_URL", "").rstrip("/"),
        "model": os.environ.get("GRAPHIFY_IMAGE_MODEL", ""),
        "api_key": _resolve_api_key("IMAGE"),
    },
}

# Supported image extensions for base64 encoding
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


# ── API caller ───────────────────────────────────────────────────────────────

def _file_content_block(path: Path) -> dict:
    """Build a content block for a file (text or image)."""
    if path.suffix.lower() in _IMAGE_EXTS:
        b64 = base64.b64encode(path.read_bytes()).decode()
        ext = path.suffix.lower().lstrip(".")
        if ext == "jpg":
            ext = "jpeg"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{ext};base64,{b64}"},
        }
    else:
        return {
            "type": "text",
            "text": path.read_text(errors="replace"),
        }


def _build_user_message(files: list[str], file_type: str) -> dict | str:
    """Build the user message content. Returns string for text-only, list for mixed/image."""
    paths = [Path(f) for f in files]
    has_images = any(p.suffix.lower() in _IMAGE_EXTS for p in paths)

    if not has_images:
        # Text-only: single string
        parts = []
        for p in paths:
            parts.append(f"--- {p.name} ---\n{p.read_text(errors='replace')}\n")
        return "\n".join(parts)

    # Contains images: content array
    content: list[dict] = [{"type": "text", "text": "Extract concepts, entities, and relationships from these files."}]
    for p in paths:
        content.append(_file_content_block(p))
    return content


def call_model(
    files: list[str],
    file_type: str,
    deep_mode: bool = False,
    timeout: int = 120,
) -> dict:
    """Call the configured model API for a chunk of files.

    Returns the parsed JSON response dict with 'nodes' and 'edges'.
    Falls back to empty extraction on any error.
    """
    config = _MODEL_CONFIG.get(file_type, {})
    url = config.get("url", "")
    model = config.get("model", "")

    if not url or not model:
        print(f"[graphify models] No model configured for '{file_type}' — "
              f"set GRAPHIFY_{file_type.upper()}_URL and GRAPHIFY_{file_type.upper()}_MODEL",
              file=sys.stderr)
        return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    system_prompt = _SYSTEM_PROMPTS.get(file_type, _SYSTEM_PROMPTS["doc"])
    if deep_mode:
        system_prompt += "\n\nDEEP_MODE active: be aggressive with INFERRED edges."
    system_prompt += "\n\n" + _SHARED_RULES

    user_content = _build_user_message(files, file_type)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 8192,
        "temperature": 0.1,
    }

    # Build request
    api_url = f"{url}/chat/completions"
    data = json.dumps(body).encode()
    api_key = config.get("api_key", "")
    auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers = {"Content-Type": "application/json", **auth}
    req = urllib.request.Request(
        api_url,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read())
    except Exception as exc:
        print(f"[graphify models] API call failed for '{file_type}' chunk: {exc}", file=sys.stderr)
        return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    # Extract response text
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[graphify models] Unexpected API response format for '{file_type}'", file=sys.stderr)
        return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    # Parse JSON from response
    text = text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    if text.startswith("```json"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[graphify models] JSON parse failed for '{file_type}': {exc}", file=sys.stderr)
        print(f"  Response snippet: {text[:200]}", file=sys.stderr)
        return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}

    # Fill token counts
    try:
        result["input_tokens"] = raw["usage"]["prompt_tokens"]
        result["output_tokens"] = raw["usage"]["completion_tokens"]
    except (KeyError, TypeError):
        pass

    return result


# ── parallel dispatch ────────────────────────────────────────────────────────

_Result = tuple[int, dict]  # (chunk_index, result)


def _process_chunk(
    chunk_index: int,
    files: list[str],
    file_type: str,
    deep_mode: bool,
    results: list[_Result | None],
    lock: threading.Lock,
) -> None:
    """Process a single chunk and store its result."""
    result = call_model(files, file_type, deep_mode=deep_mode)
    with lock:
        results[chunk_index] = (chunk_index, result)


def process_all(
    chunks: dict[str, list[list[str]]],
    deep_mode: bool = False,
    max_parallel: int = 8,
) -> dict[str, list[dict]]:
    """Process all chunks across all file types in parallel.

    *chunks* maps file_type -> list of file lists (each inner list is a chunk).
    Returns file_type -> list of result dicts, preserving chunk order.
    """
    all_tasks: list[tuple[int, str, list[str]]] = []
    for ftype, file_lists in chunks.items():
        for i, files in enumerate(file_lists):
            all_tasks.append((len(all_tasks), ftype, files))

    results: list[_Result | None] = [None] * len(all_tasks)
    lock = threading.Lock()
    threads = []

    for idx, ftype, files in all_tasks:
        t = threading.Thread(target=_process_chunk, args=(idx, files, ftype, deep_mode, results, lock))
        threads.append(t)
        t.start()
        # Cap parallelism
        if len(threads) >= max_parallel:
            threads[0].join()
            threads.pop(0)

    for t in threads:
        t.join()

    # Group results by file type, preserving chunk order
    grouped: dict[str, list[dict]] = {}
    for r in results:
        if r is None:
            continue
        idx, result = r
        # Find which type this chunk belonged to
        for ftype, file_lists in chunks.items():
            # Count how many chunks of earlier types, plus offset within this type
            pass  # simpler: rebuild from the stored index

    # Rebuild by iterating task list
    grouped = {ft: [] for ft in chunks}
    for idx, ftype, _ in all_tasks:
        if results[idx] is not None:
            grouped[ftype].append(results[idx][1])

    return grouped


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry: reads chunk files from .graphify_chunks_*.json, processes all, writes results.

    Usage: python3 -m graphify.models [--deep]
    """
    deep = "--deep" in sys.argv or os.environ.get("DEEP_MODE") == "true"

    chunks: dict[str, list[list[str]]] = {}
    chunk_files = {
        "code": ".graphify_chunks_code.json",
        "doc": ".graphify_chunks_doc.json",
        "paper": ".graphify_chunks_paper.json",
        "image": ".graphify_chunks_image.json",
    }

    for ftype, cf in chunk_files.items():
        p = Path(cf)
        if p.exists():
            chunks[ftype] = json.loads(p.read_text())
            print(f"[graphify models] {ftype}: {sum(len(c) for c in chunks[ftype])} files in {len(chunks[ftype])} chunk(s)")

    if not chunks:
        print("[graphify models] No chunk files found — nothing to process.", file=sys.stderr)
        sys.exit(0)

    results = process_all(chunks, deep_mode=deep)

    # Write per-type result files
    for ftype, res_list in results.items():
        merged = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        for r in res_list:
            merged["nodes"].extend(r.get("nodes", []))
            merged["edges"].extend(r.get("edges", []))
            merged["input_tokens"] += r.get("input_tokens", 0)
            merged["output_tokens"] += r.get("output_tokens", 0)
        out = Path(f".graphify_semantic_{ftype}.json")
        out.write_text(json.dumps(merged, indent=2))
        print(f"[graphify models] {ftype}: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges "
              f"({merged['input_tokens']} in / {merged['output_tokens']} out tokens)")

    # Write combined result for B3
    combined = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
    seen_ids: set[str] = set()
    for ftype, res_list in results.items():
        for r in res_list:
            for n in r.get("nodes", []):
                if n["id"] not in seen_ids:
                    seen_ids.add(n["id"])
                    combined["nodes"].append(n)
            combined["edges"].extend(r.get("edges", []))
            combined["input_tokens"] += r.get("input_tokens", 0)
            combined["output_tokens"] += r.get("output_tokens", 0)
    Path(".graphify_semantic_new.json").write_text(json.dumps(combined, indent=2))


if __name__ == "__main__":
    main()
