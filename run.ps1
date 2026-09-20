param(
    [ValidateSet('Check', 'Test', 'Tables', 'Prepare', 'Costs', 'Bot')]
    [string]$Mode = 'Check',
    [string]$Python = '',
    [string]$Topic = '',
    [switch]$UseLocalDependencies
)
$ErrorActionPreference = 'Stop'
if (-not $Python) { $Python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python not found. Create .venv or provide -Python with an absolute interpreter path.'
}
$Python = (Resolve-Path -LiteralPath $Python).Path
$previousPythonPath = $env:PYTHONPATH
$previousEncoding = $env:PYTHONIOENCODING
Push-Location $PSScriptRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    if ($UseLocalDependencies) {
        $env:PYTHONPATH = (Join-Path $PSScriptRoot '.runtime_deps') + ';' + (Join-Path $PSScriptRoot '.test_deps')
    }
    switch ($Mode) {
        'Check' { & $Python bot.py --check }
        'Test' { & $Python -m unittest discover -s tests -q }
        'Tables' { & $Python build_tables.py }
        'Prepare' {
            if (-not $Topic) { throw 'Prepare requires -Topic.' }
            & $Python build_index.py --topic $Topic --prepare
        }
        'Costs' { & $Python scripts/compare_models.py }
        'Bot' { & $Python bot.py }
    }
    $resultCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONIOENCODING = $previousEncoding
}
exit $resultCode
