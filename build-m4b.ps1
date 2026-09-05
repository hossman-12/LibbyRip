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

function Read-MetadataTitle([string]$metadataJsonPath) {
    if (-not (Test-Path $metadataJsonPath)) { return "" }
    $raw = Get-Content $metadataJsonPath -Raw
    $meta = $raw | ConvertFrom-Json
    if ($meta.PSObject.Properties.Name -contains "title") { return [string]$meta.title }
    return ""
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText(
        $path,
        $text,
        [System.Text.UTF8Encoding]::new($false)  # UTF-8 WITHOUT BOM (FFmpeg-friendly)
    )
}

# Clean invalid characters for Windows paths
function Clean-Name($name) {
    return ($name -replace '[<>:"/\\|?*]', '').Trim()
}

# --- Logging setup -------------------------------------------------
$LogDir = Join-Path $launchDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogBaseName = "build-m4b.log"
$LogPath = Join-Path $LogDir $LogBaseName
# Number of rotated backup files to keep (default: keep last 4 + current = 5 total)
$LogBackupsToKeep = 4

function Rotate-Logs([string]$baseLogPath, [int]$backupsToKeep) {
    if (-not (Test-Path $baseLogPath)) { return }

    # Remove oldest backup if it would exceed the limit
    $oldest = "$baseLogPath.$backupsToKeep.log"
    if (Test-Path $oldest) { Remove-Item $oldest -Force -ErrorAction SilentlyContinue }

    # Shift existing backups upward (n-1 -> n)
    for ($i = $backupsToKeep - 1; $i -ge 1; $i--) {
        $src = "$baseLogPath.$i.log"
        $dst = "$baseLogPath.$($i + 1).log"
        if (Test-Path $src) {
            Rename-Item -Path $src -NewName (Split-Path $dst -Leaf) -Force
        }
    }

    # Move current base log to .1
    $firstBackup = "$baseLogPath.1.log"
    Rename-Item -Path $baseLogPath -NewName (Split-Path $firstBackup -Leaf) -Force
}

function Write-Log([string]$msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$ts] $msg"
    try {
        Add-Content -Path $LogPath -Value $entry -Encoding UTF8
    } catch {
        # If logging fails, still write to host
        Write-Host $entry
    }
}

# Rotate current log and start a fresh one for this run
try {
    Rotate-Logs $LogPath $LogBackupsToKeep
} catch {
    # ignore rotation errors
}
Set-Content -Path $LogPath -Value "Log started: $(Get-Date -Format 'u')" -Encoding UTF8
Write-Log "Initialized logging. Log path: $LogPath (keeping $LogBackupsToKeep backups)"
# -------------------------------------------------------------------

# Output format string. Use placeholders like %LaunchDir%, %AUTHOR%, %TITLE%, %BOOKDIR%, %BOOKNAME%
# Examples:
#   "%LaunchDir%/%AUTHOR%/%TITLE%.m4b"
#   "%LaunchDir%/AudioBooks/%AUTHOR%/%TITLE%.m4b"
$OutputFormat = "%LaunchDir%/AudioBooks/%AUTHOR%/%TITLE%.m4b"

function Expand-OutputFormat([string]$fmt, [hashtable]$vars) {
    if (-not $fmt) { return "" }
    $result = $fmt
    foreach ($k in $vars.Keys) {
        $pattern = [regex]::Escape("%$k%")
        $result = [regex]::Replace($result, $pattern, [string]$vars[$k], [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    }
    # Normalize separators to backslashes and collapse repeats
    $result = $result -replace '/', '\\'
    $result = $result -replace '\\\\+', '\\'
    return $result
}


function Inject-AuthorNarratorTags([string]$ffmetaPath, [string]$author, [string]$narrator, [string]$title) {
    # Adds artist/album_artist/comment/title/album tags (if not already present) right after ;FFMETADATA1
    # Follows tag guidance from: https://github.com/seanap/Plex-Audiobook-Guide#tags-that-are-being-set
    if (-not (Test-Path $ffmetaPath)) { throw "FFmetadata file not found: $ffmetaPath" }

    $text = Get-Content $ffmetaPath -Raw

    # Ensure first line looks like FFmetadata
    if (-not ($text -match "^\s*;FFMETADATA1")) {
        throw "metadata.txt does not start with ;FFMETADATA1"
    }

    $hasArtist      = $text -match "(?m)^\s*artist="
    $hasAlbumArtist = $text -match "(?m)^\s*album_artist="
    $hasComment     = $text -match "(?m)^\s*comment="
    $hasAlbum       = $text -match "(?m)^\s*album="
    $hasTitle       = $text -match "(?m)^\s*title="
    $hasComposer    = $text -match "(?m)^\s*composer="

    # Try to infer composer from existing comment if present ("Narrated by ...")
    $composerName = ""
    if ($text -match "(?m)^\s*comment=(.+)$") {
        $c = $Matches[1].Trim()
        if ($c -match "(?i)Narrated by\s+(.+)") { $composerName = $Matches[1].Trim() }
    }
    # If a narrator parameter was supplied, prefer that
    if (-not $composerName -and $narrator) { $composerName = $narrator }

    $inserts = @()
    if ($author -and -not $hasArtist)      { $inserts += "artist=$author" }
    if ($author -and -not $hasAlbumArtist) { $inserts += "album_artist=$author" }
    if ($narrator -and -not $hasComment)   { $inserts += "comment=Narrated by $narrator" }
    if ($title -and -not $hasAlbum)        { $inserts += "album=$title" }
    if ($title -and -not $hasTitle)        { $inserts += "title=$title" }
    if ($composerName -and -not $hasComposer) { $inserts += "composer=$composerName" }

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

    # Show status screen and ask user to confirm before any processing
    $detected = Read-MetadataCreators $metadataJson
    $authorDetected = if ($detected.Author) { $detected.Author } else { "(not detected)" }
    $narratorDetected = if ($detected.Narrator) { $detected.Narrator } else { "(not detected)" }

    $metaTitle = Read-MetadataTitle $metadataJson

    $authorPreview = Clean-Name($detected.Author)
    if (-not $authorPreview) { $authorPreview = "Unknown Author" }
    if ($metaTitle) { $titlePreview = Clean-Name($metaTitle) } else { $titlePreview = Clean-Name($bookName) }
    $previewVars = @{
        "LaunchDir" = $launchDir
        "AUTHOR"    = $authorPreview
        "TITLE"     = $titlePreview
        "BOOKDIR"   = $bookDir
        "BOOKNAME"  = $bookName
    }
    $outputFilePreview = Expand-OutputFormat $OutputFormat $previewVars
    try {
        $outputFilePreview = [System.IO.Path]::GetFullPath($outputFilePreview)
    } catch {
        # If GetFullPath fails (malformed), fall back to raw preview
    }

    # If an ffmetadata file already exists, try to extract a narrator from its comment field
    $composerFromComment = ""
    if (Test-Path $ffmetaTxt) {
        try {
            $fftext = Get-Content $ffmetaTxt -Raw
            if ($fftext -match "(?m)^\s*comment=(.+)$") {
                $existingComment = $Matches[1].Trim()
                if ($existingComment -match "(?i)Narrated by\s+(.+)") {
                    $composerFromComment = $Matches[1].Trim()
                }
            }
        } catch {
            # ignore parse errors
        }
    }

    $composerPreview = if ($composerFromComment) { $composerFromComment } elseif ($detected.Narrator) { $detected.Narrator } else { "(not set)" }
    $tagTitle = if ([string]::IsNullOrEmpty($metaTitle)) { $titlePreview } else { $metaTitle }

    $status = @()
    $status += "About to build M4B with the following settings:" 
    $status += ""
    $status += "Audiobook directory: $bookDir"
    $status += "Book name: $bookName"
    $status += "Detected Author: $authorDetected"
    $status += "Detected Narrator: $narratorDetected"
    $status += ""
    $status += "Planned output (preview): $outputFilePreview"
    $status += "Log file: $LogPath"
    $status += ""
    $status += "Tags that will be set (preview):"
    $status += " - title: $tagTitle"
    $status += " - album: $tagTitle"
    $status += " - artist: $authorDetected"
    $status += " - album_artist: $authorDetected"
    $status += " - comment: Narrated by $narratorDetected"
    $status += " - composer: $composerPreview"
    $status += ""
    $status += "Planned actions:" 
    $status += " - Bake metadata into MP3 files"
    $status += " - Build chapters and ffmetadata"
    $status += " - Inject author/narrator tags"
    $status += " - Create concat file list"
    $status += " - Convert to M4B with ffmpeg"
    $status += " - Validate output with ffprobe"

    $resp = [System.Windows.Forms.MessageBox]::Show(($status -join "`r`n"), "Confirm Actions", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question)
    if ($resp -ne [System.Windows.Forms.DialogResult]::Yes) {
        Write-Log "User cancelled at confirmation screen."
        throw "User cancelled."
    }
    Write-Log "User confirmed actions. Starting processing for: $bookName"

    # 2) Bake metadata into MP3 files (per repo README) 【1-d8c35d】
    Write-Log "Running bakeMetadata.py on $bookDir"
    & python .\bakeMetadata.py "$bookDir"
    if ($LASTEXITCODE -ne 0) { Write-Log "bakeMetadata.py failed with exit code $LASTEXITCODE"; throw "bakeMetadata.py failed with exit code $LASTEXITCODE" }

    # 3) Build chapters.txt and metadata.txt using buildChapters.py (per README) 【1-d8c35d】
    Write-Log "Building chapters and ffmetadata from metadata.json"
    $chaptersOut = (Get-Content $metadataJson) | & python .\buildChapters.py --chapters | Out-String
    Write-Utf8NoBom $chaptersTxt $chaptersOut.TrimEnd()

    $ffmetaOut = (Get-Content $metadataJson) | & python .\buildChapters.py --ffmpeg | Out-String
    Write-Utf8NoBom $ffmetaTxt $ffmetaOut.TrimEnd()
    Write-Log "Wrote chapters ($chaptersTxt) and ffmetadata ($ffmetaTxt)"

    # 3b) Add author/narrator tags into ffmetadata (auto-detect from metadata.json creators) 【2-94ab89】【3-f6885e】
    $creds = Read-MetadataCreators $metadataJson
    $author = $creds.Author
    $narrator = $creds.Narrator

    # Optional: allow quick override (InputBox), defaults prefilled
    $author = [Microsoft.VisualBasic.Interaction]::InputBox("Author tag (leave as-is or edit):", "Author", $author)
    $narrator = [Microsoft.VisualBasic.Interaction]::InputBox("Narrator tag (leave as-is or edit):", "Narrator", $narrator)

    Inject-AuthorNarratorTags $ffmetaTxt $author $narrator $metaTitle
    Write-Log "Injected author/narrator tags into ffmetadata: title='$metaTitle' author='$author' narrator='$narrator'"

    # 4) Generate files.txt concat list (like your existing pattern) 【4-3d0ffd】
    Get-ChildItem "$bookDir\Part *.mp3" |
        Sort-Object Name |
        ForEach-Object { "file '$($_.Name)'" } |
        Set-Content $filesTxt -Encoding ASCII
    Write-Log "Created concat list: $filesTxt"

    # 5) Prompt for output file location & name (GUI)
    #    Default path should be where the script was launched from (your requirement)
    #$outputFile = Pick-SaveFile "Save M4B file" $launchDir "$bookName.m4b"
    # -------------------------------
    # Build output directory structure: Author\Title
    # -------------------------------

    $authorClean = Clean-Name($author)
    if ($metaTitle) { $titleClean = Clean-Name($metaTitle) } else { $titleClean  = Clean-Name($bookName) }

    $vars = @{
        "LaunchDir" = $launchDir
        "AUTHOR"    = $authorClean
        "TITLE"     = $titleClean
        "BOOKDIR"   = $bookDir
        "BOOKNAME"  = $bookName
    }
    $OutputFile = Expand-OutputFormat $OutputFormat $vars
    $OutputFile = [System.IO.Path]::GetFullPath($OutputFile)
    $outputDir = Split-Path $OutputFile -Parent

    # Create directory structure if it doesn't exist
    if (-not (Test-Path $outputDir)) {
        New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    }
    Write-Log "Ensured output directory exists: $outputDir"

    if (Test-Path $OutputFile) {
        $choice = [System.Windows.Forms.MessageBox]::Show(
            "File already exists:`n$OutputFile`n`nOverwrite?",
            "Confirm Overwrite",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )

        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) {
            Write-Log "User cancelled overwrite for existing file: $OutputFile"
            throw "User canceled overwrite."
        }
        Write-Log "User chose to overwrite existing file: $OutputFile"
    }

    Write-Host "`nOutput will be saved to:"
    Write-Host $OutputFile

    if (-not $OutputFile) { throw "No output file selected." }

    # 6) Convert to M4B (run ffmpeg from within bookDir so concat list relative names work)
    Push-Location $bookDir
    try {
                Write-Log "Starting ffmpeg to produce: $OutputFile"
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
                    "$OutputFile"

        if ($LASTEXITCODE -ne 0) { throw "FFmpeg failed with exit code $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }

    # 7) Validation: ensure file exists and show ffprobe -show_chapters in a scrollable window
    if (-not (Test-Path $OutputFile)) { throw "Output file was not created: $OutputFile" }

    $fi = Get-Item $OutputFile
    #$probe = & ffprobe -hide_banner -show_chapters "$outputFile" 2>&1 | Out-String
    #$probe = & ffprobe -hide_banner -loglevel error -show_chapters "$outputFile" 2>&1 | Out-String
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    $probe = & ffprobe -hide_banner -show_chapters "$OutputFile" 2>&1 | Out-String

    $ErrorActionPreference = $oldPref
    $report = @()
    $report += "✅ Output created successfully:"
    $report += "Path: $OutputFile"
    $report += ("Size: {0:N0} bytes" -f $fi.Length)
    $report += ""
    $report += "ffprobe -show_chapters output:"
    $report += "--------------------------------"
    $report += $probe

    Write-Log "Validation complete. Output: $OutputFile Size: $($fi.Length) bytes"
    Show-TextWindow "M4B Validation (Chapters)" ($report -join "`r`n")

}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    Show-Error $_.Exception.Message
    throw
}