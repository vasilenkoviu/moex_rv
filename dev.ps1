param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('test', 'lint', 'format', 'notebook')]
    [string]$Action
)
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
switch ($Action) {
    'test'     { & $py -m pytest -q }
    'lint'     { & $py -m ruff check . }
    'format'   { & $py -m ruff format . }
    'notebook' { & $py -m jupyter lab }
}