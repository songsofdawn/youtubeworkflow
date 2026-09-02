@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

title YouTube Workflow - 恢复失败下载

REM ============================================================
REM 基础配置
REM ============================================================

set "ROOT=C:\Users\23664\Desktop\program\youtubeworkflow"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "COOKIE=%ROOT%\private\cookies.txt"

REM 原失败任务所在日期目录
set "TARGET=%ROOT%\downloads\manual\2026-09-02"

REM 旧失败目录备份到 downloads 外面，
REM 避免控制面板继续扫描到旧的 failed manifest
set "BACKUP=%ROOT%\recovery_backup\manual_2026-09-02_%RANDOM%"

REM ============================================================
REM 环境检查
REM ============================================================

echo.
echo ============================================================
echo YouTube Workflow 批量恢复
echo ============================================================
echo.

if not exist "%PY%" (
    echo [ERROR] 找不到 Python:
    echo %PY%
    pause
    exit /b 1
)

if not exist "%COOKIE%" (
    echo [ERROR] 找不到 Cookie:
    echo %COOKIE%
    pause
    exit /b 1
)

if not exist "%TARGET%" (
    echo [ERROR] 找不到任务目录:
    echo %TARGET%
    pause
    exit /b 1
)

mkdir "%BACKUP%" >nul 2>&1

cd /d "%ROOT%"

set /a SUCCESS=0
set /a FAILED=0
set /a TOTAL=0

REM ============================================================
REM 需要恢复的视频
REM ============================================================

for %%V in (
5fSU1-OYZL0
5R6qNCaRSE8
7E0TG9uJHZ8
DJWjbWhoVI0
fVipuIKy2GQ
gCCU6comkTc
gEf69wmMiCs
hg26j0erUSo
HHXeMxnYDvs
HZYNKsX-EL8
iGL0DlUykbc
Ip9TbMZWa50
O-P7s3qF9VQ
OPSVlJt_mAM
pj6UU2LiUfc
PoNtbiKGHnM
pxrTP73P4ts
SRDeYhqD-0s
Ul1cBzpU45A
uL7ph7Iv5oo
WwhHuAAuUjw
Y6CtWQiuY7s
Zj3GdwUEtAg
_42617rwRDw
_XtmQGjtCRs
) do (
    call :RECOVER "%%V"
)

goto :FINISH


REM ============================================================
REM 单个视频恢复函数
REM ============================================================

:RECOVER

set "VID=%~1"
set "OLD_DIR="

set /a TOTAL+=1

echo.
echo ============================================================
echo [!TOTAL!] 正在恢复: !VID!
echo ============================================================

REM 找到原来的失败任务目录
for /d %%D in ("%TARGET%\!VID!_*") do (
    if not defined OLD_DIR (
        set "OLD_DIR=%%~fD"
    )
)

if defined OLD_DIR (
    echo 原任务:
    echo !OLD_DIR!
) else (
    echo [WARN] 没找到旧目录，将直接重新下载。
)

echo.
echo 开始重新下载...
echo.

"%PY%" -m src.download_video ^
    --url "https://www.youtube.com/watch?v=!VID!" ^
    --output "%ROOT%\downloads\manual" ^
    --cookies-path "%COOKIE%" ^
    --confirm-rights ^
    --rights-status PERMISSION_GRANTED

set "RC=!ERRORLEVEL!"

if "!RC!"=="0" (

    echo.
    echo [OK] !VID! 下载恢复成功。
    set /a SUCCESS+=1

    REM --------------------------------------------------------
    REM download_core 成功获取 metadata 后可能按 upload_date
    REM 创建新的正确任务目录。
    REM
    REM 如果旧目录已经被原地恢复，则里面会出现 source.mp4。
    REM 如果旧目录仍只有 failed manifest，则把它移出 downloads，
    REM 防止控制面板同时显示一个成功任务 + 一个旧失败任务。
    REM --------------------------------------------------------

    if defined OLD_DIR (

        if exist "!OLD_DIR!\video\source.mp4" (

            echo [OK] 原任务目录已原地恢复。

        ) else (

            echo [INFO] 新任务已生成，正在归档旧失败目录...

            move "!OLD_DIR!" "%BACKUP%\" >nul

            if errorlevel 1 (
                echo [WARN] 旧失败目录归档失败，请之后手动处理:
                echo !OLD_DIR!
            ) else (
                echo [OK] 旧失败目录已移动到:
                echo %BACKUP%
            )
        )
    )

    REM 避免连续快速请求再次触发 YouTube anti-bot
    set /a WAIT=8 + !RANDOM! %% 8

    echo.
    echo 等待 !WAIT! 秒后处理下一个视频...
    timeout /t !WAIT! /nobreak >nul

) else (

    echo.
    echo [FAIL] !VID! 恢复失败，错误码 !RC!
    echo 原目录不会被删除或移动。

    set /a FAILED+=1

    echo 为避免连续触发 YouTube 风控，等待 30 秒...
    timeout /t 30 /nobreak >nul
)

exit /b 0


REM ============================================================
REM 完成
REM ============================================================

:FINISH

echo.
echo ============================================================
echo 恢复完成
echo ============================================================
echo.
echo 总任务数 : !TOTAL!
echo 成功     : !SUCCESS!
echo 失败     : !FAILED!
echo.
echo 旧失败任务备份:
echo %BACKUP%
echo.
echo 请重新打开/刷新 YouTube Workflow 控制面板。
echo ============================================================
echo.

pause
endlocal