# Очистка компьютера от мусора (Windows 10/11)
# Удаляет только временные файлы, корзину и кэш обновлений.
# Перед каждым шагом спрашивает разрешение.

# Перезапуск с правами администратора (нужны для системных папок)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

function Get-FreeGB {
    [math]::Round((Get-PSDrive C).Free / 1GB, 2)
}

function Ask($question) {
    $answer = Read-Host "$question (y/n)"
    return $answer -match '^(y|д|да|yes)$'
}

function Remove-Contents($path) {
    if (Test-Path $path) {
        Get-ChildItem -Path $path -Force -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$before = Get-FreeGB
Write-Host "=== Очистка компьютера ===" -ForegroundColor Cyan
Write-Host "Свободно на диске C: $before ГБ`n"

if (Ask "1. Удалить временные файлы пользователя ($env:TEMP)?") {
    Remove-Contents $env:TEMP
    Write-Host "   Готово (занятые файлы пропущены)" -ForegroundColor Green
}

if (Ask "2. Удалить системные временные файлы (C:\Windows\Temp)?") {
    Remove-Contents "C:\Windows\Temp"
    Write-Host "   Готово" -ForegroundColor Green
}

if (Ask "3. Удалить скачанные файлы обновлений Windows (уже установленные)?") {
    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
    Stop-Service bits -Force -ErrorAction SilentlyContinue
    Remove-Contents "C:\Windows\SoftwareDistribution\Download"
    Start-Service bits -ErrorAction SilentlyContinue
    Start-Service wuauserv -ErrorAction SilentlyContinue
    Write-Host "   Готово" -ForegroundColor Green
}

if (Ask "4. Очистить корзину? (файлы из неё будут удалены навсегда)") {
    Clear-RecycleBin -Force -ErrorAction SilentlyContinue
    Write-Host "   Готово" -ForegroundColor Green
}

if (Ask "5. Открыть встроенную очистку диска Windows для остального?") {
    Start-Process cleanmgr -ArgumentList "/d C" -Wait
}

$after = Get-FreeGB
Write-Host "`n=== Итог ===" -ForegroundColor Cyan
Write-Host "Было свободно:  $before ГБ"
Write-Host "Стало свободно: $after ГБ"
Write-Host "Освобождено:    $([math]::Round($after - $before, 2)) ГБ" -ForegroundColor Green
Read-Host "`nНажми Enter, чтобы закрыть"
