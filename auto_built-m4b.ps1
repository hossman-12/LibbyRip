# Auto-built M4B conversion script
# Scans ./Books for zip files, skips those already converted to M4B in ./AudioBooks,
# extracts and converts the remaining books. Supports a dry-run mode to preview actions.

#requires -version 5.1

param(
    [switch]$DryRun,
    [switch]$Detail
)

# The $dryRun flag indicates whether we are performing a dry-run preview.
$dryRun = $DryRun

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

$ErrorActionPreference = "Stop"

# ------------------------------- Helper functions -------------------------------
function Clean-Name($name) {
    return ($name -replace '[<>:"/\\|?*]', '').Trim()
}
function Write-Log([string]$msg, [string]$logPath) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$ts] $msg"
    try { Add-Content -Path $logPath -Value $entry -Encoding UTF8 } catch { Write-Host $entry }
}
# Append a single line of arbitrary program output to the shared log file with
# the same "[date stamp] [script] message" format used everywhere else. The
# caller passes the script / program name as the second argument. Multi-line
# input is split so every line in the file gets its own header, which is what
# the user asked for.
function Write-SubprocessLog {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$LogPath
    )
    if ([string]::IsNullOrEmpty($Text)) { return }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    foreach ($line in ($Text -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $entry = "[$ts] [$ScriptName] $line"
        try { Add-Content -Path $LogPath -Value $entry -Encoding UTF8 } catch { Write-Host $entry }
    }
}
function Ensure-Command([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "$name was not found on PATH." }
    return $cmd.Source
}
function Read-MetadataCreators([string]$metadataJsonPath) {
    $author = ""
    $narrator = ""
    if (-not (Test-Path $metadataJsonPath)) { return @{ Author=$author; Narrator=$narrator } }
    $meta = Get-Content $metadataJsonPath -Raw | ConvertFrom-Json
    if ($meta.PSObject.Properties.Name -contains "creator") {
        $creators = $meta.creator
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
    $meta = Get-Content $metadataJsonPath -Raw | ConvertFrom-Json
    if ($meta.PSObject.Properties.Name -contains "title") { return [string]$meta.title }
    return ""
}
function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))
}
function Inject-AuthorNarratorTags([string]$ffmetaPath, [string]$author, [string]$narrator, [string]$title) {
    if (-not (Test-Path $ffmetaPath)) { throw "FFmetadata file not found: $ffmetaPath" }
    $txt = Get-Content $ffmetaPath -Raw
    if (-not ($txt -match "^\s*;FFMETADATA1")) { throw "metadata.txt does not start with ;FFMETADATA1" }
    $hasArtist = $txt -match "(?m)^\s*artist="
    $hasAlbumArtist = $txt -match "(?m)^\s*album_artist="
    $hasComment = $txt -match "(?m)^\s*comment="
    $hasAlbum = $txt -match "(?m)^\s*album="
    $hasTitle = $txt -match "(?m)^\s*title="
    $hasComposer = $txt -match "(?m)^\s*composer="
    $composerName = ""
    if ($txt -match "(?m)^\s*comment=(.+)$") {
        $c = $Matches[1].Trim()
        if ($c -match "(?i)Narrated by\s+(.+)") { $composerName = $Matches[1].Trim() }
    }
    if (-not $composerName -and $narrator) { $composerName = $narrator }
    $adds = @()
    if ($author -and -not $hasArtist) { $adds += "artist=$author" }
    if ($author -and -not $hasAlbumArtist) { $adds += "album_artist=$author" }
    if ($narrator -and -not $hasComment) { $adds += "comment=Narrated by $narrator" }
    if ($title -and -not $hasAlbum) { $adds += "album=$title" }
    if ($title -and -not $hasTitle) { $adds += "title=$title" }
    if ($composerName -and -not $hasComposer) { $adds += "composer=$composerName" }
    if ($adds.Count -eq 0) { return }
    $parts = $txt -split "`r?`n", 2
    $new = $parts[0] + "`n" + ($adds -join "`n") + "`n" + $parts[1]
    Write-Utf8NoBom $ffmetaPath $new
}
# Find an existing .m4b file under $audioBooksDir that corresponds to a given zip basename.
# Returns the first match (FileInfo) or $null. Matching strategy:
#   1) Look for "<basename>.m4b" anywhere under AudioBooks.
#   2) If the basename matches the "Author - Title" pattern, look for "<Author>\<Title>.m4b".
function Find-ExistingM4b([string]$basename, [string]$audioBooksDir) {
    $byFullBase = Get-ChildItem -Path $audioBooksDir -Recurse -Filter "${basename}.m4b" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($byFullBase) { return $byFullBase }
    if ($basename -match '^(.*?)\s*-\s*(.+)$') {
        $cleanAuthor = Clean-Name $Matches[1].Trim()
        $cleanTitle = Clean-Name $Matches[2].Trim()
        $candidate = Join-Path (Join-Path $audioBooksDir $cleanAuthor) ($cleanTitle + ".m4b")
        if (Test-Path $candidate) { return Get-Item $candidate }
    }
    return $null
}
# --------------------------------------------------------------------------

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$booksDir = Join-Path $scriptDir "Books"
$audioBooksDir = Join-Path $scriptDir "AudioBooks"

# NOTE: Logging of $booksDir is performed after $logPath is created (see later section).

# Ensure the destination folder exists before we start checking for existing files
if (-not (Test-Path $audioBooksDir)) {
    New-Item -ItemType Directory -Path $audioBooksDir -Force | Out-Null
}

$logDir = Join-Path $scriptDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir "auto-build-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
# Expose the shared log file to subprocesses (Python scripts, ffmpeg, etc.)
# so that they can write to the same file with the same header format.
$env:LIBBYRIP_LOG_FILE = $logPath
Write-Log "Script started (DryRun=$dryRun)" $logPath

# Log the resolved AudioBooks directory now that $logPath exists
Write-Log "AudioBooksDir resolved to: $audioBooksDir" $logPath
# Also log the resolved Books directory now that $logPath is available
Write-Log "BooksDir resolved to: $booksDir" $logPath

Ensure-Command "python" | Out-Null
Ensure-Command "ffmpeg" | Out-Null
Ensure-Command "ffprobe" | Out-Null

$zipFiles = Get-ChildItem -Path $booksDir -Filter "*.zip" -File
Write-Log "Found $($zipFiles.Count) zip files" $logPath

# Output summary to console
Write-Host "Found $($zipFiles.Count) zip file(s) in \"$booksDir\""

$toConvert = @()
foreach ($zip in $zipFiles) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($zip.Name)
    # Look for an existing .m4b matching this zip using the standard strategy:
    #   1) full basename match, 2) "Author - Title" pattern match.
    $existingM4b = Find-ExistingM4b $base $audioBooksDir
    $existingPath = if ($existingM4b) { $existingM4b.FullName } else { "<none>" }
    Write-Log "$($zip.Name): existing .m4b = $existingPath" $logPath
    $alreadyExists = $null -ne $existingM4b
    Write-Log "$($zip.Name): alreadyExists = $alreadyExists" $logPath
    if ($alreadyExists) {
        Write-Log "Skipping $($zip.Name) - already converted at $existingPath" $logPath
    } else {
        # Log the raw FullName before any trimming
        # Log detailed information about the zip file object
        Write-Log "Zip.Name: $($zip.Name)" $logPath
        Write-Log "Zip.FullName raw: $($zip.FullName)" $logPath
        Write-Log "Zip.FullName length: $($zip.FullName.Length)" $logPath
        # Trim any whitespace from the full path to avoid empty-string issues
        $cleanPath = $zip.FullName.Trim()
        Write-Log "CleanPath after Trim: '$cleanPath' (Length=$($cleanPath.Length))" $logPath
        $toConvert += $cleanPath
        Write-Log "Queued $($zip.Name) for conversion (path: $cleanPath)" $logPath
    }
}

# Remove any null, empty, or whitespace-only entries
$toConvert = $toConvert | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

# Log the final queued paths count and a sample of entries to the log file
$debugCount = $toConvert.Count
$sample = $toConvert | Select-Object -First 5
Write-Log "Final queue count: $debugCount" $logPath
Write-Log "Queue sample: $($sample -join '; ')" $logPath
# Console dump only when -Detail is requested
if ($Detail) {
    Write-Host "Queue sample (first $($sample.Count)):"
    $sample | ForEach-Object { Write-Host $_ }
}

# Output number of files to be converted
Write-Host "Files queued for conversion: $($toConvert.Count)"
# Log the count
Write-Log "Files queued for conversion count: $($toConvert.Count)" $logPath
if ($Detail) {
    Write-Host "--- Files to be converted (detail) ---"
    foreach ($p in $toConvert) {
        Write-Host $p
        Write-Log "Queued path: $p" $logPath
    }
}

$successes = @()
$failures = @()
foreach ($zipPath in $toConvert) {
    # Log raw zipPath value for debugging
    Write-Log "Raw zipPath value: '$zipPath'" $logPath
    # Skip any null or empty entries that may have slipped in
    if (-not $zipPath) { continue }
    if ($dryRun) {
        # Preview only - compute expected output path
        $tempBase = [System.IO.Path]::GetFileNameWithoutExtension((Split-Path $zipPath -Leaf))

        # Use the shared matching helper to look for an already-converted .m4b.
        $existingM4bForPreview = Find-ExistingM4b $tempBase $audioBooksDir
        if ($existingM4bForPreview) {
            $out = $existingM4bForPreview.FullName
            Write-Log "[DryRun] $($zipPath) would produce $out (existing m4b matched)" $logPath
            $successes += "[DryRun] $zipPath -> $out"
            continue
        }

        # No existing m4b - compute the expected output path from the zip name or extracted metadata.
        $authorGuess = $null
        $titleGuess = $null
        if ($tempBase -match '^(.*?)\s*-\s*(.+)$') {
            $authorGuess = $Matches[1].Trim()
            $titleGuess = $Matches[2].Trim()
        }
        $previewMeta = Join-Path (Join-Path (Join-Path $scriptDir "temp_extracted") $tempBase) "metadata\metadata.json"
        $author = if ($authorGuess) { Clean-Name $authorGuess } else { "<Author>" }
        $title = if ($titleGuess) { Clean-Name $titleGuess } else { "<Title>" }
        if (Test-Path $previewMeta) {
            $creds = Read-MetadataCreators $previewMeta
            $author = if ($creds.Author) { Clean-Name $creds.Author } else { $author }
            $metaTitle = Read-MetadataTitle $previewMeta
            $title = if ($metaTitle) { Clean-Name $metaTitle } else { $title }
        }
        $authorDir = Join-Path (Join-Path $scriptDir "AudioBooks") $author
        # In dry-run mode, do not actually create directories; just compute the expected output path
        if (-not $dryRun -and -not (Test-Path $authorDir)) { New-Item -ItemType Directory -Path $authorDir -Force | Out-Null }
        $out = Join-Path $authorDir ($title + ".m4b")
        Write-Log "[DryRun] $($zipPath) would produce $out" $logPath
        $successes += "[DryRun] $zipPath -> $out"
        continue
    }

    # Log the current zipPath being processed
    Write-Log "Processing zipPath: $zipPath" $logPath
    try {
        $zipFile = Get-Item $zipPath
        $bookBase = [System.IO.Path]::GetFileNameWithoutExtension($zipFile.Name)
        $tempExtract = Join-Path (Join-Path $scriptDir "temp_extracted") $bookBase
        if (Test-Path $tempExtract) { Remove-Item -Recurse -Force $tempExtract }
        New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null
        Write-Log "Extracting $($zipFile.Name)" $logPath
        Expand-Archive -LiteralPath $zipFile.FullName -DestinationPath $tempExtract -Force

        $metadataDir = Join-Path $tempExtract "metadata"
        $metadataJson = Join-Path $metadataDir "metadata.json"
        $coverJpg = Join-Path $metadataDir "cover.JPG"
        $chaptersTxt = Join-Path $metadataDir "chapters.txt"
        $ffmetaTxt = Join-Path $metadataDir "metadata.txt"
        $filesTxt = Join-Path $tempExtract "files.txt"

        if (-not (Test-Path $metadataJson)) { throw "Missing metadata.json" }
        if (-not (Test-Path $coverJpg)) { throw "Missing cover image" }

        $creds = Read-MetadataCreators $metadataJson
        $author = if ($creds.Author) { $creds.Author } else { "Unknown Author" }
        $narrator = $creds.Narrator
        $metaTitle = Read-MetadataTitle $metadataJson
        if (-not $metaTitle) { $metaTitle = $bookBase }

        $outputFormat = "%LaunchDir%/AudioBooks/%AUTHOR%/%TITLE%.m4b"
        $vars = @{
            "LaunchDir" = $scriptDir
            "AUTHOR"    = Clean-Name $author
            "TITLE"    = Clean-Name $metaTitle
        }
        # Use [regex]::Replace with a MatchEvaluator that closes over $vars via .NET delegate.
        # PowerShell 5.1's -replace operator coerces script-block RHS to its source text instead
        # of invoking it, so we go through the .NET API directly.
        $outputFile = [regex]::Replace(
            $outputFormat,
            '%([^%]+)%',
            [System.Text.RegularExpressions.MatchEvaluator] {
                param($m)
                $key = $m.Groups[1].Value
                if ($vars.ContainsKey($key)) { return [string]$vars[$key] }
                return $m.Value
            }
        )
        $outputFile = $outputFile -replace '/', '\\'
        $outputFile = [System.IO.Path]::GetFullPath($outputFile)
        $outDir = Split-Path $outputFile -Parent
        if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

        Write-Log "Running bakeMetadata.py" $logPath
        # PowerShell wraps stderr from native commands into a NativeCommandError ErrorRecord
        # regardless of where 2> redirects the text. With $ErrorActionPreference=Stop that
        # ErrorRecord terminates the script. Suppress it for this one call, then restore.
        # Both stdout and stderr are captured and re-emitted through Write-SubprocessLog so
        # every line ends up in the shared log file with a [date stamp] [bakeMetadata.py] header.
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $bakeStdoutFile = Join-Path $env:TEMP ("bake-stdout-$([System.Guid]::NewGuid()).log")
        $bakeStderrFile = Join-Path $env:TEMP ("bake-stderr-$([System.Guid]::NewGuid()).log")
        try {
            & python .\bakeMetadata.py "$tempExtract" 1> "$bakeStdoutFile" 2> "$bakeStderrFile"
            $bakeStderr = ""
            if (Test-Path $bakeStderrFile) { $bakeStderr = Get-Content $bakeStderrFile -Raw -ErrorAction SilentlyContinue }
            if (Test-Path $bakeStdoutFile) {
                $bakeStdout = Get-Content $bakeStdoutFile -Raw -ErrorAction SilentlyContinue
                Write-SubprocessLog "bakeMetadata.py" $bakeStdout $logPath
            }
            if ($bakeStderr) {
                Write-SubprocessLog "bakeMetadata.py" $bakeStderr $logPath
            }
        } finally {
            $ErrorActionPreference = $prevPref
            if (Test-Path $bakeStdoutFile) { Remove-Item -LiteralPath $bakeStdoutFile -Force -ErrorAction SilentlyContinue }
            if (Test-Path $bakeStderrFile) { Remove-Item -LiteralPath $bakeStderrFile -Force -ErrorAction SilentlyContinue }
        }
        Write-Log ("bakeMetadata.py exit code: $LASTEXITCODE") $logPath
        if ($LASTEXITCODE -ne 0) {
            $bakeTail = ($bakeStderr -split "`r?`n" | Select-Object -Last 5) -join " | "
            Write-Log ("bakeMetadata.py stderr tail: $bakeTail") $logPath
            if (-not (Test-Path $coverJpg)) {
                throw "bakeMetadata.py failed (exit $LASTEXITCODE) and cover image is missing"
            }
            Write-Log "bakeMetadata.py exited $LASTEXITCODE but cover image is present; continuing" $logPath
        }

        # Generate metadata.txt and chapters.txt via buildChapters.py. Suppress PowerShell's
        # stderr-to-ErrorRecord promotion around each call so the pipeline isn't terminated.
        # Stdout and stderr are captured and re-emitted through Write-SubprocessLog so
        # every line ends up in the shared log file with a [date stamp] [buildChapters.py] header.
        $bcStdoutFile = Join-Path $env:TEMP ("bc-stdout-$([System.Guid]::NewGuid()).log")
        $bcStderrFile = Join-Path $env:TEMP ("bc-stderr-$([System.Guid]::NewGuid()).log")
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        try {
            # buildChapters.py reads from stdin and writes to stdout. With stdout redirected
            # to a file, the pipeline receives nothing, so we read the file back after the
            # call rather than relying on the pipeline.
            $null = (Get-Content $metadataJson) | & python .\buildChapters.py --ffmpeg 1> "$bcStdoutFile" 2> "$bcStderrFile"
            $ffmetaOut = ""
            if (Test-Path $bcStdoutFile) {
                $ffmetaOut = Get-Content $bcStdoutFile -Raw -ErrorAction SilentlyContinue
                Write-SubprocessLog "buildChapters.py" $ffmetaOut $logPath
            }
            if (Test-Path $bcStderrFile) {
                $bcErr = Get-Content $bcStderrFile -Raw -ErrorAction SilentlyContinue
                if ($bcErr) { Write-SubprocessLog "buildChapters.py" $bcErr $logPath }
            }
            $ErrorActionPreference = $prevPref
            Write-Log ("buildChapters.py --ffmpeg exit code: $LASTEXITCODE") $logPath
            Write-Utf8NoBom $ffmetaTxt $ffmetaOut.TrimEnd()

            $ErrorActionPreference = "SilentlyContinue"
            $null = (Get-Content $metadataJson) | & python .\buildChapters.py --chapters 1> "$bcStdoutFile" 2> "$bcStderrFile"
            $chaptersOut = ""
            if (Test-Path $bcStdoutFile) {
                $chaptersOut = Get-Content $bcStdoutFile -Raw -ErrorAction SilentlyContinue
                Write-SubprocessLog "buildChapters.py" $chaptersOut $logPath
            }
            if (Test-Path $bcStderrFile) {
                $bcErr = Get-Content $bcStderrFile -Raw -ErrorAction SilentlyContinue
                if ($bcErr) { Write-SubprocessLog "buildChapters.py" $bcErr $logPath }
            }
            $ErrorActionPreference = $prevPref
            Write-Log ("buildChapters.py --chapters exit code: $LASTEXITCODE") $logPath
            Write-Utf8NoBom $chaptersTxt $chaptersOut.TrimEnd()
        } finally {
            $ErrorActionPreference = $prevPref
            if (Test-Path $bcStdoutFile) { Remove-Item -LiteralPath $bcStdoutFile -Force -ErrorAction SilentlyContinue }
            if (Test-Path $bcStderrFile) { Remove-Item -LiteralPath $bcStderrFile -Force -ErrorAction SilentlyContinue }
        }

        Inject-AuthorNarratorTags $ffmetaTxt $author $narrator $metaTitle

        Get-ChildItem "$tempExtract\Part *.mp3" | Sort-Object Name | ForEach-Object { "file '$($_.Name)'" } | Set-Content $filesTxt -Encoding ASCII

        Push-Location $tempExtract
        try {
            Write-Log "Running ffmpeg" $logPath
            # ffmpeg writes progress to stderr. PowerShell wraps each stderr line into an
            # ErrorRecord; under $ErrorActionPreference=Stop that would terminate the script.
            # Temporarily switch to SilentlyContinue so the progress messages don't blow us up,
            # capture both streams to temp files, then re-emit them through Write-SubprocessLog
            # so every line lands in the shared log with a [date stamp] [ffmpeg] header.
            $ffmpegStdoutFile = Join-Path $env:TEMP ("ffmpeg-stdout-$([System.Guid]::NewGuid()).log")
            $ffmpegStderrFile = Join-Path $env:TEMP ("ffmpeg-stderr-$([System.Guid]::NewGuid()).log")
            $prevPref = $ErrorActionPreference
            $ErrorActionPreference = "SilentlyContinue"
            try {
                $ffmpegLog = & ffmpeg -y -f concat -safe 0 -i "$filesTxt" -f ffmetadata -i "$ffmetaTxt" -i "$coverJpg" -map 0:a -map_metadata 1 -map_chapters 1 -map 2:v -c:a aac -b:a 128k -c:v mjpeg -disposition:v attached_pic -f ipod "$outputFile" 1> "$ffmpegStdoutFile" 2> "$ffmpegStderrFile"
                $ffmpegStderrText = ""
                if (Test-Path $ffmpegStderrFile) {
                    $ffmpegStderrText = Get-Content $ffmpegStderrFile -Raw -ErrorAction SilentlyContinue
                }
                $ffmpegStdoutText = ""
                if (Test-Path $ffmpegStdoutFile) {
                    $ffmpegStdoutText = Get-Content $ffmpegStdoutFile -Raw -ErrorAction SilentlyContinue
                }
                if ($ffmpegStdoutText) { Write-SubprocessLog "ffmpeg" $ffmpegStdoutText $logPath }
                if ($ffmpegStderrText) { Write-SubprocessLog "ffmpeg" $ffmpegStderrText $logPath }
            } finally {
                $ErrorActionPreference = $prevPref
                if (Test-Path $ffmpegStdoutFile) { Remove-Item -LiteralPath $ffmpegStdoutFile -Force -ErrorAction SilentlyContinue }
                if (Test-Path $ffmpegStderrFile) { Remove-Item -LiteralPath $ffmpegStderrFile -Force -ErrorAction SilentlyContinue }
            }
            Write-Log ("ffmpeg exit code: $LASTEXITCODE") $logPath
            if ($LASTEXITCODE -ne 0 -or $ffmpegStderrText) {
                $ffmpegTail = ($ffmpegStderrText -split "`r?`n" | Select-Object -Last 5) -join " | "
                Write-Log ("ffmpeg stderr tail: $ffmpegTail") $logPath
            }
        } finally { Pop-Location }

        if (-not (Test-Path $outputFile)) { throw "Output not created (ffmpeg exit $LASTEXITCODE)" }
        $fi = Get-Item $outputFile
        if ($fi.Length -lt 1024) {
            # Less than 1 KiB output is almost certainly a truncated / failed file.
            Remove-Item -LiteralPath $outputFile -Force -ErrorAction SilentlyContinue
            throw "Output file is too small ($($fi.Length) bytes); ffmpeg exit $LASTEXITCODE"
        }
        if ($LASTEXITCODE -ne 0) {
            # Non-zero exit but a real file was produced - log as warning, count as success.
            Write-Log "ffmpeg exited $LASTEXITCODE but produced valid file of size $($fi.Length); treating as success" $logPath
        }
        # ffprobe is informational only; suppress its stderr too so a non-zero exit does not throw.
        # We also forward its output to the shared log file with a [ffprobe] tag so users can
        # grep for "ffprobe" in the log to find the chapter listing for the produced m4b.
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $probeStdoutFile = Join-Path $env:TEMP ("probe-stdout-$([System.Guid]::NewGuid()).log")
        $probeStderrFile = Join-Path $env:TEMP ("probe-stderr-$([System.Guid]::NewGuid()).log")
        try {
            $probe = & ffprobe -hide_banner -show_chapters "$outputFile" 1> "$probeStdoutFile" 2> "$probeStderrFile" | Out-String
        } finally {
            $ErrorActionPreference = $prevPref
            if (Test-Path $probeStdoutFile) {
                $pOut = Get-Content $probeStdoutFile -Raw -ErrorAction SilentlyContinue
                if ($pOut) { Write-SubprocessLog "ffprobe" $pOut $logPath }
                Remove-Item -LiteralPath $probeStdoutFile -Force -ErrorAction SilentlyContinue
            }
            if (Test-Path $probeStderrFile) {
                $pErr = Get-Content $probeStderrFile -Raw -ErrorAction SilentlyContinue
                if ($pErr) { Write-SubprocessLog "ffprobe" $pErr $logPath }
                Remove-Item -LiteralPath $probeStderrFile -Force -ErrorAction SilentlyContinue
            }
        }
        Write-Log "Created $outputFile (size $($fi.Length))" $logPath
        $successes += $outputFile
    } catch {
        $msg = "Failed processing ${zipPath}: $($_.Exception.Message)"
        Write-Log $msg $logPath
        $failures += $msg
    } finally {
        if ($tempExtract -and (Test-Path $tempExtract)) { Remove-Item -Recurse -Force $tempExtract }
    }
}

Write-Host "\n=== Conversion Report ==="
Write-Host "Successful conversions: $($successes.Count)"
foreach ($s in $successes) { Write-Host "  $s" }
Write-Host "Failed conversions: $($failures.Count)"
foreach ($f in $failures) { Write-Host "  $f" }
Write-Log "Report - Success: $($successes.Count) Failure: $($failures.Count)" $logPath
