param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $root "..\..")).Path
$upstream = Join-Path $root "upstream"
$commit = "98e0c012e89469863d9c3c8bc87eac967d82b2e6"

if (-not (Test-Path (Join-Path $upstream ".git"))) {
    git clone https://github.com/para-lost/AutoPresent.git $upstream
}

$head = (git -C $upstream rev-parse HEAD).Trim()
if ($head -ne $commit) {
    git -C $upstream fetch origin $commit
    git -C $upstream checkout --detach $commit
}

if (-not (Test-Path $Python)) {
    throw "Python environment not found: $Python"
}
$Python = (Resolve-Path $Python).Path
$pythonDirectory = Split-Path -Parent $Python
$pythonEnvironment = if ((Split-Path -Leaf $pythonDirectory) -in @("Scripts", "bin")) {
    Split-Path -Parent $pythonDirectory
}
else {
    $pythonDirectory
}

& $Python (Join-Path $root "audit_dataset.py") `
    --upstream $upstream `
    --output (Join-Path $root "evidence\dataset-audit.json")
if ($LASTEXITCODE -ne 0) {
    throw "Dataset audit failed with exit code $LASTEXITCODE"
}

$evaluate = Join-Path $upstream "evaluate"
$deck = Join-Path $upstream "slidesbench\examples\food\food.pptx"
$log = Join-Path $root "evidence\official-self-eval-cached.log"
$unusedOutput = Join-Path $root "evidence\upstream-output-path-bug.json"

Push-Location $evaluate
try {
    $rawOutput = & $Python page_eval.py `
        --generated_pptx $deck `
        --generated_page 1 `
        --reference_pptx $deck `
        --reference_page 1 `
        --output_path $unusedOutput 2>&1
    $exitCode = $LASTEXITCODE
    $portableOutput = @(
        $rawOutput | ForEach-Object {
            ([string]$_).Replace($repoRoot, "<repo>").Replace(
                $pythonEnvironment,
                "<python-env>"
            ).Replace("\", "/")
        }
    )
    $portableOutput | Tee-Object -FilePath $log
    if ($exitCode -ne 0) {
        throw "SlidesBench page evaluator failed with exit code $exitCode"
    }
}
finally {
    Pop-Location
}

Write-Output "Smoke complete. Expected identity result at this commit: match=100, text=100, color=25, position=100."
Write-Output "The missing custom output file is an upstream bug recorded by this audit."
