<#
  fix-pornhub-cdn.ps1
  修复 phncdn 缩略图 / 播放流拿不到：DNS 污染注入导致封面挂起、HLS 分片取不到、播放器报"获取不到播放流"

  用法（普通权限即诊断）：
    powershell -ExecutionPolicy Bypass -File .\fix-pornhub-cdn.ps1
  写入修复（管理员）：
    powershell -ExecutionPolicy Bypass -File .\fix-pornhub-cdn.ps1 -Apply
  撤销（管理员）：
    powershell -ExecutionPolicy Bypass -File .\fix-pornhub-cdn.ps1 -Revert
#>
[CmdletBinding()]
param(
  [switch]$Apply,
  [switch]$Revert,
  [string]$Proxy = 'http://127.0.0.1:65532'
)

$ErrorActionPreference = 'Continue'
$hostsPath  = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
$markerOpen = '# >>> phncdn-fix BEGIN'
$markerEnd  = '# <<< phncdn-fix END'
$ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

$pins = [ordered]@{
  'cdn1-smallimg.phncdn.com' = '66.254.114.156'
  'ss.phncdn.com'            = '216.18.168.84'
  'ei.phncdn.com'            = '64.210.147.16'
  'el2.phncdn.com'           = '67.22.55.7'
  'ev.phncdn.com'            = '67.22.55.3'
  'hv-h.phncdn.com'          = '104.18.49.67'
  'pix-fl.phncdn.com'        = '151.101.67.52'
}

function Test-IsAdmin {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Probe {
  param([string]$Url, [string]$ProxyUrl, [string[]]$ExtraHeaders = @())
  $curlArgs = @('-s','-L','-o','NUL','--max-time','25','-A',$ua,
                '-w','http=%{http_code} bytes=%{size_download} speed=%{speed_download}B/s t=%{time_total}s')
  foreach ($h in $ExtraHeaders) { $curlArgs += @('-H', $h) }
  if ($ProxyUrl) { $curlArgs = @('-x', $ProxyUrl) + $curlArgs }
  $curlArgs += $Url
  (& curl.exe @curlArgs) 2>&1
}

function Get-Html {
  param([string]$Url, [string]$ProxyUrl, [string]$Referer)
  $curlArgs = @('-s','-L','--max-time','30','-A',$ua)
  if ($Referer) { $curlArgs += @('-H', "Referer: $Referer") }
  if ($ProxyUrl) { $curlArgs = @('-x', $ProxyUrl) + $curlArgs }
  $curlArgs += $Url
  $parts = & curl.exe @curlArgs 2>$null
  ($parts -join "`n")
}

function Edit-Hosts {
  param([switch]$Remove)
  if (-not (Test-IsAdmin)) {
    Write-Host '需要管理员权限：右键 PowerShell -> 以管理员身份运行后重试。' -ForegroundColor Yellow
    return
  }
  $backup = "$hostsPath.bak_$(Get-Date -Format yyyyMMdd_HHmmss)"
  Copy-Item $hostsPath $backup -Force -ErrorAction SilentlyContinue
  $kept = New-Object System.Collections.Generic.List[string]
  $skip = $false
  foreach ($line in (Get-Content $hostsPath)) {
    if ($line.Trim() -eq $markerOpen) { $skip = $true;  continue }
    if ($line.Trim() -eq $markerEnd)  { $skip = $false; continue }
    if (-not $skip) { $kept.Add($line) }
  }
  if (-not $Remove) {
    $kept.Add($markerOpen)
    $kept.Add("# $(Get-Date -Format s)  固定 phncdn 边缘 IP，绕开 DNS 注入的假 A 记录")
    foreach ($k in $pins.Keys) { $kept.Add(('{0,-22} {1}' -f $pins[$k], $k)) }
    $kept.Add($markerEnd)
  }
  Set-Content -Path $hostsPath -Value $kept -Encoding ASCII
  ipconfig /flushdns | Out-Null
  if ($Remove) { Write-Host "已撤销 hosts 修改（备份 $backup），DNS 缓存已刷新。" -ForegroundColor Green }
  else         { Write-Host "已写入 hosts 固定记录（备份 $backup），DNS 缓存已刷新。" -ForegroundColor Green }
}

if ($Revert) { Edit-Hosts -Remove; return }
if ($Apply)  { Edit-Hosts }

Write-Host "`n=== 1. 系统 DNS 解析（出现 205.251.* / 156.154.* / 2600:9000:* 即为污染注入） ==="
foreach ($h in $pins.Keys) {
  $ips = (Resolve-DnsName $h -Type A -ErrorAction SilentlyContinue | Where-Object IPAddress).IPAddress -join ', '
  $flag = ''
  if ($ips -match '205\.251\.|156\.154\.|2600:9000:') { $flag = '   <== 含污染记录' }
  Write-Host ('{0,-26} {1}{2}' -f $h, $ips, $flag)
}

Write-Host "`n=== 2. 主站连通性（经隧道） ==="
Write-Host ('{0,-26} {1}' -f 'www.pornhub.com', (Invoke-Probe 'https://www.pornhub.com/' $Proxy))

Write-Host "`n=== 3. 取一个真实视频页，走 master.m3u8 -> 分片，实测播放链 ==="
$homeHtml = Get-Html 'https://www.pornhub.com/' $Proxy
$vk = [regex]::Match($homeHtml, 'viewkey=([a-z0-9]{10,16})').Groups[1].Value
if (-not $vk) {
  Write-Host '主站 HTML 未取到 viewkey：隧道可能没通，先确认代理端口。' -ForegroundColor Yellow
} else {
  Write-Host "viewkey = $vk"
  $page = Get-Html "https://www.pornhub.com/view_video.php?viewkey=$vk" $Proxy
  $m3u8Raw = [regex]::Match($page, '(https?:\\/\\/[^"]*?\.m3u8[^"]*)').Value
  if (-not $m3u8Raw) {
    Write-Host '视频页里没有 m3u8 定义（该视频可能已下架或需登录）。' -ForegroundColor Yellow
  } else {
    $m3u8 = $m3u8Raw -replace '\\/', '/'
    Write-Host ('master  via proxy : ' + (Invoke-Probe $m3u8 $Proxy @('Referer: https://www.pornhub.com/')))
    Write-Host ('master  direct    : ' + (Invoke-Probe $m3u8 $null  @('Referer: https://www.pornhub.com/')))

    $baseDir = [regex]::Match($m3u8, '^(.*/)[^/]+$').Groups[1].Value
    $query   = $m3u8.Substring($m3u8.IndexOf('?'))
    $idxUrl  = $baseDir + 'index-v1-a1.m3u8' + $query
    $idx     = Get-Html $idxUrl $Proxy 'https://www.pornhub.com/'
    $seg     = [regex]::Match($idx, '(?m)^([^\r\n#]+\.(?:m4s|ts)[^\r\n]*)').Groups[1].Value
    if ($seg) {
      $segUrl = $baseDir + $seg
      Write-Host ('segment via proxy : ' + (Invoke-Probe $segUrl $Proxy @('Referer: https://www.pornhub.com/')))
      Write-Host ('segment direct    : ' + (Invoke-Probe $segUrl $null  @('Referer: https://www.pornhub.com/')))
    }
  }
}

Write-Host "`n判定：speed > 600000 B/s（约 5 Mbps）→ 720p 流畅；< 300000 B/s → 播放器会一直缓冲。"
Write-Host "      经代理快、直连慢或 000 → 有流量没走隧道，检查代理工具里 phncdn.com / pspcdn.com 是否被分流成 DIRECT。"
Write-Host "      封面空白且 DNS 含污染记录 → 执行 -Apply 固定 hosts（需管理员）。"
