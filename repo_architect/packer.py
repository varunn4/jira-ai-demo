"""Wraps the Repomix CLI to produce a packed XML file per repo.

Repomix docs: https://github.com/yamadashy/repomix
Install: `npm install -g repomix`

Skeleton mode (`--compress`) uses tree-sitter to strip function bodies and keep
signatures, types, and class structure. This is the ~90% token reduction lever.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class RepomixNotInstalled(RuntimeError):
    pass


def _ensure_repomix() -> None:
    if shutil.which("repomix") is None:
        raise RepomixNotInstalled(
            "repomix CLI not found in PATH. Install with: npm install -g repomix"
        )


# Patterns ignored beyond Repomix's built-in defaults (which already handle
# node_modules, .git, dist, build, lockfiles, and common binary types).
# Grouped by ecosystem so this is easy to scan when something unexpected slips
# through and bloats a scan.
EXTRA_IGNORES = [
    # ---- Python / JS / TS ----
    "**/*.lock", "**/*.min.js", "**/*.min.css",
    "**/migrations/**", "**/__pycache__/**", "**/.pytest_cache/**",
    "**/coverage/**", "**/.next/**", "**/.nuxt/**",
    "**/venv/**", "**/.venv/**", "**/node_modules/**",
    "**/dist/**", "**/build/**", "**/.cache/**", "**/.turbo/**",

    # ---- PHP / Laravel / Symfony ----
    # Composer deps are the #1 source of bloat in PHP repos.
    "**/vendor/**",
    # Laravel framework cache + compiled artifacts (NOT routes/, app/, config/)
    "**/bootstrap/cache/**", "**/storage/framework/**", "**/storage/logs/**",
    "**/storage/app/**", "**/storage/debugbar/**",
    # Compiled templates, view caches
    "**/resources/views/**",      # blade templates are presentation, not arch
    "**/resources/lang/**",       # translation strings
    "**/resources/css/**", "**/resources/sass/**", "**/resources/scss/**",
    # DB seeders are usually huge inline data dumps
    "**/database/seeders/**", "**/database/factories/**",
    # Symfony equivalents
    "**/var/cache/**", "**/var/log/**",

    # ---- IaC noise ----
    "**/*.tfstate", "**/*.tfstate.backup", "**/.terraform/**",
    "**/cdk.out/**", "**/.serverless/**",

    # ---- Generic vendored / third-party ----
    "**/third_party/**", "**/third-party/**", "**/thirdparty/**",

    # ---- Assets, statics, anything served as-is ----
    "**/assets/**", "**/static/**", "**/public/**",

    # ---- Tests + fixtures ----
    "**/tests/**", "**/test/**", "**/__tests__/**",
    "**/*.test.js", "**/*.test.ts", "**/*.spec.js", "**/*.spec.ts",
    "**/test_*.py", "**/*_test.py",
    "**/fixtures/**", "**/snapshots/**", "**/__snapshots__/**",
    "**/phpunit.xml", "**/phpunit.xml.dist",

    # ---- Binary / data blobs ----
    "**/*.csv", "**/*.parquet", "**/*.pkl", "**/*.h5", "**/*.onnx",
    "**/*.bin", "**/*.tar", "**/*.gz", "**/*.zip", "**/*.7z",
    "**/*.apk", "**/*.map",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.svg",
    "**/*.pdf", "**/*.ico", "**/*.woff", "**/*.woff2", "**/*.ttf", "**/*.eot",
    "**/*.mp3", "**/*.mp4", "**/*.mov", "**/*.avi",
    # Big text dumps that aren't code
    "**/error_log", "**/sitemap.xml", "**/sitemap-*.xml",
]


def pack_repo(
    repo_path: Path,
    output_file: Path,
    use_skeleton: bool = True,
    extra_ignores: list[str] | None = None,
) -> Path:
    """Pack a single repo into `output_file`. Returns the output path.

    Default ignores: Repomix already handles node_modules, .git, dist, build,
    lockfiles, and common binary types. EXTRA_IGNORES (above) layers on
    framework-specific noise that Repomix doesn't know about — particularly
    Laravel's vendor/, storage/, bootstrap/cache/, and DB seeders.
    """
    _ensure_repomix()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    ignore_patterns = EXTRA_IGNORES + [p for p in (extra_ignores or []) if p]

    cmd = [
        "repomix",
        str(repo_path),
        "--style", "xml",                # Claude handles XML cleanly
        "--output", str(output_file),
        "--no-file-summary",             # skip the per-file token count summary
        "--ignore", ",".join(ignore_patterns),
    ]
    if use_skeleton:
        cmd.append("--compress")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"repomix failed for {repo_path}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    if not output_file.exists():
        raise RuntimeError(f"repomix did not produce {output_file}")

    return output_file


def estimate_tokens(packed_file: Path) -> int:
    """Rough estimate: ~4 chars per token. Good enough for budget checks before
    paying for a Claude call. For exact counts, use anthropic.count_tokens()."""
    size_chars = packed_file.stat().st_size
    return size_chars // 4
