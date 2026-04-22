param(
  [ValidateSet("full", "thin-no-browser")]
  [string]$Variant = "full",
  [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$desktopRuntimeDir = Join-Path $repoRoot "desktop\electron\runtime\python"
$outputDir = if ($TargetDir) { $TargetDir } else { $desktopRuntimeDir }
$wheelhouseDir = Join-Path $repoRoot "desktop\electron\.wheelhouse"
$playwrightBrowsersDir = Join-Path $outputDir "ms-playwright"
$runtimeInfoPath = Join-Path $outputDir "runtime-build.json"

Write-Host "Preparing DeerFlow Windows Python runtime..."
Write-Host "Output: $outputDir"
Write-Host "Variant: $Variant"
Write-Host "Playwright browsers dir: $playwrightBrowsersDir"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "python not found on PATH. Install Python on this Windows build machine first."
}

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
New-Item -ItemType Directory -Force -Path $wheelhouseDir | Out-Null

if ($Variant -eq "full") {
  New-Item -ItemType Directory -Force -Path $playwrightBrowsersDir | Out-Null
} elseif (Test-Path $playwrightBrowsersDir) {
  Remove-Item -Recurse -Force $playwrightBrowsersDir
}

$venvDir = Join-Path $outputDir "venv"
if (-not (Test-Path $venvDir)) {
  python -m venv $venvDir
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"
$harnessProject = Join-Path $repoRoot "backend\packages\harness"
$backendProject = Join-Path $repoRoot "backend"
$harnessDist = Join-Path $wheelhouseDir "harness-dist"
$backendDist = Join-Path $wheelhouseDir "backend-dist"

if (Test-Path $harnessDist) {
  Remove-Item -Recurse -Force $harnessDist
}

if (Test-Path $backendDist) {
  Remove-Item -Recurse -Force $backendDist
}

New-Item -ItemType Directory -Force -Path $harnessDist | Out-Null
New-Item -ItemType Directory -Force -Path $backendDist | Out-Null

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip wheel --wheel-dir $harnessDist $harnessProject
& $pythonExe -m pip install --no-index --find-links $harnessDist deerflow-harness
& $pythonExe -m pip wheel --wheel-dir $backendDist $backendProject
& $pythonExe -m pip install --no-index --find-links $backendDist deer-flow

function Remove-SitePackagePatterns {
  param(
    [string]$SitePackagesDir,
    [string[]]$Patterns
  )

  foreach ($pattern in $Patterns) {
    Get-ChildItem -Path $SitePackagesDir -Filter $pattern -Force -ErrorAction SilentlyContinue | ForEach-Object {
      Write-Host "Pruning site-packages entry: $($_.Name)"
      Remove-Item -Recurse -Force $_.FullName
    }
  }
}

$sitePackagesDir = Join-Path $venvDir "Lib\site-packages"
$prunedPatterns = @()

if ($Variant -eq "thin-no-browser") {
  $prunedPatterns = @(
    "sympy*",
    "pandas*",
    "speech_recognition*",
    "onnxruntime*",
    "kubernetes*",
    "volcengine*",
    "youtube_transcript_api*"
  )
  Remove-SitePackagePatterns -SitePackagesDir $sitePackagesDir -Patterns $prunedPatterns
} else {
  $env:PLAYWRIGHT_BROWSERS_PATH = $playwrightBrowsersDir
  & $pythonExe -m playwright install chromium

  $chromiumDirs = Get-ChildItem -Path $playwrightBrowsersDir -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "chromium-*" }
  if (-not $chromiumDirs) {
    throw "Playwright Chromium install failed: no chromium-* directory found under $playwrightBrowsersDir"
  }
}

$runtimeInfo = @{
  variant = $Variant
  generatedAt = (Get-Date).ToString("o")
  playwrightBrowserMode = if ($Variant -eq "thin-no-browser") { "system" } else { "bundled" }
  prunedSitePackagePatterns = $prunedPatterns
}
$runtimeInfo | ConvertTo-Json -Depth 4 | Set-Content -Path $runtimeInfoPath -Encoding UTF8

Write-Host ""
Write-Host "Python runtime prepared."
Write-Host "Packaged desktop can use: $pythonExe"
if ($Variant -eq "full") {
  Write-Host "Bundled Playwright Chromium: $($chromiumDirs[0].FullName)"
} else {
  Write-Host "Playwright browser mode: system Chrome/Edge required at runtime"
}
Write-Host "Runtime metadata: $runtimeInfoPath"
Write-Host "Next step: copy or include '$outputDir' as runtime/python in the Windows build."
