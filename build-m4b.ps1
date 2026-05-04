Add-Type -AssemblyName System.Windows.Forms

# -------------------------------
# 1. Pick audiobook directory (GUI)
# -------------------------------
$folderDlg = New-Object System.Windows.Forms.FolderBrowserDialog
$folderDlg.Description = "Select audiobook directory (contains Part XXX.mp3 files)"

if ($folderDlg.ShowDialog() -ne 'OK') {
    Write-Error "No directory selected. Aborting."
    exit 1
}

$bookDir = $folderDlg.SelectedPath

Write-Host "Selected book directory:"
Write-Host $bookDir

# -------------------------------
# 2. Bake metadata into MP3 files
# -------------------------------
Write-Host "`nBaking metadata into MP3 files..."
python .\bakeMetadata.py "$bookDir"
if ($LASTEXITCODE -ne 0) { exit 1 }

# -------------------------------
# 3. Build chapters.txt and metadata.txt
# -------------------------------
$metadataJson = Join-Path $bookDir "metadata\metadata.json"
$metadataDir  = Join-Path $bookDir "metadata"

Write-Host "`nBuilding chapter files..."

Get-Content $metadataJson |
    python .\buildChapters.py --chapters |
    Set-Content (Join-Path $metadataDir "chapters.txt") -Encoding UTF8

Get-Content $metadataJson |
    python .\buildChapters.py --ffmpeg |
    Set-Content (Join-Path $metadataDir "metadata.txt") -Encoding UTF8

# -------------------------------
# 4. Generate files.txt concat list
# -------------------------------
Write-Host "`nGenerating files.txt..."

Get-ChildItem "$bookDir\Part *.mp3" |
    Sort-Object Name |
    ForEach-Object { "file '$($_.Name)'" } |
    Set-Content (Join-Path $bookDir "files.txt") -Encoding ASCII

# -------------------------------
# 5. Ask user for output file (GUI)
# -------------------------------
$bookName = Split-Path $bookDir -Leaf
$saveDlg = New-Object System.Windows.Forms.SaveFileDialog
$saveDlg.Filter = "Audiobook (*.m4b)|*.m4b"
$saveDlg.Title  = "Save M4B file"
$saveDlg.FileName = "$bookName.m4b"

if ($saveDlg.ShowDialog() -ne 'OK') {
    Write-Error "No output file selected. Aborting."
    exit 1
}

$OutputFile = $saveDlg.FileName

Write-Host "Output file:"
Write-Host $OutputFile

# -------------------------------
# 6. Build M4B with FFmpeg
# -------------------------------
Write-Host "`nCreating M4B audiobook..."

ffmpeg `
  -f concat -safe 0 -i "$bookDir\files.txt" `
  -f ffmetadata -i "$metadataDir\metadata.txt" `
  -i "$metadataDir\cover.JPG" `
  -map 0:a `
  -map_metadata 1 `
  -map_chapters 1 `
  -map 2:v `
  -c:a aac -b:a 128k `
  -c:v mjpeg `
  -disposition:v attached_pic `
  -f ipod `
  "$OutputFile"

if ($LASTEXITCODE -ne 0) {
    Write-Error "FFmpeg failed."
    exit 1
}

Write-Host "`n✅ Audiobook created successfully:"
Write-Host $OutputFile