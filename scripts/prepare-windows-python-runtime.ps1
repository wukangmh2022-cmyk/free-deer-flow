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
$runtimeEntryScript = Join-Path $repoRoot "scripts\windows_desktop_runtime_entry.py"
$runtimeExeName = "deerflow-runtime.exe"
$runtimeExePath = Join-Path $outputDir $runtimeExeName
$pyinstallerVenvDir = Join-Path $wheelhouseDir "pyinstaller-build-venv"
$pyinstallerWorkDir = Join-Path $wheelhouseDir "pyinstaller-work"
$pyinstallerDistDir = Join-Path $wheelhouseDir "pyinstaller-dist"
$harnessProject = Join-Path $repoRoot "backend\packages\harness"
$backendProject = Join-Path $repoRoot "backend"
$backendRequirements = Join-Path $wheelhouseDir "requirements-windows-runtime.txt"

Write-Host "Preparing DeerFlow Windows Python runtime..."
Write-Host "Output: $outputDir"
Write-Host "Variant: $Variant"
Write-Host "Playwright browsers dir: $playwrightBrowsersDir"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "python not found on PATH. Install Python on this Windows build machine first."
}

if (Test-Path $outputDir) {
  Remove-Item -Recurse -Force $outputDir
}

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
New-Item -ItemType Directory -Force -Path $wheelhouseDir | Out-Null

if ($Variant -eq "full") {
  New-Item -ItemType Directory -Force -Path $playwrightBrowsersDir | Out-Null
} elseif (Test-Path $playwrightBrowsersDir) {
  Remove-Item -Recurse -Force $playwrightBrowsersDir
}

if ($Variant -eq "thin-no-browser") {
  foreach ($path in @($pyinstallerVenvDir, $pyinstallerWorkDir, $pyinstallerDistDir)) {
    if (Test-Path $path) {
      Remove-Item -Recurse -Force $path
    }
  }

  python -m venv $pyinstallerVenvDir

  $buildPython = Join-Path $pyinstallerVenvDir "Scripts\python.exe"
  & $buildPython -m pip install --upgrade pip pyinstaller
  $requirementsGenerator = @'
import sys
import tomllib
from pathlib import Path

backend_dir = Path(sys.argv[1])
requirements_path = Path(sys.argv[2])
data = tomllib.loads((backend_dir / "pyproject.toml").read_text(encoding="utf-8"))
deps = []
for dep in data.get("project", {}).get("dependencies", []):
    if dep.strip().lower() == "deerflow-harness":
        continue
    deps.append(dep)
requirements_path.write_text("\n".join(deps) + "\n", encoding="utf-8")
'@
  & $buildPython -c $requirementsGenerator $backendProject $backendRequirements
  & $buildPython -m pip install -r $backendRequirements
  & $buildPython -m pip install $harnessProject

  & $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name deerflow-runtime `
    --distpath $pyinstallerDistDir `
    --workpath $pyinstallerWorkDir `
    --specpath $pyinstallerWorkDir `
    --paths (Join-Path $repoRoot "backend") `
    --paths (Join-Path $repoRoot "backend\packages\harness") `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    --collect-submodules deerflow `
    --collect-submodules app `
    --collect-all playwright `
    --collect-all duckdb `
    --collect-all tiktoken `
    --collect-all pypdfium2_raw `
    --collect-all numpy `
    --collect-all cryptography `
    --collect-all lxml `
    --copy-metadata deerflow-harness `
    $runtimeEntryScript

  if (-not (Test-Path (Join-Path $pyinstallerDistDir $runtimeExeName))) {
    throw "PyInstaller build failed: deerflow-runtime.exe was not produced."
  }

  Copy-Item (Join-Path $pyinstallerDistDir $runtimeExeName) $runtimeExePath -Force

  $runtimeInfo = @{
    variant = $Variant
    generatedAt = (Get-Date).ToString("o")
    playwrightBrowserMode = "system"
    runtimeKind = "pyinstaller-onefile"
    executable = $runtimeExeName
    prunedSitePackagePatterns = @(
      "sympy*",
      "pandas*",
      "speech_recognition*",
      "onnxruntime*",
      "kubernetes*",
      "volcengine*",
      "youtube_transcript_api*"
    )
  }
  $runtimeInfo | ConvertTo-Json -Depth 6 | Set-Content -Path $runtimeInfoPath -Encoding UTF8

  Write-Host ""
  Write-Host "Windows desktop runtime prepared."
  Write-Host "Packaged desktop can use: $runtimeExePath"
  Write-Host "Playwright browser mode: system Chrome/Edge required at runtime"
  Write-Host "Runtime metadata: $runtimeInfoPath"
  Write-Host "Next step: include '$outputDir' as runtime/python in the Windows build."
  return
}

$venvDir = Join-Path $outputDir "venv"
if (-not (Test-Path $venvDir)) {
  python -m venv $venvDir
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"
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
