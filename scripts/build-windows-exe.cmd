@echo off
setlocal

set "ROOT_DIR=%~dp0.."
set "DESKTOP_DIR=%ROOT_DIR%\desktop\electron"

where pnpm >NUL 2>NUL
if errorlevel 1 (
  echo pnpm not found. Please install pnpm first.
  exit /b 1
)

echo [build-windows-exe] root: %ROOT_DIR%
echo [build-windows-exe] desktop: %DESKTOP_DIR%

pushd "%DESKTOP_DIR%"
if not exist node_modules (
  echo [build-windows-exe] installing dependencies...
  call pnpm install --frozen-lockfile
  if errorlevel 1 (
    popd
    exit /b %ERRORLEVEL%
  )
)

echo [build-windows-exe] building Windows NSIS installer (.exe)...
call pnpm run dist:win
set "RC=%ERRORLEVEL%"

if %RC% NEQ 0 (
  popd
  exit /b %RC%
)

echo [build-windows-exe] build done. artifacts:
dir /b /s dist\*.exe dist\*.yml dist\*.blockmap 2>NUL

popd
exit /b 0
