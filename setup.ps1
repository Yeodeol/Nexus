#requires -Version 5.1
<#
.SYNOPSIS
  Bootstrap de Nexus: deja un clon del repo listo para desplegar TU propio hub.

.DESCRIPTION
  Script idempotente (puedes correrlo varias veces). Hace lo mecanico del despliegue:
    1. Verifica requisitos (Python 3.10+ y git).
    2. Crea un entorno virtual por modulo MCP (projects-hub, nexus-hub) e instala deps.
    3. Copia los skills a ~/.claude/commands (nexus.md, orquestar.md).
    4. Crea listener/config.json desde el ejemplo si no existe.
    5. Genera el bloque "mcpServers" con TUS rutas absolutas ya resueltas.

  NO modifica tu ~/.claude.json (config sensible): imprime el bloque y lo deja en
  mcp-servers.generated.json para que lo pegues/fusiones tu mismo.

  Pensado para Windows/PowerShell (el resto del tooling de Nexus tambien lo es). En
  Mac/Linux sigue los pasos manuales de docs/GETTING_STARTED.md.

.PARAMETER Force
  Recrea los entornos virtuales aunque ya existan.

.EXAMPLE
  ./setup.ps1

.EXAMPLE
  ./setup.ps1 -Force
#>
[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor Gray }

# En PowerShell 5.1 (siempre Windows) $IsWindows no existe (=$null); en PS Core no-Windows es $false.
if ($IsWindows -eq $false) {
  Write-Note "setup.ps1 esta pensado para Windows. En Mac/Linux sigue los pasos manuales de docs/GETTING_STARTED.md."
}

# --- 1. Requisitos ---------------------------------------------------------
Write-Step "Verificando requisitos"
$python = $null
foreach ($cmd in @('python', 'python3', 'py')) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) { $python = $found.Source; break }
}
if (-not $python) { throw "No se encontro Python en el PATH. Instala Python 3.10+ y reintenta." }
$verRaw = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$verRaw -lt [version]'3.10') { throw "Python $verRaw detectado; se requiere 3.10 o superior." }
Write-Ok "Python $verRaw ($python)"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "No se encontro git en el PATH." }
Write-Ok "git presente"

# --- 2. Entornos virtuales + dependencias ----------------------------------
foreach ($mod in @('projects-hub', 'nexus-hub')) {
  Write-Step "Modulo $mod : entorno virtual + dependencias"
  $venv   = Join-Path $root "$mod\.venv"
  $venvPy = Join-Path $venv 'Scripts\python.exe'
  if ($Force -and (Test-Path $venv)) {
    Write-Note "Recreando venv (-Force)"
    Remove-Item -Recurse -Force $venv
  }
  if (-not (Test-Path $venvPy)) {
    & $python -m venv $venv
    Write-Ok "venv creado en $mod\.venv"
  } else {
    Write-Dim "venv ya existe (usa -Force para recrear)"
  }
  & $venvPy -m pip install --quiet --upgrade pip
  & $venvPy -m pip install --quiet -r (Join-Path $root "$mod\requirements.txt")
  Write-Ok "dependencias instaladas"
}

# --- 3. Skills -------------------------------------------------------------
Write-Step "Copiando skills a ~/.claude/commands"
$cmdDir = Join-Path $HOME '.claude\commands'
if (-not (Test-Path $cmdDir)) { New-Item -ItemType Directory -Path $cmdDir -Force | Out-Null }
foreach ($skill in @('nexus.md', 'orquestar.md')) {
  Copy-Item (Join-Path $root "skills\$skill") (Join-Path $cmdDir $skill) -Force
  Write-Ok "$skill -> $cmdDir"
}

# --- 4. Config del listener ------------------------------------------------
Write-Step "Configuracion del listener"
$cfg        = Join-Path $root 'listener\config.json'
$cfgExample = Join-Path $root 'listener\config.example.json'
if (-not (Test-Path $cfg)) {
  Copy-Item $cfgExample $cfg
  Write-Ok "listener/config.json creado desde el ejemplo (edita 'responders' al registrar tus proyectos)"
} else {
  Write-Dim "listener/config.json ya existe (no se toca)"
}

# --- 5. Bloque mcpServers con rutas resueltas ------------------------------
Write-Step "Generando bloque mcpServers (rutas absolutas de este clon)"
$rootFwd = $root -replace '\\', '/'
$mcpJson = @"
{
  "mcpServers": {
    "projects-hub": {
      "type": "stdio",
      "command": "$rootFwd/projects-hub/.venv/Scripts/python.exe",
      "args": ["$rootFwd/projects-hub/server.py"]
    },
    "nexus-hub": {
      "type": "stdio",
      "command": "$rootFwd/nexus-hub/.venv/Scripts/python.exe",
      "args": ["$rootFwd/nexus-hub/server.py"]
    }
  }
}
"@
$genPath = Join-Path $root 'mcp-servers.generated.json'
$mcpJson | Out-File -FilePath $genPath -Encoding utf8
Write-Ok "escrito en mcp-servers.generated.json (gitignored)"

# --- Resumen / proximos pasos ----------------------------------------------
Write-Step "Listo. Faltan 4 pasos manuales (una sola vez):"
Write-Dim "1) Registra los servidores MCP: fusiona la clave 'mcpServers' de abajo en tu"
Write-Dim "   ~/.claude.json (o el config de tu cliente MCP). Ya viene con tus rutas:"
Write-Host ""
Write-Host $mcpJson -ForegroundColor White
Write-Host ""
Write-Dim "2) Pega el bloque de instrucciones del cerebro en ~/.claude/CLAUDE.md"
Write-Dim "   (crealo si no existe):  templates/claude-global.example.md"
Write-Dim "3) Reinicia tu cliente MCP para que cargue ambos servidores."
Write-Dim "4) Registra TUS proyectos: copia templates/seed-projects.example.json, editalo"
Write-Dim "   con los tuyos y pidele a Claude que lo lea y ejecute register_project +"
Write-Dim "   declare_capability por cada proyecto."
Write-Host ""
Write-Dim "Opcional (auto-respuestas + fichas): python listener/nexus_listener.py"
Write-Dim "Detalle completo en docs/GETTING_STARTED.md"
