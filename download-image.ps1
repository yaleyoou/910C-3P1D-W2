# Windows PowerShell 5.1 or PowerShell 7. Only the Docker image is downloaded.
param([string]$OutputDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$dir = [IO.Directory]::CreateDirectory($OutputDirectory).FullName
$expected = '0e7cfc4416208ccb7b7c90cac31d0d0bc5d8e2dd224058c8b6d9c03d1f7351e4'
$final = Join-Path $dir 'image.tar.zst'
function Test-Hash($path, $sha) {
    (Test-Path -LiteralPath $path -PathType Leaf) -and ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $sha)
}
if (Test-Hash $final $expected) {
    Write-Output "Verified: $final"
    return
}
$cache = [IO.Directory]::CreateDirectory((Join-Path $dir 'w2-image-parts')).FullName
$repo = 'https://github.com/yaleyoou/910C-3P1D-W2'
$client = New-Object Net.WebClient
$client.Headers['User-Agent'] = 'W2-image-download'
$lock = [IO.File]::Open((Join-Path $cache 'download.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    $manifest = $client.DownloadString("$repo/raw/refs/tags/w2-v1/release.sha256")
    $rows = @($manifest -split '\r?\n' | Where-Object { $_ -cmatch '^[0-9a-f]{64}  image\.seg-[0-9]{4}$' })
    if ($rows.Count -ne 836) { throw "Invalid image manifest: found $($rows.Count) parts." }
    $index = 0
    foreach ($row in $rows) {
        $sha, $name = $row -split '  '
        if ($name -ne ('image.seg-{0:D4}' -f $index)) { throw "Unexpected part: $name" }
        $index++
        $part = Join-Path $cache $name
        if (Test-Hash $part $sha) { continue }
        Write-Output "[$index/836] Downloading $name"
        $client.DownloadFile("$repo/releases/download/w2-v1/$name", "$part.partial")
        if (!(Test-Hash "$part.partial" $sha)) { throw "Checksum mismatch: $name. Run again to retry." }
        Move-Item -LiteralPath "$part.partial" -Destination $part -Force
    }
    $out = [IO.File]::Create("$final.partial")
    try {
        foreach ($row in $rows) {
            $name = ($row -split '  ')[1]
            $inputFile = [IO.File]::OpenRead((Join-Path $cache $name))
            try { $inputFile.CopyTo($out) } finally { $inputFile.Dispose() }
        }
    } finally { $out.Dispose() }
    if (!(Test-Hash "$final.partial" $expected)) { throw 'Assembled image checksum mismatch.' }
    Move-Item -LiteralPath "$final.partial" -Destination $final -Force
    Write-Output "Verified: $final"
} finally {
    $lock.Dispose()
    $client.Dispose()
}
