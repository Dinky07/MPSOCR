<#
.SYNOPSIS
  Step 2 of the shrink backfill (Dino, 19 Nov 2026): replaces the content of documents already in SharePoint with
  the smaller copies that ops/shrink_backfill.py staged on frigate (200 dpi, JPEG quality 70).

  Copy this file beside Sync-OcrOutput.ps1 (C:\MPS\ocr-sync). It uses the sync's own app (certificate in config.json)
  and reads <PipelineRoot>\shrink_backfill\plan.csv.

      .\Set-ShrunkFiles.ps1                      # check only: lists what would be replaced, changes nothing
      .\Set-ShrunkFiles.ps1 -Apply -Limit 10     # replace 10, look at them in SharePoint
      .\Set-ShrunkFiles.ps1 -Apply               # the rest (re-runnable: rows already replaced are skipped)

  A file is replaced only when the library item still has the name and the exact size the sync uploaded; anything
  else (renamed, edited, already replaced) is skipped and noted. Only the file content changes: the item keeps its
  ID, link, columns (ContentHash included) and SQL row. SharePoint keeps the old file as the previous version
  (Version history -> Restore). Results go to <PipelineRoot>\shrink_backfill\done.csv; then on frigate:
      python3 ops/shrink_backfill.py finish
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [int]$Limit = 0,
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\MPS\ocr-sync' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $here 'config.json' }
$cfg = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
$bf = Join-Path $cfg.PipelineRoot 'shrink_backfill'
$planPath = Join-Path $bf 'plan.csv'
$donePath = Join-Path $bf 'done.csv'
if (-not (Test-Path -LiteralPath $planPath)) { throw "No plan yet: run  python3 ops/shrink_backfill.py plan  on frigate first ($planPath)" }

$done = @{}
if (Test-Path -LiteralPath $donePath) {
    foreach ($d in Import-Csv -LiteralPath $donePath) { if ($d.result -eq 'replaced') { $done[$d.sidecar] = 1 } }
}
$rows = @(Import-Csv -LiteralPath $planPath | Where-Object { -not $done.ContainsKey($_.sidecar) })
if ($Limit -gt 0) { $rows = @($rows | Select-Object -First $Limit) }
Write-Host "$($rows.Count) document(s) to replace$(if (-not $Apply) { ' (check only)' })"
if ($rows.Count -eq 0) { return }

Import-Module Microsoft.Graph.Authentication
Connect-MgGraph -TenantId $cfg.TenantId -ClientId $cfg.ClientId -CertificateThumbprint $cfg.CertThumbprint -NoWelcome

function Write-Done($r, [string]$result, [string]$url, [string]$note) {
    if (-not $Apply) { return }
    [pscustomobject]@{ sidecar = $r.sidecar; item_id = $r.item_id; result = $result; at = (Get-Date).ToString('s'); url = $url; note = $note } |
        Export-Csv -LiteralPath $donePath -Append -NoTypeInformation -Encoding UTF8
}

function Set-ItemContent([string]$driveId, [string]$itemId, [string]$filePath) {
    # an upload session on the existing item: new content, same item (ID, columns, link); any size
    $body = @{ item = @{ '@microsoft.graph.conflictBehavior' = 'replace' } } | ConvertTo-Json -Depth 3
    $session = Invoke-MgGraphRequest -Method POST -ContentType 'application/json' -Body $body `
        -Uri "https://graph.microsoft.com/v1.0/drives/$driveId/items/$itemId/createUploadSession"
    $bytes = [IO.File]::ReadAllBytes($filePath)
    $total = $bytes.Length; $chunk = 320 * 1024 * 10; $pos = 0; $result = $null
    while ($pos -lt $total) {
        $len = [Math]::Min($chunk, $total - $pos)
        $part = New-Object byte[] $len
        [Array]::Copy($bytes, $pos, $part, 0, $len)
        $hdr = @{ 'Content-Range' = "bytes $pos-$($pos + $len - 1)/$total" }
        $result = Invoke-RestMethod -Method PUT -Uri $session.uploadUrl -Headers $hdr -Body $part -ContentType 'application/octet-stream'
        $pos += $len
    }
    if (-not $result -or -not $result.id) { throw "upload finished without a driveItem in the response" }
    return $result
}

$n = 0; $skip = 0; $fail = 0; $saved = 0L
foreach ($r in $rows) {
    $staged = Join-Path $cfg.PipelineRoot ($r.staged -replace '/', '\')
    $uri = "https://graph.microsoft.com/v1.0/drives/$($r.drive_id)/items/$($r.item_id)"
    try {
        if (-not (Test-Path -LiteralPath $staged)) { Write-Host "$($r.file): staged copy missing, skipped" -ForegroundColor Yellow; $skip++; Write-Done $r 'skipped' '' 'staged copy missing'; continue }
        try { $it = Invoke-MgGraphRequest GET "$($uri)?`$select=name,size,webUrl" }
        catch { Write-Host "$($r.file): not in the library any more, skipped" -ForegroundColor Yellow; $skip++; Write-Done $r 'skipped' '' 'item not found'; continue }
        if ($it.name -ne $r.sp_name) { Write-Host "$($r.file): now called '$($it.name)', skipped" -ForegroundColor Yellow; $skip++; Write-Done $r 'skipped' $it.webUrl "renamed to $($it.name)"; continue }
        if ([int64]$it.size -ne [int64]$r.bytes_in) {
            $note = if ([int64]$it.size -eq [int64]$r.bytes_out) { 'already replaced' } else { "size $($it.size), expected $($r.bytes_in)" }
            Write-Host "$($r.file): $note, skipped" -ForegroundColor Yellow; $skip++
            Write-Done $r $(if ($note -eq 'already replaced') { 'replaced' } else { 'skipped' }) $it.webUrl $note; continue
        }
        $mb = '{0:N1} MB -> {1:N1} MB' -f ([int64]$r.bytes_in / 1MB), ([int64]$r.bytes_out / 1MB)
        if (-not $Apply) { Write-Host "would replace: $($it.webUrl)  ($mb)"; $n++; $saved += [int64]$r.bytes_in - [int64]$r.bytes_out; continue }
        $new = Set-ItemContent $r.drive_id $r.item_id $staged
        if ([int64]$new.size -ne [int64]$r.bytes_out) { throw "SharePoint has $($new.size) bytes after the upload, expected $($r.bytes_out)" }
        Write-Host "replaced: $($it.webUrl)  ($mb)" -ForegroundColor Green
        Write-Done $r 'replaced' $it.webUrl $mb
        $n++; $saved += [int64]$r.bytes_in - [int64]$r.bytes_out
    } catch {
        Write-Host "$($r.file): FAILED $($_.Exception.Message)" -ForegroundColor Red
        Write-Done $r 'failed' '' $_.Exception.Message
        $fail++
    }
}
Write-Host ("{0} {1}, {2} skipped, {3} failed, {4:N2} GB saved" -f $n, $(if ($Apply) { 'replaced' } else { 'would be replaced' }), $skip, $fail, ($saved / 1GB))
if ($Apply -and $n) { Write-Host "Now on frigate:  cd /srv/ocr/pipeline && python3 ops/shrink_backfill.py finish" }
if (-not $Apply) { Write-Host "Nothing changed. Run again with -Apply (optionally -Limit 10 first)." }
