<#
.SYNOPSIS
    Управление стадиями проекта: ветка на старте, проверки, коммит и отправка на GitHub по завершении.

.EXAMPLE
    .\stage.ps1 list
    .\stage.ps1 start 1
    .\stage.ps1 finish
    .\stage.ps1 finish -Message "Stage 1: loader and RV" -Merge -Tag
    .\stage.ps1 status
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('list', 'start', 'finish', 'status')]
    [string]$Action,

    [Parameter(Position = 1)]
    [string]$Name = '',

    [string]$Message = '',
    [switch]$Merge,
    [switch]$Tag,
    [switch]$SkipTests,
    [switch]$SkipLint
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$Stages = @{
    '1' = 'data'
    '2' = 'baselines'
    '3' = 'linear'
    '4' = 'lgbm'
    '5' = 'gru'
    '6' = 'analysis'
    '7' = 'robustness'
    '8' = 'release'
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$CmdArgs)
    & $Exe @CmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Команда завершилась с ошибкой: $Exe $($CmdArgs -join ' ')"
    }
}

function Get-GitText {
    param([string[]]$CmdArgs)
    $out = & git @CmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Команда завершилась с ошибкой: git $($CmdArgs -join ' ')"
    }
    if ($null -eq $out) { return '' }
    return (($out | Out-String).Trim())
}

function Test-GitOk {
    param([string[]]$CmdArgs)
    & git @CmdArgs *> $null
    return ($LASTEXITCODE -eq 0)
}

if ($Action -eq 'list') {
    foreach ($k in ($Stages.Keys | Sort-Object { [int]$_ })) {
        Write-Host ("Стадия {0}: stage{0}-{1}" -f $k, $Stages[$k])
    }
    return
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git не найден в PATH.' }
$top = (Get-GitText @('rev-parse', '--show-toplevel')) -replace '/', '\'
if ((Resolve-Path $top).Path -ne (Resolve-Path $PSScriptRoot).Path) {
    throw "Скрипт должен лежать в корне репозитория. Корень Git: $top"
}
$branch = Get-GitText @('branch', '--show-current')
$remotes = (Get-GitText @('remote')) -split '\r?\n'
$hasRemote = $remotes -contains 'origin'
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

function Update-Main {
    if (-not (Test-GitOk @('rev-parse', '--verify', '--quiet', 'main'))) {
        throw 'Локальной ветки main с коммитами нет. Сделайте первый коммит.'
    }
    Invoke-Checked git @('switch', 'main')
    if ($hasRemote) {
        Invoke-Checked git @('fetch', 'origin')
        $remoteMain = Get-GitText @('ls-remote', '--heads', 'origin', 'main')
        if ($remoteMain) { Invoke-Checked git @('pull', '--ff-only', 'origin', 'main') }
    }
}

function Test-Project {
    if (-not (Test-Path $py)) { throw "Не найдено окружение: $py" }
    if (-not $SkipTests) {
        Write-Host '>> Тесты' -ForegroundColor Cyan
        & $py -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Тесты не пройдены. Исправьте ошибки или используйте -SkipTests.' }
    }
    if (-not $SkipLint) {
        Write-Host '>> Проверка стиля (ruff)' -ForegroundColor Cyan
        & $py -m ruff check .
        if ($LASTEXITCODE -ne 0) { throw 'Ruff нашёл замечания. Исправьте их или используйте -SkipLint.' }
    }
}

function Test-StagedFiles {
    $files = (Get-GitText @('diff', '--cached', '--name-only')) -split '\r?\n' | Where-Object { $_ }
    foreach ($f in $files) {
        if ($f -match '(^|/)\.env($|\.)') { throw "В коммит попал файл с секретами: $f" }
        if ($f -match '^\.venv/') { throw "В коммит попало окружение: $f" }
        if ($f -match '^data/' -and $f -ne 'data/.gitkeep') { throw "В коммит попали данные: $f" }
        if (Test-Path $f) {
            $mb = (Get-Item $f).Length / 1MB
            if ($mb -gt 20) { throw ("Файл слишком большой ({0:N1} МБ): {1}" -f $mb, $f) }
        }
    }
    return $files
}

switch ($Action) {

    'status' {
        Write-Host "Ветка: $branch" -ForegroundColor Cyan
        Invoke-Checked git @('status', '-sb')
        Invoke-Checked git @('log', '--oneline', '-5')
    }

    'start' {
        if (-not $Name) { throw 'Укажите номер стадии или имя ветки: .\stage.ps1 start 1' }
        if ($Name -match '^\d+$') {
            if (-not $Stages.ContainsKey($Name)) { throw "Нет стадии с номером $Name. Список: .\stage.ps1 list" }
            $Name = "stage$Name-$($Stages[$Name])"
        }
        if ($Name -notmatch '^[a-z0-9][a-z0-9._/-]*$') {
            throw 'Имя ветки: строчные латинские буквы, цифры, ".", "_", "-", "/".'
        }
        $tracked = Get-GitText @('status', '--porcelain', '--untracked-files=no')
        if ($tracked) {
            Write-Host $tracked
            throw 'Есть изменения в отслеживаемых файлах. Завершите стадию (finish) или уберите изменения (git stash).'
        }
        $untracked = Get-GitText @('ls-files', '--others', '--exclude-standard')
        if ($untracked) {
            Write-Warning "Неотслеживаемые файлы перейдут в новую ветку вместе с Вами:`n$untracked"
        }
        if (Test-GitOk @('show-ref', '--verify', '--quiet', "refs/heads/$Name")) {
            throw "Ветка уже существует: $Name"
        }
        Update-Main
        Invoke-Checked git @('switch', '-c', $Name)
        Write-Host "Создана ветка $Name от актуального main." -ForegroundColor Green
    }

    'finish' {
        if ($branch -eq 'main') { throw 'Вы на main. Перейдите на ветку стадии: .\stage.ps1 start <номер>' }
        if (-not $hasRemote) { throw 'Не задан удалённый репозиторий origin. Сначала выполните github_push.ps1.' }
        if (-not $Message) {
            if ($branch -match '^stage(\d+)-(.+)$') { $Message = "Stage $($Matches[1]): $($Matches[2])" }
            else { $Message = "Stage: $branch" }
        }

        Test-Project

        Invoke-Checked git @('add', '-A')
        $staged = Test-StagedFiles

        if ($staged) {
            if (-not (Get-GitText @('config', 'user.name')) -or -not (Get-GitText @('config', 'user.email'))) {
                throw 'Не заданы git user.name и user.email.'
            }
            Invoke-Checked git @('commit', '-m', $Message)
        } else {
            Write-Host 'Новых изменений для коммита нет.' -ForegroundColor Yellow
        }

        Invoke-Checked git @('push', '-u', 'origin', $branch)

        if ($Merge) {
            Update-Main
            Invoke-Checked git @('merge', '--no-ff', $branch, '-m', "Merge $branch")
            Test-Project
            Invoke-Checked git @('push', 'origin', 'main')
        }

        if ($Tag) {
            $tagName = "done-$($branch -replace '/', '-')"
            Invoke-Checked git @('tag', '-a', $tagName, '-m', $Message)
            Invoke-Checked git @('push', 'origin', $tagName)
        }

        Write-Host ''
        Write-Host "Стадия завершена. Последний коммит: $(Get-GitText @('log', '--oneline', '-1'))" -ForegroundColor Green
        if (-not $Merge) {
            Write-Host 'Слияние с main не выполнялось. Для слияния: .\stage.ps1 finish -Merge (или Pull Request на GitHub).'
        }
    }
}
