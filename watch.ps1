# watch.ps1 — 监控收件文件夹(默认 ~\Inbox)根目录,落盘即整理
# 用法:
#   powershell -ExecutionPolicy Bypass -File watch.ps1            # 实跑(改名+归档)
#   powershell -ExecutionPolicy Bypass -File watch.ps1 -DryRun    # 只预览不动文件
# 停止:窗口里 Ctrl+C(或停掉 TempOrganizer-Watch 计划任务)
param(
  [string]$Root = (Join-Path $env:USERPROFILE "Inbox"),
  [int]$DebounceSec = 6,
  [int]$PeriodicSec = 120,
  [switch]$DryRun
)
$ErrorActionPreference = "Stop"
# 让 PowerShell 用 UTF-8 收 python 子进程输出(否则中文按 GBK 误读成乱码)
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$py = "python"   # 或填 python 绝对路径(uv 用户:$env:USERPROFILE\.local\bin\python.exe)
$engine = Join-Path $PSScriptRoot "organize.py"
$logFile = Join-Path $Root "索引\watch.log"

function Write-Log($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  Write-Host $line
  try { Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8 } catch {}
}

# 单实例锁(系统级 mutex):已有 watcher 在跑就立刻自杀退出,从根上杜绝累积(被 kill 后是 abandoned mutex,下一个能接管)
$script:mutex = New-Object System.Threading.Mutex($false, "Global\TempOrganizerWatch_4linshi")
$acquired = $false
try { $acquired = $script:mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) { Write-Log "已有 watcher 实例在跑(mutex),本实例退出。"; exit 0 }

function Invoke-Scan($periodic) {
  $a = @($engine, "scan", "--root", $Root)
  if (-not $DryRun) { $a += "--apply" }
  if ($periodic) { $a += "--quiet" }
  & $py @a 2>&1 | ForEach-Object { Write-Log $_ }
}

# 确保 00_系统 骨架存在
& $py $engine init --root $Root | Out-Null

$fsw = New-Object System.IO.FileSystemWatcher
$fsw.Path = $Root
$fsw.IncludeSubdirectories = $false   # 只监控根目录这一层(子文件夹是归档目的地,绝不重触发)
$fsw.NotifyFilter = [System.IO.NotifyFilters]([System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::Size -bor [System.IO.NotifyFilters]::LastWrite)
$fsw.EnableRaisingEvents = $true

# 不用 -Action(跨 runspace 不可靠),改用主会话事件队列 + 去抖
Register-ObjectEvent $fsw Created -SourceIdentifier FSW_Created | Out-Null
Register-ObjectEvent $fsw Renamed -SourceIdentifier FSW_Renamed | Out-Null

Write-Log "监控启动: $Root  (debounce ${DebounceSec}s, periodic ${PeriodicSec}s, dryrun=$DryRun)"
# 启动先处理已在根目录的积压文件(FileSystemWatcher 不会对已存在文件触发)
Write-Log "启动扫描:处理已有文件…"
Invoke-Scan $false

$pendingSince = $null
$lastPeriodic = Get-Date
try {
  while ($true) {
    $events = @(Get-Event -ErrorAction SilentlyContinue)
    if ($events.Count -gt 0) {
      $pendingSince = Get-Date
      foreach ($e in $events) { Remove-Event -EventIdentifier $e.EventIdentifier }
    }
    $now = Get-Date
    $eventReady = $pendingSince -and ((New-TimeSpan -Start $pendingSince -End $now).TotalSeconds -ge $DebounceSec)
    $periodicReady = ((New-TimeSpan -Start $lastPeriodic -End $now).TotalSeconds -ge $PeriodicSec)
    if ($eventReady) {
      $pendingSince = $null; $lastPeriodic = $now
      Write-Log "检测到新文件,去抖结束,开始整理…"
      Invoke-Scan $false
      Write-Log "整理完成,继续监控。"
    }
    elseif ($periodicReady) {
      # 周期兜底:重扫捞回被跳过/漏掉的文件 + 索引对账(摘悬空、列孤儿),都静默
      $lastPeriodic = $now
      Invoke-Scan $true
      & $py $engine reconcile --root $Root --quiet 2>&1 | ForEach-Object { Write-Log $_ }
    }
    Start-Sleep -Seconds 1
  }
} finally {
  $fsw.EnableRaisingEvents = $false
  Unregister-Event -SourceIdentifier FSW_Created -ErrorAction SilentlyContinue
  Unregister-Event -SourceIdentifier FSW_Renamed -ErrorAction SilentlyContinue
  $fsw.Dispose()
  try { $script:mutex.ReleaseMutex() } catch {}
  Write-Log "监控停止。"
}
