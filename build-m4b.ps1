#requires -version 5.1
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

$ErrorActionPreference = "Stop"

# Capture the directory the script was launched from (your requirement)
$launchDir = (Get-Location).Path

function Show-Error([string]$msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, "Build M4B - Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

function Show-TextWindow([string]$title, [string]$text) {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $title
    $form.Width = 1000
    $form.Height = 750
    $form.StartPosition = "CenterScreen"

    $tb = New-Object System.Windows.Forms.TextBox
    $tb.Multiline = $true
    $tb.ReadOnly = $true
    $tb.ScrollBars = "Both"
    $tb.WordWrap = $false
    $tb.Dock = "Fill"
    $tb.Font = New-Object System.Drawing.Font("Consolas", 9)
    $tb.Text = $text

    $form.Controls.Add($tb)
    $form.ShowDialog() | Out-Null
}

# function Pick-Folder([string]$desc) {
#     $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
#     $dlg.Description = $desc
#     if ($dlg.ShowDialog() -ne "OK") { return $null }
#     return $dlg.SelectedPath
# }
function Pick-Folder([string]$desc, [string]$defaultPath) {
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = $desc
    $dlg.SelectedPath = $defaultPath   # ✅ default folder
    if ($dlg.ShowDialog() -ne "OK") { return $null }
    return $dlg.SelectedPath
}
function Pick-SaveFile([string]$title, [string]$initialDir, [string]$defaultName) {
    $dlg = New-Object System.Windows.Forms.SaveFileDialog
    $dlg.Title = $title
    $dlg.Filter = "Audiobook (*.m4b)|*.m4b"
    $dlg.InitialDirectory = $initialDir
    $dlg.FileName = $defaultName
    if ($dlg.ShowDialog() -ne "OK") { return $null }
    return $dlg.FileName
}

function Ensure-Command([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "$name was not found on PATH." }
    return $cmd.Source
}

function Read-MetadataCreators([string]$metadataJsonPath) {
    # Returns @{ Author = "..."; Narrator = "..." } if found, otherwise empty strings
    $author = ""
    $narrator = ""

    if (-not (Test-Path $metadataJsonPath)) { return @{ Author=""; Narrator="" } }

    $raw = Get-Content $metadataJsonPath -Raw
    $meta = $raw | ConvertFrom-Json

    # Libby exports often use a "creator" collection with role/name pairs (author/narrator) 【2-94ab89】【3-f6885e】
    $creators = $null
    if ($meta.PSObject.Properties.Name -contains "creator") {
        $creators = $meta.creator
    }

    if ($creators) {
        # Normalize single object vs array
        if ($creators -isnot [System.Array]) { $creators = @($creators) }

        $a = $creators | Where-Object { $_.role -eq "author" } | Select-Object -First 1
        if ($a -and $a.name) { $author = [string]$a.name }

        $ns = $creators | Where-Object { $_.role -eq "narrator" } | ForEach-Object { $_.name } | Where-Object { $_ }
        if ($ns) { $narrator = ($ns -join ", ") }
    }

    return @{ Author=$author; Narrator=$narrator }
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText(
        $path,
        $text,
        [System.Text.UTF8Encoding]::new($false)  # UTF-8 WITHOUT BOM (FFmpeg-friendly)
    )
}

function Inject-AuthorNarratorTags([string]$ffmetaPath, [string]$author, [string]$narrator) {
    # Adds artist/album_artist/comment tags (if not already present) right after ;FFMETADATA1
    if (-not (Test-Path $ffmetaPath)) { throw "FFmetadata file not found: $ffmetaPath" }

    $text = Get-Content $ffmetaPath -Raw

    # Ensure first line looks like FFmetadata
    if (-not ($text -match "^\s*;FFMETADATA1")) {
        throw "metadata.txt does not start with ;FFMETADATA1"
    }

    $hasArtist      = $text -match "(?m)^\s*artist="
    $hasAlbumArtist = $text -match "(?m)^\s*album_artist="
    $hasComment     = $text -match "(?m)^\s*comment="

    $inserts = @()
    if ($author -and -not $hasArtist)      { $inserts += "artist=$author" }
    if ($author -and -not $hasAlbumArtist) { $inserts += "album_artist=$author" }
    if ($narrator -and -not $hasComment)   { $inserts += "comment=Narrated by $narrator" }

    if ($inserts.Count -eq 0) { return } # nothing to do

    # Insert after the first line
    $lines = $text -split "`r?`n", 2
    $newText = $lines[0] + "`n" + ($inserts -join "`n") + "`n" + $lines[1]
    Write-Utf8NoBom $ffmetaPath $newText
}

try {
    # Validate tools
    Ensure-Command "python" | Out-Null
    Ensure-Command "ffmpeg" | Out-Null
    Ensure-Command "ffprobe" | Out-Null

    # 1) Pick audiobook directory (GUI)
    #$bookDir = Pick-Folder "Select audiobook directory (contains Part XXX.mp3 and metadata\metadata.json)"
    $bookDir = Pick-Folder "Select audiobook directory (contains Part XXX.mp3 and metadata\metadata.json)" $launchDir
    if (-not $bookDir) { throw "No audiobook directory selected." }

    $bookName = Split-Path $bookDir -Leaf
    $metadataDir = Join-Path $bookDir "metadata"
    $metadataJson = Join-Path $metadataDir "metadata.json"
    $chaptersTxt = Join-Path $metadataDir "chapters.txt"
    $ffmetaTxt = Join-Path $metadataDir "metadata.txt"
    $filesTxt = Join-Path $bookDir "files.txt"
    $coverJpg = Join-Path $metadataDir "cover.JPG"

    if (-not (Test-Path $metadataJson)) { throw "Missing required file: $metadataJson" }
    if (-not (Test-Path $coverJpg))     { throw "Missing required cover image: $coverJpg" }

    # 2) Bake metadata into MP3 files (per repo README) 【1-d8c35d】
    & python .\bakeMetadata.py "$bookDir"
    if ($LASTEXITCODE -ne 0) { throw "bakeMetadata.py failed with exit code $LASTEXITCODE" }

    # 3) Build chapters.txt and metadata.txt using buildChapters.py (per README) 【1-d8c35d】
    $chaptersOut = (Get-Content $metadataJson) | & python .\buildChapters.py --chapters | Out-String
    Write-Utf8NoBom $chaptersTxt $chaptersOut.TrimEnd()

    $ffmetaOut = (Get-Content $metadataJson) | & python .\buildChapters.py --ffmpeg | Out-String
    Write-Utf8NoBom $ffmetaTxt $ffmetaOut.TrimEnd()

    # 3b) Add author/narrator tags into ffmetadata (auto-detect from metadata.json creators) 【2-94ab89】【3-f6885e】
    $creds = Read-MetadataCreators $metadataJson
    $author = $creds.Author
    $narrator = $creds.Narrator

    # Optional: allow quick override (InputBox), defaults prefilled
    $author = [Microsoft.VisualBasic.Interaction]::InputBox("Author tag (leave as-is or edit):", "Author", $author)
    $narrator = [Microsoft.VisualBasic.Interaction]::InputBox("Narrator tag (leave as-is or edit):", "Narrator", $narrator)

    Inject-AuthorNarratorTags $ffmetaTxt $author $narrator

    # 4) Generate files.txt concat list (like your existing pattern) 【4-3d0ffd】
    Get-ChildItem "$bookDir\Part *.mp3" |
        Sort-Object Name |
        ForEach-Object { "file '$($_.Name)'" } |
        Set-Content $filesTxt -Encoding ASCII

    # 5) Prompt for output file location & name (GUI)
    #    Default path should be where the script was launched from (your requirement)
    $outputFile = Pick-SaveFile "Save M4B file" $launchDir "$bookName.m4b"
    if (-not $outputFile) { throw "No output file selected." }

    # 6) Convert to M4B (run ffmpeg from within bookDir so concat list relative names work)
    Push-Location $bookDir
    try {
        & ffmpeg `
          -f concat -safe 0 -i "$filesTxt" `
          -f ffmetadata -i "$ffmetaTxt" `
          -i "$coverJpg" `
          -map 0:a `
          -map_metadata 1 `
          -map_chapters 1 `
          -map 2:v `
          -c:a aac -b:a 128k `
          -c:v mjpeg `
          -disposition:v attached_pic `
          -f ipod `
          "$outputFile"

        if ($LASTEXITCODE -ne 0) { throw "FFmpeg failed with exit code $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }

    # 7) Validation: ensure file exists and show ffprobe -show_chapters in a scrollable window
    if (-not (Test-Path $outputFile)) { throw "Output file was not created: $outputFile" }

    $fi = Get-Item $outputFile
    $probe = & ffprobe -hide_banner -show_chapters "$outputFile" 2>&1 | Out-String

    $report = @()
    $report += "✅ Output created successfully:"
    $report += "Path: $outputFile"
    $report += ("Size: {0:N0} bytes" -f $fi.Length)
    $report += ""
    $report += "ffprobe -show_chapters output:"
    $report += "--------------------------------"
    $report += $probe

    Show-TextWindow "M4B Validation (Chapters)" ($report -join "`r`n")

}
catch {
    Show-Error $_.Exception.Message
    throw
}