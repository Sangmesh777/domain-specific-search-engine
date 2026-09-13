$ErrorActionPreference = "Stop"

Write-Host "=== Phase 12 Regression Runner ==="

Write-Host "`n[1/3] Checking Flask..."
try {
    $status = Invoke-RestMethod `
        -Uri "http://127.0.0.1:5000/api/status" `
        -Method GET `
        -TimeoutSec 10
}
catch {
    Write-Host "Flask is not reachable at http://127.0.0.1:5000" -ForegroundColor Red
    Write-Host "Start it first with: python app.py"
    exit 1
}

Write-Host ("Flask READY. Documents: {0}" -f $status.documents)

Write-Host "`n[2/3] Running pytest..."
python -m pytest tests/test_phase12_live.py -v

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nRegression suite FAILED." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n[3/3] Final database status..."
Invoke-RestMethod `
    -Uri "http://127.0.0.1:5000/api/status" `
    -Method GET

Write-Host "`nRegression suite PASSED." -ForegroundColor Green
