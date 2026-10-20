# Build single-file exe (Node SEA): inject notion-proxy.js into node.exe
# Usage: powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1
# Output: ai-proxy\notion-proxy.exe  (~90MB, double-click to run, no Node install needed)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path 'notion-proxy.js')) {
  throw 'notion-proxy.js not found; run this script inside the ai-proxy folder'
}

New-Item -ItemType Directory -Force -Path '.tmp\sea' | Out-Null
$sea = @{
  main                          = 'notion-proxy.js'
  output                        = '.tmp/sea/sea-prep.blob'
  disableExperimentalSEAWarning = $true
  useCodeCache                  = $false
  useSnapshot                   = $false
} | ConvertTo-Json
[System.IO.File]::WriteAllText(
  (Join-Path $root '.tmp\sea\sea-config.json'),
  $sea,
  [System.Text.UTF8Encoding]::new($false)
)

Write-Host '[1/3] generating SEA blob ...'
node --experimental-sea-config '.tmp\sea\sea-config.json'
if (-not (Test-Path '.tmp\sea\sea-prep.blob')) { throw 'SEA blob generation failed' }

Write-Host '[2/3] copying node.exe ...'
$nodeExe = (Get-Command node).Source
Copy-Item $nodeExe 'notion-proxy.exe' -Force

Write-Host '[3/3] injecting with postject ...'
npx --yes postject@1.0.0-alpha.6 notion-proxy.exe NODE_SEA_BLOB '.tmp\sea\sea-prep.blob' --sentinel-fuse NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2

Write-Host ''
Write-Host ('Build OK: ' + (Join-Path $root 'notion-proxy.exe'))
Write-Host ('Size: ' + [math]::Round((Get-Item 'notion-proxy.exe').Length / 1MB, 1) + ' MB')
Write-Host 'Note: exe must stay in the same folder as config.json, secret\ and tools\.'
