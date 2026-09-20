<#
.SYNOPSIS
  一键修好"板子连不进来"的防火墙设置：关掉全局入站拦截，放行遥测端口。

.DESCRIPTION
  真机联调时最隐蔽的坑：Windows 防火墙里 Block 规则的优先级高于 Allow。
  只要存在一条"拦住全部入站"的 Block（例如某些沙箱/安全软件会加），
  那么无论加多少条针对 8000 的放行规则都完全无效，板子一律收到
  "连接被拒"（HTTPClient 报 HTTP -1），而服务端日志里什么都看不到。

  这个脚本做两件事，都需要管理员权限，所以会自动请求提权（弹 UAC，点是）：
    1. 把覆盖"全部入站"的 Block 规则禁用（只禁用，不删除，随时可恢复）
    2. 为遥测端口加一条入站放行规则（已存在则跳过）

  只改防火墙，不碰任何别的东西。跑完建议再用 doctor.py 复查一遍。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1
  powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1 -Port 8000
#>
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"

# ---- 自我提权：非管理员时重新以 RunAs 启动自己，把参数带过去 ----
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "需要管理员权限，正在请求提权（请在 UAC 弹窗点“是”）..." -ForegroundColor Yellow
    $self = $MyInvocation.MyCommand.Path
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$self`"","-Port",$Port `
        -Verb RunAs
    exit 0
}

Write-Host "=== 修复防火墙：遥测端口 $Port ===" -ForegroundColor Cyan

# ---- 1. 找出"拦住全部入站"的 Block 规则并禁用 ----
# 判定标准：启用 + 方向 In + 动作 Block + 协议 Any + 本地端口 Any。
# 协议或端口有限定的 Block 是正常业务规则，不动它。
$ruleName = "Telemetry $Port"
$blanket = Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True |
    Where-Object {
        $pf = $_ | Get-NetFirewallPortFilter
        ($pf.Protocol -eq "Any") -and ($pf.LocalPort -eq "Any" -or $null -eq $pf.LocalPort)
    }

if ($blanket) {
    foreach ($r in $blanket) {
        Write-Host "  禁用全局拦截规则: $($r.DisplayName)" -ForegroundColor Yellow
        Set-NetFirewallRule -Name $r.Name -Enabled False
    }
    Write-Host "  （只禁用未删除，需要恢复时：Set-NetFirewallRule -Name <名称> -Enabled True）"
} else {
    Write-Host "  没有发现全局入站拦截规则" -ForegroundColor Green
}

# ---- 2. 放行遥测端口 ----
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  放行规则已存在: $ruleName" -ForegroundColor Green
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    Write-Host "  已添加放行规则: $ruleName (TCP $Port, 所有网络配置)" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== 当前生效状态 ===" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName $ruleName |
    Select-Object DisplayName, Enabled, Direction, Action | Format-Table -AutoSize

Write-Host "下一步：跑 python server\tools\doctor.py 复查整条链路。" -ForegroundColor Cyan