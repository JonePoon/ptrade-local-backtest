param([string]$ProjectRoot = (Join-Path $PSScriptRoot '..'), [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
foreach ($project in @('native-fetch', 'native-store')) {
    $manifest = Join-Path $root "$project/Cargo.toml"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Missing manifest: $manifest" }
    if (-not $SkipTests) {
        & cargo test --release --locked --manifest-path $manifest
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & cargo build --release --locked --manifest-path $manifest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& (Join-Path $root 'native-fetch/target/release/ptrade-fetch.exe') doctor
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $root 'native-store/target/release/ptrade-store.exe') doctor
exit $LASTEXITCODE
