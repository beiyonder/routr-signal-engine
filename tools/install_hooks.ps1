# Install local git hooks. Run once per clone:
#   .\.venv\Scripts\python.exe -V; .\tools\install_hooks.ps1
#
# The hook then runs automatically before every `git commit` and blocks
# any credential-shaped file or content from being committed.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath ".git/hooks")) {
    Write-Host "ERR: no .git/hooks directory (not a git repo, or shallow worktree)." -ForegroundColor Red
    exit 1
}

$src = Join-Path $root "tools/git-hooks/pre-commit"
$dst = Join-Path $root ".git/hooks/pre-commit"

# Wrapper that invokes the Python hook through the local venv so the user
# does not need a system-wide Python on PATH.
$wrapper = @'
#!/bin/sh
# Invokes the Python pre-commit guard via the local .venv.
ROOT="$(git rev-parse --show-toplevel)"
PY_WIN="$ROOT/.venv/Scripts/python.exe"
PY_NIX="$ROOT/.venv/bin/python"
if [ -x "$PY_WIN" ]; then PYTHON="$PY_WIN"
elif [ -x "$PY_NIX" ]; then PYTHON="$PY_NIX"
else PYTHON="python3"
fi
exec "$PYTHON" "$ROOT/tools/git-hooks/pre-commit"
'@

Set-Content -LiteralPath $dst -Value $wrapper -Encoding ascii -NoNewline
Write-Host "Installed: $dst" -ForegroundColor Green
Write-Host "Pre-commit guard active. Test it by staging any file whose content" -ForegroundColor Gray
Write-Host "matches an OpenAI / Anthropic / Gemini / Discord-webhook shape and" -ForegroundColor Gray
Write-Host "trying to commit. The hook will block with a redacted preview of the" -ForegroundColor Gray
Write-Host "matched pattern. Bypass intentionally only via 'git commit --no-verify'." -ForegroundColor Gray
