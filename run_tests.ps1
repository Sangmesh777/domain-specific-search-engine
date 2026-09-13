$ErrorActionPreference = "Stop"

Write-Host "=== Search Engine Regression Runner ==="

python -m pytest

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nRegression tests FAILED." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`nRegression tests PASSED." -ForegroundColor Green
