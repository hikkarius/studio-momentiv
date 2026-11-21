# Architectural Safety Rules — AI Video Moments Analyzer Studio

> Синхронізовано з `.antigravity/ARCHITECTURE.md` (v3.0).

## 1. Path & Boundary Protection
- Never validate directory containment via `str.startswith()` (vulnerable to sibling prefix bypass). Always use `Path(target).resolve().is_relative_to(Path(allowed_root).resolve())`.
- Validate all `project_id` path parameters strictly (`^[\w\-]{1,128}$`, no `..` or path separators).
- Restrict `/api/upload` to video extensions whitelist and `/api/media` to safe media extensions with `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`.
- Block `localhost`, loopback, and private/link-local IPs in `download_from_url()` and always pass `--` before the URL to `yt-dlp`.

## 2. Windows Console & Subprocess Discipline
- `setup_console_encoding()` is mandatory in every entrypoint to prevent `cp1251` crashes on Ukrainian characters.
- Every `subprocess.run()` must enforce explicit `timeout=`.

## 3. Concurrency, Hardware Probing & Database Safety
- Always read GPU capabilities via `app_state.get_gpu_capabilities()` (cached) rather than calling `probe_gpu_capabilities()` directly per request.
- Detect CUDA via `ctranslate2.get_cuda_device_count()` first (native `faster-whisper` backend), falling back to `torch`.
- Use per-resource mutexes (`_get_slice_path_lock`) rather than global locks across unrelated clips/projects.
- Use `INSERT ... ON CONFLICT(id) DO UPDATE` instead of `INSERT OR REPLACE` on tables referenced by `ON DELETE CASCADE`.
- Never execute `db.init_db()` as a module import side effect; initialize explicitly in FastAPI `lifespan`.

## 4. Filter Mode Canonicalization & Frontend Safety
- Always canonicalize incoming UI/CLI `filter_mode` values via `resolve_filter_mode()` before looking up `FILTER_INSTRUCTIONS`.
- Never interpolate dynamic IDs inside inline HTML `onclick="..."` attributes; attach handlers via `addEventListener`.
