# AGENTS.md — graphify

## What this project does

**graphify** is a Python library + Claude Code skill that turns any folder of files (code, docs, PDFs, images) into a persistent, queryable knowledge graph with community detection. Outputs: interactive HTML, JSON, Obsidian vault, Wikipedia-style wiki, SVG, GraphML, Neo4j Cypher.

## Project documentation map

| File | What it covers |
|------|----------------|
| `README.md` | Project overview, install (pip/pipx/curl), usage, worked examples, tech stack |
| `ARCHITECTURE.md` | Pipeline stages, module responsibilities, extraction schema, confidence labels, adding languages, security |
| `SECURITY.md` | Full threat model: SSRF, XSS, path traversal, secret leakage, prompt injection |
| `CHANGELOG.md` | Version history and release notes |
| `skills/graphify/skill.md` | Claude Code skill definition — `/graphify` trigger, full pipeline orchestration, subagent prompts |
| `graphify/skill.md` | Same skill.md bundled in the Python package for `graphify install` |
| `AGENTS.md` | This file — what an agent needs to work in this codebase |
| `worked/{slug}/` | Real output examples: `karpathy-repos/`, `httpx/`, `mixed-corpus/`. Each has raw inputs + `GRAPH_REPORT.md` + `review.md` + `graph.json` |

The skill.md at `skills/graphify/skill.md` is the **primary user-facing doc** — it defines the entire `/graphify` workflow (detect → extract → build → cluster → analyze → report → export). When a user types `/graphify`, this is the instruction set that runs. It also contains the extraction subagent prompts with all rules for EXTRACTED/INFERRED/AMBIGUOUS confidence, semantic similarity, hyperedges, and confidence scoring.

## Quick-start commands

```bash
pip install -e ".[mcp,pdf,watch]"   # install with all extras
pytest tests/ -q --tb=short          # run all tests (pure unit tests, no network)
pytest tests/test_pipeline.py -q     # end-to-end pipeline smoke test
graphify --help                      # CLI for setup commands
python3 -c "from graphify import *"  # verify import works
```

CI runs on Python 3.10 and 3.12 (see `.github/workflows/ci.yml`).

## Code organization

```
graphify/           # library package (20 modules)
tests/              # one test file per module (pure unit tests, tmp_path only)
tests/fixtures/     # sample code files for all 14 supported languages + extraction.json
skills/graphify/    # Claude Code skill definition (skill.md)
worked/             # real-world output examples (karpathy-repos, httpx, mixed-corpus)
```

## Pipeline architecture (strict linear dependency)

```
detect() → extract() → build_graph() → cluster() → analyze() → report() → export()
```

Each stage is one function in its own module. They communicate through plain dicts and NetworkX graphs — no shared state, no side effects outside `graphify-out/`.

| Module | Responsibility |
|--------|---------------|
| `detect.py` | File discovery, type classification (.code/.document/.paper/.image), corpus health checks |
| `extract.py` | AST extraction via tree-sitter (14 languages) + call-graph pass |
| `build.py` | Assembles NetworkX graph from extraction dicts, prunes dangling edges and degree-0 code nodes |
| `cluster.py` | Leiden community detection (graspologic), auto-splits oversized communities |
| `analyze.py` | God nodes, surprising connections (cross-file/cross-community), suggested questions |
| `report.py` | Renders GRAPH_REPORT.md with all findings |
| `export.py` | HTML (vis.js), JSON, SVG, GraphML, Obsidian vault, Neo4j Cypher |
| `cache.py` | SHA256-based per-file cache (`graphify-out/cache/{hash}.json`) |
| `ingest.py` | URL fetching (tweets, arxiv, webpages, PDFs) with security validation |
| `validate.py` | Schema enforcement on extraction JSON before graph assembly |
| `security.py` | URL validation, safe fetch (SSRF protection), path guards, label sanitization |
| `serve.py` | MCP stdio server — BFS/DFS query, node lookup, shortest path |
| `watch.py` | File watcher (watchdog) — code changes auto-rebuild (no LLM) |
| `hooks.py` | Git post-commit hook install/uninstall/status |
| `wiki.py` | Wikipedia-style export per community |
| `benchmark.py` | Token reduction benchmark vs naive full-corpus reading |
| `manifest.py` | Backwards-compat re-exports for detect_incremental |
| `models.py` | OpenAI-compatible API caller per file type — reads chunk files, routes to configured model endpoint, writes results |

## Extraction output schema

Every extractor returns:
```json
{
  "nodes": [
    {"id": "unique_string", "label": "human name", "file_type": "code",
     "source_file": "path", "source_location": "L42"}
  ],
  "edges": [
    {"source": "id_a", "target": "id_b", "relation": "calls|imports|uses|...",
     "confidence": "EXTRACTED|INFERRED|AMBIGUOUS", "source_file": "path",
     "source_location": "L10", "weight": 1.0}
  ]
}
```

**Confidence labels**: `EXTRACTED` (explicit in source), `INFERRED` (reasonable deduction), `AMBIGUOUS` (flagged for review).

## Key coding patterns & gotchas

### Lazy imports are required
Heavy deps (`graspologic`, tree-sitter grammars, `mcp`, `watchdog`) are imported **inside functions**, not at module top level. This lets `graphify install` work before those deps are installed. See `extract.py:20-22`, `cluster.py:39`, `serve.py:105-110`.

### `__init__.py` uses `__getattr__` for lazy loading
All public API functions are wireable via `from graphify import <name>` with lazy import magic. Don't add direct imports to `__init__.py`.

### Node IDs are stable, lowered, underscore-separated
`_make_id()` in `extract.py` converts arbitrary names to stable IDs: `_make_id("_auth")` → `"auth"`. Used throughout for cross-file matching.

### NetworkX graph is undirected with directional preservation
The graph is `nx.Graph()` (undirected) for Leiden clustering, but each edge stores `_src` and `_tgt` attributes so display functions show correct direction. See `build.py:25-27`.

### Degree-0 code nodes are pruned
`build_from_json()` strips isolated code nodes — they're synthetic symbols with no connections that inflate centrality metrics. Document/paper/image nodes are kept even when isolated.

### File-level hub nodes excluded from analysis
`_is_file_node()` in `analyze.py` filters out filename labels (e.g. "client.py") and method stubs (".method_name()") from god nodes and surprising connections — they accumulate mechanical edges.

### AST runner vs semantic extraction
AST extraction (tree-sitter) is deterministic, instant, and handles code files. Semantic extraction (Claude vision/LLM) is needed for docs, papers, images. The CLI orchestrates both.

### Surprising connections ranking
Composite score weighing: confidence (AMBIGUOUS>INFERRED>EXTRACTED), cross file-type (code↔paper), cross-repo, cross-community, peripheral→hub connections. Each result includes a 'why' field.

### SHA256 cache
Every extraction result is cached by file hash at `graphify-out/cache/{sha256}.json`. Incremental runs (`--update`) only process files whose hash changed.

### All tests are pure unit tests
No network calls, no filesystem side effects outside `tmp_path` (pytest fixture). One test file per module. Run with `pytest tests/ -q --tb=short`.

### PyPI package is named `graphifyy`
The import and CLI are `graphify`; the pip package is `graphifyy` while the name is reclaimed. Important when reading CI config or dependency declarations.

### `.gitignore` excludes `graphify-out/`
All output artifacts go under `graphify-out/` which is gitignored. Also excludes `.graphify/` and `.graphify_*.json`.

## Adding a new language extractor

1. Add `extract_<lang>()` in `extract.py` following existing pattern (tree-sitter parse → walk AST → collect nodes/edges → call-graph second pass)
2. Register file suffix in `extract()` dispatch and `collect_files()`
3. Add suffix to `CODE_EXTENSIONS` in `detect.py` and `_WATCHED_EXTENSIONS` in `watch.py`
4. Add tree-sitter package to `pyproject.toml`
5. Add fixture to `tests/fixtures/` and tests to `tests/test_languages.py`

## Model routing (env vars per resource type)

Semantic extraction can be routed to different LLM providers/endpoints/models per artifact type. A dedicated Python module (`graphify/models.py`) calls the configured APIs directly — no Agent subagents involved.

Three resource types, each with three env vars:

| Env var prefix | Resource type | Suggested provider | Suggested model |
|----------------|---------------|-------------------|-----------------|
| `GRAPHIFY_CODE_SEMANTIC_*` | Code semantic pass (beyond AST) | `ollama`, `openai` | `qwen2.5-coder`, `gpt-4o-mini` |
| `GRAPHIFY_DOC_*` | Documents + papers (`.md .txt .rst .pdf`) | `anthropic`, `openai` | `claude-sonnet-4`, `gpt-4o` |
| `GRAPHIFY_IMAGE_*` | Images (`.png .jpg .webp .gif`) | `anthropic`, `openai` | `claude-sonnet-4`, `gpt-4o` |

Per resource type, set any combination of:

| Env var | Purpose |
|---------|---------|
| `GRAPHIFY_{TYPE}_PROVIDER` | Provider name (`openai`, `anthropic`, `ollama`, `vllm`, `azure`) |
| `GRAPHIFY_{TYPE}_URL` | API endpoint URL (OpenAI-compatible chat completions) |
| `GRAPHIFY_{TYPE}_MODEL` | Model name/ID passed in the request body |
| `GRAPHIFY_{TYPE}_API_KEY` | Per-type API key (optional — fallback: `GRAPHIFY_API_KEY` → `OPENAI_API_KEY`) |

If `URL` + `MODEL` are unset for a type, that type is skipped with a warning. If `API_KEY` is unset for a type, falls back to `GRAPHIFY_API_KEY`, then `OPENAI_API_KEY`. If none are set, no `Authorization` header is sent (works for Ollama/local endpoints).

Example:
```bash
export GRAPHIFY_CODE_SEMANTIC_URL=http://localhost:11434/v1
export GRAPHIFY_CODE_SEMANTIC_MODEL=qwen2.5-coder:14b
export GRAPHIFY_DOC_URL=http://jetson:8081/v1
export GRAPHIFY_DOC_MODEL=gpt-4o-mini
export GRAPHIFY_IMAGE_URL=http://localhost:8000
export GRAPHIFY_IMAGE_MODEL=llava
graphify /path
```

## Security model (see `SECURITY.md`)

All external input passes through `graphify/security.py`:
- URLs: http/https only (`validate_url()`), redirects re-validated (`_NoFileRedirectHandler`)
- Fetched content: size-capped streaming, timeout
- Graph paths: must resolve inside `graphify-out/`
- Labels: control chars stripped, capped at 256 chars, HTML-escaped

## Dep graph within graphify/

```
__init__.py ──────────────────────────→ (all modules via lazy __getattr__)
__main__.py → hooks.py, watch.py
extract.py → cache.py
build.py → validate.py
models.py → (standalone, stdlib only)
export.py → security.py
serve.py → security.py
ingest.py → security.py
detect.py → (standalone, no internal deps)
cluster.py → (standalone, external: graspologic)
analyze.py → (standalone, external: networkx)
report.py → analyze.py
wiki.py → (standalone, external: networkx)
benchmark.py → (standalone, external: networkx)
```

No circular dependencies. The pipeline is strictly linear — each stage consumes the output of the previous one.
