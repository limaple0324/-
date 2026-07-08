<#
  build_exe.ps1
  一鍵把 scan.py 打包成 scan.exe
  需求：已安裝 Python 3.8+，且已加入 PATH
#>

$ErrorActionPreference = "Stop"

function Find-Python {
    $candidates = @("python", "py")
    foreach ($cmd in $candidates) {
        $p = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
        if ($p) { return $p }
    }
    throw "找不到 Python，可先到 https://python.org 下載並在安裝時勾選『Add python.exe to PATH』"
}

$python = Find-Python

Write-Host "✔ 使用 Python 路徑：$python"

# 確保 PyInstaller 已安裝
& $python -m pip install --upgrade --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "安裝 PyInstaller 失敗" }

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

$iconParam = if (Test-Path "$root\scan_icon.ico") { "--icon scan_icon.ico" } else { "" }

& $python -m PyInstaller --onefile --noconsole $iconParam scan.py
if ($LASTEXITCODE -eq 0) {
    Write-Host "🎉 打包完成！請到 dist\scan.exe 取得可執行檔。"
} else {
    throw "PyInstaller 打包失敗，請檢查錯誤訊息"
}