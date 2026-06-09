# run-backend.ps1 — 读取 .env 并启动 NovelForge 后端（FastAPI / uvicorn :8787）
#
# 用法：在项目根执行  .\run-backend.ps1
# 会把 .env 里的 KEY=value 注入当前进程环境变量，再启动后端。
# .env 已被 .gitignore 忽略，密钥不会进版本库。

$ErrorActionPreference = "Stop"
$envFile = Join-Path $PSScriptRoot ".env"

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $name = $line.Substring(0, $idx).Trim()
            $value = $line.Substring($idx + 1).Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
            # 仅提示变量名，不回显值
            Write-Host "  loaded $name" -ForegroundColor DarkGray
        }
    }
    Write-Host "已从 .env 注入环境变量" -ForegroundColor Green
} else {
    Write-Host "未找到 .env —— 将以无 LLM key 模式启动（确定性核心仍可用，生成章节会报错）" -ForegroundColor Yellow
}

python -m uvicorn novelforge.app.main:app --host 127.0.0.1 --port 8787 --reload
