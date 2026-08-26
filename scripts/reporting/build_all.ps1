param(
  [string]$Audit = "audit/example/project_audit.json",
  [string]$Events = "audit/example/events.jsonl",
  [string]$Output = "reports/generated",
  [string]$Python = $env:RUNTIME_PYTHON,
  [string]$Node = $env:RUNTIME_NODE,
  [string]$NodeModules = $env:RUNTIME_NODE_MODULES,
  [string]$RuntimeBinDir = $env:RUNTIME_BIN_DIR,
  [string]$SkillDir = $env:SKILL_DIR
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
if (-not $Python) { $Python = "python" }
if (-not $Node) { throw "RUNTIME_NODE is required by the Presentations workflow" }
if (-not $NodeModules) { throw "RUNTIME_NODE_MODULES is required by the Presentations workflow" }
if (-not $RuntimeBinDir) { throw "RUNTIME_BIN_DIR is required by the Presentations workflow" }
if (-not $SkillDir) { throw "SKILL_DIR must point to the Presentations skill" }
$env:RUNTIME_NODE = $Node
$env:RUNTIME_NODE_MODULES = $NodeModules
$env:RUNTIME_BIN_DIR = $RuntimeBinDir

$AuditPath = Join-Path $Root $Audit
$EventsPath = Join-Path $Root $Events
$OutputPath = Join-Path $Root $Output

& $Python (Join-Path $PSScriptRoot "verify_audit.py") $AuditPath $EventsPath
& $Python (Join-Path $PSScriptRoot "build_report.py") --audit $AuditPath --events $EventsPath --output $OutputPath

$RuntimeDir = Join-Path $OutputPath ".reporting-runtime"
$RuntimeModules = Join-Path $RuntimeDir "node_modules"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "build_interview_deck.mjs") -Destination (Join-Path $RuntimeDir "build_interview_deck.mjs") -Force
if (-not (Test-Path -LiteralPath $RuntimeModules)) {
  New-Item -ItemType Junction -Path $RuntimeModules -Target $NodeModules | Out-Null
}
$DeckBuilder = Join-Path $RuntimeDir "build_interview_deck.mjs"
$Marker = Join-Path $SkillDir "container_tools/mark_artifact_operation_started.mjs"
& $Node $Marker --operation-kind create --expected-output-count 1 --output-format pptx
& $Node $DeckBuilder --input (Join-Path $OutputPath "deck-data.json") --output (Join-Path $OutputPath "ppt-eval-interview.pptx")
& $Python (Join-Path $SkillDir "container_tools/slides_test.py") (Join-Path $OutputPath "ppt-eval-interview.pptx")

Write-Host "HTML: $(Join-Path $OutputPath 'project-audit.html')"
Write-Host "PPTX: $(Join-Path $OutputPath 'ppt-eval-interview.pptx')"
