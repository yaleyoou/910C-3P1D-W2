@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
set "base=https://github.com/yaleyoou/910C-3P1D-W2/releases/download/w2-v1"
set "imagehash=0e7cfc4416208ccb7b7c90cac31d0d0bc5d8e2dd224058c8b6d9c03d1f7351e4"
set "manifesthash=6dd1a8925d5630b6e92e15acff2627d7b02b11da0733a3be086c505c68c9ab29"
set "out=910C-files"
set "parts=w2-image-parts"
if not exist "%out%\" mkdir "%out%" || goto fail
if not exist "%parts%\" mkdir "%parts%" || goto fail
where curl.exe >nul 2>nul || goto fail
where powershell.exe >nul 2>nul || goto fail
where certutil.exe >nul 2>nul || goto fail
call :check release.sha256 %manifesthash% || goto fail
set "ready=no"
call :check "%out%\image.tar.zst" %imagehash% && set "ready=yes"
for /f "usebackq tokens=1,2" %%H in ("release.sha256") do (
    call :fetch "%%I" "%%H" || goto fail
)
if "%ready%"=="yes" goto done
echo Merging 836 verified parts...
powershell.exe -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; $o=[IO.File]::Create('910C-files\image.tar.zst.partial'); try { foreach($i in 0..835){$f=[IO.File]::OpenRead(('w2-image-parts\image.seg-{0:D4}' -f $i)); try{$f.CopyTo($o)}finally{$f.Dispose()}} } finally {$o.Dispose()}"
if errorlevel 1 goto fail
call :check "%out%\image.tar.zst.partial" %imagehash% || goto fail
move /y "%out%\image.tar.zst.partial" "%out%\image.tar.zst" >nul || goto fail
:done
echo READY - Copy the three files in the 910C-files folder to your server.
dir /b "%out%"
pause
popd
exit /b 0

:fetch
set "name=%~1"
set "target=%out%\%~1"
if "%name:~0,10%"=="image.seg-" (
    if "%ready%"=="yes" exit /b 0
    set "target=%parts%\%~1"
)
call :check "%target%" %~2 && exit /b 0
echo Downloading %name% ...
curl.exe -fLsS --retry 3 --connect-timeout 30 -o "%target%.partial" "%base%/%name%" || exit /b 1
call :check "%target%.partial" %~2 || exit /b 1
move /y "%target%.partial" "%target%" >nul || exit /b 1
exit /b 0

:check
if not exist "%~1" exit /b 1
certutil.exe -hashfile "%~1" SHA256 | findstr.exe /i /x /c:"%~2" >nul
exit /b %errorlevel%

:fail
echo Download or verification failed. Check the network and free space, then double-click again.
pause
popd
exit /b 1
